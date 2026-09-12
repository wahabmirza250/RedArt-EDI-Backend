"""
Shared Django settings for the EDI microservice.
Environment-specific values live in local.py / production.py.
"""

from datetime import timedelta
from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env(
    DJANGO_DEBUG=(bool, False),
    DJANGO_LOG_LEVEL=(str, "INFO"),
    DJANGO_SECURE_SSL_REDIRECT=(bool, True),
    DJANGO_SECURE_HSTS_SECONDS=(int, 31536000),
    DJANGO_DB_CONN_MAX_AGE=(int, 60),
    CELERY_TASK_ALWAYS_EAGER=(bool, False),
)

environ.Env.read_env(BASE_DIR / ".env")

SECRET_KEY = env("DJANGO_SECRET_KEY", default="change-me-in-local-or-production")

DEBUG = env("DJANGO_DEBUG")

DATA_UPLOAD_MAX_MEMORY_SIZE = env.int("DATA_UPLOAD_MAX_MEMORY_SIZE", default=12 * 1024 * 1024)
FILE_UPLOAD_MAX_MEMORY_SIZE = env.int("FILE_UPLOAD_MAX_MEMORY_SIZE", default=12 * 1024 * 1024)

ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=[])

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Third-party
    "rest_framework",
    "rest_framework_simplejwt",
    "rest_framework_simplejwt.token_blacklist",
    "corsheaders",
    "django_filters",
    "drf_spectacular",
    "django_celery_beat",
    # Project apps
    "apps.core",
    "apps.trading_partner",
    "apps.provider_billing_profile",
    "apps.patient",
    "apps.nemt_trip",
    "apps.long_distance_rule",
    "apps.claim",
    "apps.claim_service_line",
    "apps.edi",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "redartdigital.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "redartdigital.wsgi.application"
ASGI_APPLICATION = "redartdigital.asgi.application"

# SQLite locally unless POSTGRES_HOST is set (Docker / production).
_postgres_host = env("POSTGRES_HOST", default=None)
if _postgres_host:
    DATABASES = {
        "default": {
            "ENGINE": env("DJANGO_DB_ENGINE", default="django.db.backends.postgresql"),
            "NAME": env("POSTGRES_DB", default="edi"),
            "USER": env("POSTGRES_USER", default="edi"),
            "PASSWORD": env("POSTGRES_PASSWORD", default="edi"),
            "HOST": _postgres_host,
            "PORT": env("POSTGRES_PORT", default="5432"),
            "CONN_MAX_AGE": env("DJANGO_DB_CONN_MAX_AGE"),
            "OPTIONS": {"sslmode": env("POSTGRES_SSLMODE", default="prefer")},
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
# Admin / local display in Pakistan Standard Time (UTC+5). DB still stores UTC (USE_TZ).
TIME_ZONE = "Asia/Karachi"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedStaticFilesStorage",
    },
}

MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ],
    "DEFAULT_PAGINATION_CLASS": "apps.core.pagination.StandardPagination",
    "PAGE_SIZE": 50,
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "auth_burst": env("API_AUTH_THROTTLE", default="20/min"),
    },
}

# RedArt server-to-server auth (obtain via POST /api/v1/auth/token/).
SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(
        minutes=env.int("JWT_ACCESS_MINUTES", default=60)
    ),
    "REFRESH_TOKEN_LIFETIME": timedelta(
        days=env.int("JWT_REFRESH_DAYS", default=7)
    ),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": env.bool("JWT_BLACKLIST_AFTER_ROTATION", default=True),
    "UPDATE_LAST_LOGIN": True,
    "AUTH_HEADER_TYPES": ("Bearer",),
    "AUTH_HEADER_NAME": "HTTP_AUTHORIZATION",
    "USER_ID_FIELD": "id",
    "USER_ID_CLAIM": "user_id",
}

# Optional bootstrap values for create_api_service_user / entrypoint.
EDI_API_SERVICE_USERNAME = env("EDI_API_SERVICE_USERNAME", default="")
EDI_API_SERVICE_EMAIL = env("EDI_API_SERVICE_EMAIL", default="")

# Comma-separated override for HCPF designated rural counties (125-mile threshold).
# Empty → use seeded list in apps.long_distance_rule.utils.counties.
EDI_RURAL_COUNTIES = env("EDI_RURAL_COUNTIES", default="")

# Public TEST/PROD API base URL shared with RedArt / Lovable (no trailing slash).
# Example: https://redart-edi-test.onrender.com
EDI_PUBLIC_BASE_URL = env("EDI_PUBLIC_BASE_URL", default="").rstrip("/")

