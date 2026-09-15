"""
Seed WALLA billing-prep patients + trip legs into an existing DB.

Optionally creates one claim per paper with A0120 and S0215 service lines.
Does NOT create batches or 837P files.
Does NOT create/update provider or trading partner (reuse existing rows).

Usage (on the target host that already has the DB):

  python manage.py seed_walla_billing_draft \\
    --data-file /path/to/walla_billing_seed.json \\
    --provider-id 2 \\
    --dry-run

  python manage.py seed_walla_billing_draft \\
    --data-file /path/to/walla_billing_seed.json \\
    --provider-id 2

Idempotent:
  - patients: get_or_create by medicaid_member_id
  - trips: skip if same patient + service_date + pickup + dropoff + provider exists
  - claims: one deterministic external_id per paper; matching claims are unchanged

Never commit real PHI seed files. Keep them outside git (see local_data/).
"""

from __future__ import annotations

import json
from collections import defaultdict
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.claim.choices import ClaimStatus
from apps.claim.models import Claim
from apps.claim.utils.service import create_claim_from_trip, validate_claim_for_edi
from apps.nemt_trip.models import NemtTrip
from apps.patient.models import Patient
from apps.provider_billing_profile.models import ProviderBillingProfile

MONEY_PLACES = Decimal("0.01")


