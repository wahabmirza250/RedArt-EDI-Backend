#!/usr/bin/env bash
# ========
# Container entrypoint
# Roles: web | worker | beat | shell
# ========
set -euo pipefail

# Decode the protected single-line Render key into an owner-only runtime file.
if [[ -n "${HCPF_SFTP_PRIVATE_KEY_B64:-}" ]]; then
  export HCPF_SFTP_PRIVATE_KEY_PATH="/tmp/edifecs_sftp_private_key.pem"
  python - <<'PY'
import base64
import os
from pathlib import Path

target = Path(os.environ["HCPF_SFTP_PRIVATE_KEY_PATH"])
target.write_bytes(base64.b64decode(os.environ["HCPF_SFTP_PRIVATE_KEY_B64"]))
os.chmod(target, 0o600)
PY
  echo "[entrypoint] Edifecs private key prepared from protected environment."
fi

# ========
# Wait for Postgres
# ========
wait_for_postgres() {
  echo "[entrypoint] Waiting for Postgres at ${POSTGRES_HOST:-postgres}:${POSTGRES_PORT:-5432} ..."
  python - <<'PY'
import os
import time

import psycopg

host = os.environ.get("POSTGRES_HOST", "postgres")
port = int(os.environ.get("POSTGRES_PORT", "5432"))
user = os.environ.get("POSTGRES_USER", "edi")
password = os.environ.get("POSTGRES_PASSWORD", "edi")
dbname = os.environ.get("POSTGRES_DB", "edi")

deadline = time.time() + 60
while True:
    try:
        with psycopg.connect(
            host=host,
            port=port,
            user=user,
            password=password,
            dbname=dbname,
            connect_timeout=3,
        ) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        print("[entrypoint] Postgres is ready.")
        break
    except Exception as exc:
        if time.time() > deadline:
            raise SystemExit(f"[entrypoint] Postgres not ready: {exc}") from exc
        time.sleep(2)
PY
}

# ========
# Wait for Redis
# ========
wait_for_redis() {
  echo "[entrypoint] Waiting for Redis ..."
  python - <<'PY'
import os
import time
from urllib.parse import urlparse

import redis

url = os.environ.get("REDIS_AUTH_URL") or os.environ.get(
    "CELERY_BROKER_URL", "redis://redis:6379/0"
)
parsed = urlparse(url)

deadline = time.time() + 60
# Keep the full URL so authenticated and TLS Redis connections retain their
# username, password, database, and SSL query options.
client = redis.Redis(
    host=parsed.hostname or "redis",
    port=parsed.port or 6379,
    username=parsed.username,
    password=parsed.password,
    ssl=parsed.scheme == "rediss",
    socket_connect_timeout=3,
)
while True:
    try:
        client.ping()
        print("[entrypoint] Redis is ready.")
        break
    except Exception as exc:
        if time.time() > deadline:
            raise SystemExit(f"[entrypoint] Redis not ready: {exc}") from exc
        time.sleep(2)
PY
}

