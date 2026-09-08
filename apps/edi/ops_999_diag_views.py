from __future__ import annotations

import hmac
import os

from django.http import JsonResponse
from django.views.decorators.http import require_GET

from apps.edi.models import EDI999Import
from apps.edi.utils.sftp_client import download_bytes_via_sftp
from apps.edi.utils.x12 import parse_999


@require_GET
def real_999_diag(request):
    configured = os.environ.get("OPS_999_DIAG_TOKEN", "")
    supplied = request.GET.get("token", "")
    if not configured or not supplied or not hmac.compare_digest(configured, supplied):
        response = JsonResponse({"detail": "not found"}, status=404)
        response["Cache-Control"] = "no-store"
        return response

    try:
        import_id = int(request.GET.get("import_id", "5"))
    except ValueError:
        response = JsonResponse({"detail": "invalid import_id"}, status=400)
        response["Cache-Control"] = "no-store"
        return response

    row = (
        EDI999Import.objects.select_related("credentials")
        .filter(pk=import_id, is_active=True)
        .first()
    )
    if row is None or not row.credentials_id or not row.remote_path:
        response = JsonResponse({"detail": "999 import unavailable"}, status=404)
        response["Cache-Control"] = "no-store"
        return response

    data = download_bytes_via_sftp(
        credentials=row.credentials,
        remote_path=row.remote_path,
    )
    parsed = parse_999(data.decode("utf-8", errors="replace"))

    response = JsonResponse(
        {
            "import_id": row.id,
            "status": parsed.get("status"),
            "ik5": parsed.get("ik5_code"),
            "ak9": parsed.get("ak9_code"),
            "ak1_functional_id": parsed.get("ak1", {}).get("functional_id"),
            "ak1_group_control": parsed.get("ak1", {}).get("group_control"),
            "ak2_transaction_set": parsed.get("ak2", {}).get("transaction_set"),
            "ak2_st02": parsed.get("ak2", {}).get("st02"),
            "ack_isa13": parsed.get("isa13"),
        }
    )
    response["Cache-Control"] = "no-store"
    return response
