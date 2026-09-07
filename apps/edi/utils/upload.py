"""Queue and run EDI file uploads (SFTP + MinIO) with transfer logs."""

from __future__ import annotations

import logging
import os
import traceback
from pathlib import Path
from types import SimpleNamespace

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.edi.choices import (
    EDIFileStatus,
    SFTPDirectoryPurpose,
    TransferChannel,
    TransferLogStatus,
)
from apps.edi.models import EDIFile, EDIFileTransferLog, SFTPCredentials, SFTPDirectory
from apps.edi.utils.s3_client import upload_bytes_to_s3
from apps.edi.utils.sftp_client import upload_bytes_via_sftp
from apps.edi.utils.service import mark_edi_file_uploaded
from apps.edi.utils.pyx12_preflight import assert_pyx12_valid

logger = logging.getLogger(__name__)

# HCPF MFT guide and live directory listing: outbound claims go to ToEdifecs;
# received acknowledgments are retrieved from FromEdifecs.
HCPF_837P_SEND_PATH = "Organizational/Outgoing/edifecs.stco.hosted/toedifecs"
HCPF_ACK_RECEIVE_PATH = "Organizational/Incoming/fromedifecs/edifecs.stco.hosted"


def sync_hcpf_directory_paths(*, credentials) -> int:
    """
    Point every active Edifecs directory to the confirmed send/receive path.
    Keep outbound claims separate from incoming acknowledgments.
    Safe to call on every upload or poll — uses update() for atomicity.
    """
    if credentials is None:
        return 0
    host = (getattr(credentials, "host", None) or "").lower()
    if "edifecs" not in host:
        return 0
    return SFTPDirectory.objects.filter(
        credentials_id=credentials.id,
        is_active=True,
    ).update(
        sending_path=HCPF_837P_SEND_PATH,
        receiving_path=HCPF_ACK_RECEIVE_PATH,
    )


def resolve_outbound_directory(*, trading_partner_id=None, credentials_id=None):
    qs = SFTPDirectory.objects.with_relations().filter(
        is_active=True,
        purpose=SFTPDirectoryPurpose.OUTBOUND_837P,
        credentials__is_active=True,
    )
    if credentials_id:
        qs = qs.filter(credentials_id=credentials_id)
    elif trading_partner_id:
        qs = qs.filter(credentials__trading_partner_id=trading_partner_id)
    directory = qs.order_by("-id").first()
    if directory is None:
        # Fall back to the shared active HCPF transport. A provider-specific
        # trading-partner row is not required because one platform MFT account
        # carries files for every authorised client company.
        qs = SFTPDirectory.objects.with_relations().filter(
            is_active=True,
            credentials__is_active=True,
        )
        if credentials_id:
            qs = qs.filter(credentials_id=credentials_id)
        directory = qs.order_by("-id").first()
    if directory is not None:
        sync_hcpf_directory_paths(credentials=directory.credentials)
        directory.refresh_from_db()
        return directory
    if directory is None:
        # Self-heal the production row from the active Render-managed Edifecs
        # credential. The credential is securely seeded at every web startup.
        credentials = SFTPCredentials.objects.all()
        if credentials_id:
            credentials = credentials.filter(pk=credentials_id)
        credential = credentials.order_by("-id").first()
        if credential is None:
            private_key_pem = os.environ.get("HCPF_SFTP_PRIVATE_KEY_PEM", "").strip()
            if not private_key_pem:
                configured_key_path = os.environ.get("HCPF_SFTP_PRIVATE_KEY_PATH")
                key_candidates = [
                    Path(configured_key_path) if configured_key_path else None,
                    Path("/etc/secrets/edifecs_sftp_private_key.pem"),
                    Path("/app/edifecs_sftp_private_key.pem"),
                    Path.cwd() / "edifecs_sftp_private_key.pem",
                ]
                key_path = next(
                    (path for path in key_candidates if path is not None and path.is_file()),
                    None,
                )
                if key_path is None:
                    raise ValueError(
                        "The Render Edifecs private key is unavailable."
                    )
                private_key_pem = key_path.read_text(encoding="utf-8")
            credential = SimpleNamespace(
                host="sftp.mft.edifecsfedcloud.com",
                port=22,
                username="mft_task_01fce47a-0498-4fb4-wt4m",
                auth_type="PRIVATE_KEY",
                password=None,
                private_key_pem=private_key_pem,
                private_key_passphrase=None,
                host_fingerprint="SHA256:xhCbKNBog9ztBEubwfUfb1ODz8e/azOlVeaVb77ug8Q",
                timeout_seconds=45,
            )
            return SimpleNamespace(
                credentials=credential,
                sending_path=HCPF_837P_SEND_PATH,
            )
        directory, _ = SFTPDirectory.objects.update_or_create(
            credentials=credential,
            purpose=SFTPDirectoryPurpose.OUTBOUND_837P,
            defaults={
                "name": "HCPF 837P production send",
                "sending_path": HCPF_837P_SEND_PATH,
                "receiving_path": HCPF_ACK_RECEIVE_PATH,
                "is_active": True,
            },
        )
        sync_hcpf_directory_paths(credentials=credential)
        directory.refresh_from_db()
    return directory


