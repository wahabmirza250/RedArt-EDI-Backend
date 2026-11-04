"""
Discover and import inbound 999 files from SFTP.

Tracks each remote file as EDI999Import (queued → imported / failed / skipped).
"""

from __future__ import annotations

import hashlib
import logging
import re
import traceback

from django.db import transaction
from django.utils import timezone

from apps.edi.choices import (
    EDI999ImportStatus,
    SFTPDirectoryPurpose,
    ValidationReportStatus,
    ValidationReportType,
)
from apps.edi.models import (
    EDI999Import,
    EDIControlNumber,
    EDIFile,
    EDIValidationReport,
    SFTPDirectory,
)
from apps.edi.utils.import_errors import PermanentImportError
from apps.edi.utils.service import import_999_acknowledgement
from apps.edi.utils.sftp_client import download_bytes_via_sftp, list_remote_files
from apps.edi.utils.x12 import parse_999

logger = logging.getLogger(__name__)

# Files that are definitely not X12 payloads and not error/rejection reports.
_SKIP_NAME_RE = re.compile(
    r"\.(tmp|part|bak)$",
    re.IGNORECASE,
)

# Edifecs rejection/error report extensions — download and save, never silently skip.
_REPORT_EXT_RE = re.compile(
    r"\.(rsp|rjct|description|html?)$",
    re.IGNORECASE,
)

# Filenames that look like a 999/TA1 X12 payload.
_LOOKS_LIKE_999_NAME = re.compile(r"999|TA1|\.edi$|\.x12$|\.txt$", re.IGNORECASE)


def _mark(row: EDI999Import, *, status, message=None, detail=None, finished=False):
    row.status = status
    if message is not None:
        row.message = (message or "")[:500]
    if detail is not None:
        # Never persist full stack traces for API/DB leakage; keep a short note.
        row.detail = (detail or "")[:2000]
    now = timezone.now()
    fields = ["status", "message", "detail", "updated_at"]
    if row.started_at is None and status in (
        EDI999ImportStatus.DOWNLOADING,
        EDI999ImportStatus.PARSING,
        EDI999ImportStatus.QUEUED,
    ):
        row.started_at = now
        fields.append("started_at")
    if finished or status in (
        EDI999ImportStatus.IMPORTED,
        EDI999ImportStatus.FAILED,
        EDI999ImportStatus.SKIPPED,
    ):
        row.finished_at = now
        fields.append("finished_at")
    row.save(update_fields=list(dict.fromkeys(fields)))
    return row


def resolve_inbound_999_directories(*, credentials_id=None):
    """Active dirs used for inbound 999 pulls."""
    from apps.edi.utils.upload import sync_hcpf_directory_paths

    qs = SFTPDirectory.objects.with_relations().filter(
        is_active=True,
        credentials__is_active=True,
        purpose__in=(
            SFTPDirectoryPurpose.INBOUND_999,
            SFTPDirectoryPurpose.GENERAL,
        ),
    )
    if credentials_id:
        qs = qs.filter(credentials_id=credentials_id)
    directories = list(qs.order_by("-id"))
    # Keep Edifecs poll path current (ops may rotate Incoming/Outgoing roles).
    seen = set()
    for directory in directories:
        cred_id = directory.credentials_id
        if cred_id and cred_id not in seen:
            sync_hcpf_directory_paths(credentials=directory.credentials)
            seen.add(cred_id)
    if seen:
        directories = list(qs.order_by("-id"))
    return directories


def resolve_batch_for_999(parsed: dict):
    """
    Map 999 AK1 group control (GS06) and/or ISA13 to a submission batch.
    Returns (batch, edi_file_or_none).
    """
    gs06 = (parsed.get("gs06") or parsed.get("ak1", {}).get("group_control") or "")
    gs06 = str(gs06).strip()
    isa13 = str(parsed.get("isa13") or "").strip()

    control = None
    if gs06:
        control = (
            EDIControlNumber.objects.select_related("batch")
            .filter(is_active=True, gs06=gs06)
            .order_by("-id")
            .first()
        )
        if control is None and gs06.isdigit():
            control = (
                EDIControlNumber.objects.select_related("batch")
                .filter(is_active=True, gs06=str(int(gs06)))
                .order_by("-id")
                .first()
            )
    if control is None and isa13:
        padded = isa13.zfill(9) if isa13.isdigit() else isa13
        control = (
            EDIControlNumber.objects.select_related("batch")
            .filter(is_active=True, isa13=padded)
            .order_by("-id")
            .first()
        )

    batch = control.batch if control else None
    edi_file = None
    if control is not None:
        # T8: match by the exact control number, not just "latest file in batch".
        # A batch may have multiple physical files with different ISA13/GS06 values;
        # attaching the ack to the wrong file corrupts the audit trail.
        edi_file = (
            EDIFile.objects.filter(control_number_id=control.id, is_active=True)
            .order_by("-id")
            .first()
        )
    if edi_file is None and batch is not None:
        # Fallback for legacy records created before per-file control linking.
        edi_file = (
            EDIFile.objects.filter(batch_id=batch.id, is_active=True)
            .order_by("-id")
            .first()
        )
    return batch, edi_file


