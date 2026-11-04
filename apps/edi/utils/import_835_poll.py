"""
Discover and import inbound 835 ERA files from SFTP.

Tracks each remote file as EDI835Import (queued → imported / failed / skipped).
"""

from __future__ import annotations

import hashlib
import logging
import re
import traceback

from django.db import transaction
from django.utils import timezone

from apps.edi.choices import EDI835ImportStatus, SFTPDirectoryPurpose
from apps.edi.models import EDI835Import, SFTPDirectory
from apps.edi.utils.import_835 import import_835_remittance
from apps.edi.utils.sftp_client import download_bytes_via_sftp, list_remote_files

logger = logging.getLogger(__name__)

_SKIP_NAME_RE = re.compile(
    r"\.(rsp|rjct|description|tmp|part|bak)$",
    re.IGNORECASE,
)
_LOOKS_LIKE_835_NAME = re.compile(r"835|\.edi$|\.x12$|\.txt$", re.IGNORECASE)


def _mark(row: EDI835Import, *, status, message=None, detail=None, finished=False):
    row.status = status
    if message is not None:
        row.message = (message or "")[:500]
    if detail is not None:
        row.detail = (detail or "")[:2000]
    now = timezone.now()
    fields = ["status", "message", "detail", "updated_at"]
    if row.started_at is None and status in (
        EDI835ImportStatus.DOWNLOADING,
        EDI835ImportStatus.PARSING,
        EDI835ImportStatus.QUEUED,
    ):
        row.started_at = now
        fields.append("started_at")
    if finished or status in (
        EDI835ImportStatus.IMPORTED,
        EDI835ImportStatus.FAILED,
        EDI835ImportStatus.SKIPPED,
    ):
        row.finished_at = now
        fields.append("finished_at")
    row.save(update_fields=list(dict.fromkeys(fields)))
    return row


def resolve_inbound_835_directories(*, credentials_id=None):
    qs = SFTPDirectory.objects.with_relations().filter(
        is_active=True,
        credentials__is_active=True,
        purpose__in=(
            SFTPDirectoryPurpose.INBOUND_835,
            SFTPDirectoryPurpose.GENERAL,
        ),
    )
    if credentials_id:
        qs = qs.filter(credentials_id=credentials_id)
    return list(qs.order_by("-id"))


def _candidate_filename(name: str) -> bool:
    if not name or name.startswith("."):
        return False
    if _SKIP_NAME_RE.search(name):
        return False
    return bool(_LOOKS_LIKE_835_NAME.search(name))


def discover_edi_835_imports(*, credentials_id=None):
    directories = resolve_inbound_835_directories(credentials_id=credentials_id)
    if not directories:
        raise ValueError("No active SFTP inbound directories configured for 835 import.")

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
            logger.exception("List inbound 835 failed directory_id=%s", directory.id)
            errors.append({"directory_id": directory.id, "error": str(exc)[:500]})
            continue

        for entry in entries:
            filename = entry["filename"]
            remote_path = entry["remote_path"]
            if not _candidate_filename(filename):
                continue

            with transaction.atomic():
                existing = (
                    EDI835Import.objects.select_for_update()
                    .filter(
                        credentials_id=directory.credentials_id,
                        remote_path=remote_path,
                        is_active=True,
                    )
                    .first()
                )
                if existing:
                    skipped_existing += 1
                    if existing.status == EDI835ImportStatus.FAILED:
                        existing.status = EDI835ImportStatus.QUEUED
                        existing.message = "Re-queued by 835 import poll."
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

                row = EDI835Import.objects.create(
                    credentials=directory.credentials,
                    directory=directory,
                    filename=filename,
                    remote_path=remote_path,
                    status=EDI835ImportStatus.QUEUED,
                    attempt=0,
                    message="Discovered on SFTP; queued for 835 import.",
                    is_active=True,
                )
                created.append(row)

    return created, skipped_existing, errors


