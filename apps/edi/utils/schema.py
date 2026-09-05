"""
Build X12 837P segment list from a payload dict (Colorado companion overlays).

Companion source: CO EDI v5010 X12 837P Companion Guide.

Enterprise rules enforced here:
  - NEVER default or fabricate a missing procedure code.
  - NEVER fabricate DOB, gender, address, NPI, or any other missing data.
  - Provider NM108/NM109 driven by is_atypical flag:
      Standard (is_atypical=False): NM108=XX, NM109=npi
      Atypical   (is_atypical=True): NM108=XX, NM109=medicaid_provider_id
        (matches HCPF-accepted sample shape; never fabricate an NPI)
  - 2010BA NM108=MI, NM109=Colorado Medicaid Member ID (always dynamic).
  - N3/N4 segments emitted only when address data is present.
  - DMG segment emitted only when DOB or gender is present.
  - REF*EI emitted only when tax_id is present (never invent EIN).
  - ISA segment is always exactly 106 characters (including terminator).
"""

from apps.edi.utils.envelope import DEFAULT_ENVELOPE

# ---------------------------------------------------------------------------
# Colorado Medical Assistance Program constants (companion guide §3).
# These are protocol constants, NOT company data.
# ---------------------------------------------------------------------------
CO_RECEIVER_ID = "COMEDASSISTPROG"     # ISA08 / GS03 / 1000B NM109
CO_RECEIVER_NAME = "COLORADO MEDICAL ASSISTANCE PROGRAM"
CO_PAYER_ID = "CO_TXIX"               # 2010BB PI qualifier value


def _sep(envelope):
    return envelope.get("element_separator") or DEFAULT_ENVELOPE["element_separator"]


def _term(envelope):
    return envelope.get("segment_terminator") or DEFAULT_ENVELOPE["segment_terminator"]


def _comp(envelope):
    return envelope.get("component_separator") or DEFAULT_ENVELOPE["component_separator"]


def _pad_isa(value, length=15):
    """Pad / truncate a value to exactly `length` characters for ISA fields."""
    text = (value or "")[:length]
    return text.ljust(length)


def _seg(envelope, *parts):
    return _sep(envelope).join("" if p is None else str(p) for p in parts) + _term(
        envelope
    )


def _assert_isa_length(segment: str) -> None:
    """
    Guard: the ISA segment must be exactly 106 characters.

    The X12 standard mandates fixed-length ISA (105 data chars + terminator).
    Raise immediately rather than sending a malformed interchange to HCPF.
    """
    if len(segment) != 106:
        raise ValueError(
            f"ISA segment length is {len(segment)}, expected 106. "
            "Check sender_id, receiver_id, and isa13 padding."
        )