def _is_report_file(name: str) -> bool:
    """True for Edifecs .rjct / .rsp / .description / .html rejection/error reports."""
    return bool(name and _REPORT_EXT_RE.search(name))


def _candidate_filename(name: str) -> bool:
    """True for filenames that may be 999/TA1 X12 payloads."""
    if not name or name.startswith("."):
        return False
    if _SKIP_NAME_RE.search(name):
        return False
    return bool(_LOOKS_LIKE_999_NAME.search(name))


def save_rejection_report(
    *,
    credentials,
    directory,
    filename: str,
    remote_path: str,
    batch=None,
    edi_file=None,
) -> EDIValidationReport | None:
    """
    Download a .rjct / .rsp / .description / .html file from SFTP and save it
    as an EDIValidationReport so it is never silently lost.

    Returns the saved row or None if already imported (idempotent on hash).
    """
    try:
        data = download_bytes_via_sftp(
            credentials=credentials,
            remote_path=remote_path,
        )
    except Exception as exc:
        logger.exception(
            "Failed to download rejection report %s: %s", remote_path, exc
        )
        return None

    if not data:
        logger.warning("Rejection report %s is empty; skipping.", remote_path)
        return None

    digest = hashlib.sha256(data).hexdigest()

    # Idempotent — if we already saved this exact file, skip.
    existing = EDIValidationReport.objects.filter(
        file_hash=digest, is_active=True
    ).first()
    if existing:
        logger.debug("Rejection report already saved (hash=%s); skipping.", digest)
        return existing

    # Detect report type from extension / filename.
    name_lower = filename.lower()
    if name_lower.endswith(".rjct"):
        report_type = ValidationReportType.AUDIT
        status = ValidationReportStatus.REJECTED
    elif name_lower.endswith(".rsp"):
        report_type = ValidationReportType.SUMMARY
        status = ValidationReportStatus.UNKNOWN
    elif name_lower.endswith(".description"):
        report_type = ValidationReportType.SUMMARY
        status = ValidationReportStatus.ERROR
    else:
        # .html or unknown — attempt parse; fall back to UNKNOWN.
        report_type = ValidationReportType.AUDIT
        status = ValidationReportStatus.UNKNOWN

    # Try XML parse for richer metadata; if it fails, store raw text only.
    parsed_summary: dict = {}
    try:
        from apps.edi.utils.edifecs_report import parse_edifecs_report

        parsed = parse_edifecs_report(
            data.decode("utf-8", errors="replace"), file_name=filename
        )
        report_type = parsed.get("report_type") or report_type
        status = parsed.get("status") or status
        parsed_summary = parsed.get("parsed_summary") or {}
    except Exception:
        pass  # Raw binary / non-XML — store as-is with minimal metadata.

    try:
        report = EDIValidationReport.objects.create(
            batch=batch,
            edi_file=edi_file,
            report_type=report_type,
            status=status,
            file_name=filename,
            file_hash=digest,
            raw_file_ref=remote_path,
            message=f"Edifecs response file: {filename}",
            parsed_summary=parsed_summary or None,
            is_active=True,
        )
        logger.info(
            "Saved rejection/response report filename=%s type=%s status=%s hash=%s",
            filename,
            report_type,
            status,
            digest,
        )
        return report
    except Exception as exc:
        logger.exception("Could not save rejection report %s: %s", filename, exc)
        return None


