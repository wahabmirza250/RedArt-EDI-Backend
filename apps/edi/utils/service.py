"""EDI control-number allocation and file record helpers."""

from zoneinfo import ZoneInfo

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.claim.choices import BatchStatus, ClaimStatus
from apps.claim.models import BatchClaim, Claim, SubmissionBatch
from apps.edi.choices import (
    AcknowledgementStatus,
    AcknowledgementType,
    EDIFileStatus,
    TransactionType,
)
from apps.edi.models import EDIAcknowledgement, EDIControlNumber, EDIFile, EDIValidationReport
from apps.edi.utils.readiness import assert_batch_ready_for_837p_generation


def _digits_only(value):
    if value is None:
        return None
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    return digits or None


def next_isa13(environment):
    """Next 9-digit ISA13 for the environment (starts at 000000001)."""
    from django.db.models import Max

    # Lock existing rows for this env so concurrent allocators serialize.
    (
        EDIControlNumber.objects.select_for_update()
        .filter(environment=environment, is_active=True)
        .order_by("id")
        .first()
    )
    agg = (
        EDIControlNumber.objects.filter(environment=environment, is_active=True)
        .exclude(isa13__isnull=True)
        .exclude(isa13="")
        .aggregate(m=Max("isa13"))
    )
    raw = agg.get("m")
    max_n = int(_digits_only(raw) or 0) if raw else 0
    # Prefer numeric max: Max on zero-padded strings works for fixed width.
    rows = (
        EDIControlNumber.objects.filter(environment=environment, is_active=True)
        .exclude(isa13__isnull=True)
        .exclude(isa13="")
        .values_list("isa13", flat=True)[:5000]
    )
    for value in rows:
        digits = _digits_only(value)
        if digits:
            max_n = max(max_n, int(digits))
    return f"{max_n + 1:09d}"


def next_gs06(environment):
    """Next GS06 for the environment (integer sequence as string)."""
    (
        EDIControlNumber.objects.select_for_update()
        .filter(environment=environment, is_active=True)
        .order_by("id")
        .first()
    )
    max_n = 0
    rows = (
        EDIControlNumber.objects.filter(environment=environment, is_active=True)
        .exclude(gs06__isnull=True)
        .exclude(gs06="")
        .values_list("gs06", flat=True)[:5000]
    )
    for value in rows:
        digits = _digits_only(value)
        if digits:
            max_n = max(max_n, int(digits))
    return str(max_n + 1)


def build_colorado_837p_filename(
    *,
    sender_id,
    generated_at=None,
    part=1,
    of_parts=1,
):
    """
    HCPF production file name:
    tp{sender}-837P-{YYYYMMDDHHMMSSmmm}-1of1.x12

    HCPF requires the timestamp in Mountain Time, regardless of Django's
    configured TIME_ZONE (the service runs in UTC), and accepts only 1of1 for
    837 transactions.
    """
    if part != 1 or of_parts != 1:
        raise ValueError("HCPF 837P filenames only support the required 1of1 value.")

    when = generated_at or timezone.now()
    if timezone.is_naive(when):
        when = timezone.make_aware(when, timezone=ZoneInfo("UTC"))
    mountain = when.astimezone(ZoneInfo("America/Denver"))
    stamp = mountain.strftime("%Y%m%d%H%M%S") + f"{mountain.microsecond // 1000:03d}"

    sender = (sender_id or "").strip()
    if not sender:
        raise ValueError("HCPF Trading Partner ID is required for the 837P filename.")
    if sender[:2].lower() == "tp":
        sender = sender[2:]
    sender = f"tp{sender}"
    return f"{sender}-837P-{stamp}-1of1.x12"


