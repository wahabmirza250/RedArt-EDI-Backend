"""Claim domain services."""

from decimal import Decimal

from django.db import transaction
from django.db.models import Count, Sum
from django.utils import timezone

from apps.claim.choices import (
    AttachmentRoute,
    AttachmentStatus,
    AttachmentSubmissionStatus,
    ClaimStatus,
    DocumentStatus,
    DocumentType,
)
from apps.claim.models import (
    BatchClaim,
    Claim,
    ClaimDocument,
    SubmissionBatch,
)
from apps.claim_service_line.models import ClaimServiceLine
from apps.long_distance_rule.utils.service import evaluate_trip_mileage
from apps.nemt_trip.models import NemtTrip

# Required package for long-distance / attachment_required claims (Ali 78-mile path).
REQUIRED_LONG_DISTANCE_DOC_TYPES = (
    DocumentType.STANDARD_TRIP_LOG,
    DocumentType.MILE_25_VERIFICATION,
)


def apply_long_distance_flags(claim, trip):
    """
    Set attachment flags once from trip mileage rules.
    Call at create/update-from-trip time — do not re-decide after 999.
    """
    county = None
    if trip.patient_id:
        county = trip.patient.county

    result = evaluate_trip_mileage(
        one_way_miles=trip.one_way_miles,
        mileage_units=trip.mileage_units,
        county=county,
    )
    claim.attachment_required = bool(result["attachment_required"])
    if claim.attachment_required:
        claim.attachment_route = AttachmentRoute.HCPF_APPROVED_CHANNEL
        claim.attachment_status = AttachmentStatus.PENDING
        if claim.status in (None, ClaimStatus.DRAFT, ""):
            claim.status = ClaimStatus.DOCUMENTS_REQUIRED
    else:
        claim.attachment_route = AttachmentRoute.NONE
        claim.attachment_status = AttachmentStatus.NOT_REQUIRED
        if claim.status in (None, ClaimStatus.DRAFT, ""):
            claim.status = ClaimStatus.READY_FOR_837P
    return result


def document_is_ready(doc):
    return (
        doc is not None
        and doc.is_active
        and doc.status == DocumentStatus.COMPLETE
        and bool(doc.is_signed)
        and bool((doc.blob_ref or "").strip())
    )


def prefetch_claim_documents_map(claim_ids):
    """Bulk-load active documents keyed by claim_id → document_type."""
    if not claim_ids:
        return {}
    rows = ClaimDocument.objects.filter(
        claim_id__in=claim_ids,
        is_active=True,
    ).only(
        "claim_id",
        "document_type",
        "status",
        "is_signed",
        "blob_ref",
        "is_active",
    )
    by_claim: dict[int, dict] = {}
    for doc in rows:
        if not doc.document_type:
            continue
        by_claim.setdefault(doc.claim_id, {})[doc.document_type] = doc
    return by_claim


def evaluate_claim_documents(claim, docs_by_type=None):
    """
    Return completeness snapshot for a claim.
    Long-distance claims need trip log + 25+ verification, both signed COMPLETE.
    """
    required = []
    if claim.attachment_required:
        required = list(REQUIRED_LONG_DISTANCE_DOC_TYPES)

    if docs_by_type is None:
        docs = {
            d.document_type: d
            for d in ClaimDocument.objects.filter(claim_id=claim.id, is_active=True)
            if d.document_type
        }
    else:
        docs = docs_by_type

    missing = []
    incomplete = []
    for doc_type in required:
        doc = docs.get(doc_type)
        if doc is None:
            missing.append(doc_type)
        elif not document_is_ready(doc):
            incomplete.append(doc_type)

    complete = not missing and not incomplete
    return {
        "attachment_required": claim.attachment_required,
        "required_types": required,
        "missing_types": missing,
        "incomplete_types": incomplete,
        "documents_complete": complete if required else True,
        "can_submit": (not claim.attachment_required) or complete,
    }


