"""Discover and import inbound 277 claim status files from SFTP."""

from __future__ import annotations

import hashlib
import logging
import re
import traceback

from django.db import transaction
from django.utils import timezone

from apps.claim.models import BatchClaim, Claim
from apps.edi.choices import EDI999ImportStatus, SFTPDirectoryPurpose
from apps.edi.models import EDI277Import, EDIFile, SFTPDirectory
from apps.edi.utils.service import import_277_acknowledgement
from apps.edi.utils.sftp_client import download_bytes_via_sftp, list_remote_files
from apps.edi.utils.x12 import parse_277

logger = logging.getLogger(__name__)

_SKIP_NAME_RE = re.compile(
    r"\.(rsp|rjct|description|tmp|part|bak)$",
    re.IGNORECASE,
)
_LOOKS_LIKE_277_NAME = re.compile(r"277|\.edi$|\.x12$|\.txt$", re.IGNORECASE)


def _mark(row: EDI277Import, *, status, message=None, detail=None, finished=False):
    row.status = status
    if message is not None:
        row.message = (message or "")[:500]
    if detail is not None:
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


def resolve_inbound_277_directories(*, credentials_id=None):
    qs = SFTPDirectory.objects.with_relations().filter(
        is_active=True,
        credentials__is_active=True,
        purpose__in=(
            SFTPDirectoryPurpose.INBOUND_277,
            SFTPDirectoryPurpose.GENERAL,
        ),
    )
    if credentials_id:
        qs = qs.filter(credentials_id=credentials_id)
    return list(qs.order_by("-id"))


def resolve_batch_for_277(parsed: dict, batch_id=None):
    """Resolve batch (and optional edi_file) from explicit id or claim numbers."""
    if batch_id:
        from apps.claim.models import SubmissionBatch

        batch = SubmissionBatch.objects.filter(pk=batch_id, is_active=True).first()
        if batch:
            edi_file = (
                EDIFile.objects.filter(batch_id=batch.id, is_active=True)
                .order_by("-id")
                .first()
            )
            return batch, edi_file

    for line in parsed.get("claim_statuses") or []:
        claim_number = (line.get("claim_number") or "").strip()
        if not claim_number:
            continue
        claim = Claim.objects.filter(
            claim_number__iexact=claim_number,
            is_active=True,
        ).first()
        if claim is None:
            continue
        batch_claim = (
            BatchClaim.objects.select_related("batch")
            .filter(claim_id=claim.id, is_active=True)
            .order_by("-id")
            .first()
        )
        if batch_claim and batch_claim.batch_id:
            edi_file = (
                EDIFile.objects.filter(batch_id=batch_claim.batch_id, is_active=True)
                .order_by("-id")
                .first()
            )
            return batch_claim.batch, edi_file
    return None, None


def _candidate_filename(name: str) -> bool:
    if not name or name.startswith("."):
        return False
    if _SKIP_NAME_RE.search(name):
        return False
    return bool(_LOOKS_LIKE_277_NAME.search(name))


