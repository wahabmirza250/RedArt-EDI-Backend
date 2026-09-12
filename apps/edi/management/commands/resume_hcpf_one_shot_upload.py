from __future__ import annotations

import json
import os
import subprocess
import sys
from types import SimpleNamespace

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.edi.choices import EDIFileStatus, TransferChannel, TransferLogStatus
from apps.edi.models import EDIFile, EDIFileTransferLog
from apps.edi.utils.pyx12_preflight import assert_pyx12_valid
from apps.edi.utils.service import mark_edi_file_uploaded
from apps.edi.utils.sftp_client import upload_bytes_via_sftp
from apps.edi.utils.upload import HCPF_837P_SEND_PATH, read_edi_file_bytes, run_edi_file_upload


def _env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise CommandError(f"Missing required environment variable: {name}")
    return value


def _worker_credential_in_memory():
    mapping = {
        "POSTGRES_DB": "DIAG_WORKER_POSTGRES_DB",
        "POSTGRES_HOST": "DIAG_WORKER_POSTGRES_HOST",
        "POSTGRES_PASSWORD": "DIAG_WORKER_POSTGRES_PASSWORD",
        "POSTGRES_PORT": "DIAG_WORKER_POSTGRES_PORT",
        "POSTGRES_SSLMODE": "DIAG_WORKER_POSTGRES_SSLMODE",
        "POSTGRES_USER": "DIAG_WORKER_POSTGRES_USER",
        "DJANGO_SECRET_KEY": "DIAG_WORKER_DJANGO_SECRET_KEY",
        "DJANGO_SETTINGS_MODULE": "DIAG_WORKER_DJANGO_SETTINGS_MODULE",
    }
    worker_env = os.environ.copy()
    for target, source in mapping.items():
        value = os.environ.get(source, "").strip()
        if not value:
            raise CommandError(f"Worker credential bridge is missing {source}")
        worker_env[target] = value

    script = r'''
import json
import django

django.setup()
from apps.core.crypto_secrets import decrypt_secret
from apps.edi.models import SFTPCredentials

row = (
    SFTPCredentials.objects.filter(is_active=True, host__icontains="edifecs")
    .order_by("-id")
    .first()
)
if row is None:
    raise SystemExit(42)
print(json.dumps({
    "host": row.host,
    "port": row.port or 22,
    "username": row.username,
    "auth_type": row.auth_type,
    "password": decrypt_secret(row.password),
    "private_key_pem": decrypt_secret(row.private_key_pem),
    "private_key_passphrase": decrypt_secret(row.private_key_passphrase),
    "host_fingerprint": row.host_fingerprint,
    "timeout_seconds": row.timeout_seconds or 45,
}))
'''
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd="/app",
        env=worker_env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    if proc.returncode != 0:
        raise CommandError("Unable to load the existing worker Edifecs credential")
    try:
        payload = json.loads((proc.stdout or "").strip())
    except Exception as exc:
        raise CommandError("Worker Edifecs credential bridge returned invalid data") from exc
    if not payload.get("host") or not payload.get("username") or not payload.get("private_key_pem"):
        raise CommandError("Worker Edifecs credential is incomplete")
    return SimpleNamespace(**payload)


def _finish_existing_attempt_with_worker_credential(*, edi_file, sftp_log):
    data = read_edi_file_bytes(edi_file)
    assert_pyx12_valid(data.decode("utf-8"))
    credentials = _worker_credential_in_memory()

    now = timezone.now()
    sftp_log.status = TransferLogStatus.IN_PROGRESS
    sftp_log.started_at = sftp_log.started_at or now
    sftp_log.message = "Uploading existing attempt to HCPF SFTP."
    sftp_log.celery_task_id = "ops-one-shot-resume"
    sftp_log.save()

    try:
        remote_path = upload_bytes_via_sftp(
            credentials=credentials,
            remote_dir=HCPF_837P_SEND_PATH,
            filename=edi_file.filename,
            data=data,
        )
    except Exception as exc:
        sftp_log.status = TransferLogStatus.FAILED
        sftp_log.finished_at = timezone.now()
        sftp_log.message = (f"SFTP failed: {type(exc).__name__}: {str(exc)}")[:500]
        sftp_log.detail = (f"{type(exc).__name__}: {str(exc)}")[:2000]
        sftp_log.save()
        raise CommandError(
            f"SFTP transport failed: {type(exc).__name__}: {str(exc)[:250]}"
        ) from exc

    sftp_log.status = TransferLogStatus.SUCCESS
    sftp_log.finished_at = timezone.now()
    sftp_log.remote_path = remote_path
    sftp_log.message = "Uploaded to HCPF SFTP successfully."
    sftp_log.save()
    mark_edi_file_uploaded(edi_file.id)
    return remote_path