def discover_edi_999_imports(*, credentials_id=None, batch_id=None):
    """
    List SFTP receiving folders and create DISCOVERED/QUEUED EDI999Import rows.
    Returns (created_rows, skipped_existing_count, errors).
    """
    directories = resolve_inbound_999_directories(credentials_id=credentials_id)
    if not directories:
        raise ValueError("No active SFTP inbound directories configured for 999 import.")

    created = []
    skipped_existing = 0
    errors = []

    for directory in directories:
        recv = (directory.receiving_path or "").strip()
        if not recv:
            errors.append(
                {
                    "directory_id": directory.id,
                    "error": "receiving_path is empty.",
                }
            )
            continue
        try:
            # SFTP I/O must stay outside DB locks — list before atomic writes.
            entries = list_remote_files(
                credentials=directory.credentials,
                remote_dir=recv,
            )
        except Exception as exc:
            logger.exception("List inbound 999 failed directory_id=%s", directory.id)
            errors.append(
                {
                    "directory_id": directory.id,
                    "error": str(exc)[:500],
                }
            )
            continue

        for entry in entries:
            filename = entry["filename"]
            remote_path = entry["remote_path"]

            # Rejection / error report files (.rjct, .rsp, .description, .html):
            # download and save as EDIValidationReport — never silently skip.
            if _is_report_file(filename):
                save_rejection_report(
                    credentials=directory.credentials,
                    directory=directory,
                    filename=filename,
                    remote_path=remote_path,
                )
                continue

            if not _candidate_filename(filename):
                continue

            with transaction.atomic():
                existing = (
                    EDI999Import.objects.select_for_update()
                    .filter(
                        credentials_id=directory.credentials_id,
                        remote_path=remote_path,
                        is_active=True,
                    )
                    .first()
                )
                if existing:
                    skipped_existing += 1
                    if existing.status == EDI999ImportStatus.FAILED:
                        if existing.status not in (
                            EDI999ImportStatus.DOWNLOADING,
                            EDI999ImportStatus.PARSING,
                        ):
                            existing.status = EDI999ImportStatus.QUEUED
                            existing.message = "Re-queued by 999 import poll."
                            existing.finished_at = None
                            existing.save(
                                update_fields=[
                                    "status",
                                    "message",
                                    "finished_at",
                                    "updated_at",
                                ]
                            )
                            created.append(existing)
                    continue

                row = EDI999Import.objects.create(
                    credentials=directory.credentials,
                    directory=directory,
                    batch_id=batch_id,
                    filename=filename,
                    remote_path=remote_path,
                    status=EDI999ImportStatus.QUEUED,
                    attempt=0,
                    message="Discovered on SFTP; queued for 999 import.",
                    is_active=True,
                )
                created.append(row)

    return created, skipped_existing, errors


