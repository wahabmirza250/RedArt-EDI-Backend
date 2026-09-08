from django.core.management.base import BaseCommand, CommandError

from apps.edi.models import EDI999Import
from apps.edi.utils.sftp_client import download_bytes_via_sftp
from apps.edi.utils.x12 import parse_999


def _el(seg, index):
    elements = (seg or {}).get("elements") or []
    i = index - 1
    if i < 0 or i >= len(elements):
        return ""
    return str(elements[i] or "").strip()


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
        by_id = parsed.get("by_id") or {}

        # IK3: segment id / segment position / loop id / segment syntax error code.
        # IK4: element position / data-element reference / element syntax error code.
        # Deliberately omit IK404 (copy of bad data) to avoid PHI/PII leakage.
        ik3_bits = []
        for seg in by_id.get("IK3") or []:
            ik3_bits.append(
                ":".join(
                    [
                        _el(seg, 1) or "-",
                        _el(seg, 2) or "-",
                        _el(seg, 3) or "-",
                        _el(seg, 4) or "-",
                    ]
                )
            )

        ik4_bits = []
        for seg in by_id.get("IK4") or []:
            ik4_bits.append(
                ":".join(
                    [
                        _el(seg, 1) or "-",
                        _el(seg, 2) or "-",
                        _el(seg, 3) or "-",
                    ]
                )
            )

        # CTX situational-trigger metadata can identify what caused an I6.
        # Emit only the fixed trigger type and structural positions/references.
        # Do not emit business-unit/claim identifiers or copied bad data.
        ctx_bits = []
        for seg in by_id.get("CTX") or []:
            ctx01 = _el(seg, 1)
            if not ctx01.upper().startswith("SITUATIONAL TRIGGER"):
                continue
            ctx_bits.append(
                ":".join(
                    [
                        _el(seg, 2) or "-",  # triggering segment id
                        _el(seg, 3) or "-",  # segment position
                        _el(seg, 4) or "-",  # loop id
                        _el(seg, 5) or "-",  # element position composite
                        _el(seg, 6) or "-",  # reference composite, if present
                    ]
                )
            )

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
            f"ack_isa13={parsed.get('isa13')} "
            f"ik3={'|'.join(ik3_bits) or '-'} "
            f"ik4={'|'.join(ik4_bits) or '-'} "
            f"ctx={'|'.join(ctx_bits) or '-'}"
        )
