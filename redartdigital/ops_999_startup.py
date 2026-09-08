from __future__ import annotations

import os
import subprocess
import sys


def _run_and_print(command, *, env, prefixes, timeout=30):
    try:
        proc = subprocess.run(
            command,
            cwd="/app",
            env=env,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    except Exception as exc:
        print(f"OPS_DIAG_ERROR type={type(exc).__name__}", flush=True)
        return
    if proc.returncode != 0:
        print("OPS_DIAG_ERROR command_failed=true", flush=True)
        return
    for line in (proc.stdout or "").splitlines():
        if any(line.startswith(prefix) for prefix in prefixes):
            print(line, flush=True)


def run_once() -> None:
    if os.environ.get("OPS_RUN_999_DIAG_ON_START") != "1":
        return

    lock_path = "/tmp/redart_real_999_diag.lock"
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
    except FileExistsError:
        return

    # First inspect the API service's own database. This is read-only and emits
    # only EDI/control/transport metadata, never claim/member/provider content.
    api_env = os.environ.copy()
    _run_and_print(
        [sys.executable, "manage.py", "inspect_recent_edi_files", "--limit", "12"],
        env=api_env,
        prefixes=("API_EDI_FILE ", "API_EDI_FILES "),
    )

    # Then inspect the real 999 in the worker's database/SFTP context.
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
        value = os.environ.get(source, "")
        if value:
            worker_env[target] = value

    import_id = os.environ.get("OPS_999_IMPORT_ID", "5")
    _run_and_print(
        [sys.executable, "manage.py", "inspect_real_999", "--import-id", import_id],
        env=worker_env,
        prefixes=("REAL_999 ",),
    )
