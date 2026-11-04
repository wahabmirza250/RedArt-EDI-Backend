"""Long-distance pilot orchestration API tests."""

from unittest.mock import patch

from django.urls import reverse
from rest_framework import status

from apps.claim.choices import BatchStatus, ClaimStatus
from apps.claim.models import SubmissionBatch
from apps.claim.tests_attachment_workflow import AttachmentWorkflowAPITests
from apps.claim_service_line.models import ClaimServiceLine
from apps.edi.choices import EDIFileStatus
from apps.edi.models import EDIFile
from apps.trading_partner.models import TradingPartner


class LongDistancePilotAPITests(AttachmentWorkflowAPITests):
    def setUp(self):
        super().setUp()
        self.partner = TradingPartner.objects.create(
            name="Test Transport LLC",
            sender_id="TP123457",  # Synthetic numeric TPID, matching HCPF filenames.
            receiver_id="COMEDASSISTPROG",
            environment="TEST",
        )
        if not ClaimServiceLine.objects.filter(claim=self.claim, is_active=True).exists():
            ClaimServiceLine.objects.create(
                claim=self.claim,
                procedure_code="A0120",
                from_date=self.trip.service_date,
                to_date=self.trip.service_date,
                units=self.trip.mileage_units,
                mileage=self.trip.one_way_miles,
                charge=self.trip.charge,
                is_active=True,
            )
        self.patient.gender = "M"
        self.patient.address_line_1 = "100 Main St"
        self.patient.city = "Denver"
        self.patient.state = "CO"
        self.patient.zip = "80202"
        self.patient.phone = "3035550100"
        self.patient.save(
            update_fields=[
                "gender",
                "address_line_1",
                "city",
                "state",
                "zip",
                "phone",
                "updated_at",
            ]
        )
        self.provider.taxonomy_code = "343900000X"
        self.provider.tax_id = "999999999"  # required for REF*EI (NPI provider)
        self.provider.address_line_1 = "100 Main St"
        self.provider.city = "Denver"
        self.provider.state = "CO"
        self.provider.zip = "80202"
        self.provider.phone = "3035550199"
        self.provider.save(
            update_fields=[
                "taxonomy_code",
                "tax_id",
                "address_line_1",
                "city",
                "state",
                "zip",
                "phone",
                "updated_at",
            ]
        )
        self.claim.diagnosis_code = "R68.89"
        self.claim.place_of_service = "41"
        self.claim.save(update_fields=["diagnosis_code", "place_of_service", "updated_at"])

    @patch("apps.edi.utils.s3_client.upload_bytes_to_s3", return_value="s3://edi-files/x.pdf")
    def test_pilot_runs_attachments_batch_and_837p(self, _mock_s3):
        self._complete_documents()
        response = self.client.post(
            reverse("pilot-long-distance"),
            {
                "claim_id": self.claim.id,
                "trading_partner_id": self.partner.id,
                "batch_number": "PILOT-BATCH-1",
                "submit_attachments": True,
                "attachment_channel": "HCPF_PORTAL",
                "attachment_reference": "PILOT-ATT-1",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        data = response.data["data"]
        self.assertEqual(data["claim_id"], self.claim.id)
        self.assertTrue(data["batch_id"])
        self.assertTrue(data["edi_file_id"])
        self.assertTrue(data["attachment_submission_id"])
        step_names = [s["step"] for s in data["steps"]]
        self.assertEqual(
            step_names,
            [
                "documents_ready",
                "attachments_submitted",
                "batch_claim_added",
                "837p_generated",
            ],
        )
        self.assertIn("import-999", data["next_actions"][0])

        self.claim.refresh_from_db()
        # After 837P generation the claim advances to EDI_GENERATED;
        # attachment flow may also have advanced it to ATTACHMENT_SUBMITTED.
        self.assertIn(
            self.claim.status,
            (ClaimStatus.READY_FOR_837P, ClaimStatus.ATTACHMENT_SUBMITTED, ClaimStatus.EDI_GENERATED),
        )

        batch = SubmissionBatch.objects.get(pk=data["batch_id"])
        self.assertEqual(batch.batch_number, "PILOT-BATCH-1")
        self.assertEqual(batch.trading_partner_id, self.partner.id)
        self.assertEqual(batch.status, BatchStatus.GENERATED)

        edi = EDIFile.objects.get(pk=data["edi_file_id"])
        self.assertEqual(edi.status, EDIFileStatus.GENERATED)
        self.assertEqual(edi.transaction_type, "837P")

    @patch("apps.edi.utils.s3_client.upload_bytes_to_s3", return_value="s3://edi-files/x.pdf")
    def test_pilot_skip_attachments(self, _mock_s3):
        self._complete_documents()
        response = self.client.post(
            reverse("pilot-long-distance"),
            {
                "claim_id": self.claim.id,
                "trading_partner_id": self.partner.id,
                "submit_attachments": False,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        data = response.data["data"]
        self.assertIsNone(data["attachment_submission_id"])
        step_names = [s["step"] for s in data["steps"]]
        self.assertEqual(
            step_names,
            ["documents_ready", "batch_claim_added", "837p_generated"],
        )

    def test_pilot_requires_attachment_claim(self):
        short_trip_claim = self.claim
        short_trip_claim.attachment_required = False
        short_trip_claim.save(update_fields=["attachment_required", "updated_at"])

        response = self.client.post(
            reverse("pilot-long-distance"),
            {
                "claim_id": short_trip_claim.id,
                "trading_partner_id": self.partner.id,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