@transaction.atomic
def sync_claim_document_status(claim):
    """
    Flip claim status from documents:
    - incomplete long-distance package → DOCUMENTS_REQUIRED (blocked)
    - complete package → READY_FOR_837P
    Does not downgrade claims already past READY_FOR_837P / EDI statuses.
    """
    if claim is None:
        return None

    claim = Claim.objects.select_for_update().filter(pk=claim.pk).first()
    if claim is None:
        return None

    snapshot = evaluate_claim_documents(claim)
    terminal = {
        ClaimStatus.EDI_SENT,
        ClaimStatus.EDI_ACCEPTED,
        ClaimStatus.UNDER_REVIEW,
        ClaimStatus.PAID,
        ClaimStatus.DENIED,
        ClaimStatus.ATTACHMENT_QUEUED,
        ClaimStatus.ATTACHMENT_SUBMITTED,
        ClaimStatus.ATTACHMENT_CONFIRMED,
    }
    if claim.status in terminal:
        return snapshot

    if not claim.attachment_required:
        if claim.status in (
            ClaimStatus.DRAFT,
            ClaimStatus.DOCUMENTS_REQUIRED,
            ClaimStatus.DOCUMENTS_COMPLETE,
            None,
            "",
        ):
            claim.status = ClaimStatus.READY_FOR_837P
            claim.save(update_fields=["status", "updated_at"])
        return snapshot

    if snapshot["documents_complete"]:
        claim.status = ClaimStatus.READY_FOR_837P
        claim.save(update_fields=["status", "updated_at"])
    else:
        claim.status = ClaimStatus.DOCUMENTS_REQUIRED
        claim.save(update_fields=["status", "updated_at"])
    return snapshot


def assert_claim_ready_for_batch(claim):
    """Raise ValueError if claim cannot enter an EDI batch (docs incomplete)."""
    if claim is None or not claim.is_active:
        raise ValueError("Claim not found or inactive.")

    sync_claim_document_status(claim)
    claim.refresh_from_db()
    snapshot = evaluate_claim_documents(claim)
    if not snapshot["can_submit"] or claim.status == ClaimStatus.DOCUMENTS_REQUIRED:
        missing = snapshot["missing_types"] + snapshot["incomplete_types"]
        raise ValueError(
            "Claim documents incomplete; submission blocked. "
            f"Needs: {', '.join(missing) or 'required signed documents'}."
        )
    return snapshot