def _candidate_local_paths(edi_file: EDIFile):
    """Possible on-disk locations for a generated 837P file."""
    media = Path(settings.MEDIA_ROOT)
    paths = []
    ref = (edi_file.path_or_blob_ref or "").strip()
    if ref and not ref.startswith("s3://") and "://" not in ref:
        paths.append(media / ref)
    if edi_file.filename and edi_file.batch_id:
        paths.append(media / "edi" / "837p" / str(edi_file.batch_id) / edi_file.filename)
    if edi_file.filename:
        paths.append(media / "edi" / "837p" / edi_file.filename)
    # Deduplicate while preserving order
    seen = set()
    unique = []
    for path in paths:
        key = str(path)
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def _download_s3_bytes(s3_uri: str) -> bytes:
    """Download s3://bucket/key via MinIO/S3 client."""
    without = s3_uri[len("s3://") :]
    bucket, _, key = without.partition("/")
    if not bucket or not key:
        raise ValueError(f"Invalid S3 URI: {s3_uri}")
    from apps.edi.utils.s3_client import get_s3_client

    max_bytes = int(getattr(settings, "EDI_MAX_SFTP_DOWNLOAD_BYTES", 5_000_000))
    client = get_s3_client()
    head = client.head_object(Bucket=bucket, Key=key)
    content_length = head.get("ContentLength")
    if content_length is not None and content_length > max_bytes:
        raise ValueError(f"EDI file exceeds maximum size of {max_bytes} bytes.")
    obj = client.get_object(Bucket=bucket, Key=key)
    body = obj["Body"].read(max_bytes + 1)
    if len(body) > max_bytes:
        raise ValueError(f"EDI file exceeds maximum size of {max_bytes} bytes.")
    return body


def read_edi_file_bytes(edi_file: EDIFile) -> bytes:
    """
    Load EDI bytes for upload.
    Prefers local MEDIA_ROOT; repairs path_or_blob_ref if an older upload
    overwrote it with an s3:// URI; falls back to S3 download.
    """
    for path in _candidate_local_paths(edi_file):
        if path.is_file():
            try:
                rel = str(path.relative_to(settings.MEDIA_ROOT)).replace("\\", "/")
            except ValueError:
                rel = str(path)
            if edi_file.path_or_blob_ref != rel:
                EDIFile.objects.filter(pk=edi_file.id).update(
                    path_or_blob_ref=rel,
                    updated_at=timezone.now(),
                )
                edi_file.path_or_blob_ref = rel
                logger.info(
                    "Repaired EDIFile id=%s path_or_blob_ref -> %s",
                    edi_file.id,
                    rel,
                )
            max_bytes = int(getattr(settings, "EDI_MAX_SFTP_DOWNLOAD_BYTES", 5_000_000))
            size = path.stat().st_size
            if size > max_bytes:
                raise ValueError(f"EDI file exceeds maximum size of {max_bytes} bytes.")
            return path.read_bytes()

    if edi_file.content:
        data = edi_file.content.encode("utf-8")
        max_bytes = int(getattr(settings, "EDI_MAX_SFTP_DOWNLOAD_BYTES", 5_000_000))
        if len(data) > max_bytes:
            raise ValueError(f"EDI file exceeds maximum size of {max_bytes} bytes.")
        return data

    ref = (edi_file.path_or_blob_ref or "").strip()
    if ref.startswith("s3://"):
        logger.info("Reading EDIFile id=%s from S3 URI %s", edi_file.id, ref)
        return _download_s3_bytes(ref)

    raise ValueError(
        "EDI file missing on disk"
        + (f": {ref}" if ref else " (no path_or_blob_ref).")
    )


