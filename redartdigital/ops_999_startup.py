from __future__ import annotations

import os
import subprocess
import sys


def run_once() -> None:
    if os.environ.get("OPS_RUN_999_DIAG_ON_START") != "1":
        return

    lock_path = "/tmp/redart_real_999_diag.lock"
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
    except FileExistsError:
        return

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
    for target, source in mapping.items():
        value = os.environ.get(source, "")
        if value:
            env[target] = value

    import_id = os.environ.get("OPS_999_IMPORT_ID", "5")
    try:
        proc = subprocess.run(
            [sys.executable, "manage.py", "inspect_real_999", "--import-id", import_id],
            cwd="/app",
            env=env,
            text=True,
            capture_output=True,
            timeout=30,
        )
    except Exception as exc:
        print(f"REAL_999_DIAG_ERROR type={type(exc).__name__}", flush=True)
        return

    if proc.returncode != 0:
        print("REAL_999_DIAG_ERROR command_failed=true", flush=True)
        return

    found = False
    for line in (proc.stdout or "").splitlines():
        if line.startswith("REAL_999 "):
            print(line, flush=True)
            found = True
            break
    if not found:
        print("REAL_999_DIAG_ERROR output_missing=true", flush=True)
