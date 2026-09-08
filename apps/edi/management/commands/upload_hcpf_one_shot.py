from __future__ import annotations

import os

from django.core.management.base import BaseCommand, CommandError

from apps.edi.choices import EDIFileStatus, TransferChannel, TransferLogStatus
from apps.edi.models import EDIFile, EDIFileTransferLog
from apps.edi.utils.upload import queue_edi_file_upload, run_edi_file_upload


def _env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise CommandError(f"Missing required environment variable: {name}")
    return value


class Command(BaseCommand):
    help = "Upload exactly one pre-validated HCPF 837P once; refuses any prior SFTP attempt."

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
        if edi_file.status != EDIFileStatus.GENERATED:
            raise CommandError(f"EDI file status must be GENERATED, got {edi_file.status}")

        control = edi_file.control_number
        if control is None:
            raise CommandError("EDI file has no control-number record")
        if (control.isa13 or "") != expected_isa13 or (control.gs06 or "") != expected_gs06:
            raise CommandError("EDI control numbers do not match the authorized upload target")

        if EDIFileTransferLog.objects.filter(
            edi_file_id=edi_file.id,
            channel=TransferChannel.SFTP,
            is_active=True,
        ).exists():
            raise CommandError("An SFTP attempt already exists for this EDI file; refusing to resend")

        queued_file, attempt, _sftp_log, _s3_log = queue_edi_file_upload(
            edi_file_id=edi_file.id,
            async_mode=False,
        )

        run_edi_file_upload(
            edi_file_id=queued_file.id,
            attempt=attempt,
            task_id="ops-one-shot",
        )

        sftp_log = EDIFileTransferLog.objects.filter(
            edi_file_id=queued_file.id,
            channel=TransferChannel.SFTP,
            attempt=attempt,
            is_active=True,
        ).order_by("-id").first()
        queued_file.refresh_from_db()

        if sftp_log is None:
            raise CommandError("SFTP transfer log missing after upload attempt")
        if sftp_log.status != TransferLogStatus.SUCCESS:
            raise CommandError(
                f"SFTP upload attempt finished with status={sftp_log.status}; do not retry automatically"
            )

        self.stdout.write(
            self.style.SUCCESS(
                "OPS_UPLOADED "
                f"edi_file_id={queued_file.id} attempt={attempt} status={queued_file.status} "
                f"isa13={control.isa13} gs06={control.gs06} "
                f"remote_path={sftp_log.remote_path or '-'}"
            )
        )
