from django.core.management.base import BaseCommand, CommandError

from apps.edi.models import EDI999Import, EDIFile
from apps.edi.utils.sftp_client import download_bytes_via_sftp
from apps.edi.utils.x12 import parse_999, parse_x12


def _el(seg, index):
    elements = (seg or {}).get("elements") or []
    i = index - 1
    if i < 0 or i >= len(elements):
        return ""
    return str(elements[i] or "").strip()


def _original_837_path(ack_path: str) -> str:
    path = str(ack_path or "")
    for suffix in ("_999.x12", "_999.txt", ".999.x12", ".999.txt"):
        if path.lower().endswith(suffix.lower()):
            return path[: -len(suffix)] + (".x12" if suffix.lower().endswith("x12") else ".txt")
    return ""


def _safe_outbound_structure(raw_text: str) -> dict:
    outbound = parse_x12(raw_text or "")
    if not outbound:
        return {}
    isa = next((seg for seg in outbound if seg.get("id") == "ISA"), None)
    gs = next((seg for seg in outbound if seg.get("id") == "GS"), None)
    st = next((seg for seg in outbound if seg.get("id") == "ST"), None)
    sbrs = [seg for seg in outbound if seg.get("id") == "SBR"]
    return {
        "segments": ",".join(seg.get("id") or "?" for seg in outbound[:50]),
        "isa13": _el(isa, 13) or "-",
        "gs06": _el(gs, 6) or "-",
        "st02": _el(st, 2) or "-",
        "sbr": "|".join(
            ":".join([_el(seg, 1) or "-", _el(seg, 2) or "-", _el(seg, 9) or "-"])
            for seg in sbrs
        ) or "-",
    }


def _find_db_outbound(group_control: str):
    if not group_control:
        return None
    candidates = (
        EDIFile.objects.filter(is_active=True)
        .exclude(content__isnull=True)
        .exclude(content="")
        .order_by("-id")[:250]
    )
    for edi_file in candidates:
        try:
            structure = _safe_outbound_structure(edi_file.content or "")
        except Exception:
            continue
        if structure.get("gs06") == group_control:
            return edi_file, structure
    return None


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

        data = download_bytes_via_sftp(credentials=row.credentials, remote_path=row.remote_path)
        if not data:
            raise CommandError("Remote 999 file is empty")

        parsed = parse_999(data.decode("utf-8", errors="replace"))
        by_id = parsed.get("by_id") or {}

        ik3_bits = [
            ":".join([_el(seg, 1) or "-", _el(seg, 2) or "-", _el(seg, 3) or "-", _el(seg, 4) or "-"])
            for seg in by_id.get("IK3") or []
        ]
        ik4_bits = [
            ":".join([_el(seg, 1) or "-", _el(seg, 2) or "-", _el(seg, 3) or "-"])
            for seg in by_id.get("IK4") or []
        ]
        ctx_bits = []
        for seg in by_id.get("CTX") or []:
            if not _el(seg, 1).upper().startswith("SITUATIONAL TRIGGER"):
                continue
            ctx_bits.append(
                ":".join([_el(seg, 2) or "-", _el(seg, 3) or "-", _el(seg, 4) or "-", _el(seg, 5) or "-", _el(seg, 6) or "-"])
            )

        outbound_present = "false"
        outbound_source = "-"
        outbound_file_id = "-"
        structure = {}

        outbound_path = _original_837_path(row.remote_path)
        if outbound_path:
            try:
                raw_837 = download_bytes_via_sftp(credentials=row.credentials, remote_path=outbound_path)
                structure = _safe_outbound_structure(raw_837.decode("utf-8", errors="replace"))
                if structure:
                    outbound_present = "true"
                    outbound_source = "sftp"
            except Exception:
                pass

        if not structure:
            found = _find_db_outbound(parsed.get("ak1", {}).get("group_control") or "")
            if found:
                edi_file, structure = found
                outbound_present = "true"
                outbound_source = "worker_db"
                outbound_file_id = str(edi_file.id)

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
            f"ctx={'|'.join(ctx_bits) or '-'} "
            f"outbound_present={outbound_present} "
            f"outbound_source={outbound_source} "
            f"outbound_file_id={outbound_file_id} "
            f"outbound_isa13={structure.get('isa13', '-')} "
            f"outbound_gs06={structure.get('gs06', '-')} "
            f"outbound_st02={structure.get('st02', '-')} "
            f"outbound_sbr={structure.get('sbr', '-')} "
            f"outbound_segments={structure.get('segments', '-')}"
        )