# ========
# Register Celery Beat schedules (django-celery-beat DB)
# ========
setup_celery_beat_schedules() {
  echo "[entrypoint] Ensuring Celery Beat cleanup schedule ..."
  python - <<'PY'
import os

import django

os.environ.setdefault(
    "DJANGO_SETTINGS_MODULE",
    os.environ.get("DJANGO_SETTINGS_MODULE", "redartdigital.settings.docker"),
)
django.setup()

from django_celery_beat.models import CrontabSchedule, PeriodicTask

schedule, _ = CrontabSchedule.objects.get_or_create(
    minute="0",
    hour="0",
    day_of_week="*",
    day_of_month="*",
    month_of_year="*",
    timezone="UTC",
)

PeriodicTask.objects.update_or_create(
    name="cleanup-celery-storage-every-24h",
    defaults={
        "crontab": schedule,
        "interval": None,
        "task": "redartdigital.tasks.cleanup_celery_storage",
        "enabled": True,
        "description": "Verify Celery result expiry without flushing Redis or queued work.",
    },
)

every_minute, _ = CrontabSchedule.objects.get_or_create(
    minute="*",
    hour="*",
    day_of_week="*",
    day_of_month="*",
    month_of_year="*",
    timezone="UTC",
)
PeriodicTask.objects.update_or_create(
    name="poll-edi-999-imports-every-minute",
    defaults={
        "crontab": every_minute,
        "interval": None,
        "task": "apps.edi.tasks.poll_edi_999_imports",
        "enabled": True,
        "description": "Import 999: poll SFTP inbound folders every minute.",
    },
)
every_two_minutes, _ = CrontabSchedule.objects.get_or_create(
    minute="*/2",
    hour="*",
    day_of_week="*",
    day_of_month="*",
    month_of_year="*",
    timezone="UTC",
)
PeriodicTask.objects.update_or_create(
    name="poll-edi-277-imports-every-two-minutes",
    defaults={
        "crontab": every_two_minutes,
        "interval": None,
        "task": "apps.edi.tasks.poll_edi_277_imports",
        "enabled": True,
        "description": "Import 277 claim statuses every two minutes.",
    },
)
every_five_minutes, _ = CrontabSchedule.objects.get_or_create(
    minute="*/5",
    hour="*",
    day_of_week="*",
    day_of_month="*",
    month_of_year="*",
    timezone="UTC",
)
PeriodicTask.objects.update_or_create(
    name="poll-edi-835-imports-every-five-minutes",
    defaults={
        "crontab": every_five_minutes,
        "interval": None,
        "task": "apps.edi.tasks.poll_edi_835_imports",
        "enabled": True,
        "description": "Import 835 remittances every five minutes.",
    },
)
PeriodicTask.objects.filter(
    name__in=(
        "poll-edi-999-imports-hourly",
        "poll-edi-835-imports-hourly",
    )
).update(enabled=False)
print("[entrypoint] Beat schedule ready: cleanup, poll-999, poll-277, poll-835")
PY
}

ROLE="${1:-web}"

# ========
# Shared startup
# ========
wait_for_postgres
wait_for_redis

# ========
# Django migrate + beat schedules (web only)
# migrate is safe/idempotent: "No migrations to apply" when already up to date
# ========
ensure_api_service_user() {
  if [[ -z "${EDI_API_SERVICE_USERNAME:-}" || -z "${EDI_API_SERVICE_PASSWORD:-}" ]]; then
    echo "[entrypoint] EDI API service user skipped (set EDI_API_SERVICE_USERNAME/PASSWORD)."
    return 0
  fi
  echo "[entrypoint] Ensuring EDI API service user exists ..."
  python manage.py create_api_service_user \
    --username "${EDI_API_SERVICE_USERNAME}" \
    --email "${EDI_API_SERVICE_EMAIL:-${EDI_API_SERVICE_USERNAME}@edi.local}" \
    --password-from-env \
    --rotate-password \
    || echo "[entrypoint] WARNING: create_api_service_user failed"
}

configure_hcpf_sftp() {
  local key_path="${HCPF_SFTP_PRIVATE_KEY_PATH:-/etc/secrets/edifecs_sftp_private_key.pem}"
  if [[ ! -f "${key_path}" ]]; then
    echo "[entrypoint] Edifecs SFTP secret not present; skipping production SFTP configuration."
    return 0
  fi
  echo "[entrypoint] Configuring HCPF Edifecs production SFTP ..."
  PYTHONPATH=/app python scripts/wire_hcpf_sftp.py
}

ensure_superuser() {
  if [[ -z "${DJANGO_SUPERUSER_USERNAME:-}" ]]; then
    return 0
  fi
  echo "[entrypoint] Ensuring Django superuser exists ..."
  python - <<'PY'
import os

import django

os.environ.setdefault(
    "DJANGO_SETTINGS_MODULE",
    os.environ.get("DJANGO_SETTINGS_MODULE", "redartdigital.settings.docker"),
)
django.setup()

from django.contrib.auth import get_user_model

User = get_user_model()
username = os.environ["DJANGO_SUPERUSER_USERNAME"]
email = os.environ.get("DJANGO_SUPERUSER_EMAIL", "admin@example.com")
password = os.environ.get("DJANGO_SUPERUSER_PASSWORD", "")

if not password:
    print("[entrypoint] DJANGO_SUPERUSER_PASSWORD empty — skip superuser create")
else:
    user, created = User.objects.get_or_create(
        username=username,
        defaults={"email": email, "is_staff": True, "is_superuser": True},
    )
    user.email = email
    user.is_staff = True
    user.is_superuser = True
    user.set_password(password)
    user.save()
    print(f"[entrypoint] Superuser {'created' if created else 'updated'}: {username}")
PY
}