@transaction.atomic
def allocate_control_numbers(
    *,
    batch_id,
    isa13=None,
    gs06=None,
    environment=None,
    force_new=False,
):
    """
    Allocate an EDIControlNumber for a batch.

    force_new=False (default / idempotent retry): return the existing active
      control row for this batch when one exists.  Safe for retrying the same
      physical upload without creating a duplicate interchange.

    force_new=True (new physical file / regeneration): always create a fresh
      ISA13/GS06 pair.  Required whenever a new X12 file is generated so that
      no two different physical files share the same interchange control number.
    """
    batch = (
        SubmissionBatch.objects.select_for_update(of=("self",))
        .select_related("trading_partner")
        .filter(pk=batch_id, is_active=True)
        .first()
    )
    if batch is None:
        raise ValueError("Batch not found or inactive.")

    if not force_new:
        existing = (
            EDIControlNumber.objects.select_for_update(of=("self",))
            .filter(batch_id=batch.id, is_active=True)
            .first()
        )
        if existing is not None:
            return existing, False

    env = (environment or batch.environment or "TEST").strip().upper()
    last_error = None
    for _ in range(5):
        isa = _digits_only(isa13) or next_isa13(env)
        gs = _digits_only(gs06) or next_gs06(env)

        if len(isa) > 9:
            raise ValueError("isa13 must be at most 9 digits.")
        isa = isa.zfill(9)

        try:
            with transaction.atomic():
                row = EDIControlNumber.objects.create(
                    batch=batch,
                    environment=env,
                    isa13=isa,
                    gs06=gs,
                    is_active=True,
                )
            return row, True
        except IntegrityError as exc:
            last_error = exc
            # Another worker may have created this batch's control row.
            existing = (
                EDIControlNumber.objects.select_for_update(of=("self",))
                .filter(batch_id=batch.id, is_active=True)
                .first()
            )
            if existing is not None and not force_new:
                return existing, False
            # ISA13/GS06 collision — retry with next numbers (clear overrides).
            isa13 = None
            gs06 = None
            continue

    raise ValueError(
        f"Unable to allocate unique ISA13/GS06 after retries: {last_error}"
    )


@transaction.atomic
def create_edi_file_for_batch(
    *,
    batch_id,
    transaction_type=TransactionType.X837P,
    filename=None,
    file_hash=None,
    path_or_blob_ref=None,
    status=EDIFileStatus.GENERATED,
    uploaded_at=None,
    allocate_controls=True,
):
    """
    Create an EDIFile row for a batch.
    Optionally allocates control numbers first.
    Does not generate X12 payload yet — stores transport metadata only.
    """
    batch = (
        SubmissionBatch.objects.select_for_update(of=("self",))
        .select_related("trading_partner")
        .filter(pk=batch_id, is_active=True)
        .first()
    )
    if batch is None:
        raise ValueError("Batch not found or inactive.")

    # Docs complete + demographics + trading partner + service lines.
    assert_batch_ready_for_837p_generation(batch)

    if batch.claim_count < 1:
        raise ValueError("Batch has no claims; cannot create EDI file.")

    control = None
    if allocate_controls:
        control, _ = allocate_control_numbers(batch_id=batch.id)

    txn = (transaction_type or TransactionType.X837P).strip().upper()
    if txn not in TransactionType.values:
        raise ValueError("Invalid transaction_type.")

    if not filename:
        sender = None
        if batch.trading_partner_id:
            sender = batch.trading_partner.sender_id
        filename = build_colorado_837p_filename(sender_id=sender)

    if EDIFile.objects.filter(filename=filename).exists():
        raise ValueError("An EDI file with this filename already exists.")

    status_value = (status or EDIFileStatus.GENERATED).strip().upper()
    if status_value not in EDIFileStatus.values:
        raise ValueError("Invalid EDI file status.")

    if status_value == EDIFileStatus.UPLOADED and uploaded_at is None:
        uploaded_at = timezone.now()

    edi_file = EDIFile.objects.create(
        batch=batch,
        control_number=control,
        transaction_type=txn,
        filename=filename,
        file_hash=file_hash,
        path_or_blob_ref=path_or_blob_ref,
        status=status_value,
        uploaded_at=uploaded_at,
        is_active=True,
    )

    if batch.status in (BatchStatus.DRAFT, BatchStatus.READY, None, ""):
        batch.status = BatchStatus.GENERATED
        batch.save(update_fields=["status", "updated_at"])

    # Advance claim status: READY_FOR_837P → EDI_GENERATED
    # "837P generated ≠ uploaded" — per client requirement.
    if edi_file.batch_id:
        claim_ids = BatchClaim.objects.filter(
            batch_id=edi_file.batch_id,
            is_active=True,
            claim_id__isnull=False,
        ).values_list("claim_id", flat=True)
        Claim.objects.filter(
            id__in=claim_ids,
            is_active=True,
            status__in=(
                ClaimStatus.READY_FOR_837P,
                ClaimStatus.DOCUMENTS_COMPLETE,
            ),
        ).update(status=ClaimStatus.EDI_GENERATED, updated_at=timezone.now())

    return edi_file