def validate_claim_for_edi(claim, *, update_status=True):
    """
    Handoff-style readiness check for RedArt.
    Returns {"ready": bool, "errors": [str, ...], ...} without raising.
    """
    if claim is None:
        return {
            "ready": False,
            "errors": ["Claim not found."],
            "warnings": [],
            "claim_id": None,
            "status": None,
        }

    claim = (
        Claim.objects.with_relations()
        .filter(pk=getattr(claim, "pk", claim), is_active=True)
        .first()
    )
    if claim is None:
        return {
            "ready": False,
            "errors": ["Claim not found or inactive."],
            "warnings": [],
            "claim_id": None,
            "status": None,
        }

    errors = []
    warnings = []

    if update_status:
        sync_claim_document_status(claim)
        claim.refresh_from_db()

    snapshot = evaluate_claim_documents(claim)
    if not snapshot["can_submit"]:
        for doc_type in snapshot.get("missing_types") or []:
            errors.append(f"Required document missing: {doc_type}")
        for doc_type in snapshot.get("incomplete_types") or []:
            errors.append(f"Required document incomplete/unsigned: {doc_type}")
        if not errors:
            errors.append("Supporting documents incomplete for long-distance claim.")

    if not claim.diagnosis_code:
        errors.append("Diagnosis code missing.")
    if not claim.place_of_service:
        errors.append("Place of service missing.")
    if claim.total_charge is None:
        warnings.append("Total charge is empty (will use service-line sum when present).")

    trip = claim.trip
    if trip is None:
        errors.append("Claim is missing trip.")
    else:
        if not trip.service_date:
            errors.append("Trip service date missing.")
        patient = trip.patient
        if patient is None:
            errors.append("Trip is missing patient.")
        else:
            if not (patient.medicaid_member_id or "").strip():
                errors.append(
                    "Patient medicaid_member_id is missing "
                    "(required — Colorado Medicaid Member ID, NM1*IL MI in 837P). "
                    "Never fabricated; must be supplied by RedArt."
                )
        provider = trip.provider
        if provider is None:
            errors.append("Trip is missing billing provider.")
        else:
            is_atypical = bool(getattr(provider, "is_atypical", False))
            if is_atypical:
                if not (getattr(provider, "medicaid_provider_id", None) or "").strip():
                    errors.append(
                        "Provider medicaid_provider_id is missing "
                        "(required for atypical providers, NM108=XX / NM109 "
                        "medicaid_provider_id in 837P)."
                    )
            else:
                if not (provider.npi or "").strip():
                    errors.append(
                        "Provider npi is missing "
                        "(required for standard providers, NM108=XX in 837P). "
                        "If this is an atypical provider, set is_atypical=True."
                    )
                tax_id = "".join(
                    ch for ch in str(getattr(provider, "tax_id", None) or "") if ch.isdigit()
                )
                if not tax_id:
                    errors.append(
                        "Provider tax_id (EIN/TIN) is missing "
                        "(required for REF*EI in 837P 2010AA when NM108=XX). "
                        "Set via the provider API; never hard-coded."
                    )

    if not claim.service_lines.filter(is_active=True).exists():
        errors.append("Claim has no active service lines.")

    ready = len(errors) == 0
    if (
        update_status
        and ready
        and claim.status
        in (
            ClaimStatus.DRAFT,
            ClaimStatus.DOCUMENTS_REQUIRED,
            ClaimStatus.DOCUMENTS_COMPLETE,
            None,
            "",
        )
    ):
        claim.status = ClaimStatus.READY_FOR_837P
        claim.save(update_fields=["status", "updated_at"])
        claim.refresh_from_db()

    return {
        "ready": ready,
        "errors": errors,
        "warnings": warnings,
        "claim_id": claim.id,
        "claim_number": claim.claim_number,
        "external_id": claim.external_id,
        "status": claim.status,
        "attachment_required": claim.attachment_required,
        "attachment_status": claim.attachment_status,
        "document_snapshot": {
            "can_submit": snapshot["can_submit"],
            "documents_complete": snapshot["documents_complete"],
            "required_types": snapshot.get("required_types") or [],
            "missing_types": snapshot.get("missing_types") or [],
            "incomplete_types": snapshot.get("incomplete_types") or [],
        },
    }


def get_claim_status_payload(claim):
    """Aggregate claim + latest batch / EDI file / 999 for RedArt UI."""
    from apps.edi.models import EDIAcknowledgement, EDIFile

    claim = (
        Claim.objects.with_relations()
        .filter(pk=getattr(claim, "pk", claim), is_active=True)
        .first()
    )
    if claim is None:
        raise ValueError("Claim not found or inactive.")

    readiness = validate_claim_for_edi(claim, update_status=False)

    batch_row = (
        BatchClaim.objects.select_related("batch", "batch__trading_partner")
        .filter(claim_id=claim.id, is_active=True)
        .order_by("-id")
        .first()
    )
    batch_data = None
    edi_data = None
    ack_data = None
    if batch_row and batch_row.batch_id:
        batch = batch_row.batch
        batch_data = {
            "id": batch.id,
            "batch_number": batch.batch_number,
            "status": batch.status,
            "environment": batch.environment,
            "st02": batch_row.st02,
            "trading_partner_id": batch.trading_partner_id,
        }
        edi = (
            EDIFile.objects.filter(batch_id=batch.id, is_active=True)
            .order_by("-id")
            .first()
        )
        if edi is not None:
            edi_data = {
                "id": edi.id,
                "filename": edi.filename,
                "status": edi.status,
                "uploaded_at": edi.uploaded_at,
            }
        ack = (
            EDIAcknowledgement.objects.filter(
                batch_id=batch.id,
                is_active=True,
            )
            .order_by("-id")
            .first()
        )
        if ack is not None:
            ack_data = {
                "id": ack.id,
                "ack_type": ack.ack_type,
                "status": ack.status,
                "affected_st02": ack.affected_st02,
                "message": ack.message,
                "acknowledged_at": ack.acknowledged_at,
            }

    return {
        "claim_id": claim.id,
        "claim_number": claim.claim_number,
        "external_id": claim.external_id,
        "status": claim.status,
        "attachment_required": claim.attachment_required,
        "attachment_status": claim.attachment_status,
        "total_charge": str(claim.total_charge) if claim.total_charge is not None else None,
        "ready": readiness["ready"],
        "errors": readiness["errors"],
        "batch": batch_data,
        "edi_file": edi_data,
        "acknowledgement": ack_data,
        "updated_at": claim.updated_at,
    }


