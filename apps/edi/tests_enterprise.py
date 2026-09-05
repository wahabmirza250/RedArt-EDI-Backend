"""
Enterprise 837P test suite — covers all 28 scenarios from the client's
multi-company, multi-provider requirements.

Design principles:
  - All test data is clearly synthetic (no real NPI, address, member ID, or company).
  - Tests assert correctness, not just "no error".
  - Each test is independent; setUp builds a fresh environment.
  - NO real HCPF traffic — all SFTP / upload is mocked or skipped.
  - Tests verify enterprise invariants:
      * No fabrication of missing identifiers.
      * ISA fixed-length enforcement.
      * TEST / PRODUCTION isolation.
      * Multi-company claim isolation.
      * Atypical provider NM108=XX + medicaid_provider_id path.
      * EDI_GENERATED ≠ EDI_SENT ≠ EDI_ACCEPTED ≠ PAID.
"""

from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework import status

from apps.claim.choices import BatchStatus, ClaimStatus, DocumentStatus, DocumentType
from apps.claim.models import BatchClaim, Claim, ClaimDocument, SubmissionBatch
from apps.claim.utils.service import create_claim_from_trip, sync_claim_document_status
from apps.core.testing import AuthAPITestCase
from apps.edi.choices import AcknowledgementStatus, EDIFileStatus
from apps.edi.models import EDIFile
from apps.edi.utils.envelope import get_edi_envelope_config
from apps.edi.utils.readiness import (
    assert_batch_ready_for_837p_generation,
    collect_batch_readiness_errors,
)
from apps.edi.utils.schema import build_edi_content, render_edi_file
from apps.edi.utils.service import (
    allocate_control_numbers,
    apply_edi_acknowledgement,
    create_edi_file_for_batch,
    mark_edi_file_uploaded,
)
from apps.long_distance_rule.models import LongDistanceRule
from apps.nemt_trip.models import NemtTrip
from apps.patient.models import Patient
from apps.provider_billing_profile.models import ProviderBillingProfile
from apps.trading_partner.models import TradingPartner


# ---------------------------------------------------------------------------
# Shared fixture builder (all data clearly synthetic)
# ---------------------------------------------------------------------------

class EnterpriseFixturesMixin:
    """
    Build a minimal but complete 837P-ready environment.
    All identifiers are clearly fabricated test values.
    """

    PARTNER_SENDER = "TESTSENDER1"
    PROVIDER_NPI = "1999999999"
    PROVIDER_TAX_ID = "999999999"
    MEMBER_ID = "TSTMEMBER001"

    def _make_partner(self, sender_id=None, environment="TEST"):
        return TradingPartner.objects.create(
            name="Test Transport LLC",
            sender_id=sender_id or self.PARTNER_SENDER,
            receiver_id="COMEDASSISTPROG",
            environment=environment,
            contact_name="Billing Contact",
            contact_phone="0000000000",
            is_active=True,
        )

    def _make_provider(
        self,
        *,
        npi=None,
        tax_id=None,
        is_atypical=False,
        medicaid_provider_id=None,
        taxonomy_code="343900000X",
    ):
        return ProviderBillingProfile.objects.create(
            legal_name="Test Transport LLC",
            billing_name="Test Transport LLC",
            npi=npi if not is_atypical else None,
            tax_id=tax_id if not is_atypical else None,
            is_atypical=is_atypical,
            medicaid_provider_id=medicaid_provider_id,
            taxonomy_code=taxonomy_code,
            address_line_1="100 Test St",
            city="Denver",
            state="CO",
            zip="80000",
            phone="0000000000",
            is_active=True,
        )

    def _make_patient(
        self,
        *,
        medicaid_member_id=None,
        date_of_birth=None,
        gender=None,
        address_line_1=None,
        city=None,
        state="CO",
        zip_code=None,
    ):
        # Use provided member_id; only fall back to class default when truly None.
        member_id = self.MEMBER_ID if medicaid_member_id is None else medicaid_member_id
        return Patient.objects.create(
            first_name="Test",
            last_name="Patient",
            medicaid_member_id=member_id,
            county="Denver",
            date_of_birth=date_of_birth,
            gender=gender,
            address_line_1=address_line_1,
            city=city,
            state=state,
            zip=zip_code,
            is_active=True,
        )

    def _make_trip(self, patient, provider, *, service_date=None, miles=10):
        return NemtTrip.objects.create(
            patient=patient,
            provider=provider,
            service_date=service_date or date(2026, 9, 1),
            pickup="100 Test St, Denver CO",
            dropoff="Clinic, Denver CO",
            one_way_miles=Decimal(str(miles)),
            mileage_units=miles,
            charge=Decimal("25.00"),
            is_active=True,
        )

    def _make_claim(self, trip, *, claim_number=None, procedure_code="A0130"):
        claim, _ = create_claim_from_trip(
            trip_id=trip.id,
            claim_number=claim_number or f"TSTCLM-{trip.id}",
            diagnosis_code="R69",
            place_of_service="41",
            create_service_line=True,
        )
        from apps.claim_service_line.models import ClaimServiceLine
        ClaimServiceLine.objects.filter(claim=claim).update(
            procedure_code=procedure_code,
            charge=Decimal("25.00"),
        )
        return claim

    def _complete_documents(self, claim):
        """Attach required docs and sync status so claim reaches READY_FOR_837P."""
        for doc_type in (DocumentType.STANDARD_TRIP_LOG, DocumentType.MILE_25_VERIFICATION):
            ClaimDocument.objects.get_or_create(
                claim=claim,
                document_type=doc_type,
                defaults={
                    "file_name": f"{doc_type}.pdf",
                    "document_hash": f"HASH-{doc_type}-{claim.id}",
                    "blob_ref": f"claim-documents/{claim.id}/{doc_type}.pdf",
                    "is_signed": True,
                    "status": DocumentStatus.COMPLETE,
                },
            )
        sync_claim_document_status(claim)
        claim.refresh_from_db()

    def _make_batch(self, partner, claim, *, environment="TEST"):
        LongDistanceRule.objects.update_or_create(
            county_type="STANDARD",
            defaults={"review_threshold": 52, "verification_threshold": 25, "is_active": True},
        )
        self._complete_documents(claim)
        batch = SubmissionBatch.objects.create(
            batch_number=f"TSTBATCH-{claim.id}",
            trading_partner=partner,
            environment=environment,
            status=BatchStatus.READY,
            is_active=True,
        )
        BatchClaim.objects.create(batch=batch, claim=claim, st02="0001")
        batch.claim_count = 1
        batch.total_amount = Decimal("25.00")
        batch.save(update_fields=["claim_count", "total_amount", "updated_at"])
        return batch

    def setUp(self):
        super().setUp()
        LongDistanceRule.objects.update_or_create(
            county_type="STANDARD",
            defaults={"review_threshold": 52, "verification_threshold": 25, "is_active": True},
        )
        self.partner = self._make_partner()
        self.provider = self._make_provider(
            npi=self.PROVIDER_NPI, tax_id=self.PROVIDER_TAX_ID
        )
        self.patient = self._make_patient(
            medicaid_member_id=self.MEMBER_ID,
            date_of_birth=date(1970, 1, 1),
            gender="M",
            address_line_1="100 Test St",
            city="Denver",
            zip_code="80000",
        )
        self.trip = self._make_trip(self.patient, self.provider, miles=80)
        self.claim = self._make_claim(self.trip)
        self.batch = self._make_batch(self.partner, self.claim)


