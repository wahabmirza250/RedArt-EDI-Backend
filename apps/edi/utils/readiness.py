"""
Pre-flight validation before 837P generation.

Rules (Colorado Medicaid NEMT):

  Claim:
    - diagnosis_code and place_of_service required
    - at least one active service line; every line must have procedure_code + charge
    - NEVER default/fabricate a missing procedure code

  Patient (subscriber):
    - medicaid_member_id is the ONLY mandatory identifier (NM1*IL MI)
    - DOB, gender, address are OPTIONAL — emitted when present, never fabricated

  Provider (billing):
    Standard NPI provider (is_atypical=False):
      - npi required (NM108=XX)
      - tax_id required (REF*EI in 2010AA)
      - taxonomy_code required

    Atypical Colorado Medicaid provider (is_atypical=True, no NPI):
      - medicaid_provider_id required (NM108=XX, NM109=medicaid_provider_id)
      - npi must be absent — never fabricate one

  Batch:
    - trading_partner with sender_id + receiver_id (ISA/GS)
    - environment (TEST or PRODUCTION)

Validation returns a flat list of human-readable error strings.  A non-empty
list means the batch is NOT ready and every error is reported to RedArt.
"""

from __future__ import annotations

from django.db.models import Prefetch

from apps.claim.models import BatchClaim, SubmissionBatch
from apps.claim.utils.service import assert_claim_ready_for_batch
from apps.claim_service_line.models import ClaimServiceLine
from apps.edi.utils.envelope import get_edi_envelope_config


# ---------------------------------------------------------------------------
# Internal sub-validators — each returns a list of error strings.
# ---------------------------------------------------------------------------

def _validate_trading_partner(batch) -> list[str]:
    errors = []
    if not batch.trading_partner_id:
        errors.append("Batch is missing a trading_partner (required for ISA/GS sender/receiver).")
        return errors
    partner = batch.trading_partner
    if not partner.is_active:
        errors.append(f"TradingPartner {partner.id} is inactive.")
    if not (partner.sender_id or "").strip():
        errors.append(f"TradingPartner {partner.id} is missing sender_id (ISA06/GS02).")
    if not (partner.receiver_id or "").strip():
        errors.append(f"TradingPartner {partner.id} is missing receiver_id (ISA08/GS03).")
    return errors


def _validate_envelope(batch) -> list[str]:
    try:
        get_edi_envelope_config(batch.environment or "TEST")
    except Exception as exc:  # pragma: no cover
        return [f"EDI envelope configuration error: {exc}"]
    return []


def _validate_patient(patient, claim_label: str) -> list[str]:
    """
    For Colorado NEMT 837P, medicaid_member_id is the ONLY mandatory field.

    DOB / gender / address are optional — the generator emits them when present.
    Never fabricate any missing demographic.
    """
    errors = []
    medicaid_id = (patient.medicaid_member_id or "").strip()
    if not medicaid_id:
        errors.append(
            f"{claim_label}: patient {patient.id} is missing medicaid_member_id "
            "(NM1*IL MI — the Colorado Medicaid Member ID, mandatory for 837P)."
        )
    return errors


def _validate_provider(provider, claim_label: str) -> list[str]:
    """
    Standard NPI provider:  npi + tax_id + taxonomy_code required.
    Atypical provider:       medicaid_provider_id + taxonomy_code required; no NPI.
    """
    errors = []
    is_atypical = bool(getattr(provider, "is_atypical", False))

    if is_atypical:
        medicaid_pid = (getattr(provider, "medicaid_provider_id", None) or "").strip()
        if not medicaid_pid:
            errors.append(
                f"{claim_label}: provider {provider.id} is marked atypical but "
                "is missing medicaid_provider_id "
                "(NM108=XX, NM109=medicaid_provider_id)."
            )
        npi = (provider.npi or "").strip()
        if npi:
            errors.append(
                f"{claim_label}: provider {provider.id} is marked atypical but has "
                "an NPI set. Atypical providers must not have an NPI — never fabricate one."
            )
    else:
        if not (provider.taxonomy_code or "").strip():
            errors.append(
                f"{claim_label}: provider {provider.id} is missing taxonomy_code "
                "(required for standard NPI billing provider identity)."
            )
        npi = (provider.npi or "").strip()
        if not npi:
            errors.append(
                f"{claim_label}: provider {provider.id} is missing NPI "
                "(required for standard 837P billing provider NM108=XX). "
                "If this is an atypical provider, set is_atypical=True."
            )
        tax_id_raw = (getattr(provider, "tax_id", None) or "").strip()
        tax_id = "".join(ch for ch in tax_id_raw if ch.isdigit())
        if not tax_id:
            errors.append(
                f"{claim_label}: provider {provider.id} is missing tax_id (EIN/TIN). "
                "REF*EI is required in 837P 2010AA when NM108=XX (NPI provider). "
                "Set tax_id via the provider API — never hard-code it."
            )

    return errors