class Command(BaseCommand):
    help = (
        "Seed WALLA patients/trips and optionally authorized draft claims. "
        "Never creates a batch or 837P."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--data-file",
            required=True,
            help="Path to walla_billing_seed.json (patients + trips).",
        )
        parser.add_argument(
            "--provider-id",
            type=int,
            default=2,
            help="Existing ProviderBillingProfile id (default: 2).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Parse and report counts without writing.",
        )
        parser.add_argument(
            "--update-patients",
            action="store_true",
            help="Update existing patient demographics when medicaid_member_id matches.",
        )
        parser.add_argument(
            "--create-claims",
            action="store_true",
            help="Create one claim per paper and validate it for EDI readiness.",
        )
        parser.add_argument(
            "--billing-authorized",
            action="store_true",
            help="Confirm the company authorized the supplied rates and unit rule.",
        )
        parser.add_argument(
            "--base-rate",
            help="Authorized A0120 rate per one-way trip (required with --create-claims).",
        )
        parser.add_argument(
            "--mileage-rate",
            help="Authorized S0215 rate per whole mile (required with --create-claims).",
        )

    def handle(self, *args, **options):
        data_path = Path(options["data_file"]).expanduser()
        if not data_path.is_file():
            raise CommandError(f"Data file not found: {data_path}")

        provider = ProviderBillingProfile.objects.filter(
            pk=options["provider_id"], is_active=True
        ).first()
        if provider is None:
            raise CommandError(
                f"Active ProviderBillingProfile id={options['provider_id']} not found."
            )

        payload = json.loads(data_path.read_text(encoding="utf-8"))
        patients_data = payload.get("patients") or []
        trips_data = payload.get("nemt_trips") or []
        if not patients_data or not trips_data:
            raise CommandError("JSON must include non-empty patients and nemt_trips.")

        claim_rates = self._claim_rates(options)
        paper_groups = self._group_papers(trips_data) if options["create_claims"] else {}

        self.stdout.write(
            f"Provider id={provider.id} npi={provider.npi} "
            f"name={provider.legal_name or provider.billing_name}"
        )
        self.stdout.write(
            f"Patients in file: {len(patients_data)} | "
            f"Trip legs in file: {len(trips_data)}"
        )
        if options["create_claims"]:
            self.stdout.write(
                f"Claims requested: {len(paper_groups)} | "
                f"A0120={claim_rates['base_rate']} | "
                f"S0215={claim_rates['mileage_rate']}"
            )
        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("Dry run — no DB writes."))
            return

        with transaction.atomic():
            patient_map, created_p, updated_p, skipped_p = self._seed_patients(
                patients_data, update_existing=options["update_patients"]
            )
            created_t, skipped_t = self._seed_trips(
                trips_data, patient_map=patient_map, provider=provider
            )
            claim_result = None
            if options["create_claims"]:
                claim_result = self._seed_claims(
                    paper_groups,
                    patient_map=patient_map,
                    provider=provider,
                    **claim_rates,
                )

        self.stdout.write(
            self.style.SUCCESS(
                f"Patients created={created_p} updated={updated_p} "
                f"unchanged={skipped_p} | Trips created={created_t} skipped={skipped_t}"
            )
        )
        if claim_result:
            created_c, unchanged_c, ready_c, failed_c = claim_result
            self.stdout.write(
                self.style.SUCCESS(
                    f"Claims created={created_c} unchanged={unchanged_c} | "
                    f"base validation passed={ready_c} failed={failed_c}"
                )
            )
        else:
            self.stdout.write(
                self.style.WARNING(
                    "No claims created. Trip charge/mileage_units remain as supplied."
                )
            )
        self.stdout.write(
            self.style.WARNING(
                "No submission batch or EDI file was created. "
                "Review validation results before any production release."
            )
        )
        if (provider.medicaid_provider_id or "").strip() == (
            provider.taxonomy_code or ""
        ).strip() and (provider.taxonomy_code or "").strip():
            self.stdout.write(
                self.style.WARNING(
                    "Provider medicaid_provider_id currently matches taxonomy_code "
                    f"({provider.taxonomy_code}). Confirm that is intentional before 837P."
                )
            )
        if not (provider.tax_id or "").strip():
            self.stdout.write(
                self.style.WARNING(
                    "Provider tax_id is empty. 837P REF*EI usually needs EIN/TIN."
                )
            )

    def _seed_patients(self, patients_data, *, update_existing):
        patient_map = {}
        created = updated = skipped = 0

        for row in patients_data:
            medicaid_id = (row.get("medicaid_member_id") or "").strip().upper()
            if not medicaid_id:
                raise CommandError("Patient row missing medicaid_member_id.")

            defaults = {
                "first_name": (row.get("first_name") or "").strip(),
                "last_name": (row.get("last_name") or "").strip(),
                "date_of_birth": row.get("date_of_birth") or None,
                "gender": (row.get("gender") or None),
                "address_line_1": row.get("address_line_1") or None,
                "address_line_2": row.get("address_line_2") or None,
                "city": row.get("city") or None,
                "state": (row.get("state") or None),
                "zip": row.get("zip") or None,
                "county": row.get("county") or None,
                "is_active": bool(row.get("is_active", True)),
            }
            if defaults["state"]:
                defaults["state"] = str(defaults["state"]).strip().upper()
            if defaults["gender"]:
                defaults["gender"] = str(defaults["gender"]).strip().upper()

            existing = Patient.objects.filter(
                medicaid_member_id__iexact=medicaid_id
            ).first()
            if existing is None:
                patient = Patient.objects.create(
                    medicaid_member_id=medicaid_id, **defaults
                )
                created += 1
            else:
                patient = existing
                if update_existing:
                    for key, value in defaults.items():
                        setattr(patient, key, value)
                    patient.medicaid_member_id = medicaid_id
                    patient.save()
                    updated += 1
                else:
                    skipped += 1

            patient_map[medicaid_id] = patient

        return patient_map, created, updated, skipped

    def _seed_trips(self, trips_data, *, patient_map, provider):
        created = skipped = 0

        for row in trips_data:
            medicaid_id = (row.get("patient_medicaid_member_id") or "").strip().upper()
            patient = patient_map.get(medicaid_id)
            if patient is None:
                raise CommandError(
                    f"Trip references unknown medicaid_member_id={medicaid_id}. "
                    "Ensure patients section includes this member."
                )

            service_date = row.get("service_date")
            pickup = (row.get("pickup") or "").strip() or None
            dropoff = (row.get("dropoff") or "").strip() or None
            if not service_date or not pickup or not dropoff:
                raise CommandError(
                    f"Trip for {medicaid_id} missing service_date/pickup/dropoff."
                )

            exists = NemtTrip.objects.filter(
                patient=patient,
                provider=provider,
                service_date=service_date,
                pickup=pickup,
                dropoff=dropoff,
                is_active=True,
            ).exists()
            if exists:
                skipped += 1
                continue

            miles = self._decimal_or_none(row.get("one_way_miles"))
            charge = self._decimal_or_none(row.get("charge"))
            units = row.get("mileage_units")
            if units in ("", None):
                units = None

            NemtTrip.objects.create(
                patient=patient,
                provider=provider,
                service_date=service_date,
                pickup=pickup,
                dropoff=dropoff,
                one_way_miles=miles,
                mileage_units=units,
                driver_first_name=(row.get("driver_first_name") or None),
                driver_last_name=(row.get("driver_last_name") or None),
                charge=charge,
                is_active=bool(row.get("is_active", True)),
            )
            created += 1

        return created, skipped

    def _seed_claims(
        self,
        paper_groups,
        *,
        patient_map,
        provider,
        base_rate,
        mileage_rate,
    ):
        created = unchanged = ready = failed = 0

        for paper, rows in sorted(paper_groups.items()):
            first = rows[0]
            medicaid_id = first["patient_medicaid_member_id"].strip().upper()
            patient = patient_map[medicaid_id]
            anchor = self._find_trip(first, patient=patient, provider=provider)

            reviewed_miles = sum(
                (self._decimal_or_none(row.get("one_way_miles")) or Decimal("0"))
                for row in rows
            )
            mileage_units = int(reviewed_miles.to_integral_value(rounding=ROUND_DOWN))
            base_units = len(rows)
            base_charge = (base_rate * base_units).quantize(MONEY_PLACES)
            mileage_charge = (mileage_rate * mileage_units).quantize(MONEY_PLACES)
            total_charge = base_charge + mileage_charge
            external_id = f"WALLA-PAPER-{paper}"
            claim_number = f"WALLA-{first['service_date'].replace('-', '')}-P{paper:02d}"

            existing = Claim.objects.filter(
                external_id=external_id, is_active=True
            ).first()
            if existing is not None:
                self._assert_existing_claim_matches(
                    existing,
                    anchor=anchor,
                    claim_number=claim_number,
                    total_charge=total_charge,
                    base_units=base_units,
                    base_charge=base_charge,
                    mileage_units=mileage_units,
                    reviewed_miles=reviewed_miles,
                    mileage_charge=mileage_charge,
                )
                claim = existing
                unchanged += 1
            else:
                anchor.mileage_units = mileage_units
                anchor.charge = total_charge
                anchor.save(update_fields=["mileage_units", "charge", "updated_at"])
                claim, _ = create_claim_from_trip(
                    trip_id=anchor.id,
                    claim_number=claim_number,
                    external_id=external_id,
                    diagnosis_code="R68.89",
                    place_of_service="41",
                    create_service_line=False,
                    service_lines=[
                        {
                            "procedure_code": "A0120",
                            "units": base_units,
                            "charge": base_charge,
                        },
                        {
                            "procedure_code": "S0215",
                            "units": mileage_units,
                            "mileage": reviewed_miles,
                            "charge": mileage_charge,
                        },
                    ],
                )
                if claim.status == ClaimStatus.READY_FOR_837P:
                    claim.status = ClaimStatus.DRAFT
                    claim.save(update_fields=["status", "updated_at"])
                created += 1

            # This command prepares draft billing data only. The generic validator
            # does not yet represent service-line modifiers or a separate pay-to
            # address, so it must not promote these claims to READY_FOR_837P.
            result = validate_claim_for_edi(claim, update_status=False)
            if result["ready"]:
                ready += 1
            else:
                failed += 1
                self.stderr.write(
                    self.style.ERROR(
                        f"Paper {paper} claim {claim.id} validation failed: "
                        + "; ".join(result["errors"])
                    )
                )

        return created, unchanged, ready, failed

    def _group_papers(self, trips_data):
        grouped = defaultdict(list)
        for row in trips_data:
            paper = row.get("paper")
            leg = row.get("leg")
            if paper in (None, "") or leg in (None, ""):
                raise CommandError(
                    "Every trip needs paper and leg metadata when --create-claims is used."
                )
            grouped[int(paper)].append(row)

        for paper, rows in grouped.items():
            rows.sort(key=lambda row: int(row["leg"]))
            member_ids = {
                (row.get("patient_medicaid_member_id") or "").strip().upper()
                for row in rows
            }
            service_dates = {row.get("service_date") for row in rows}
            if len(member_ids) != 1 or len(service_dates) != 1:
                raise CommandError(
                    f"Paper {paper} must contain one member and one service date."
                )
            legs = [int(row["leg"]) for row in rows]
            if legs != list(range(1, len(rows) + 1)):
                raise CommandError(f"Paper {paper} has invalid leg sequence: {legs}.")
        return grouped

    def _find_trip(self, row, *, patient, provider):
        trip = NemtTrip.objects.filter(
            patient=patient,
            provider=provider,
            service_date=row["service_date"],
            pickup=(row.get("pickup") or "").strip(),
            dropoff=(row.get("dropoff") or "").strip(),
            is_active=True,
        ).first()
        if trip is None:
            raise CommandError(
                f"Seeded trip not found for paper {row['paper']} leg {row['leg']}."
            )
        return trip

    def _assert_existing_claim_matches(
        self,
        claim,
        *,
        anchor,
        claim_number,
        total_charge,
        base_units,
        base_charge,
        mileage_units,
        reviewed_miles,
        mileage_charge,
    ):
        if (
            claim.trip_id != anchor.id
            or claim.claim_number != claim_number
            or claim.total_charge != total_charge
            or claim.diagnosis_code != "R68.89"
            or claim.place_of_service != "41"
        ):
            raise CommandError(
                f"Existing claim {claim.id} for {claim.external_id} does not match "
                "the authorized seed calculation; review it manually."
            )

        lines = list(claim.service_lines.filter(is_active=True).order_by("id"))
        expected = [
            ("A0120", base_units, None, base_charge),
            ("S0215", mileage_units, reviewed_miles, mileage_charge),
        ]
        actual = [
            (line.procedure_code, line.units, line.mileage, line.charge)
            for line in lines
        ]
        if actual != expected:
            raise CommandError(
                f"Existing claim {claim.id} service lines do not match the "
                "authorized seed calculation; review them manually."
            )

    def _claim_rates(self, options):
        if not options["create_claims"]:
            return {}
        if not options["billing_authorized"]:
            raise CommandError(
                "--billing-authorized is required with --create-claims."
            )
        base_rate = self._positive_decimal(options.get("base_rate"), "--base-rate")
        mileage_rate = self._positive_decimal(
            options.get("mileage_rate"), "--mileage-rate"
        )
        return {"base_rate": base_rate, "mileage_rate": mileage_rate}

    def _positive_decimal(self, value, label):
        parsed = self._decimal_or_none(value)
        if parsed is None or parsed <= 0:
            raise CommandError(f"{label} must be a positive decimal.")
        return parsed.quantize(MONEY_PLACES)

    @staticmethod
    def _decimal_or_none(value):
        if value in (None, ""):
            return None
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise CommandError(f"Invalid decimal value: {value!r}") from exc