def process_edi_835_import(import_id):
    with transaction.atomic():
        row = (
            EDI835Import.objects.select_for_update(of=("self",))
            .select_related("credentials", "directory", "remittance")
            .filter(pk=import_id, is_active=True)
            .first()
        )
        if row is None:
            raise ValueError("EDI 835 import row not found or inactive.")

        if row.status == EDI835ImportStatus.IMPORTED:
            return {
                "id": row.id,
                "status": row.status,
                "remittance_id": row.remittance_id,
                "skipped": True,
            }

        if not row.credentials_id:
            _mark(
                row,
                status=EDI835ImportStatus.FAILED,
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
        status=EDI835ImportStatus.DOWNLOADING,
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
            status=EDI835ImportStatus.FAILED,
            message=str(exc)[:500],
            detail=str(exc)[:2000],
            finished=True,
        )
        raise

    if not data or not data.strip():
        _mark(
            row,
            status=EDI835ImportStatus.SKIPPED,
            message="Remote file is empty; skipped.",
            finished=True,
        )
        return {"id": row.id, "status": row.status, "skipped": True}

    digest = hashlib.sha256(data).hexdigest()
    row.file_hash = digest
    row.save(update_fields=["file_hash", "updated_at"])

    dup = (
        EDI835Import.objects.filter(
            file_hash=digest,
            status=EDI835ImportStatus.IMPORTED,
            is_active=True,
        )
        .exclude(pk=row.pk)
        .first()
    )
    if dup:
        _mark(
            row,
            status=EDI835ImportStatus.SKIPPED,
            message=f"Duplicate of already imported 835 (import_id={dup.id}).",
            finished=True,
        )
        return {
            "id": row.id,
            "status": row.status,
            "duplicate_of": dup.id,
            "skipped": True,
        }

    text = data.decode("utf-8", errors="replace")
    _mark(row, status=EDI835ImportStatus.PARSING, message="Parsing 835 X12.")

    try:
        remittance, claim_ids, meta = import_835_remittance(
            content=text,
            raw_file_ref=row.remote_path,
            apply_claim_status=True,
        )
    except ValueError as exc:
        _mark(
            row,
            status=EDI835ImportStatus.SKIPPED,
            message=str(exc)[:500],
            detail=str(exc)[:2000],
            finished=True,
        )
        return {"id": row.id, "status": row.status, "skipped": True}
    except Exception as exc:
        _mark(
            row,
            status=EDI835ImportStatus.FAILED,
            message=str(exc)[:500],
            detail=str(exc)[:2000],
            finished=True,
        )
        raise

    row.remittance = remittance
    row.save(update_fields=["remittance", "updated_at"])
    _mark(
        row,
        status=EDI835ImportStatus.IMPORTED,
        message=(
            f"Imported 835 remittance_id={remittance.id} "
            f"lines={remittance.claim_line_count} applied={len(claim_ids)} "
            f"idempotent={meta.get('idempotent')}."
        ),
        finished=True,
    )
    logger.info(
        "EDI835Import id=%s remittance_id=%s claims=%s",
        row.id,
        remittance.id,
        claim_ids,
    )
    return {
        "id": row.id,
        "status": row.status,
        "remittance_id": remittance.id,
        "updated_claim_ids": claim_ids,
        "idempotent": meta.get("idempotent"),
    }


def queue_edi_835_import_poll(*, credentials_id=None):
    from apps.edi.tasks import process_edi_835_import_task
    from apps.edi.utils.import_dispatch import dispatch_import

    created, skipped_existing, errors = discover_edi_835_imports(
        credentials_id=credentials_id,
    )
    queued_ids = []
    for row in created:
        dispatch_import(row, process_edi_835_import_task)
        queued_ids.append(row.id)

    return {
        "message": "Importing started.",
        "queued_import_ids": queued_ids,
        "queued_count": len(queued_ids),
        "skipped_existing": skipped_existing,
        "list_errors": errors,
        "task_ids": [
            EDI835Import.objects.filter(pk=i)
            .values_list("celery_task_id", flat=True)
            .first()
            for i in queued_ids
        ],
    }