def get_batch_status_payload(batch):
    """Aggregate batch status for RedArt UI."""
    from apps.edi.models import EDIAcknowledgement, EDIFile

    batch = (
        SubmissionBatch.objects.select_related("trading_partner")
        .filter(pk=getattr(batch, "pk", batch), is_active=True)
        .first()
    )
    if batch is None:
        raise ValueError("Batch not found or inactive.")

    rows = list(
        BatchClaim.objects.select_related("claim")
        .filter(batch_id=batch.id, is_active=True)
        .order_by("id")
    )
    edi_files = list(
        EDIFile.objects.filter(batch_id=batch.id, is_active=True)
        .order_by("-id")
        .values("id", "filename", "status", "uploaded_at")[:10]
    )
    acks = list(
        EDIAcknowledgement.objects.filter(batch_id=batch.id, is_active=True)
        .order_by("-id")
        .values(
            "id",
            "ack_type",
            "status",
            "affected_st02",
            "message",
            "acknowledged_at",
        )[:10]
    )

    return {
        "batch_id": batch.id,
        "batch_number": batch.batch_number,
        "status": batch.status,
        "environment": batch.environment,
        "claim_count": batch.claim_count,
        "total_amount": str(batch.total_amount)
        if batch.total_amount is not None
        else None,
        "trading_partner_id": batch.trading_partner_id,
        "claims": [
            {
                "claim_id": row.claim_id,
                "claim_number": row.claim.claim_number if row.claim_id else None,
                "claim_status": row.claim.status if row.claim_id else None,
                "st02": row.st02,
            }
            for row in rows
        ],
        "edi_files": edi_files,
        "acknowledgements": acks,
        "updated_at": batch.updated_at,
    }


def refresh_batch_totals(batch):
    """Recompute claim_count and total_amount from active BatchClaim rows."""
    if batch is None:
        return None
    agg = BatchClaim.objects.filter(batch_id=batch.id, is_active=True).aggregate(
        count=Count("id"),
        total=Sum("claim__total_charge"),
    )
    batch.claim_count = agg["count"] or 0
    batch.total_amount = agg["total"] or Decimal("0.00")
    batch.save(update_fields=["claim_count", "total_amount", "updated_at"])
    return batch


def next_st02_for_batch(batch):
    """Allocate next ST02 as zero-padded 4-digit sequence within the batch."""
    existing = (
        BatchClaim.objects.filter(batch_id=batch.id, is_active=True)
        .exclude(st02__isnull=True)
        .exclude(st02="")
        .values_list("st02", flat=True)
    )
    max_n = 0
    for value in existing:
        if str(value).isdigit():
            max_n = max(max_n, int(value))
    return f"{max_n + 1:04d}"


@transaction.atomic
def add_claim_to_batch(*, batch_id, claim_id, st02=None):
    batch = (
        SubmissionBatch.objects.select_for_update()
        .filter(pk=batch_id, is_active=True)
        .first()
    )
    if batch is None:
        raise ValueError("Batch not found or inactive.")

    claim = Claim.objects.select_for_update().filter(pk=claim_id, is_active=True).first()
    if claim is None:
        raise ValueError("Claim not found or inactive.")

    assert_claim_ready_for_batch(claim)

    if BatchClaim.objects.filter(
        batch_id=batch.id, claim_id=claim.id, is_active=True
    ).exists():
        raise ValueError("Claim is already in this batch.")

    st02 = (st02 or "").strip() or next_st02_for_batch(batch)
    if BatchClaim.objects.filter(
        batch_id=batch.id, st02=st02, is_active=True
    ).exists():
        raise ValueError(f"ST02 {st02} is already used in this batch.")

    row = BatchClaim.objects.create(
        batch=batch,
        claim=claim,
        st02=st02,
        is_active=True,
    )
    refresh_batch_totals(batch)
    return row