def discover_edi_277_imports(*, credentials_id=None, batch_id=None):
    directories = resolve_inbound_277_directories(credentials_id=credentials_id)
    if not directories:
        raise ValueError("No active SFTP inbound directories configured for 277 import.")

    created = []
    skipped_existing = 0
    errors = []

    for directory in directories:
        recv = (directory.receiving_path or "").strip()
        if not recv:
            errors.append(
                {"directory_id": directory.id, "error": "receiving_path is empty."}
            )
            continue
        try:
            entries = list_remote_files(
                credentials=directory.credentials,
                remote_dir=recv,
            )
        except Exception as exc:
            logger.exception("List inbound 277 failed directory_id=%s", directory.id)
            errors.append(
                {"directory_id": directory.id, "error": str(exc)[:500]}
            )
            continue

        for entry in entries:
            filename = entry["filename"]
            remote_path = entry["remote_path"]
            if not _candidate_filename(filename):
                continue

            with transaction.atomic():
                existing = (
                    EDI277Import.objects.select_for_update()
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
                        existing.status = EDI999ImportStatus.QUEUED
                        existing.message = "Re-queued by 277 import poll."
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

                row = EDI277Import.objects.create(
                    credentials=directory.credentials,
                    directory=directory,
                    batch_id=batch_id,
                    filename=filename,
                    remote_path=remote_path,
                    status=EDI999ImportStatus.QUEUED,
                    attempt=0,
                    message="Discovered on SFTP; queued for 277 import.",
                    is_active=True,
                )
                created.append(row)

    return created, skipped_existing, errors


def process_edi_277_import(import_id, *, batch_id=None):
    with transaction.atomic():
        row = (
            EDI277Import.objects.select_for_update(of=("self",))
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
            raise ValueError("EDI 277 import row not found or inactive.")

        if row.status == EDI999ImportStatus.IMPORTED:
            return {
                "id": row.id,
                "status": row.status,
                "acknowledgement_id": row.acknowledgement_id,
                "skipped": True,
            }

        if not row.credentials_id:
            _mark(
                row,
                status=EDI999ImportStatus.FAILED,
                message="Missing SFTP credentials on import row.",
                finished=True,
            )
            raise ValueError(row.message)

        row.attempt = (row.attempt or 0) + 1
        row.started_at = row.started_at or timezone.now()
        row.save(update_fields=["attempt", "started_at", "updated_at"])
        credentials = row.credentials
        remote_path = row.remote_path
        attempt = row.attempt

    _mark(
        row,
        status=EDI999ImportStatus.DOWNLOADING,
        message=f"Downloading from SFTP (attempt {attempt}).",
    )
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
        EDI277Import.objects.filter(
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
            message=f"Duplicate of already imported 277 (import_id={dup.id}).",
            finished=True,
        )
        return {
            "id": row.id,
            "status": row.status,
            "duplicate_of": dup.id,
            "skipped": True,
        }

    text = data.decode("utf-8", errors="replace")
    _mark(row, status=EDI999ImportStatus.PARSING, message="Parsing 277 X12.")

    try:
        parsed = parse_277(text)
    except ValueError as exc:
        _mark(
            row,
            status=EDI999ImportStatus.SKIPPED,
            message=str(exc)[:500],
            detail=str(exc)[:2000],
            finished=True,
        )
        return {"id": row.id, "status": row.status, "skipped": True}

    resolved_batch, resolved_edi = resolve_batch_for_277(
        parsed,
        batch_id=batch_id or row.batch_id,
    )
    use_batch_id = (
        batch_id
        or row.batch_id
        or (resolved_batch.id if resolved_batch else None)
    )
    if not use_batch_id:
        _mark(
            row,
            status=EDI999ImportStatus.FAILED,
            message="Could not resolve submission batch from 277 content.",
            detail=str(parsed.get("message")),
            finished=True,
        )
        raise ValueError(row.message)

    if resolved_batch and not row.batch_id:
        row.batch = resolved_batch
    if resolved_edi and not row.edi_file_id:
        row.edi_file = resolved_edi
    row.save(update_fields=["batch", "edi_file", "updated_at"])

    try:
        (ack, claim_ids), parsed_out = import_277_acknowledgement(
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
            f"Imported 277 status={ack.status} st02={ack.affected_st02}; "
            f"claims={claim_ids}."
        ),
        detail=str(parsed_out.get("message") or ""),
        finished=True,
    )
    logger.info(
        "EDI277Import id=%s imported acknowledgement_id=%s claims=%s",
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


def queue_edi_277_import_poll(*, credentials_id=None, batch_id=None):
    from apps.edi.tasks import process_edi_277_import_task
    from apps.edi.utils.import_dispatch import dispatch_import

    created, skipped_existing, errors = discover_edi_277_imports(
        credentials_id=credentials_id,
        batch_id=batch_id,
    )
    queued_ids = []
    for row in created:
        dispatch_import(row, process_edi_277_import_task, batch_id)
        queued_ids.append(row.id)

    return {
        "message": "Importing started.",
        "queued_import_ids": queued_ids,
        "queued_count": len(queued_ids),
        "skipped_existing": skipped_existing,
        "list_errors": errors,
        "task_ids": [
            EDI277Import.objects.filter(pk=i).values_list("celery_task_id", flat=True).first()
            for i in queued_ids
        ],
    }
