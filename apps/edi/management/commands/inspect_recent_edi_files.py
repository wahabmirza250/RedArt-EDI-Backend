from django.core.management.base import BaseCommand

from apps.edi.models import EDIFile, SFTPCredentials, SFTPDirectory
from apps.edi.utils.x12 import parse_x12


def _el(seg, index):
    elements = (seg or {}).get("elements") or []
    i = index - 1
    if i < 0 or i >= len(elements):
        return ""
    return str(elements[i] or "").strip()


def _clean(text):
    value = " ".join(str(text or "").split())
    return value[:180] if value else "-"


class Command(BaseCommand):
    help = "Read-only recent EDI file metadata; prints no claim/member/provider content."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=10)

    def handle(self, *args, **options):
        active_credentials = SFTPCredentials.objects.filter(is_active=True).count()
        active_directories = SFTPDirectory.objects.filter(is_active=True).count()
        self.stdout.write(
            f"API_SFTP_READY active_credentials={active_credentials} active_directories={active_directories}"
        )

        limit = max(1, min(int(options["limit"]), 25))
        rows = EDIFile.objects.filter(is_active=True).order_by("-id")[:limit]
        if not rows:
            self.stdout.write("API_EDI_FILES count=0")
            return

        for row in rows:
            isa13 = "-"
            gs06 = "-"
            st02 = "-"
            sbr_count = 0
            try:
                segments = parse_x12(row.content or "")
                isa = next((s for s in segments if s.get("id") == "ISA"), None)
                gs = next((s for s in segments if s.get("id") == "GS"), None)
                st = next((s for s in segments if s.get("id") == "ST"), None)
                isa13 = _el(isa, 13) or "-"
                gs06 = _el(gs, 6) or "-"
                st02 = _el(st, 2) or "-"
                sbr_count = sum(1 for s in segments if s.get("id") == "SBR")
            except Exception:
                pass

            sftp = row.transfer_logs.filter(is_active=True, channel="SFTP").order_by("-id").first()
            sftp_status = getattr(sftp, "status", None) or "-"
            remote = "yes" if getattr(sftp, "remote_path", None) else "no"
            sftp_message = _clean(getattr(sftp, "message", None))
            sftp_attempt = getattr(sftp, "attempt", None) or "-"
            created = getattr(row, "created_at", None)
            uploaded = getattr(row, "uploaded_at", None)
            self.stdout.write(
                "API_EDI_FILE "
                f"id={row.id} "
                f"status={row.status or '-'} "
                f"created={created.isoformat() if created else '-'} "
                f"uploaded={uploaded.isoformat() if uploaded else '-'} "
                f"isa13={isa13} gs06={gs06} st02={st02} "
                f"sbr_count={sbr_count} "
                f"sftp_attempt={sftp_attempt} sftp_status={sftp_status} "
                f"remote_path_present={remote} sftp_message={sftp_message}"
            )