if [[ "${ROLE}" == "web" ]]; then
  if [[ "${RUN_MIGRATE_ON_START:-true}" == "true" ]]; then
    echo "[entrypoint] Running migrations ..."
    python manage.py migrate --noinput
  else
    echo "[entrypoint] Skipping migrations (RUN_MIGRATE_ON_START=false)"
  fi
  setup_celery_beat_schedules
  ensure_superuser
  ensure_api_service_user
  configure_hcpf_sftp
  # collectstatic only needed for Gunicorn + WhiteNoise
  if [[ "${USE_GUNICORN:-false}" == "true" ]]; then
    echo "[entrypoint] Collecting static files ..."
    python manage.py collectstatic --noinput
  fi
fi

# ========
# Launch role
# ========
case "${ROLE}" in
  web)
    # Local/Docker default: runserver (auto-reloads on .py changes).
    # Set USE_GUNICORN=true for production-like serving.
    if [[ "${USE_GUNICORN:-false}" == "true" ]]; then
      if [[ "${AUTO_RELOAD:-false}" == "true" ]]; then
        echo "[entrypoint] Starting Gunicorn with --reload ..."
        exec gunicorn redartdigital.wsgi:application \
          --bind "0.0.0.0:${PORT:-8000}" \
          --workers "${GUNICORN_WORKERS:-3}" \
          --timeout "${GUNICORN_TIMEOUT:-120}" \
          --reload
      fi
      echo "[entrypoint] Starting Gunicorn ..."
      exec gunicorn redartdigital.wsgi:application \
        --bind "0.0.0.0:${PORT:-8000}" \
        --workers "${GUNICORN_WORKERS:-3}" \
        --timeout "${GUNICORN_TIMEOUT:-120}"
    else
      echo "[entrypoint] Starting Django runserver (auto-reload on) ..."
      exec python manage.py runserver "0.0.0.0:${PORT:-8000}"
    fi
    ;;
  worker)
    if [[ "${AUTO_RELOAD:-true}" == "true" ]]; then
      echo "[entrypoint] Starting Celery worker with auto-reload ..."
      exec watchmedo auto-restart \
        --directory=/app \
        --pattern="*.py" \
        --recursive \
        -- \
        celery -A redartdigital worker \
          --loglevel="${CELERY_LOG_LEVEL:-INFO}" \
          --concurrency="${CELERY_CONCURRENCY:-2}"
    fi
    echo "[entrypoint] Starting Celery worker ..."
    exec celery -A redartdigital worker \
      --loglevel="${CELERY_LOG_LEVEL:-INFO}" \
      --concurrency="${CELERY_CONCURRENCY:-2}"
    ;;
  beat)
    if [[ "${AUTO_RELOAD:-true}" == "true" ]]; then
      echo "[entrypoint] Starting Celery beat with auto-reload ..."
      exec watchmedo auto-restart \
        --directory=/app \
        --pattern="*.py" \
        --recursive \
        -- \
        celery -A redartdigital beat \
          --loglevel="${CELERY_LOG_LEVEL:-INFO}" \
          --scheduler django_celery_beat.schedulers:DatabaseScheduler
    fi
    echo "[entrypoint] Starting Celery beat ..."
    exec celery -A redartdigital beat \
      --loglevel="${CELERY_LOG_LEVEL:-INFO}" \
      --scheduler django_celery_beat.schedulers:DatabaseScheduler
    ;;
  shell)
    exec python manage.py shell
    ;;
  flower)
    echo "[entrypoint] Starting Flower (Celery monitor) ..."
    FLOWER_AUTH="${FLOWER_BASIC_AUTH:-}"
    if [ -z "${FLOWER_AUTH}" ]; then
      echo "[entrypoint] ERROR: FLOWER_BASIC_AUTH must be set (user:password)." >&2
      exit 1
    fi
    exec celery -A redartdigital flower \
      --address=0.0.0.0 \
      --port="${FLOWER_PORT:-5555}" \
      --broker="${CELERY_BROKER_URL:-redis://redis:6379/0}" \
      --basic-auth="${FLOWER_AUTH}"
    ;;
  *)
    echo "[entrypoint] Unknown role: ${ROLE}"
    echo "Usage: entrypoint.sh [web|worker|beat|flower|shell]"
    exit 1
    ;;
esac