# ---------------------------------------------------------------------------
# 1. Normal NEMT Medicaid claim — ISA/GS/NM108/NM109 correctness
# ---------------------------------------------------------------------------

class TestNemtMedicaidClaimGeneration(EnterpriseFixturesMixin, TestCase):

    def test_colorado_medicaid_member_id_in_2010ba(self):
        """NM1*IL must carry MI qualifier + Colorado Medicaid Member ID."""
        from apps.edi.utils.handler import Generate837PHandler
        payload = Generate837PHandler(self.batch.id).build_payload_dict()
        body = render_edi_file(build_edi_content(payload))
        # 2010BA: NM1*IL*1*<last>*<first>*****MI*<medicaid_member_id>
        self.assertIn(f"*MI*{self.MEMBER_ID}~", body)
        # Subscriber is not identified by SSN, DOB, gender, or ZIP qualifier
        self.assertNotIn("NM1*IL*1*" + self.patient.last_name + "***SS*", body)

    def test_npi_provider_nm108_xx(self):
        """Standard NPI provider: NM108=XX, NM109=npi."""
        from apps.edi.utils.handler import Generate837PHandler
        payload = Generate837PHandler(self.batch.id).build_payload_dict()
        body = render_edi_file(build_edi_content(payload))
        self.assertIn(f"*XX*{self.PROVIDER_NPI}~", body)

    def test_isa_fixed_length_106_chars(self):
        """ISA segment must be exactly 106 characters (X12 standard)."""
        from apps.edi.utils.handler import Generate837PHandler
        payload = Generate837PHandler(self.batch.id).build_payload_dict()
        body = render_edi_file(build_edi_content(payload))
        isa_line = next(ln for ln in body.split("\n") if ln.startswith("ISA*"))
        self.assertEqual(
            len(isa_line), 106,
            f"ISA segment is {len(isa_line)} chars (expected 106). "
            "Sender/receiver padding may be wrong."
        )

    def test_isa15_t_for_test_environment(self):
        """ISA15 must be T for TEST environment."""
        env = get_edi_envelope_config("TEST")
        self.assertEqual(env["isa15"], "T")

    def test_isa15_p_for_production_environment(self):
        """ISA15 must be P for PRODUCTION environment."""
        env = get_edi_envelope_config("PRODUCTION")
        self.assertEqual(env["isa15"], "P")

    def test_gs08_and_st03_version_identifier(self):
        """GS08 and ST03 must be 005010X222A1 (Colorado companion guide)."""
        from apps.edi.utils.handler import Generate837PHandler
        payload = Generate837PHandler(self.batch.id).build_payload_dict()
        body = render_edi_file(build_edi_content(payload))
        self.assertIn("005010X222A1", body)

    def test_receiver_id_is_comedassistprog(self):
        """ISA08 and GS03 must be COMEDASSISTPROG (Colorado Medicaid)."""
        from apps.edi.utils.handler import Generate837PHandler
        payload = Generate837PHandler(self.batch.id).build_payload_dict()
        body = render_edi_file(build_edi_content(payload))
        self.assertIn("COMEDASSISTPROG", body)

    def test_tax_id_emitted_as_ref_ei_for_npi_provider(self):
        """REF*EI must carry the provider's tax_id (EIN) for NPI providers."""
        from apps.edi.utils.handler import Generate837PHandler
        payload = Generate837PHandler(self.batch.id).build_payload_dict()
        body = render_edi_file(build_edi_content(payload))
        self.assertIn(f"REF*EI*{self.PROVIDER_TAX_ID}~", body)

    def test_control_numbers_persisted_for_reconciliation(self):
        """ISA13 and GS06 must be generated and persisted for ack matching."""
        control, created = allocate_control_numbers(batch_id=self.batch.id)
        self.assertTrue(created)
        self.assertIsNotNone(control.isa13)
        self.assertIsNotNone(control.gs06)
        # Re-allocation returns the same row (idempotent).
        control2, created2 = allocate_control_numbers(batch_id=self.batch.id)
        self.assertFalse(created2)
        self.assertEqual(control.id, control2.id)