def process_edi_999_import(import_id, *, batch_id=None):
    """
    Download one remote 999, parse, persist EDIAcknowledgement, update tracking row.
    Raises on retryable failures; marks SKIPPED for non-retryable junk.
    """
    with transaction.atomic():
        row = (
            EDI999Import.objects.select_for_update(of=("self",))
            .select_related(
                "credentials",
                "directory",
                "batch",
                "edi_file",
            )
            .filter(pk=import_id, is_active=True)
            .first()
        )
        if row is None:
            raise ValueError("EDI 999 import row not found or inactive.")

        if row.status == EDI999ImportStatus.IMPORTED:
            return {
                "id": row.id,
                "status": row.status,
                "acknowledgement_id": row.acknowledgement_id,
                "skipped": True,
            }

        if row.status in (
            EDI999ImportStatus.DOWNLOADING,
            EDI999ImportStatus.PARSING,
        ):
            return {
                "id": row.id,
                "status": row.status,
                "skipped": True,
                "message": "Import already in progress.",
            }

        if not row.credentials_id:
            _mark(
                row,
                status=EDI999ImportStatus.FAILED,
                message="Missing SFTP credentials on import row.",
                finished=True,
            )
            raise PermanentImportError(row.message)

        row.attempt = (row.attempt or 0) + 1
        row.started_at = row.started_at or timezone.now()
        row.status = EDI999ImportStatus.DOWNLOADING
        row.message = f"Downloading from SFTP (attempt {row.attempt})."
        row.save(
            update_fields=["attempt", "started_at", "status", "message", "updated_at"]
        )
        credentials = row.credentials
        remote_path = row.remote_path
        attempt = row.attempt
        row_id = row.id

    try:
        data = download_bytes_via_sftp(
            credentials=credentials,
            remote_path=remote_path,
        )
    except Exception as exc:
        _mark(
            row,
            status=EDI999ImportStatus.FAILED,
            message=str(exc)[:500],
            detail=str(exc)[:2000],
            finished=True,
        )
        raise

    if not data or not data.strip():
        _mark(
            row,
            status=EDI999ImportStatus.SKIPPED,
            message="Remote file is empty; skipped.",
            finished=True,
        )
        return {"id": row.id, "status": row.status, "skipped": True}

    digest = hashlib.sha256(data).hexdigest()
    row.file_hash = digest
    row.save(update_fields=["file_hash", "updated_at"])

    dup = (
        EDI999Import.objects.filter(
            file_hash=digest,
            status=EDI999ImportStatus.IMPORTED,
            is_active=True,
        )
        .exclude(pk=row.pk)
        .first()
    )
    if dup:
        _mark(
            row,
            status=EDI999ImportStatus.SKIPPED,
            message=f"Duplicate of already imported 999 (import_id={dup.id}).",
            finished=True,
        )
        return {
            "id": row.id,
            "status": row.status,
            "duplicate_of": dup.id,
            "skipped": True,
        }

    text = data.decode("utf-8", errors="replace")
    _mark(row, status=EDI999ImportStatus.PARSING, message="Parsing 999 X12.")

    try:
        parsed = parse_999(text)
    except ValueError as exc:
        # Non-999 / malformed — do not retry forever.
        _mark(
            row,
            status=EDI999ImportStatus.SKIPPED,
            message=str(exc)[:500],
            detail=str(exc)[:2000],
            finished=True,
        )
        return {"id": row.id, "status": row.status, "skipped": True}

    resolved_batch, resolved_edi = resolve_batch_for_999(parsed)
    use_batch_id = batch_id or row.batch_id or (resolved_batch.id if resolved_batch else None)
    if not use_batch_id:
        _mark(
            row,
            status=EDI999ImportStatus.FAILED,
            message=(
                "Could not resolve submission batch from 999 "
                f"(gs06={parsed.get('gs06')}, isa13={parsed.get('isa13')})."
            ),
            detail=str(parsed.get("ak1")),
            finished=True,
        )
        raise PermanentImportError(row.message)

    with transaction.atomic():
        row = (
            EDI999Import.objects.select_for_update(of=("self",))
            .filter(pk=import_id, is_active=True)
            .first()
        )
        if row is None:
            raise ValueError("EDI 999 import row not found or inactive.")
        if row.status == EDI999ImportStatus.IMPORTED:
            return {
                "id": row.id,
                "status": row.status,
                "acknowledgement_id": row.acknowledgement_id,
                "skipped": True,
            }

        if resolved_batch and not row.batch_id:
            row.batch = resolved_batch
        if resolved_edi and not row.edi_file_id:
            row.edi_file = resolved_edi
        row.save(update_fields=["batch", "edi_file", "updated_at"])

        try:
            (ack, claim_ids), parsed_out = import_999_acknowledgement(
                content=text,
                batch_id=use_batch_id,
                edi_file_id=row.edi_file_id,
                raw_file_ref=row.remote_path,
                apply_claim_status=True,
            )
        except Exception as exc:
            _mark(
                row,
                status=EDI999ImportStatus.FAILED,
                message=str(exc)[:500],
                detail=str(exc)[:2000],
                finished=True,
            )
            raise

        row.acknowledgement = ack
        row.batch_id = ack.batch_id or use_batch_id
        row.save(update_fields=["acknowledgement", "batch", "updated_at"])
        _mark(
            row,
            status=EDI999ImportStatus.IMPORTED,
            message=(
                f"Imported 999 status={ack.status} st02={ack.affected_st02}; "
                f"claims={claim_ids}."
            ),
            detail=str(parsed_out.get("message") or ""),
            finished=True,
        )
    logger.info(
        "EDI999Import id=%s imported acknowledgement_id=%s claims=%s",
        row.id,
        ack.id,
        claim_ids,
    )
    return {
        "id": row.id,
        "status": row.status,
        "acknowledgement_id": ack.id,
        "updated_claim_ids": claim_ids,
        "affected_st02": ack.affected_st02,
    }


def queue_edi_999_import_poll(*, credentials_id=None, batch_id=None):
    """
    Discover remote 999 files and enqueue Celery process tasks.
    Returns summary dict for the API (importing started).
    """
    from apps.edi.tasks import process_edi_999_import_task
    from apps.edi.utils.import_dispatch import dispatch_import

    created, skipped_existing, errors = discover_edi_999_imports(
        credentials_id=credentials_id,
        batch_id=batch_id,
    )
    queued_ids = []
    for row in created:
        dispatch_import(row, process_edi_999_import_task, batch_id)
        queued_ids.append(row.id)

    return {
        "message": "Importing started.",
        "queued_import_ids": queued_ids,
        "queued_count": len(queued_ids),
        "skipped_existing": skipped_existing,
        "list_errors": errors,
        "task_ids": [
            EDI999Import.objects.filter(pk=i).values_list("celery_task_id", flat=True).first()
            for i in queued_ids
        ],
    }
