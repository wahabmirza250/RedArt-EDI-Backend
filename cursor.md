# RedArt EDI Backend — Cursor Developer Guide

> **How to start a new Cursor session:**  
> Open a new chat and say: *"Read* `cursor.md` *and* `docs/HANDOFF.md`*, then help me with [your task]."*

---

## Project at a glance


| Item     | Detail                                                                                                   |
| -------- | -------------------------------------------------------------------------------------------------------- |
| Branch   | `Ayaz/local-main`                                                                                        |
| Repo     | [https://github.com/ayazkhan1410/RedArt-EDI-Backend](https://github.com/ayazkhan1410/RedArt-EDI-Backend) |
| API base | `http://127.0.0.1:7000/api/v1/` *(local dev only — replace with deployed HTTPS base URL in TEST/PROD)*   |
| Swagger  | `http://127.0.0.1:7000/api/docs/` *(local dev only — deployed docs URL will be different)*               |
| Health   | `GET /api/health/`                                                                                       |


---



## 1. First-time setup

```bash
# Clone
git clone https://github.com/ayazkhan1410/RedArt-EDI-Backend.git
cd RedArt-EDI-Backend
git checkout Ayaz/local-main

# Copy env and start services
cp .env.example .env
docker compose up -d

# Apply all migrations
docker compose exec backend python manage.py migrate

# (Optional) Load synthetic demo data
docker compose exec backend python manage.py seed_demo_data
```

---



## 2. Daily commands

```bash
# Start all services
docker compose up -d

# Stop all services
docker compose down

# View backend logs
docker compose logs -f backend

# Django shell
docker compose exec backend python manage.py shell

# Run all tests
docker compose exec backend python manage.py test apps

# Run tests for a specific app
docker compose exec backend python manage.py test apps.edi
docker compose exec backend python manage.py test apps.claim

# Create a migration (after model change — always review before applying)
docker compose exec backend python manage.py makemigrations <app_name>

# Apply migrations
docker compose exec backend python manage.py migrate

# Check for pending migrations
docker compose exec backend python manage.py showmigrations
```

---



## 3. Project structure

```
apps/
  claim/               # Claims, batches, attachments, documents
  claim_service_line/  # Service lines (procedures + charges)
  core/                # BaseModel, auth constants, shared pagination
  edi/                 # 837P generation, SFTP, 999/277/835, readiness
  long_distance_rule/  # DB-driven 52-mile / rural thresholds
  nemt_trip/           # Trips linking patient ↔ provider ↔ dates
  patient/             # Patient demographics (medicaid_member_id required)
  provider_billing_profile/ # Provider/company billing config (NPI or atypical)
  trading_partner/     # ISA/GS sender/receiver config per environment

redartdigital/
  settings/
    base.py            # Shared settings
    docker.py          # Docker Compose settings
    local.py           # Local dev
    production.py      # Production

docs/
  API_USER_GUIDE.md    # Full API reference (start here for integration)
  DEPLOYMENT_GUIDE.md  # Render / Docker deploy steps
  REDART_API_SAMPLES.md # curl / JSON examples
  HANDOFF.md           # Dev agent context
```

---



## 4. Key files you will touch


| File                                      | What it does                                        |
| ----------------------------------------- | --------------------------------------------------- |
| `apps/edi/utils/schema.py`                | Builds X12 837P segment list from payload dict      |
| `apps/edi/utils/readiness.py`             | Pre-flight validation before 837P generation        |
| `apps/edi/utils/handler.py`               | Orchestrates 837P generation → file → status update |
| `apps/edi/utils/service.py`               | EDI service helpers (control numbers, ack, 835)     |
| `apps/claim/utils/service.py`             | Claim creation, validation, document sync           |
| `apps/claim/choices.py`                   | `ClaimStatus`, `BatchStatus`, attachment choices    |
| `apps/provider_billing_profile/models.py` | Provider model (NPI vs atypical)                    |
| `apps/patient/models.py`                  | Patient model (`medicaid_member_id` is key)         |
| `redartdigital/api_v1_urls.py`            | **Only place** to wire new API routes               |


---



## 5. Enterprise invariants — never violate these



### Zero fabrication

- **Never** default a missing `procedure_code` to anything — raise a validation error.
- **Never** fabricate NPI, SSN, DOB, gender, ZIP, or `medicaid_member_id`.
- `medicaid_member_id` is the **only** mandatory patient identifier for Colorado NEMT 837P.
- `date_of_birth` / gender / address are **optional** — emit in DMG/N3/N4 only when present.



### Provider types

```python
# Standard NPI provider (is_atypical=False):
NM108 = "XX", NM109 = provider.npi
REF*EI = provider.tax_id when present

# Atypical Colorado Medicaid provider (is_atypical=True):
NM108 = "XX", NM109 = provider.medicaid_provider_id
# Never invent NPI; REF*EI only if real tax_id exists
```



### ISA fixed length

The ISA segment must be **exactly 106 characters**. The schema builder enforces this and raises immediately if the length is wrong.

### TEST vs PRODUCTION

- `ISA15 = "T"` for TEST, `ISA15 = "P"` for PRODUCTION — always set via the batch's environment.
- Never hard-code `P` or `T` anywhere.



### Claim status lifecycle

```
DRAFT → READY_FOR_837P → EDI_GENERATED → EDI_SENT → EDI_ACCEPTED → PAID
                                                   ↘ EDI_REJECTED
```

- `EDI_GENERATED` = file generated, **not yet uploaded**.
- `EDI_SENT` = uploaded to HCPF.
- `EDI_REJECTED` = 999/TA1 came back rejected; claim needs correction.
- Terminal states `PAID` and `DENIED` are **never overwritten** by later ACKs.



### No company data in source code

All company names, NPIs, addresses, phone numbers, and credentials must come from the **database** (set via API). The `seed_demo_data` management command uses only clearly synthetic placeholders.

---



## 6. Adding a new provider / company (no code change needed)

```bash
# 1. Create trading partner (ISA/GS sender-receiver)
POST /api/v1/trading-partners/
{
  "name": "ACME Transport LLC",
  "sender_id": "ACME123456",
  "receiver_id": "COMEDASSISTPROG",
  "environment": "TEST",
  "contact_name": "Billing Contact",
  "contact_phone": "3035550100"
}

# 2. Create provider billing profile
POST /api/v1/provider-billing-profiles/
{
  "legal_name": "ACME Transport LLC",
  "billing_name": "ACME Transport LLC",
  "npi": "1234567890",
  "tax_id": "123456789",
  "taxonomy_code": "343900000X",
  "is_atypical": false
}

# 3. Create patients, trips, claims via API as normal
```

---



## 7. Onboarding an atypical provider (no NPI)

```json
POST /api/v1/provider-billing-profiles/
{
  "legal_name": "Atypical Transport LLC",
  "is_atypical": true,
  "medicaid_provider_id": "CO12345678",
  "taxonomy_code": "343900000X"
}
```

The 837P generator will use `NM108=XX, NM109=CO12345678` (never invent an NPI). `REF*EI` is emitted only when a real `tax_id` exists.

---



## 8. Full billing workflow (API calls in order)

```
1. POST /api/v1/provider-billing-profiles/   # sync provider
2. POST /api/v1/patients/                    # sync patient
3. POST /api/v1/nemt-trips/                  # create trip
4. POST /api/v1/claims/                      # create claim (sets procedure code, diagnosis, POS)
5. POST /api/v1/claim-documents/upload/      # attach trip log / 25-mile docs (long-distance)
6. POST /api/v1/claims/{id}/validate/        # → {"ready": true/false, "errors": [...]}
7. POST /api/v1/submission-batches/          # create batch
8. POST /api/v1/submission-batches/{id}/add-claim/
9. POST /api/v1/edi-files/generate-837p/    # generates X12 file → claim → EDI_GENERATED
10. POST /api/v1/edi-files/{id}/queue-upload/ # SFTP upload → claim → EDI_SENT
11. POST /api/v1/edi-acknowledgements/import-999/ # process 999 → EDI_ACCEPTED or EDI_REJECTED
12. GET  /api/v1/claims/{id}/status/         # check current status
```

---



## 9. Running tests

```bash
# All 229 tests
docker compose exec backend python manage.py test apps

# Enterprise scenario tests only (atypical provider, ISA length, isolation, etc.)
docker compose exec backend python manage.py test apps.edi.tests_enterprise

# EDI 837P generation tests
docker compose exec backend python manage.py test apps.edi.tests

# Claim tests
docker compose exec backend python manage.py test apps.claim
```

---



## 10. Environment variables (important ones)

```env
# Required for all environments
DJANGO_SECRET_KEY=...
POSTGRES_HOST=...
POSTGRES_DB=edi
POSTGRES_USER=edi
POSTGRES_PASSWORD=...

# JWT signing
SIMPLE_JWT_SIGNING_KEY=...

# S3 / MinIO for document storage
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
AWS_STORAGE_BUCKET_NAME=...

# EDI envelope defaults (usually fine as-is)
EDI_ENVELOPE_ISA05=ZZ
EDI_ENVELOPE_ISA07=ZZ

# TEST or PRODUCTION — set per deployment, not per claim
# (claim environment is controlled by the trading partner)

# Attachments
ATTACHMENT_PRODUCTION_MODE=false     # set true only when live with HCPF
ATTACHMENT_MFT_ENABLED=false
```

Do **not** add `EDI_DEFAULT_BILLING_TAX_ID` — it was removed. Tax IDs are now stored per provider via the API.

---



## 11. Migrations — important rules

- **Never** run or apply migrations without first reviewing the migration file.
- **Never** drop or rename columns without a data migration plan.
- Always check `python manage.py showmigrations` before `migrate`.
- Migrations are numbered sequentially per app — do not reorder them.

Current latest migrations:

- `claim/0007_claim_status_edi_generated_and_rejected`
- `patient/0003_patient_dob_nullable`
- `provider_billing_profile/0003_provider_is_atypical_and_tax_id`

---



## 12. Remaining work (DevOps / Client)


| Task                                        | Owner          |
| ------------------------------------------- | -------------- |
| Deploy TEST API URL (public HTTPS)          | DevOps         |
| Configure SFTP credentials for TEST HCPF    | DevOps         |
| Live 837P TEST → confirm 999 + Edifecs      | DevOps / HCPF  |
| Wire RedArt backend to this EDI API         | Client (Wahab) |
| Map RedArt bill/trip fields to EDI payloads | Client         |
| Display validation/status in RedArt UI      | Client         |
| Confirm HCPF attachment channel             | Client / HCPF  |