@transaction.atomic
def queue_edi_file_upload(*, edi_file_id, credentials_id=None, async_mode=False):
    """
    Mark file UPLOAD_QUEUED and create PENDING transfer log rows.
    Allows resend after GENERATED / FAILED / UPLOADED (not while in-flight).
    Returns (edi_file, attempt, sftp_log, s3_log).
    """
    edi_file = (
        EDIFile.objects.select_for_update(of=("self",))
        .select_related("batch", "batch__trading_partner")
        .filter(pk=edi_file_id, is_active=True)
        .first()
    )
    if edi_file is None:
        raise ValueError("EDI file not found or inactive.")

    allowed = {
        EDIFileStatus.GENERATED,
        EDIFileStatus.FAILED,
        EDIFileStatus.UPLOADED,
    }
    if edi_file.status == EDIFileStatus.UPLOAD_QUEUED:
        in_flight = EDIFileTransferLog.objects.filter(
            edi_file_id=edi_file.id,
            status__in=(TransferLogStatus.PENDING, TransferLogStatus.IN_PROGRESS),
            is_active=True,
        ).exists()
        if in_flight:
            raise ValueError("EDI file upload is already queued or in progress.")
        allowed = allowed | {EDIFileStatus.UPLOAD_QUEUED}
    if edi_file.status not in allowed:
        raise ValueError(
            f"EDI file status {edi_file.status} cannot be queued for upload."
        )

    last_attempt = (
        EDIFileTransferLog.objects.filter(edi_file_id=edi_file.id)
        .order_by("-attempt")
        .values_list("attempt", flat=True)
        .first()
    )
    attempt = (last_attempt or 0) + 1

    assert_pyx12_valid(read_edi_file_bytes(edi_file).decode("utf-8"))

    edi_file.status = EDIFileStatus.UPLOAD_QUEUED
    edi_file.save(update_fields=["status", "updated_at"])

    sftp_log = EDIFileTransferLog.objects.create(
        edi_file=edi_file,
        channel=TransferChannel.SFTP,
        status=TransferLogStatus.PENDING,
        attempt=attempt,
        message=f"Queued for SFTP upload (attempt {attempt}).",
        is_active=True,
    )
    s3_log = EDIFileTransferLog.objects.create(
        edi_file=edi_file,
        channel=TransferChannel.S3,
        status=TransferLogStatus.PENDING,
        attempt=attempt,
        message=f"Queued for MinIO/S3 upload (attempt {attempt}).",
        is_active=True,
    )
    return edi_file, attempt, sftp_log, s3_log


def _mark_log(log, *, status, message, detail=None, remote_path=None, task_id=None):
    now = timezone.now()
    if log.started_at is None and status == TransferLogStatus.IN_PROGRESS:
        log.started_at = now
    if status in (TransferLogStatus.SUCCESS, TransferLogStatus.FAILED):
        log.finished_at = now
        if log.started_at is None:
            log.started_at = now
    log.status = status
    log.message = (message or "")[:500]
    if detail is not None:
        log.detail = detail
    if remote_path is not None:
        log.remote_path = remote_path
    if task_id is not None:
        log.celery_task_id = task_id
    log.save()
    return log


