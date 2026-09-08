from __future__ import annotations

import os

from django.core.management.base import BaseCommand, CommandError

from apps.edi.choices import EDIFileStatus, TransferChannel, TransferLogStatus
from apps.edi.models import EDIFile, EDIFileTransferLog
from apps.edi.utils.upload import run_edi_file_upload


def _env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise CommandError(f"Missing required environment variable: {name}")
    return value


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

        run_edi_file_upload(
            edi_file_id=edi_file.id,
            attempt=sftp_log.attempt,
            task_id="ops-one-shot-resume",
        )

        edi_file.refresh_from_db()
        sftp_log.refresh_from_db()
        if sftp_log.status != TransferLogStatus.SUCCESS:
            raise CommandError(
                f"Existing SFTP attempt finished status={sftp_log.status}; do not retry automatically"
            )

        self.stdout.write(
            self.style.SUCCESS(
                "OPS_UPLOADED "
                f"edi_file_id={edi_file.id} attempt={sftp_log.attempt} status={edi_file.status} "
                f"isa13={control.isa13} gs06={control.gs06} "
                f"remote_path={sftp_log.remote_path or '-'}"
            )
        )