@transaction.atomic
def mark_edi_file_uploaded(edi_file_id, *, path_or_blob_ref=None, file_hash=None):
    edi_file = (
        EDIFile.objects.select_for_update(of=("self",))
        .select_related("batch")
        .filter(pk=edi_file_id, is_active=True)
        .first()
    )
    if edi_file is None:
        raise ValueError("EDI file not found or inactive.")

    edi_file.status = EDIFileStatus.UPLOADED
    edi_file.uploaded_at = timezone.now()
    update_fields = ["status", "uploaded_at", "updated_at"]
    if path_or_blob_ref is not None:
        edi_file.path_or_blob_ref = path_or_blob_ref
        update_fields.append("path_or_blob_ref")
    if file_hash is not None:
        edi_file.file_hash = file_hash
        update_fields.append("file_hash")
    edi_file.save(update_fields=update_fields)

    batch = edi_file.batch
    if batch is not None and batch.status in (
        BatchStatus.READY,
        BatchStatus.GENERATED,
        BatchStatus.DRAFT,
    ):
        batch.status = BatchStatus.SUBMITTED
        batch.save(update_fields=["status", "updated_at"])

    # Business claim status: EDI_GENERATED → EDI_SENT.
    # EDI_SENT means "written to HCPF SFTP/MFT" — not gateway pickup, not paid.
    # HCPF pickup evidence requires a 999/TA1 (→ EDI_ACCEPTED or EDI_REJECTED).
    # Also accepts READY_FOR_837P / DOCUMENTS_COMPLETE for backward compat with
    # flows that did not pass through the EDI_GENERATED intermediate state.
    if edi_file.batch_id:
        claim_ids = BatchClaim.objects.filter(
            batch_id=edi_file.batch_id,
            is_active=True,
            claim_id__isnull=False,
        ).values_list("claim_id", flat=True)
        Claim.objects.filter(
            id__in=claim_ids,
            is_active=True,
            status__in=(
                ClaimStatus.READY_FOR_837P,
                ClaimStatus.DOCUMENTS_COMPLETE,
                ClaimStatus.EDI_GENERATED,
            ),
        ).update(status=ClaimStatus.EDI_SENT, updated_at=timezone.now())

    return edi_file


def _normalize_st02(value):
    if value in (None, ""):
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return text.zfill(4) if len(text) <= 4 else text
    return text