def run_edi_file_upload(*, edi_file_id, attempt, task_id=None, credentials_id=None):
    """
    Perform SFTP then MinIO uploads for one attempt.
    Updates transfer logs and EDIFile status.
    """
    edi_file = (
        EDIFile.objects.select_related("batch", "batch__trading_partner")
        .filter(pk=edi_file_id, is_active=True)
        .first()
    )
    if edi_file is None:
        raise ValueError("EDI file not found or inactive.")

    logs = {
        row.channel: row
        for row in EDIFileTransferLog.objects.filter(
            edi_file_id=edi_file.id,
            attempt=attempt,
            is_active=True,
        )
    }
    sftp_log = logs.get(TransferChannel.SFTP)
    s3_log = logs.get(TransferChannel.S3)
    if sftp_log is None or s3_log is None:
        raise ValueError("Transfer logs missing for this upload attempt.")

    data = read_edi_file_bytes(edi_file)
    partner_id = edi_file.batch.trading_partner_id if edi_file.batch_id else None
    directory = resolve_outbound_directory(
        trading_partner_id=partner_id,
        credentials_id=credentials_id,
    )
    credentials = directory.credentials
    filename = edi_file.filename
    send_path = directory.sending_path or "/send"

    sftp_ok = False
    s3_ok = False
    s3_uri = None
    remote_sftp = None

    if sftp_log.status == TransferLogStatus.SUCCESS:
        sftp_ok = True
        remote_sftp = sftp_log.remote_path
    else:
        _mark_log(
            sftp_log,
            status=TransferLogStatus.IN_PROGRESS,
            message="Uploading to SFTP…",
            task_id=task_id,
        )
        try:
            assert_pyx12_valid(data.decode("utf-8"))
            remote_sftp = upload_bytes_via_sftp(
                credentials=credentials,
                remote_dir=send_path,
                filename=filename,
                data=data,
            )
            _mark_log(
                sftp_log,
                status=TransferLogStatus.SUCCESS,
                message="Uploaded to SFTP successfully.",
                remote_path=remote_sftp,
                task_id=task_id,
            )
            sftp_ok = True
        except Exception as exc:
            _mark_log(
                sftp_log,
                status=TransferLogStatus.FAILED,
                message=str(exc)[:500],
                detail=str(exc)[:2000],
                task_id=task_id,
            )
            logger.exception("SFTP upload failed edi_file_id=%s", edi_file_id)

    if s3_log.status == TransferLogStatus.SUCCESS:
        s3_ok = True
        s3_uri = s3_log.remote_path
    else:
        _mark_log(
            s3_log,
            status=TransferLogStatus.IN_PROGRESS,
            message="Uploading to MinIO/S3…",
            task_id=task_id,
        )
        try:
            key = f"edi/837p/{edi_file.batch_id or 'unknown'}/{filename}"
            # T10: upload_bytes_to_s3 is non-fatal; returns None when S3 unavailable.
            s3_uri = upload_bytes_to_s3(key=key, data=data)
            if s3_uri:
                _mark_log(
                    s3_log,
                    status=TransferLogStatus.SUCCESS,
                    message="Uploaded to S3 audit archive.",
                    remote_path=s3_uri,
                    task_id=task_id,
                )
                s3_ok = True
            else:
                _mark_log(
                    s3_log,
                    status=TransferLogStatus.FAILED,
                    message="S3 bucket not configured or unreachable. Set AWS_* env vars.",
                    task_id=task_id,
                )
        except Exception as exc:
            _mark_log(
                s3_log,
                status=TransferLogStatus.FAILED,
                message=str(exc)[:500],
                detail=str(exc)[:2000],
                task_id=task_id,
            )
            logger.warning("S3 audit upload failed (non-fatal) edi_file_id=%s: %s", edi_file_id, exc)

    if sftp_ok:
        # SFTP delivery is the authoritative payer submission. S3 is optional
        # archival; generated content is already persisted on the EDIFile row.
        mark_edi_file_uploaded(edi_file.id)
        return {
            "edi_file_id": edi_file.id,
            "status": EDIFileStatus.UPLOADED,
            "sftp_path": remote_sftp,
            "s3_uri": s3_uri,
            "s3_ok": s3_ok,
            "attempt": attempt,
        }

    EDIFile.objects.filter(pk=edi_file.id).update(
        status=EDIFileStatus.FAILED,
        updated_at=timezone.now(),
    )
    return {
        "edi_file_id": edi_file.id,
        "status": EDIFileStatus.FAILED,
        "sftp_ok": sftp_ok,
        "s3_ok": s3_ok,
        "attempt": attempt,
    }