def _validate_service_lines(claim, service_lines: list, claim_label: str) -> list[str]:
    errors = []
    if not service_lines:
        errors.append(
            f"{claim_label}: has no active service lines. "
            "At least one service line with procedure_code and charge is required."
        )
        return errors

    for idx, line in enumerate(service_lines, start=1):
        proc = (line.procedure_code or "").strip()
        if not proc:
            errors.append(
                f"{claim_label}: service line {idx} (id={line.id}) is missing "
                "procedure_code. RedArt must supply the finalized procedure code — "
                "this backend never defaults or fabricates one."
            )
        if line.charge is None:
            errors.append(
                f"{claim_label}: service line {idx} (id={line.id}) is missing charge."
            )

    return errors


def _validate_claim(row, claim_label: str) -> list[str]:
    """Full pre-flight for one BatchClaim row."""
    errors = []
    claim = row.claim
    if claim is None or not claim.is_active:
        return [f"BatchClaim {row.id}: has no active claim."]

    try:
        assert_claim_ready_for_batch(claim)
    except ValueError as exc:
        errors.append(f"{claim_label}: {exc}")

    if not (claim.diagnosis_code or "").strip():
        errors.append(f"{claim_label}: missing diagnosis_code.")
    if not (claim.place_of_service or "").strip():
        errors.append(f"{claim_label}: missing place_of_service.")

    trip = claim.trip
    if trip is None:
        errors.append(f"{claim_label}: missing trip.")
        return errors

    if trip.patient_id is None:
        errors.append(f"{claim_label}: trip has no patient.")
    else:
        errors.extend(_validate_patient(trip.patient, claim_label))

    if trip.provider_id is None:
        errors.append(f"{claim_label}: trip has no billing provider.")
    else:
        errors.extend(_validate_provider(trip.provider, claim_label))

    service_lines = list(claim.service_lines.all())
    errors.extend(_validate_service_lines(claim, service_lines, claim_label))

    return errors


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def assert_batch_ready_for_837p_generation(batch) -> bool:
    """
    Raise ValueError with a combined error message if the batch cannot safely
    feed an 837P generator.  Returns True on success.

    This is the single authoritative pre-flight gate — never silently allow
    missing required data through to the X12 generator.
    """
    if batch is None or not getattr(batch, "is_active", False):
        raise ValueError("Batch not found or inactive.")

    errors: list[str] = []
    errors.extend(_validate_trading_partner(batch))
    errors.extend(_validate_envelope(batch))

    rows = list(
        BatchClaim.objects.with_relations()
        .filter(batch_id=batch.id, is_active=True)
        .select_related(
            "claim",
            "claim__trip",
            "claim__trip__patient",
            "claim__trip__provider",
        )
        .prefetch_related(
            Prefetch(
                "claim__service_lines",
                queryset=ClaimServiceLine.objects.filter(is_active=True).order_by("id"),
            )
        )
    )

    if not rows:
        raise ValueError("Batch has no active claims; cannot generate 837P.")

    for row in rows:
        claim = row.claim
        label = f"Claim {(claim.claim_number or claim.id) if claim else row.id}"
        errors.extend(_validate_claim(row, label))

    if errors:
        # Return ALL errors in one shot so RedArt can fix everything at once.
        combined = "\n".join(f"  • {e}" for e in errors)
        raise ValueError(
            f"Batch {batch.batch_number or batch.id} is not ready for 837P "
            f"generation ({len(errors)} error(s)):\n{combined}"
        )

    return True


def collect_batch_readiness_errors(batch) -> list[str]:
    """
    Like assert_batch_ready_for_837p_generation but returns errors as a list
    instead of raising — useful for the validate API endpoint.
    """
    if batch is None or not getattr(batch, "is_active", False):
        return ["Batch not found or inactive."]

    errors: list[str] = []
    errors.extend(_validate_trading_partner(batch))
    errors.extend(_validate_envelope(batch))

    rows = list(
        BatchClaim.objects.with_relations()
        .filter(batch_id=batch.id, is_active=True)
        .select_related(
            "claim",
            "claim__trip",
            "claim__trip__patient",
            "claim__trip__provider",
        )
        .prefetch_related(
            Prefetch(
                "claim__service_lines",
                queryset=ClaimServiceLine.objects.filter(is_active=True).order_by("id"),
            )
        )
    )

    if not rows:
        errors.append("Batch has no active claims.")
        return errors

    for row in rows:
        claim = row.claim
        label = f"Claim {(claim.claim_number or claim.id) if claim else row.id}"
        errors.extend(_validate_claim(row, label))

    return errors


def load_batch_for_837p(batch_id):
    batch = (
        SubmissionBatch.objects.select_related("trading_partner")
        .filter(pk=batch_id, is_active=True)
        .first()
    )
    if batch is None:
        raise ValueError("Batch not found or inactive.")
    assert_batch_ready_for_837p_generation(batch)
    return batch