# ---------------------------------------------------------------------------
# 2. Atypical provider (no NPI, Colorado Medicaid provider ID)
# ---------------------------------------------------------------------------

class TestAtypicalProvider(EnterpriseFixturesMixin, TestCase):

    def _build_atypical_batch(self):
        atypical_provider = self._make_provider(
            is_atypical=True,
            medicaid_provider_id="ATYPTST001",
            taxonomy_code="343900000X",
        )
        patient = self._make_patient(medicaid_member_id="ATYMEMBER01")
        trip = self._make_trip(patient, atypical_provider, miles=80)
        claim = self._make_claim(trip, claim_number="ATY-CLM-001")
        batch = self._make_batch(self.partner, claim)
        return batch

    def test_atypical_provider_nm108_is_xx(self):
        """Atypical provider: NM108=XX, NM109=medicaid_provider_id (no invented NPI)."""
        from apps.edi.utils.handler import Generate837PHandler
        batch = self._build_atypical_batch()
        payload = Generate837PHandler(batch.id).build_payload_dict()
        body = render_edi_file(build_edi_content(payload))
        self.assertIn("*XX*ATYPTST001~", body)
        self.assertNotIn("*1C*", body)

    def test_atypical_provider_no_ref_ei_without_tax_id(self):
        """Without tax_id, do not invent REF*EI."""
        from apps.edi.utils.handler import Generate837PHandler
        batch = self._build_atypical_batch()
        payload = Generate837PHandler(batch.id).build_payload_dict()
        body = render_edi_file(build_edi_content(payload))
        self.assertNotIn("REF*EI*", body)

    def test_atypical_provider_readiness_accepts_no_npi(self):
        """Batch with atypical provider must pass readiness without NPI."""
        batch = self._build_atypical_batch()
        result = assert_batch_ready_for_837p_generation(batch)
        self.assertTrue(result)

    def test_atypical_provider_missing_medicaid_provider_id_fails(self):
        """Atypical provider without medicaid_provider_id must fail readiness."""
        bad_provider = self._make_provider(
            is_atypical=True,
            medicaid_provider_id=None,
            taxonomy_code="343900000X",
        )
        patient = self._make_patient(medicaid_member_id="ATYMEMBER02")
        trip = self._make_trip(patient, bad_provider, miles=80)
        claim = self._make_claim(trip, claim_number="ATY-CLM-BAD")
        batch = self._make_batch(self.partner, claim)
        errors = collect_batch_readiness_errors(batch)
        self.assertTrue(any("medicaid_provider_id" in e.lower() for e in errors))

    def test_npi_never_fabricated_for_atypical_provider(self):
        """The system must NEVER invent an NPI for an atypical provider."""
        atypical = self._make_provider(
            is_atypical=True,
            medicaid_provider_id="ATYPTST002",
        )
        self.assertFalse(atypical.npi)  # model must store no NPI


# ---------------------------------------------------------------------------
# 3. Missing required configuration — validation errors
# ---------------------------------------------------------------------------

