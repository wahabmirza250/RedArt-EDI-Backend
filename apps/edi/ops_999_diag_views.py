from __future__ import annotations

import hmac
import os
import subprocess
import sys

from django.http import JsonResponse
from django.views.decorators.http import require_GET


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
    env = os.environ.copy()
    missing = []
    for target, source in mapping.items():
        value = os.environ.get(source, "")
        if value:
            env[target] = value
        elif target not in ("POSTGRES_SSLMODE", "DJANGO_SETTINGS_MODULE"):
            missing.append(source)

    if missing:
        response = JsonResponse({"detail": "worker diagnostic wiring incomplete"}, status=503)
        response["Cache-Control"] = "no-store"
        return response

    proc = subprocess.run(
        [sys.executable, "manage.py", "inspect_real_999", "--import-id", str(import_id)],
        cwd="/app",
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    if proc.returncode != 0:
        response = JsonResponse({"detail": "worker diagnostic failed"}, status=502)
        response["Cache-Control"] = "no-store"
        return response

    line = ""
    for candidate in (proc.stdout or "").splitlines():
        if candidate.startswith("REAL_999 "):
            line = candidate.strip()
            break
    if not line:
        response = JsonResponse({"detail": "999 diagnostic output missing"}, status=502)
        response["Cache-Control"] = "no-store"
        return response

    fields = {}
    for token in line.split()[1:]:
        if "=" in token:
            key, value = token.split("=", 1)
            fields[key] = value

    response = JsonResponse(fields)
    response["Cache-Control"] = "no-store"
    return response