CORS_ALLOWED_ORIGINS = env.list("CORS_ALLOWED_ORIGINS", default=[])
CORS_ALLOWED_ORIGIN_REGEXES = env.list("CORS_ALLOWED_ORIGIN_REGEXES", default=[])
# Lovable hosts the UI only — allow its HTTPS origins when explicitly enabled.
# Prefer server-to-server: Lovable/UI → RedArt backend → this EDI API (JWT).
if env.bool("EDI_ALLOW_LOVABLE_ORIGINS", default=False):
    _lovable_regexes = [
        r"^https://[\w.-]+\.lovable\.app$",
        r"^https://[\w.-]+\.lovableproject\.com$",
    ]
    CORS_ALLOWED_ORIGIN_REGEXES = list(CORS_ALLOWED_ORIGIN_REGEXES) + [
        r for r in _lovable_regexes if r not in CORS_ALLOWED_ORIGIN_REGEXES
    ]
CORS_ALLOW_CREDENTIALS = env.bool("CORS_ALLOW_CREDENTIALS", default=False)

SPECTACULAR_SETTINGS = {
    "TITLE": "RedArt EDI API",
    "DESCRIPTION": (
        "Colorado Medicaid 837P EDI microservice. "
        "Authenticate with POST /api/v1/auth/token/ then send "
        "Authorization: Bearer <access>."
    ),
    "VERSION": "0.1.0",
    "ENUM_NAME_OVERRIDES": {
        "ClaimStatus": "apps.claim.choices.ClaimStatus",
        "BatchStatus": "apps.claim.choices.BatchStatus",
        "DocumentStatus": "apps.claim.choices.DocumentStatus",
        "AttachmentStatus": "apps.claim.choices.AttachmentStatus",
        "AttachmentSubmissionStatus": "apps.claim.choices.AttachmentSubmissionStatus",
        "EDIFileStatus": "apps.edi.choices.EDIFileStatus",
        "TransferLogStatus": "apps.edi.choices.TransferLogStatus",
        "AcknowledgementStatus": "apps.edi.choices.AcknowledgementStatus",
        "AcknowledgementType": "apps.edi.choices.AcknowledgementType",
    },
    "APPEND_COMPONENTS": {
        "securitySchemes": {
            "bearerAuth": {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": "JWT",
            }
        }
    },
    "SECURITY": [{"bearerAuth": []}],
    "TAGS": [
        {
            "name": "integration",
            "description": "Lovable / RedArt integration catalog (endpoints, auth, happy path).",
        },
        {
            "name": "auth",
            "description": "JWT obtain / refresh / verify for RedArt server-to-server calls.",
        },
        {
            "name": "trading_partner",
            "description": "Trading partner CRUD (ISA/GS sender & receiver IDs).",
        },
        {
            "name": "provider_billing_profile",
            "description": "Provider billing profile CRUD (NPI / Medicaid billing identity).",
        },
        {
            "name": "patient",
            "description": "Patient / Medicaid member CRUD (county drives long-distance rules).",
        },
        {
            "name": "nemt_trip",
            "description": "NEMT trip CRUD and long-distance rule preview.",
        },
        {
            "name": "long_distance_rule",
            "description": "Configurable 52/125 and 25+ mile thresholds by county type.",
        },
        {
            "name": "claim",
            "description": "Claim CRUD and create-from-trip with long-distance flags.",
        },
        {
            "name": "claim_service_line",
            "description": "Claim service line CRUD (procedure / units / charge).",
        },
        {
            "name": "edi",
            "description": "EDI control numbers, 837P files, generate/upload, transfer logs, 999 acknowledgements.",
        },
        {
            "name": "edi_acknowledgement",
            "description": "Inbound 999 acknowledgements (structural accept — not payment).",
        },
        {
            "name": "attachment_submission",
            "description": "HCPF attachment-channel submissions (separate from 837P).",
        },
        {
            "name": "sftp",
            "description": "SFTP credentials and remote directories.",
        },
    ],
}

# ========
# Celery / Redis
# ========
CELERY_BROKER_URL = env("CELERY_BROKER_URL", default="redis://localhost:6379/0")
CELERY_RESULT_BACKEND = env("CELERY_RESULT_BACKEND", default="redis://localhost:6379/1")
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = TIME_ZONE
CELERY_ENABLE_UTC = True
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = env.int("CELERY_TASK_TIME_LIMIT", default=30 * 60)
CELERY_TASK_ALWAYS_EAGER = env("CELERY_TASK_ALWAYS_EAGER")
# Auto-expire task results after 24 hours (defense in depth with daily flush).
CELERY_RESULT_EXPIRES = env.int("CELERY_RESULT_EXPIRES", default=60 * 60 * 24)
# Celery Beat
CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"
CELERY_BEAT_SCHEDULE = {
    "cleanup-celery-storage-every-24h": {
        "task": "redartdigital.tasks.cleanup_celery_storage",
        "schedule": 60 * 60 * 24,
    },
}