class Command(BaseCommand):
    help = "Resume exactly one already-queued HCPF upload attempt; never creates a retry."

    def handle(self, *args, **options):
        if _env("OPS_REAL_SUBMIT_ENABLED") != "YES_ONE_HCPF_CLAIM":
            raise CommandError("One-shot guard is not enabled")

        try:
            edi_file_id = int(_env("OPS_UPLOAD_EDI_FILE_ID"))
        except ValueError as exc:
            raise CommandError("OPS_UPLOAD_EDI_FILE_ID must be an integer") from exc

        expected_isa13 = _env("OPS_UPLOAD_EXPECTED_ISA13")
        expected_gs06 = _env("OPS_UPLOAD_EXPECTED_GS06")

        edi_file = (
            EDIFile.objects.select_related("control_number", "batch", "batch__trading_partner")
            .filter(pk=edi_file_id, is_active=True)
            .first()
        )
        if edi_file is None:
            raise CommandError("EDI file not found")
        if edi_file.status != EDIFileStatus.UPLOAD_QUEUED:
            raise CommandError(f"EDI file must already be UPLOAD_QUEUED, got {edi_file.status}")

        control = edi_file.control_number
        if control is None:
            raise CommandError("EDI file has no control-number record")
        if (control.isa13 or "") != expected_isa13 or (control.gs06 or "") != expected_gs06:
            raise CommandError("EDI control numbers do not match the authorized upload target")

        sftp_logs = list(
            EDIFileTransferLog.objects.filter(
                edi_file_id=edi_file.id,
                channel=TransferChannel.SFTP,
                is_active=True,
            ).order_by("attempt", "id")
        )
        if len(sftp_logs) != 1:
            raise CommandError(f"Expected exactly one SFTP attempt, found {len(sftp_logs)}; refusing to continue")

        sftp_log = sftp_logs[0]
        if sftp_log.status == TransferLogStatus.SUCCESS:
            raise CommandError("SFTP attempt is already SUCCESS; refusing to resend")
        if sftp_log.status not in {TransferLogStatus.PENDING, TransferLogStatus.IN_PROGRESS}:
            raise CommandError(
                f"Existing SFTP attempt status={sftp_log.status}; refusing automatic retry"
            )

        s3_log = EDIFileTransferLog.objects.filter(
            edi_file_id=edi_file.id,
            channel=TransferChannel.S3,
            attempt=sftp_log.attempt,
            is_active=True,
        ).first()
        if s3_log is None:
            raise CommandError("Matching S3 audit log is missing; refusing to alter attempt state")

        remote_path = None
        try:
            run_edi_file_upload(
                edi_file_id=edi_file.id,
                attempt=sftp_log.attempt,
                task_id="ops-one-shot-resume",
            )
        except Exception as exc:
            message = str(exc).replace("\n", " ")[:300]
            if "Edifecs private key is unavailable" not in message:
                raise CommandError(
                    f"Transport execution failed: {type(exc).__name__}: {message}"
                ) from exc
            sftp_log.refresh_from_db()
            if sftp_log.status not in {TransferLogStatus.PENDING, TransferLogStatus.IN_PROGRESS}:
                raise CommandError(
                    f"Transport stopped with SFTP status={sftp_log.status}; refusing fallback"
                ) from exc
            remote_path = _finish_existing_attempt_with_worker_credential(
                edi_file=edi_file,
                sftp_log=sftp_log,
            )

        edi_file.refresh_from_db()
        sftp_log.refresh_from_db()
        if sftp_log.status != TransferLogStatus.SUCCESS:
            raise CommandError(
                f"Existing SFTP attempt finished status={sftp_log.status}; do not retry automatically"
            )
        remote_path = remote_path or sftp_log.remote_path

        self.stdout.write(
            self.style.SUCCESS(
                "OPS_UPLOADED "
                f"edi_file_id={edi_file.id} attempt={sftp_log.attempt} status={edi_file.status} "
                f"isa13={control.isa13} gs06={control.gs06} "
                f"remote_path={remote_path or '-'}"
            )
        )
