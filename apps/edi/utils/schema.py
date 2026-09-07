"""
Build X12 837P segment list from a payload dict (Colorado companion overlays).

Important rules:
  - Never fabricate provider, member, procedure, demographic, or tax identifiers.
  - Standard provider: 2010AA NM108=XX / NM109=NPI.
  - Atypical provider: omit NM108/NM109; never fabricate an NPI.
  - X222A1 Billing Provider Tax Identification is REF*EI (or SY when explicitly
    configured as an SSN flow). RedArt currently uses business EIN/TIN via EI.
  - Colorado Medicaid atypical provider ID is payer-assigned secondary ID
    REF*G2 in 2010BB, after NM1*PR.
  - 2010BA NM108=MI / NM109=Colorado Medicaid member ID.
  - ISA is exactly 106 characters including terminator.
"""

from apps.edi.utils.envelope import DEFAULT_ENVELOPE
from apps.edi.utils.required_claim_data import billing_address_errors, subscriber_errors

CO_RECEIVER_ID = "COMEDASSISTPROG"
CO_RECEIVER_NAME = "COLORADO MEDICAL ASSISTANCE PROGRAM"
CO_PAYER_ID = "CO_TXIX"


def _sep(envelope):
    return envelope.get("element_separator") or DEFAULT_ENVELOPE["element_separator"]


def _term(envelope):
    return envelope.get("segment_terminator") or DEFAULT_ENVELOPE["segment_terminator"]


def _comp(envelope):
    return envelope.get("component_separator") or DEFAULT_ENVELOPE["component_separator"]


def _pad_isa(value, length=15):
    text = (value or "")[:length]
    return text.ljust(length)


def _seg(envelope, *parts):
    """Render one segment while removing illegal trailing empty elements."""
    rendered = ["" if p is None else str(p) for p in parts]
    while len(rendered) > 1 and rendered[-1] == "":
        rendered.pop()
    return _sep(envelope).join(rendered) + _term(envelope)


def _assert_isa_length(segment: str) -> None:
    if len(segment) != 106:
        raise ValueError(
            f"ISA segment length is {len(segment)}, expected 106. "
            "Check sender_id, receiver_id, and isa13 padding."
        )


def _tax_id_digits(provider: dict) -> str:
    return "".join(ch for ch in str(provider.get("tax_id") or "") if ch.isdigit())


def build_edi_content(payload: dict) -> list[str]:
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
        errors = billing_address_errors(provider) + subscriber_errors(
            patient.get("date_of_birth"), patient.get("gender")
        )
        if errors:
            raise ValueError("; ".join(errors))
        st_start = len(edi_content)

        edi_content.append(_seg(envelope, "ST", "837", st02, gs08))
        edi_content.append(
            _seg(envelope, "BHT", "0019", "00", st02, gs_date, gs_time, "CH")
        )

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

        contact_name = (
            partner.get("contact_name") or partner.get("name") or "SUBMITTER"
        ).strip()
        raw_phone = (partner.get("contact_phone") or "").strip()
        phone = "".join(ch for ch in raw_phone if ch.isdigit()) or "0000000000"
        edi_content.append(_seg(envelope, "PER", "IC", contact_name, "TE", phone))

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

        billing_hl = 1
        edi_content.append(_seg(envelope, "HL", str(billing_hl), "", "20", "1"))

        is_atypical = bool(provider.get("is_atypical"))
        medicaid_pid = (provider.get("medicaid_provider_id") or "").strip()
        if is_atypical and not medicaid_pid:
            raise ValueError(
                f"Claim {claim.get('claim_number', claim.get('claim_id'))}: "
                "atypical provider is missing medicaid_provider_id."
            )

        if is_atypical:
            billing_qualifier = ""
            billing_id = ""
        else:
            npi = (provider.get("npi") or "").strip()
            if not npi:
                raise ValueError(
                    f"Claim {claim.get('claim_number', claim.get('claim_id'))}: "
                    "standard provider is missing NPI."
                )
            billing_qualifier = "XX"
            billing_id = npi

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

        if (provider.get("address_line_1") or "").strip():
            edi_content.append(_seg(envelope, "N3", provider["address_line_1"]))
            city = provider.get("city") or ""
            state = provider.get("state") or ""
            zip_code = provider.get("zip") or ""
            if city or state or zip_code:
                edi_content.append(_seg(envelope, "N4", city, state, zip_code))

        tax_id = _tax_id_digits(provider)
        if is_atypical:
            if len(tax_id) != 9:
                raise ValueError(
                    f"Claim {claim.get('claim_number', claim.get('claim_id'))}: "
                    "atypical provider requires a real 9-digit billing Tax ID/EIN "
                    "for 2010AA REF*EI. Never substitute the Medicaid provider ID."
                )
            edi_content.append(_seg(envelope, "REF", "EI", tax_id))
        elif tax_id:
            if len(tax_id) != 9:
                raise ValueError(
                    f"Claim {claim.get('claim_number', claim.get('claim_id'))}: "
                    "provider tax_id must contain exactly 9 digits."
                )
            edi_content.append(_seg(envelope, "REF", "EI", tax_id))

        edi_content.append(_seg(envelope, "HL", "2", str(billing_hl), "22", "0"))
        edi_content.append(
            _seg(envelope, "SBR", "P", "18", "", "", "", "", "", "", "MC")
        )

        medicaid_id = (patient.get("medicaid_member_id") or "").strip()
        if not medicaid_id:
            raise ValueError(
                f"Claim {claim.get('claim_number', claim.get('claim_id'))}: "
                "patient medicaid_member_id is missing."
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

        if (patient.get("address_line_1") or "").strip():
            edi_content.append(_seg(envelope, "N3", patient["address_line_1"]))
            city = patient.get("city") or ""
            state = patient.get("state") or ""
            zip_code = patient.get("zip") or ""
            if city or state or zip_code:
                edi_content.append(_seg(envelope, "N4", city, state, zip_code))

        dob = (patient.get("date_of_birth") or "").strip()
        gender = (patient.get("gender") or "").strip().upper()
        edi_content.append(_seg(envelope, "DMG", "D8", dob, gender))

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

        if is_atypical:
            edi_content.append(_seg(envelope, "REF", "G2", medicaid_pid))

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

        for idx, line in enumerate(claim.get("service_lines") or [], start=1):
            proc = (line.get("procedure_code") or "").strip()
            if not proc:
                raise ValueError(
                    f"Claim {claim.get('claim_number', claim.get('claim_id'))}: "
                    f"service line {idx} is missing procedure_code."
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

        body_count = len(edi_content) - st_start + 1
        edi_content.append(_seg(envelope, "SE", str(body_count), st02))

    edi_content.append(_seg(envelope, "GE", str(st_count), gs06))
    edi_content.append(_seg(envelope, "IEA", "1", isa13))
    return edi_content


def render_edi_file(edi_content: list[str]) -> str:
    return "\n".join(edi_content) + ("\n" if edi_content else "")