# X12 837P envelope constants (sender/receiver IDs come from TradingPartner).
EDI_ENVELOPE = {
    "isa05": env("EDI_ISA05", default="ZZ"),
    "isa07": env("EDI_ISA07", default="ZZ"),
    "gs01": env("EDI_GS01", default="HC"),
    "gs08": env("EDI_GS08", default="005010X222A1"),
    "element_separator": env("EDI_ELEMENT_SEPARATOR", default="*"),
    "component_separator": env("EDI_COMPONENT_SEPARATOR", default=":"),
    "segment_terminator": env("EDI_SEGMENT_TERMINATOR", default="~"),
    "repetition_separator": env("EDI_REPETITION_SEPARATOR", default="^"),
}

# MinIO / S3 (local docker uses MinIO as S3-compatible store)
AWS_ACCESS_KEY_ID = env("AWS_ACCESS_KEY_ID", default="minioadmin")
AWS_SECRET_ACCESS_KEY = env("AWS_SECRET_ACCESS_KEY", default="minioadmin")
AWS_STORAGE_BUCKET_NAME = env("AWS_STORAGE_BUCKET_NAME", default="edi-files")
AWS_S3_ENDPOINT_URL = env("AWS_S3_ENDPOINT_URL", default="http://127.0.0.1:9000")
AWS_S3_REGION_NAME = env("AWS_S3_REGION_NAME", default="us-east-1")

# tax_id is now stored per ProviderBillingProfile (API-managed).
# EDI_DEFAULT_BILLING_TAX_ID was removed — never use a global fallback TIN.
EDI_MAX_SFTP_DOWNLOAD_BYTES = env.int("EDI_MAX_SFTP_DOWNLOAD_BYTES", default=5_000_000)
EDI_SFTP_LIST_MAX_FILES = env.int("EDI_SFTP_LIST_MAX_FILES", default=5000)
EDI_MAX_X12_CONTENT_CHARS = env.int("EDI_MAX_X12_CONTENT_CHARS", default=2_000_000)
EDI_SFTP_REQUIRE_HOST_FINGERPRINT = env.bool(
    "EDI_SFTP_REQUIRE_HOST_FINGERPRINT", default=True
)

# Claim document blob storage (MinIO/S3 primary; local MEDIA fallback).
CLAIM_DOCUMENT_MAX_BYTES = env.int("CLAIM_DOCUMENT_MAX_BYTES", default=10 * 1024 * 1024)
CLAIM_DOCUMENT_ALLOWED_CONTENT_TYPES = tuple(
    env.list(
        "CLAIM_DOCUMENT_ALLOWED_CONTENT_TYPES",
        default=[
            "application/pdf",
            "image/jpeg",
            "image/png",
            "image/webp",
        ],
    )
)
ATTACHMENT_ADAPTER_DEFAULT = env(
    "ATTACHMENT_ADAPTER_DEFAULT", default="HCPF_PORTAL"
)
ATTACHMENT_MFT_ENABLED = env.bool("ATTACHMENT_MFT_ENABLED", default=False)
ATTACHMENT_MFT_ENVIRONMENT = env("ATTACHMENT_MFT_ENVIRONMENT", default="TEST")
ATTACHMENT_PRODUCTION_MODE = env.bool("ATTACHMENT_PRODUCTION_MODE", default=False)
ATTACHMENT_PRODUCTION_DEFAULT_CHANNEL = env(
    "ATTACHMENT_PRODUCTION_DEFAULT_CHANNEL",
    default="HCPF_APPROVED_CHANNEL",
)
ATTACHMENT_MFT_REMOTE_PATH_TEMPLATE = env(
    "ATTACHMENT_MFT_REMOTE_PATH_TEMPLATE",
    default="{claim_number}/{document_type}/{filename}",
)

# Optional seed for real/test SFTP (never commit secrets — put in .env)
SFTP_SEED_NAME = env("SFTP_SEED_NAME", default="SEED-SFTP-CLOUD")
SFTP_SEED_HOST = env("SFTP_SEED_HOST", default="")
SFTP_SEED_PORT = env.int("SFTP_SEED_PORT", default=22)
SFTP_SEED_USERNAME = env("SFTP_SEED_USERNAME", default="")
SFTP_SEED_PASSWORD = env("SFTP_SEED_PASSWORD", default="")
SFTP_SEED_SEND_PATH = env("SFTP_SEED_SEND_PATH", default="/send")
SFTP_SEED_RECV_PATH = env("SFTP_SEED_RECV_PATH", default="/recv")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "standard": {
            "format": "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "standard",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": env("DJANGO_LOG_LEVEL"),
    },
    "loggers": {
        "django.request": {
            "handlers": ["console"],
            "level": "ERROR",
            "propagate": False,
        },
    },
}