@transaction.atomic
def apply_edi_acknowledgement(
    *,
    batch_id,
    ack_type=AcknowledgementType.X999,
    status=AcknowledgementStatus.ACCEPTED,
    affected_st02=None,
    raw_file_ref=None,
    edi_file_id=None,
    message=None,
    acknowledged_at=None,
    apply_claim_status=True,
):
    """
    Persist a 999 (or related) acknowledgement and optionally advance claims.
    ACCEPTED → Claim.status EDI_ACCEPTED for matching ST02. Never sets PAID.
    """
    batch = (
        SubmissionBatch.objects.select_for_update(of=("self",))
        .filter(pk=batch_id, is_active=True)
        .first()
    )
    if batch is None:
        raise ValueError("Batch not found or inactive.")

    st02 = _normalize_st02(affected_st02)
    ack_type = str(ack_type or AcknowledgementType.X999).strip().upper()
    status = str(status or AcknowledgementStatus.ACCEPTED).strip().upper()
    when = acknowledged_at or timezone.now()

    edi_file = None
    if edi_file_id:
        edi_file = EDIFile.objects.filter(
            pk=edi_file_id, is_active=True, batch_id=batch.id
        ).first()
        if edi_file is None:
            raise ValueError("EDI file not found for this batch.")
    else:
        edi_file = (
            EDIFile.objects.filter(batch_id=batch.id, is_active=True)
            .order_by("-id")
            .first()
        )

    ref_key = (raw_file_ref or "").strip() or None
    existing_ack = None
    if ref_key:
        existing_ack = (
            EDIAcknowledgement.objects.filter(
                batch_id=batch.id,
                ack_type=ack_type,
                affected_st02=st02,
                raw_file_ref=ref_key,
                is_active=True,
            )
            .order_by("-id")
            .first()
        )
    if existing_ack is None and st02:
        existing_ack = (
            EDIAcknowledgement.objects.filter(
                batch_id=batch.id,
                ack_type=ack_type,
                affected_st02=st02,
                is_active=True,
            )
            .order_by("-id")
            .first()
        )

    if existing_ack is not None:
        return existing_ack, []

    ack = EDIAcknowledgement.objects.create(
        batch=batch,
        edi_file=edi_file,
        ack_type=ack_type,
        status=status,
        affected_st02=st02,
        raw_file_ref=ref_key,
        message=(message or "").strip() or None,
        acknowledged_at=when,
        is_active=True,
    )

    updated_claim_ids = []
    if apply_claim_status and st02:
        row = (
            BatchClaim.objects.select_related("claim")
            .filter(batch_id=batch.id, st02=st02, is_active=True)
            .first()
        )
        if row is None:
            # Also try unpadded match
            row = (
                BatchClaim.objects.select_related("claim")
                .filter(batch_id=batch.id, is_active=True, st02__isnull=False)
                .filter(st02=str(int(st02)) if st02.isdigit() else st02)
                .first()
            )
        claim = row.claim if row else None
        if claim and claim.is_active:
            # Terminal states are never overwritten by acknowledgement signals.
            terminal = (ClaimStatus.PAID, ClaimStatus.DENIED)
            if claim.status not in terminal:
                if status == AcknowledgementStatus.ACCEPTED:
                    # 999 ACCEPTED → EDI_ACCEPTED (never jumps directly to PAID).
                    claim.status = ClaimStatus.EDI_ACCEPTED
                    claim.save(update_fields=["status", "updated_at"])
                    updated_claim_ids.append(claim.id)
                elif status in (
                    AcknowledgementStatus.REJECTED,
                    AcknowledgementStatus.ERROR,
                ):
                    # 999 REJECTED / ERROR → EDI_REJECTED so RedArt knows
                    # this claim needs correction before resubmission.
                    claim.status = ClaimStatus.EDI_REJECTED
                    claim.save(update_fields=["status", "updated_at"])
                    updated_claim_ids.append(claim.id)
                # PARTIAL / ACCEPTED_WITH_ERRORS — leave status as-is for now;
                # the payer's 277 will carry the authoritative adjudication.

    if status == AcknowledgementStatus.ACCEPTED:
        if edi_file and edi_file.status in (
            EDIFileStatus.UPLOADED,
            EDIFileStatus.GENERATED,
            EDIFileStatus.UPLOAD_QUEUED,
        ):
            edi_file.status = EDIFileStatus.ACKNOWLEDGED
            edi_file.save(update_fields=["status", "updated_at"])
        if batch.status in (
            BatchStatus.SUBMITTED,
            BatchStatus.GENERATED,
            BatchStatus.READY,
        ):
            batch.status = BatchStatus.ACKNOWLEDGED
            batch.save(update_fields=["status", "updated_at"])

    return ack, updated_claim_ids