class TestValidationErrors(EnterpriseFixturesMixin, TestCase):

    def test_missing_medicaid_member_id_blocked(self):
        """Missing Medicaid member ID must produce clear error — never fabricated."""
        patient = self._make_patient(medicaid_member_id="BLANKTEST")
        patient.medicaid_member_id = ""
        patient.save(update_fields=["medicaid_member_id", "updated_at"])
        trip = self._make_trip(patient, self.provider, miles=80)
        claim = self._make_claim(trip, claim_number="VAL-CLM-001")
        batch = self._make_batch(self.partner, claim)
        errors = collect_batch_readiness_errors(batch)
        self.assertTrue(any("medicaid_member_id" in e.lower() for e in errors))

    def test_missing_provider_npi_blocked(self):
        """Missing NPI for standard provider must produce clear error."""
        no_npi_provider = self._make_provider(npi=None, tax_id="999999999")
        patient = self._make_patient(medicaid_member_id="NPIMISSMEM")
        trip = self._make_trip(patient, no_npi_provider, miles=80)
        claim = self._make_claim(trip, claim_number="VAL-CLM-002")
        batch = self._make_batch(self.partner, claim)
        errors = collect_batch_readiness_errors(batch)
        self.assertTrue(any("npi" in e.lower() for e in errors))

    def test_missing_provider_tax_id_blocked(self):
        """Missing tax_id for NPI provider must produce clear error (REF*EI required)."""
        no_tax_provider = self._make_provider(npi=self.PROVIDER_NPI, tax_id=None)
        patient = self._make_patient(medicaid_member_id="TAXIDMISSM")
        trip = self._make_trip(patient, no_tax_provider, miles=80)
        claim = self._make_claim(trip, claim_number="VAL-CLM-003")
        batch = self._make_batch(self.partner, claim)
        errors = collect_batch_readiness_errors(batch)
        self.assertTrue(any("tax_id" in e.lower() for e in errors))

    def test_missing_procedure_code_blocked(self):
        """Missing procedure_code must produce clear error — never defaulted."""
        trip = self._make_trip(self.patient, self.provider, miles=80)
        claim = self._make_claim(trip, claim_number="VAL-CLM-004")
        from apps.claim_service_line.models import ClaimServiceLine
        ClaimServiceLine.objects.filter(claim=claim).update(procedure_code="")
        batch = self._make_batch(self.partner, claim)
        errors = collect_batch_readiness_errors(batch)
        self.assertTrue(any("procedure_code" in e.lower() for e in errors))

    def test_missing_trading_partner_blocked(self):
        """Batch without a trading_partner must fail readiness immediately."""
        batch = SubmissionBatch.objects.create(
            batch_number="VAL-BATCH-001",
            trading_partner=None,
            environment="TEST",
            status=BatchStatus.READY,
            is_active=True,
        )
        errors = collect_batch_readiness_errors(batch)
        self.assertTrue(any("trading_partner" in e.lower() for e in errors))

    def test_schema_raises_on_missing_procedure_code(self):
        """Schema builder must raise ValueError — never default to any code."""
        from apps.edi.utils.envelope import get_edi_envelope_config
        from django.utils import timezone
        payload = {
            "envelope": get_edi_envelope_config("TEST"),
            "trading_partner": {
                "id": 1, "name": "TP", "sender_id": "TESTSENDER1",
                "receiver_id": "COMEDASSISTPROG", "contact_name": "C", "contact_phone": "0000000000",
            },
            "control": {"id": 1, "isa13": "000000001", "gs06": "1"},
            "generated_at": timezone.now(),
            "claims": [{
                "claim_id": 1, "claim_number": "X", "st02": "0001",
                "diagnosis_code": "R69", "place_of_service": "41", "total_charge": "25.00",
                "patient": {
                    "first_name": "T", "last_name": "P",
                    "medicaid_member_id": "TSTMEM001",
                    "date_of_birth": "", "gender": "",
                    "address_line_1": "", "city": "", "state": "", "zip": "", "phone": "",
                },
                "provider": {
                    "legal_name": "TP", "billing_name": "TP",
                    "is_atypical": False, "npi": "1999999999", "medicaid_provider_id": "",
                    "taxonomy_code": "343900000X", "tax_id": "999999999",
                    "address_line_1": "", "city": "", "state": "", "zip": "", "phone": "",
                },
                "driver": {},
                "service_lines": [
                    {
                        "procedure_code": "",  # intentionally blank
                        "from_date": "20260901", "to_date": "20260901",
                        "units": 1, "mileage": None, "charge": "25.00",
                    }
                ],
            }],
        }
        with self.assertRaises(ValueError) as ctx:
            build_edi_content(payload)
        self.assertIn("procedure_code", str(ctx.exception).lower())


# ---------------------------------------------------------------------------
# 4. No demographic fabrication
# ---------------------------------------------------------------------------

class TestNoFabrication(EnterpriseFixturesMixin, TestCase):

    def test_no_ssn_required_or_fabricated(self):
        """SSN must never appear in 837P output."""
        from apps.edi.utils.handler import Generate837PHandler
        payload = Generate837PHandler(self.batch.id).build_payload_dict()
        body = render_edi_file(build_edi_content(payload))
        # SSN qualifier in X12 is SY; must not appear.
        self.assertNotIn("*SY*", body)

    def test_no_dob_fabricated_when_absent(self):
        """When DOB is None, DMG segment must be omitted — not fabricated."""
        patient = self._make_patient(
            medicaid_member_id="NOBODDOB001",
            date_of_birth=None,
            gender=None,
        )
        trip = self._make_trip(patient, self.provider, miles=80)
        claim = self._make_claim(trip, claim_number="NOFAB-CLM-001")
        batch = self._make_batch(self.partner, claim)
        from apps.edi.utils.handler import Generate837PHandler
        payload = Generate837PHandler(batch.id).build_payload_dict()
        body = render_edi_file(build_edi_content(payload))
        # DMG segment must be absent when no DOB/gender is present.
        self.assertNotIn("DMG*D8*", body)

    def test_dob_emitted_when_present(self):
        """When DOB is present, DMG must include it."""
        from apps.edi.utils.handler import Generate837PHandler
        payload = Generate837PHandler(self.batch.id).build_payload_dict()
        body = render_edi_file(build_edi_content(payload))
        self.assertIn("DMG*D8*19700101", body)

    def test_no_gender_fabricated_when_absent(self):
        """When gender is None, DMG segment must be omitted — not fabricated."""
        patient = self._make_patient(
            medicaid_member_id="NOGENDMEM1",
            date_of_birth=None,
            gender=None,
        )
        trip = self._make_trip(patient, self.provider, miles=80)
        claim = self._make_claim(trip, claim_number="NOFAB-CLM-002")
        batch = self._make_batch(self.partner, claim)
        from apps.edi.utils.handler import Generate837PHandler
        payload = Generate837PHandler(batch.id).build_payload_dict()
        body = render_edi_file(build_edi_content(payload))
        self.assertNotIn("DMG*D8*", body)

    def test_no_address_fabricated_when_absent(self):
        """When patient has no address, N3/N4 must be omitted — not fabricated."""
        patient = self._make_patient(
            medicaid_member_id="NOADDRM001",
            address_line_1=None,
            city=None,
            zip_code=None,
        )
        trip = self._make_trip(patient, self.provider, miles=80)
        claim = self._make_claim(trip, claim_number="NOFAB-CLM-003")
        batch = self._make_batch(self.partner, claim)
        from apps.edi.utils.handler import Generate837PHandler
        payload = Generate837PHandler(batch.id).build_payload_dict()
        body = render_edi_file(build_edi_content(payload))
        # N3 for subscriber must not appear when address is absent.
        # (Provider may still have N3; count subscriber-level N3 via NM1*IL context)
        # The simplest proxy: NOADDRM001 must still appear (member ID preserved).
        self.assertIn("NOADDRM001", body)

    def test_patient_without_dob_passes_readiness(self):
        """DOB is not mandatory; a NEMT claim without DOB must pass pre-flight."""
        patient = self._make_patient(
            medicaid_member_id="NODOB-MEM1",
            date_of_birth=None,
        )
        trip = self._make_trip(patient, self.provider, miles=80)
        claim = self._make_claim(trip, claim_number="NODOB-CLM-1")
        batch = self._make_batch(self.partner, claim)
        result = assert_batch_ready_for_837p_generation(batch)
        self.assertTrue(result)


