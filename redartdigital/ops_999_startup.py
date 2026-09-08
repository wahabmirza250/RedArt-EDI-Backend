from __future__ import annotations

import os
import subprocess
import sys


def _run_and_print(command, *, env, prefixes, label, timeout=30):
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
        print(f"{label}_ERROR type={type(exc).__name__}", flush=True)
        return False
    if proc.returncode != 0:
        print(f"{label}_ERROR command_failed=true returncode={proc.returncode}", flush=True)
        return False
    found = False
    for line in (proc.stdout or "").splitlines():
        if any(line.startswith(prefix) for prefix in prefixes):
            print(line, flush=True)
            found = True
    if not found and prefixes:
        print(f"{label}_NOTICE matching_output_missing=true", flush=True)
    return True


def run_once() -> None:
    run_diag = os.environ.get("OPS_RUN_999_DIAG_ON_START") == "1"
    run_prepare = os.environ.get("OPS_RUN_PREPARE_ON_START") == "1"
    if not run_diag and not run_prepare:
        return

    lock_path = "/tmp/redart_real_ops_once.lock"
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
    except FileExistsError:
        return

    api_env = os.environ.copy()

    # Explicitly authorized one-claim preparation. This command creates the
    # local Django claim/batch/837 and runs pyx12, but never uploads to SFTP.
    if run_prepare:
        _run_and_print(
            [sys.executable, "manage.py", "prepare_hcpf_one_shot"],
            env=api_env,
            prefixes=("OPS_PREPARED ",),
            label="OPS_PREPARE",
            timeout=45,
        )

    if not run_diag:
        return

    # Inspect API EDI metadata after preparation; no claim/member/provider data.
    _run_and_print(
        [sys.executable, "manage.py", "inspect_recent_edi_files", "--limit", "12"],
        env=api_env,
        prefixes=("API_EDI_FILE ", "API_EDI_FILES "),
        label="API_EDI_DIAG",
    )

    # Inspect the existing real 999 in the worker's database/SFTP context.
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
        label="REAL_999_DIAG",
    )
