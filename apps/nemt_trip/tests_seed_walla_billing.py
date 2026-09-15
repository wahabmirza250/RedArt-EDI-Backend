import json
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from apps.claim.models import Claim
from apps.nemt_trip.models import NemtTrip
from apps.patient.models import Patient
from apps.provider_billing_profile.models import ProviderBillingProfile


class SeedWallaBillingTests(TestCase):
    def setUp(self):
        self.provider = ProviderBillingProfile.objects.create(
            legal_name="SAMPLE TRANSPORT",
            billing_name="SAMPLE TRANSPORT",
            npi="1999999999",
            tax_id="123456789",
            taxonomy_code="343900000X",
            location_id="SAMPLELOCATION",
            address_line_1="100 SAMPLE ST",
            city="DENVER",
            state="CO",
            zip="80000",
            is_active=True,
        )
        self.temp_dir = TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.data_file = Path(self.temp_dir.name) / "seed.json"
        self.data_file.write_text(json.dumps(self._payload()), encoding="utf-8")

    def _payload(self):
        patient = {
            "first_name": "SAMPLE",
            "last_name": "MEMBER",
            "date_of_birth": "1990-01-01",
            "gender": "F",
            "medicaid_member_id": "SMPLMEMBER001",
            "address_line_1": "200 SAMPLE AVE",
            "city": "DENVER",
            "state": "CO",
            "zip": "80001",
            "is_active": True,
        }
        common = {
            "patient_medicaid_member_id": "SMPLMEMBER001",
            "service_date": "2026-08-25",
            "driver_first_name": "SAMPLE",
            "driver_last_name": "DRIVER",
            "charge": None,
            "mileage_units": None,
            "is_active": True,
            "paper": 1,
        }
        return {
            "patients": [patient],
            "nemt_trips": [
                {
                    **common,
                    "leg": 1,
                    "pickup": "200 SAMPLE AVE",
                    "dropoff": "300 SAMPLE RD",
                    "one_way_miles": "1.50",
                },
                {
                    **common,
                    "leg": 2,
                    "pickup": "300 SAMPLE RD",
                    "dropoff": "200 SAMPLE AVE",
                    "one_way_miles": "1.75",
                },
            ],
        }

    def _seed(self):
        call_command(
            "seed_walla_billing_draft",
            data_file=str(self.data_file),
            provider_id=self.provider.id,
            create_claims=True,
            billing_authorized=True,
            base_rate="12.15",
            mileage_rate="2.74",
        )

    def test_creates_combined_claim_and_is_idempotent(self):
        self._seed()

        self.assertEqual(Patient.objects.count(), 1)
        self.assertEqual(NemtTrip.objects.count(), 2)
        self.assertEqual(Claim.objects.count(), 1)

        claim = Claim.objects.get()
        self.assertEqual(claim.claim_number, "WALLA-20260825-P01")
        self.assertEqual(claim.total_charge, Decimal("32.52"))
        self.assertEqual(claim.status, "DRAFT")

        lines = {
            line.procedure_code: line
            for line in claim.service_lines.filter(is_active=True)
        }
        self.assertEqual(lines["A0120"].units, 2)
        self.assertEqual(lines["A0120"].charge, Decimal("24.30"))
        self.assertEqual(lines["A0120"].modifier_1, "76")
        self.assertEqual(lines["S0215"].units, 3)
        self.assertEqual(lines["S0215"].mileage, Decimal("3.25"))
        self.assertEqual(lines["S0215"].charge, Decimal("8.22"))
        self.assertIsNone(lines["S0215"].modifier_1)

        self._seed()
        self.assertEqual(Patient.objects.count(), 1)
        self.assertEqual(NemtTrip.objects.count(), 2)
        self.assertEqual(Claim.objects.count(), 1)
        self.assertEqual(claim.service_lines.filter(is_active=True).count(), 2)
        claim.refresh_from_db()
        self.assertEqual(
            claim.service_lines.get(procedure_code="A0120").modifier_1, "76"
        )

    def test_claim_creation_requires_explicit_authorization(self):
        with self.assertRaisesMessage(CommandError, "--billing-authorized"):
            call_command(
                "seed_walla_billing_draft",
                data_file=str(self.data_file),
                provider_id=self.provider.id,
                create_claims=True,
                base_rate="12.15",
                mileage_rate="2.74",
            )

        self.assertFalse(Patient.objects.exists())
        self.assertFalse(NemtTrip.objects.exists())
        self.assertFalse(Claim.objects.exists())

    def test_existing_claim_without_modifier_is_backfilled(self):
        self._seed()
        claim = Claim.objects.get()
        line = claim.service_lines.get(procedure_code="A0120")
        line.modifier_1 = None
        line.save(update_fields=["modifier_1", "updated_at"])

        self._seed()
        line.refresh_from_db()
        self.assertEqual(line.modifier_1, "76")