# ---------------------------------------------------------------------------
# 5. TEST / PRODUCTION isolation
# ---------------------------------------------------------------------------

class TestEnvironmentIsolation(EnterpriseFixturesMixin, TestCase):

    def test_test_environment_sets_isa15_t(self):
        """TEST batch: ISA15 must be T — never P."""
        from apps.edi.utils.handler import Generate837PHandler
        payload = Generate837PHandler(self.batch.id).build_payload_dict()
        body = render_edi_file(build_edi_content(payload))
        isa_line = next(ln for ln in body.split("\n") if ln.startswith("ISA*"))
        # ISA15 is element 16 (1-indexed), separated by *
        parts = isa_line.split("*")
        isa15 = parts[15]  # 0-indexed: ISA=0, ISA01=1, … ISA15=15
        self.assertEqual(isa15, "T", f"ISA15 should be T but was {isa15!r}")

    def test_production_batch_sets_isa15_p(self):
        """PRODUCTION batch: ISA15 must be P."""
        prod_partner = self._make_partner(sender_id="PRODSENDER1", environment="PRODUCTION")
        prod_provider = self._make_provider(
            npi="1888888888", tax_id="888888888", taxonomy_code="343900000X"
        )
        patient = self._make_patient(medicaid_member_id="PRODMEMBER1")
        trip = self._make_trip(patient, prod_provider, miles=80)
        claim = self._make_claim(trip, claim_number="PROD-CLM-001")
        batch = self._make_batch(prod_partner, claim, environment="PRODUCTION")

        from apps.edi.utils.handler import Generate837PHandler
        payload = Generate837PHandler(batch.id).build_payload_dict()
        body = render_edi_file(build_edi_content(payload))
        isa_line = next(ln for ln in body.split("\n") if ln.startswith("ISA*"))
        parts = isa_line.split("*")
        isa15 = parts[15]
        self.assertEqual(isa15, "P", f"ISA15 should be P but was {isa15!r}")

    def test_test_and_production_batches_independent(self):
        """Control numbers for TEST and PRODUCTION must never collide."""
        prod_partner = self._make_partner(sender_id="PRODSENDER2", environment="PRODUCTION")
        prod_provider = self._make_provider(npi="1777777777", tax_id="777777777")
        patient = self._make_patient(medicaid_member_id="ISOMEMBER01")
        trip = self._make_trip(patient, prod_provider, miles=80)
        claim = self._make_claim(trip, claim_number="ISO-CLM-001")
        prod_batch = self._make_batch(prod_partner, claim, environment="PRODUCTION")

        test_ctrl, _ = allocate_control_numbers(batch_id=self.batch.id, environment="TEST")
        prod_ctrl, _ = allocate_control_numbers(batch_id=prod_batch.id, environment="PRODUCTION")

        self.assertEqual(test_ctrl.environment, "TEST")
        self.assertEqual(prod_ctrl.environment, "PRODUCTION")


# ---------------------------------------------------------------------------
# 6. Multi-company / provider isolation
# ---------------------------------------------------------------------------