@transaction.atomic
def import_999_acknowledgement(
    *,
    content,
    batch_id,
    edi_file_id=None,
    raw_file_ref=None,
    apply_claim_status=True,
):
    """
    Parse raw 999 X12, map to EDIAcknowledgement fields, and apply side effects.
    """
    from apps.edi.utils.x12 import parse_999

    parsed = parse_999(content)
    return apply_edi_acknowledgement(
        batch_id=batch_id,
        ack_type=parsed["ack_type"],
        status=parsed["status"],
        affected_st02=parsed.get("affected_st02"),
        raw_file_ref=raw_file_ref,
        edi_file_id=edi_file_id,
        message=parsed.get("message"),
        apply_claim_status=apply_claim_status,
    ), parsed


@transaction.atomic
def apply_277_claim_statuses(parsed: dict) -> list[int]:
    """Apply parsed 277 STC lines to matching active claims by claim_number."""
    updated = []
    for line in parsed.get("claim_statuses") or []:
        claim_number = (line.get("claim_number") or "").strip()
        outcome = line.get("outcome")
        if not claim_number or not outcome:
            continue
        claim = (
            Claim.objects.select_for_update()
            .filter(claim_number__iexact=claim_number, is_active=True)
            .first()
        )
        if claim is None:
            continue
        if claim.status in (ClaimStatus.PAID, ClaimStatus.DENIED):
            continue
        if claim.status != outcome:
            claim.status = outcome
            claim.save(update_fields=["status", "updated_at"])
            updated.append(claim.id)
    return updated


@transaction.atomic
def import_277_acknowledgement(
    *,
    content,
    batch_id,
    edi_file_id=None,
    raw_file_ref=None,
    apply_claim_status=True,
):
    """Parse raw 277 X12, persist EDIAcknowledgement, apply claim statuses."""
    from apps.edi.utils.x12 import parse_277

    parsed = parse_277(content)
    ack, _ = apply_edi_acknowledgement(
        batch_id=batch_id,
        ack_type=parsed["ack_type"],
        status=parsed["status"],
        affected_st02=parsed.get("affected_st02"),
        raw_file_ref=raw_file_ref,
        edi_file_id=edi_file_id,
        message=parsed.get("message"),
        apply_claim_status=False,
    )
    claim_ids = []
    if apply_claim_status:
        claim_ids = apply_277_claim_statuses(parsed)
    return (ack, claim_ids), parsed


@transaction.atomic
def import_validation_report(
    *,
    content: str,
    batch_id=None,
    edi_file_id=None,
    raw_file_ref=None,
    file_name=None,
):
    """Parse Edifecs XML report and persist EDIValidationReport (idempotent hash)."""
    from apps.edi.models import EDIValidationReport, EDIFile
    from apps.edi.utils.edifecs_report import parse_edifecs_report

    parsed = parse_edifecs_report(content, file_name=file_name)
    existing = (
        EDIValidationReport.objects.filter(
            file_hash=parsed["file_hash"],
            is_active=True,
        ).first()
    )
    if existing is not None:
        return existing, parsed, False

    batch = None
    if batch_id:
        batch = SubmissionBatch.objects.filter(pk=batch_id, is_active=True).first()
        if batch is None:
            raise ValueError("Batch not found or inactive.")

    edi_file = None
    if edi_file_id:
        edi_file = EDIFile.objects.filter(pk=edi_file_id, is_active=True).first()
        if edi_file is None:
            raise ValueError("EDI file not found or inactive.")
        if batch is None and edi_file.batch_id:
            batch = edi_file.batch

    row = EDIValidationReport.objects.create(
        batch=batch,
        edi_file=edi_file,
        report_type=parsed["report_type"],
        status=parsed["status"],
        task_id=parsed.get("task_id"),
        report_guid=parsed.get("report_guid"),
        file_name=parsed.get("file_name"),
        file_hash=parsed.get("file_hash"),
        error_count=parsed.get("error_count") or 0,
        accepted_claims=parsed.get("accepted_claims"),
        accepted_charge=parsed.get("accepted_charge"),
        raw_file_ref=(raw_file_ref or "").strip() or None,
        message=parsed.get("message"),
        parsed_summary=parsed.get("parsed_summary"),
        is_active=True,
    )
    return row, parsed, True

