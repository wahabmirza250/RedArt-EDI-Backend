from django.core.management.base import BaseCommand, CommandError

from apps.edi.models import EDI999Import
from apps.edi.utils.sftp_client import download_bytes_via_sftp
from apps.edi.utils.x12 import parse_999


class Command(BaseCommand):
    help = "Read-only inspection of one imported 999. Prints only non-PHI acknowledgement metadata."

    def add_arguments(self, parser):
        parser.add_argument("--import-id", type=int, required=True)

    def handle(self, *args, **options):
        row = (
            EDI999Import.objects.select_related("credentials")
            .filter(pk=options["import_id"], is_active=True)
            .first()
        )
        if row is None:
            raise CommandError("999 import row not found")
        if not row.credentials_id or not row.remote_path:
            raise CommandError("999 import row is missing credentials or remote path")

        data = download_bytes_via_sftp(
            credentials=row.credentials,
            remote_path=row.remote_path,
        )
        if not data:
            raise CommandError("Remote 999 file is empty")

        parsed = parse_999(data.decode("utf-8", errors="replace"))
        self.stdout.write(
            "REAL_999 "
            f"import_id={row.id} "
            f"status={parsed.get('status')} "
            f"ik5={parsed.get('ik5_code')} "
            f"ak9={parsed.get('ak9_code')} "
            f"ak1_functional_id={parsed.get('ak1', {}).get('functional_id')} "
            f"ak1_group_control={parsed.get('ak1', {}).get('group_control')} "
            f"ak2_transaction_set={parsed.get('ak2', {}).get('transaction_set')} "
            f"ak2_st02={parsed.get('ak2', {}).get('st02')} "
            f"ack_isa13={parsed.get('isa13')}"
        )