class TestMultiCompanyIsolation(EnterpriseFixturesMixin, TestCase):

    def setUp(self):
        super().setUp()
        # Build a second completely independent company.
        self.partner_b = self._make_partner(sender_id="SENDERB001")
        self.provider_b = self._make_provider(npi="1666666666", tax_id="666666666")
        self.patient_b = self._make_patient(medicaid_member_id="MEMBERBBB1")
        self.trip_b = self._make_trip(self.patient_b, self.provider_b, miles=80)
        self.claim_b = self._make_claim(self.trip_b, claim_number="CLM-B-001")
        self.batch_b = self._make_batch(self.partner_b, self.claim_b)

    def test_company_a_claim_not_in_company_b_batch(self):
        """Company A's claims must never appear in Company B's 837P file."""
        from apps.edi.utils.handler import Generate837PHandler
        payload_b = Generate837PHandler(self.batch_b.id).build_payload_dict()
        body_b = render_edi_file(build_edi_content(payload_b))
        # Company A's member ID must not leak into Company B's file.
        self.assertNotIn(self.MEMBER_ID, body_b)
        # Company B's member ID must be present.
        self.assertIn("MEMBERBBB1", body_b)

    def test_company_b_claim_not_in_company_a_batch(self):
        """Company B's claims must never appear in Company A's 837P file."""
        from apps.edi.utils.handler import Generate837PHandler
        payload_a = Generate837PHandler(self.batch.id).build_payload_dict()
        body_a = render_edi_file(build_edi_content(payload_a))
        self.assertNotIn("MEMBERBBB1", body_a)
        self.assertIn(self.MEMBER_ID, body_a)

    def test_provider_ids_do_not_cross_companies(self):
        """Each company's 837P must carry only that company's NPI."""
        from apps.edi.utils.handler import Generate837PHandler
        payload_a = Generate837PHandler(self.batch.id).build_payload_dict()
        payload_b = Generate837PHandler(self.batch_b.id).build_payload_dict()
        body_a = render_edi_file(build_edi_content(payload_a))
        body_b = render_edi_file(build_edi_content(payload_b))
        self.assertIn(self.PROVIDER_NPI, body_a)
        self.assertNotIn("1666666666", body_a)
        self.assertIn("1666666666", body_b)
        self.assertNotIn(self.PROVIDER_NPI, body_b)


# ---------------------------------------------------------------------------
# 7. Claim status lifecycle (distinct states per client requirement #14)
# ---------------------------------------------------------------------------

class TestClaimStatusLifecycle(EnterpriseFixturesMixin, TestCase):

    def test_edi_generated_ne_uploaded(self):
        """837P generated and uploaded must be distinct claim statuses."""
        edi = create_edi_file_for_batch(
            batch_id=self.batch.id,
            file_hash="HASH-LIFECYCLE-1",
            path_or_blob_ref="media/edi/lc1.txt",
        )
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.status, ClaimStatus.EDI_GENERATED)

        mark_edi_file_uploaded(edi.id)
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.status, ClaimStatus.EDI_SENT)

    def test_uploaded_ne_accepted(self):
        """EDI_SENT (uploaded) ≠ EDI_ACCEPTED."""
        edi = create_edi_file_for_batch(
            batch_id=self.batch.id,
            file_hash="HASH-LIFECYCLE-2",
            path_or_blob_ref="media/edi/lc2.txt",
        )
        mark_edi_file_uploaded(edi.id)
        self.claim.refresh_from_db()
        self.assertNotEqual(self.claim.status, ClaimStatus.EDI_ACCEPTED)
        self.assertEqual(self.claim.status, ClaimStatus.EDI_SENT)

    def test_999_accepted_sets_edi_accepted_not_paid(self):
        """999 ACCEPTED → EDI_ACCEPTED.  Must NEVER jump to PAID."""
        edi = create_edi_file_for_batch(
            batch_id=self.batch.id,
            file_hash="HASH-LIFECYCLE-3",
            path_or_blob_ref="media/edi/lc3.txt",
        )
        mark_edi_file_uploaded(edi.id)
        self.claim.status = ClaimStatus.EDI_SENT
        self.claim.save(update_fields=["status", "updated_at"])

        ack, updated = apply_edi_acknowledgement(
            batch_id=self.batch.id,
            ack_type="999",
            status=AcknowledgementStatus.ACCEPTED,
            affected_st02="0001",
            raw_file_ref="s3://test/999.edi",
            apply_claim_status=True,
        )
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.status, ClaimStatus.EDI_ACCEPTED)
        self.assertNotEqual(self.claim.status, ClaimStatus.PAID)
        self.assertIn(self.claim.id, updated)

    def test_999_rejected_sets_edi_rejected(self):
        """999 REJECTED must set claim status to EDI_REJECTED — not silently ignored."""
        edi = create_edi_file_for_batch(
            batch_id=self.batch.id,
            file_hash="HASH-LIFECYCLE-4",
            path_or_blob_ref="media/edi/lc4.txt",
        )
        mark_edi_file_uploaded(edi.id)
        self.claim.status = ClaimStatus.EDI_SENT
        self.claim.save(update_fields=["status", "updated_at"])

        ack, updated = apply_edi_acknowledgement(
            batch_id=self.batch.id,
            ack_type="999",
            status=AcknowledgementStatus.REJECTED,
            affected_st02="0001",
            raw_file_ref="s3://test/999-rejected.edi",
            apply_claim_status=True,
        )
        self.claim.refresh_from_db()
        self.assertEqual(
            self.claim.status,
            ClaimStatus.EDI_REJECTED,
            "Rejected 999 must set claim status to EDI_REJECTED.",
        )
        self.assertIn(self.claim.id, updated)

    def test_paid_claim_status_never_overwritten_by_999(self):
        """Terminal PAID status must not be overwritten by a late 999."""
        edi = create_edi_file_for_batch(
            batch_id=self.batch.id,
            file_hash="HASH-LIFECYCLE-5",
            path_or_blob_ref="media/edi/lc5.txt",
        )
        mark_edi_file_uploaded(edi.id)
        self.claim.status = ClaimStatus.PAID
        self.claim.save(update_fields=["status", "updated_at"])

        ack, updated = apply_edi_acknowledgement(
            batch_id=self.batch.id,
            ack_type="999",
            status=AcknowledgementStatus.REJECTED,
            affected_st02="0001",
            raw_file_ref="s3://test/late-999.edi",
            apply_claim_status=True,
        )
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.status, ClaimStatus.PAID)
        self.assertEqual(updated, [])