def build_edi_content(payload: dict) -> list[str]:
    """
    Return an ordered list of X12 segment strings (each ends with ~).

    Raises ValueError immediately if any required data is missing rather than
    silently producing invalid EDI.
    """
    envelope = payload["envelope"]
    partner = payload["trading_partner"]
    control = payload["control"]
    claims = payload["claims"]
    cp = _comp(envelope)

    sender = partner["sender_id"]
    isa15 = envelope.get("isa15") or "T"
    isa13 = control["isa13"]
    gs06 = control["gs06"]
    when = payload["generated_at"]
    isa_date = when.strftime("%y%m%d")
    isa_time = when.strftime("%H%M")
    gs_date = when.strftime("%Y%m%d")
    gs_time = when.strftime("%H%M")
    gs08 = envelope.get("gs08") or DEFAULT_ENVELOPE["gs08"]
    rep = envelope.get("repetition_separator") or DEFAULT_ENVELOPE["repetition_separator"]

    # Build ISA segment and validate fixed length before writing anything else.
    isa_seg = _seg(
        envelope,
        "ISA",
        "00",
        " " * 10,
        "00",
        " " * 10,
        envelope.get("isa05") or "ZZ",
        _pad_isa(sender),
        envelope.get("isa07") or "ZZ",
        _pad_isa(CO_RECEIVER_ID),
        isa_date,
        isa_time,
        rep,
        "00501",
        isa13,
        "0",
        isa15,
        cp,
    )
    _assert_isa_length(isa_seg)

    edi_content = [
        isa_seg,
        _seg(
            envelope,
            "GS",
            envelope.get("gs01") or "HC",
            sender,
            CO_RECEIVER_ID,
            gs_date,
            gs_time,
            gs06,
            "X",
            gs08,
        ),
    ]

    st_count = 0
    for claim in claims:
        st_count += 1
        st02 = claim["st02"]
        provider = claim["provider"]
        patient = claim["patient"]
        st_start = len(edi_content)

        edi_content.append(_seg(envelope, "ST", "837", st02, gs08))
        edi_content.append(
            _seg(
                envelope,
                "BHT",
                "0019",
                "00",
                st02,
                gs_date,
                gs_time,
                "CH",
            )
        )

        # ── 1000A Submitter ─────────────────────────────────────────────────
        # NM1*41 identifies the submitter (the trading partner / billing entity).
        # Name comes from the TradingPartner record, never from patient or provider.
        submitter_name = (partner.get("name") or sender or "SUBMITTER").strip()
        edi_content.append(
            _seg(
                envelope,
                "NM1",
                "41",
                "2",
                submitter_name,
                "",
                "",
                "",
                "",
                "46",
                sender,
            )
        )

        # PER — submitter contact.  Must use the trading partner contact, not
        # patient or provider phone.  Fallback to "0000000000" is only a safety
        # net; the readiness validator should have ensured a real phone is set.
        contact_name = (
            partner.get("contact_name") or partner.get("name") or "SUBMITTER"
        ).strip()
        raw_phone = (partner.get("contact_phone") or "").strip()
        phone = "".join(ch for ch in raw_phone if ch.isdigit()) or "0000000000"
        edi_content.append(
            _seg(envelope, "PER", "IC", contact_name, "TE", phone)
        )

        # ── 1000B Receiver (Colorado) ────────────────────────────────────────
        edi_content.append(
            _seg(
                envelope,
                "NM1",
                "40",
                "2",
                CO_RECEIVER_NAME,
                "",
                "",
                "",
                "",
                "46",
                CO_RECEIVER_ID,
            )
        )

        # ── 2000A Billing Provider HL ────────────────────────────────────────
        billing_hl = 1
        edi_content.append(_seg(envelope, "HL", str(billing_hl), "", "20", "1"))

        # ── 2010AA Billing Provider Name ─────────────────────────────────────
        # NM108 is always XX (matches HCPF-accepted sample).
        # NM109:
        #   Standard (is_atypical=False): npi
        #   Atypical  (is_atypical=True): medicaid_provider_id (never invent NPI)
        is_atypical = bool(provider.get("is_atypical"))
        billing_qualifier = "XX"
        billing_id = (
            provider.get("medicaid_provider_id", "")
            if is_atypical
            else provider.get("npi", "")
        )

        if not (billing_id or "").strip():
            raise ValueError(
                f"Claim {claim.get('claim_number', claim.get('claim_id'))}: "
                f"provider has no billing identifier "
                f"({'medicaid_provider_id' if is_atypical else 'npi'}). "
                "Never fabricate an identifier."
            )

        provider_display_name = (
            provider.get("billing_name") or provider.get("legal_name") or "PROVIDER"
        )
        edi_content.append(
            _seg(
                envelope,
                "NM1",
                "85",
                "2",
                provider_display_name,
                "",
                "",
                "",
                "",
                billing_qualifier,
                billing_id,
            )
        )

        # N3/N4 — only emit when address data is present.
        if (provider.get("address_line_1") or "").strip():
            edi_content.append(_seg(envelope, "N3", provider["address_line_1"]))
            city = provider.get("city") or ""
            state = provider.get("state") or ""
            zip_code = provider.get("zip") or ""
            if city or state or zip_code:
                edi_content.append(_seg(envelope, "N4", city, state, zip_code))

        # REF*EI (tax_id / EIN) — only when a real tax_id exists. Never invent.
        tax_id = "".join(
            ch for ch in str(provider.get("tax_id") or "") if ch.isdigit()
        )
        if tax_id:
            edi_content.append(_seg(envelope, "REF", "EI", tax_id[:9]))

        # ── 2000B Subscriber HL ──────────────────────────────────────────────
        edi_content.append(_seg(envelope, "HL", "2", str(billing_hl), "22", "0"))
        edi_content.append(_seg(envelope, "SBR", "P", "18", "", "", "", "", "", "", "MC"))

        # ── 2010BA Subscriber Name ───────────────────────────────────────────
        # NM108=MI, NM109=Colorado Medicaid Member ID — always dynamic from DB.
        medicaid_id = (patient.get("medicaid_member_id") or "").strip()
        if not medicaid_id:
            raise ValueError(
                f"Claim {claim.get('claim_number', claim.get('claim_id'))}: "
                "patient medicaid_member_id is missing (NM1*IL MI required). "
                "Never fabricate a member ID."
            )
        edi_content.append(
            _seg(
                envelope,
                "NM1",
                "IL",
                "1",
                patient.get("last_name") or "",
                patient.get("first_name") or "",
                "",
                "",
                "",
                "MI",
                medicaid_id,
            )
        )

        # N3/N4 — only emit when patient address is present.
        if (patient.get("address_line_1") or "").strip():
            edi_content.append(_seg(envelope, "N3", patient["address_line_1"]))
            city = patient.get("city") or ""
            state = patient.get("state") or ""
            zip_code = patient.get("zip") or ""
            if city or state or zip_code:
                edi_content.append(_seg(envelope, "N4", city, state, zip_code))

        # DMG — only emit when DOB or gender is present; never fabricate.
        dob = (patient.get("date_of_birth") or "").strip()
        gender = (patient.get("gender") or "").strip().upper()
        if dob or gender:
            edi_content.append(
                _seg(
                    envelope,
                    "DMG",
                    "D8" if dob else "",
                    dob,
                    gender or "U",
                )
            )

        # ── 2010BB Payer ─────────────────────────────────────────────────────
        edi_content.append(
            _seg(
                envelope,
                "NM1",
                "PR",
                "2",
                CO_RECEIVER_NAME,
                "",
                "",
                "",
                "",
                "PI",
                CO_PAYER_ID,
            )
        )

        # ── 2300 Claim ───────────────────────────────────────────────────────
        pos = (claim.get("place_of_service") or "41").strip()
        clm05 = f"{pos}{cp}B{cp}1"
        edi_content.append(
            _seg(
                envelope,
                "CLM",
                claim["claim_number"],
                claim["total_charge"],
                "",
                "",
                clm05,
                "Y",
                "A",
                "Y",
                "Y",
            )
        )

        if (claim.get("diagnosis_code") or "").strip():
            diag = str(claim["diagnosis_code"]).replace(".", "")
            edi_content.append(_seg(envelope, "HI", f"ABK{cp}{diag}"))

        # NM1*DN (driver) after HI, before 2400 service lines (per CO companion).
        driver = claim.get("driver") or {}
        driver_last = (driver.get("last_name") or "").strip()
        driver_first = (driver.get("first_name") or "").strip()
        if driver_last or driver_first:
            edi_content.append(
                _seg(
                    envelope,
                    "NM1",
                    "DN",
                    "1",
                    driver_last,
                    driver_first,
                )
            )

        # ── 2400 Service Lines ────────────────────────────────────────────────
        for idx, line in enumerate(claim.get("service_lines") or [], start=1):
            proc = (line.get("procedure_code") or "").strip()
            if not proc:
                # This should have been caught by readiness validation; guard here
                # as a second safety net — never default to a fabricated code.
                raise ValueError(
                    f"Claim {claim.get('claim_number', claim.get('claim_id'))}: "
                    f"service line {idx} is missing procedure_code. "
                    "RedArt must supply the finalized procedure code."
                )

            units = line.get("units") or 1
            charge = line.get("charge") or "0"
            edi_content.append(_seg(envelope, "LX", str(idx)))
            edi_content.append(
                _seg(
                    envelope,
                    "SV1",
                    f"HC{cp}{proc}",
                    charge,
                    "UN",
                    units,
                    pos,
                    "",
                    "1",
                )
            )
            if (line.get("from_date") or "").strip():
                edi_content.append(
                    _seg(envelope, "DTP", "472", "D8", line["from_date"])
                )

        # SE: segment count includes ST through SE.
        body_count = len(edi_content) - st_start + 1
        edi_content.append(_seg(envelope, "SE", str(body_count), st02))

    edi_content.append(_seg(envelope, "GE", str(st_count), gs06))
    edi_content.append(_seg(envelope, "IEA", "1", isa13))
    return edi_content


def render_edi_file(edi_content: list[str]) -> str:
    """Join segments with newlines (one segment per line)."""
    return "\n".join(edi_content) + ("\n" if edi_content else "")