@transaction.atomic
def create_claim_from_trip(
    *,
    trip_id,
    claim_number=None,
    external_id=None,
    diagnosis_code=None,
    place_of_service=None,
    procedure_code=None,
    create_service_line=True,
    service_lines=None,
):
    """
    Create a claim (and optional demo service line) from an existing trip.
    Enforces one active claim per trip via DB unique constraint.
    """
    trip = NemtTrip.objects.with_relations().filter(pk=trip_id, is_active=True).first()
    if trip is None:
        raise ValueError("Trip not found or inactive.")

    if Claim.objects.filter(trip_id=trip.id, is_active=True).exists():
        raise ValueError("A claim already exists for this trip.")

    claim = Claim(
        claim_number=claim_number,
        external_id=external_id,
        trip=trip,
        diagnosis_code=diagnosis_code,
        place_of_service=place_of_service,
        total_charge=trip.charge,
        is_active=True,
    )
    apply_long_distance_flags(claim, trip)
    claim.save()

    line = None
    if service_lines:
        for item in service_lines:
            created = ClaimServiceLine.objects.create(
                claim=claim,
                procedure_code=item.get("procedure_code"),
                from_date=trip.service_date,
                to_date=trip.service_date,
                units=item.get("units"),
                mileage=item.get("mileage"),
                charge=item.get("charge"),
                is_active=True,
            )
            if line is None:
                line = created
    elif create_service_line:
        line = ClaimServiceLine.objects.create(
            claim=claim,
            procedure_code=procedure_code,
            from_date=trip.service_date,
            to_date=trip.service_date,
            units=trip.mileage_units,
            mileage=trip.one_way_miles,
            charge=trip.charge,
            is_active=True,
        )

    return claim, line


@transaction.atomic
def sync_claim_from_attachment_submission(submission):
    """
    Mirror AttachmentSubmission onto Claim.attachment_status.
    Never re-decides attachment_required. Never sets PAID.
    """
    if submission is None or not submission.claim_id:
        return None

    claim = Claim.objects.select_for_update().filter(pk=submission.claim_id).first()
    if claim is None:
        return None

    status = submission.status
    update_fields = ["attachment_status", "updated_at"]

    if status == AttachmentSubmissionStatus.CONFIRMED:
        claim.attachment_status = AttachmentStatus.CONFIRMED
        if submission.confirmed_at is None:
            submission.confirmed_at = timezone.now()
            submission.save(update_fields=["confirmed_at", "updated_at"])
        if claim.status in (
            ClaimStatus.ATTACHMENT_REQUIRED,
            ClaimStatus.ATTACHMENT_QUEUED,
            ClaimStatus.ATTACHMENT_SUBMITTED,
            ClaimStatus.DOCUMENTS_COMPLETE,
            ClaimStatus.DOCUMENTS_REQUIRED,
            ClaimStatus.READY_FOR_837P,
        ):
            claim.status = ClaimStatus.ATTACHMENT_CONFIRMED
            update_fields.append("status")
    elif status == AttachmentSubmissionStatus.SUBMITTED:
        claim.attachment_status = AttachmentStatus.SUBMITTED
        if submission.submitted_at is None:
            submission.submitted_at = timezone.now()
            submission.save(update_fields=["submitted_at", "updated_at"])
        if claim.status in (
            ClaimStatus.ATTACHMENT_REQUIRED,
            ClaimStatus.ATTACHMENT_QUEUED,
            ClaimStatus.DOCUMENTS_COMPLETE,
            ClaimStatus.READY_FOR_837P,
        ):
            claim.status = ClaimStatus.ATTACHMENT_SUBMITTED
            update_fields.append("status")
    elif status == AttachmentSubmissionStatus.QUEUED:
        claim.attachment_status = AttachmentStatus.QUEUED
        if claim.status in (
            ClaimStatus.ATTACHMENT_REQUIRED,
            ClaimStatus.DOCUMENTS_COMPLETE,
            ClaimStatus.READY_FOR_837P,
        ):
            claim.status = ClaimStatus.ATTACHMENT_QUEUED
            update_fields.append("status")
    elif status == AttachmentSubmissionStatus.FAILED:
        claim.attachment_status = AttachmentStatus.FAILED

    claim.save(update_fields=update_fields)
    return claim