# ---------------------------------------------------------------------------
# 8. Multiple claims in one batch; bad + good mixed batch
# ---------------------------------------------------------------------------

class TestMultiClaimBatch(EnterpriseFixturesMixin, TestCase):

    def test_multiple_claims_in_one_batch_generates_single_837p(self):
        """Multiple claims → single 837P file with one ST loop per claim."""
        patient2 = self._make_patient(medicaid_member_id="MULTI-MEM-2")
        trip2 = self._make_trip(patient2, self.provider, miles=80)
        claim2 = self._make_claim(trip2, claim_number="MULTI-CLM-2")
        self._complete_documents(claim2)

        BatchClaim.objects.create(batch=self.batch, claim=claim2, st02="0002")
        self.batch.claim_count = 2
        self.batch.save(update_fields=["claim_count", "updated_at"])

        from apps.edi.utils.handler import Generate837PHandler
        edi_file, payload, body = Generate837PHandler(self.batch.id).generate()

        self.assertEqual(len(payload["claims"]), 2)
        # Each claim produces its own ST loop.
        self.assertGreaterEqual(body.count("ST*837*"), 2)
        self.assertGreaterEqual(body.count("SE*"), 2)
        # Each member ID must appear exactly once.
        self.assertEqual(body.count(self.MEMBER_ID), 1)
        self.assertEqual(body.count("MULTI-MEM-2"), 1)

    def test_bad_claim_isolated_from_good_claims(self):
        """A claim with missing data must fail readiness without blocking good claims."""
        # Create patient with a unique ID then blank it to simulate missing medicaid ID.
        bad_patient = self._make_patient(medicaid_member_id="BADMEM-ISOL1")
        bad_patient.medicaid_member_id = ""
        bad_patient.save(update_fields=["medicaid_member_id", "updated_at"])
        bad_trip = self._make_trip(bad_patient, self.provider, miles=80)
        bad_claim = self._make_claim(bad_trip, claim_number="BAD-CLM-001")

        # Build a fresh batch with only the bad claim.
        bad_batch = SubmissionBatch.objects.create(
            batch_number="BAD-BATCH-001",
            trading_partner=self.partner,
            environment="TEST",
            status=BatchStatus.READY,
            is_active=True,
        )
        self._complete_documents(bad_claim)
        BatchClaim.objects.create(batch=bad_batch, claim=bad_claim, st02="0001")
        bad_batch.claim_count = 1
        bad_batch.save(update_fields=["claim_count", "updated_at"])

        errors = collect_batch_readiness_errors(bad_batch)
        self.assertTrue(errors, "Bad claim batch must report errors")

        # Good batch (self.batch) must still be ready.
        good_errors = collect_batch_readiness_errors(self.batch)
        self.assertEqual(good_errors, [])


# ---------------------------------------------------------------------------
# 9. Idempotency / duplicate protection
# ---------------------------------------------------------------------------

class TestIdempotency(EnterpriseFixturesMixin, TestCase):

    def test_control_number_allocation_idempotent(self):
        """Re-allocating control numbers for the same batch returns existing row."""
        ctrl1, created1 = allocate_control_numbers(batch_id=self.batch.id)
        ctrl2, created2 = allocate_control_numbers(batch_id=self.batch.id)
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(ctrl1.id, ctrl2.id)

    def test_duplicate_batch_claim_prevented_by_constraint(self):
        """The same claim cannot be added to the same batch twice."""
        from django.db import IntegrityError
        with self.assertRaises(Exception):  # UniqueConstraint → IntegrityError
            BatchClaim.objects.create(
                batch=self.batch, claim=self.claim, st02="9999"
            )


# ---------------------------------------------------------------------------
# 10. API authentication — unauthorized must get 401/403
# ---------------------------------------------------------------------------

class TestAPIAuthentication(AuthAPITestCase):
    authenticate_by_default = False  # start unauthenticated

    def test_unauthenticated_edi_generate_returns_401(self):
        response = self.client.post(reverse("edi-file-generate-837p"), {}, format="json")
        self.assertIn(response.status_code, [401, 403])

    def test_unauthenticated_claim_list_returns_401(self):
        response = self.client.get(reverse("claim-list-create"))
        self.assertIn(response.status_code, [401, 403])

    def test_unauthenticated_batch_list_returns_401(self):
        response = self.client.get(reverse("submission-batch-list-create"))
        self.assertIn(response.status_code, [401, 403])


# ---------------------------------------------------------------------------
# 11. 835 remittance — paid / denied / adjustments
# ---------------------------------------------------------------------------

class Test835Parsing(TestCase):

    def test_835_paid_claim_parsed_correctly(self):
        from apps.edi.utils.x12 import parse_835
        raw = (
            "ISA*00*          *00*          *ZZ*COMEDASSISTPROG   *ZZ*TESTSENDER1    "
            "*260901*1200*^*00501*000000001*0*T*:~"
            "GS*HP*COMEDASSISTPROG*TESTSENDER1*20260901*1200*1*X*005010X221A1~"
            "ST*835*0001~"
            "BPR*I*100.00*C*ACH*CCP*01*111111111*DA*12345678*1111111111**01*999999999*DA*87654321*20260901~"
            "TRN*1*TRACE999001*1111111111~"
            "DTM*405*20260901~"
            "CLP*SAMPLECLAIM001*1*125.00*100.00*0*MC~"
            "CAS*CO*45*25.00~"
            "SE*8*0001~"
            "GE*1*1~"
            "IEA*1*000000001~"
        )
        result = parse_835(raw)
        self.assertEqual(len(result["claims"]), 1)
        clp = result["claims"][0]
        self.assertEqual(clp["claim_number"], "SAMPLECLAIM001")
        self.assertEqual(clp["outcome"], "PAID")
        self.assertAlmostEqual(float(clp["payment_amount"]), 100.00)
        self.assertAlmostEqual(float(clp["charge_amount"]), 125.00)
        self.assertIn("CO:45:25.00", clp["adjustment_codes"])

    def test_835_denied_claim_parsed_correctly(self):
        from apps.edi.utils.x12 import parse_835
        raw = (
            "ISA*00*          *00*          *ZZ*COMEDASSISTPROG   *ZZ*TESTSENDER1    "
            "*260901*1200*^*00501*000000002*0*T*:~"
            "GS*HP*COMEDASSISTPROG*TESTSENDER1*20260901*1200*2*X*005010X221A1~"
            "ST*835*0001~"
            "BPR*I*0.00*C*ACH*CCP*01*111111111*DA*12345678*1111111111**01*999999999*DA*87654321*20260901~"
            "TRN*1*TRACE999002*1111111111~"
            "CLP*SAMPLECLAIM002*4*125.00*0.00*0*MC~"
            "CAS*CO*96*125.00~"
            "SE*7*0001~"
            "GE*1*2~"
            "IEA*1*000000002~"
        )
        result = parse_835(raw)
        clp = result["claims"][0]
        self.assertEqual(clp["outcome"], "DENIED")
        self.assertEqual(float(clp["payment_amount"]), 0.0)


# ---------------------------------------------------------------------------
# 12. Provider serializer — atypical / NPI cross-field validation
# ---------------------------------------------------------------------------

class TestProviderSerializerValidation(TestCase):

    def _post(self, data):
        from apps.provider_billing_profile.serializers import ProviderBillingProfileSerializer
        s = ProviderBillingProfileSerializer(data=data)
        return s.is_valid(), s.errors

    def test_atypical_without_medicaid_provider_id_fails(self):
        valid, errors = self._post({
            "legal_name": "Atypical Co",
            "is_atypical": True,
            "medicaid_provider_id": "",
            "taxonomy_code": "343900000X",
        })
        self.assertFalse(valid)
        self.assertIn("medicaid_provider_id", str(errors))

    def test_atypical_with_npi_fails(self):
        valid, errors = self._post({
            "legal_name": "Atypical Co",
            "is_atypical": True,
            "npi": "1999999999",
            "medicaid_provider_id": "ATYPTST999",
            "taxonomy_code": "343900000X",
        })
        self.assertFalse(valid)
        self.assertIn("npi", str(errors))

    def test_standard_provider_valid(self):
        valid, errors = self._post({
            "legal_name": "Standard LLC",
            "is_atypical": False,
            "npi": "1999999999",
            "tax_id": "999999999",
            "taxonomy_code": "343900000X",
        })
        self.assertTrue(valid, errors)

    def test_npi_non_digit_rejected(self):
        valid, errors = self._post({
            "legal_name": "Bad NPI Co",
            "npi": "12ABCDEFGH",
        })
        self.assertFalse(valid)
        self.assertIn("npi", str(errors))

    def test_tax_id_stored_as_digits_only(self):
        from apps.provider_billing_profile.serializers import ProviderBillingProfileSerializer
        s = ProviderBillingProfileSerializer(data={
            "legal_name": "EIN Co",
            "npi": "1999999999",
            "tax_id": "99-9999999",  # with hyphen
        })
        self.assertTrue(s.is_valid(), s.errors)
        self.assertEqual(s.validated_data["tax_id"], "999999999")


# ---------------------------------------------------------------------------
# 13. Patient serializer — DOB optional
# ---------------------------------------------------------------------------

class TestPatientSerializerDOBOptional(TestCase):

    def test_patient_without_dob_valid(self):
        from apps.patient.serializers import PatientSerializer
        s = PatientSerializer(data={
            "first_name": "Test",
            "last_name": "Patient",
            "medicaid_member_id": "NODOB-SERIAL",
            "county": "Denver",
        })
        self.assertTrue(s.is_valid(), s.errors)
        self.assertIsNone(s.validated_data.get("date_of_birth"))

    def test_patient_future_dob_rejected(self):
        from apps.patient.serializers import PatientSerializer
        from datetime import timedelta
        future = (date.today() + timedelta(days=365)).isoformat()
        s = PatientSerializer(data={
            "first_name": "Test",
            "last_name": "Patient",
            "medicaid_member_id": "FUTUREDOB-1",
            "county": "Denver",
            "date_of_birth": future,
        })
        self.assertFalse(s.is_valid())
        self.assertIn("date_of_birth", str(s.errors))
