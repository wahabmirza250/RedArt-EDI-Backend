from datetime import date
from decimal import Decimal

from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework import status
from apps.core.testing import AuthAPITestCase

from apps.claim.choices import BatchStatus, ClaimStatus, DocumentStatus, DocumentType
from apps.claim.models import BatchClaim, Claim, ClaimDocument, SubmissionBatch
from apps.claim.utils.service import create_claim_from_trip, sync_claim_document_status
from apps.edi.choices import EDIFileStatus, TransferLogStatus
from apps.edi.models import EDIControlNumber, EDIFile, EDIFileTransferLog
from apps.edi.utils.envelope import get_edi_envelope_config
from apps.edi.utils.readiness import assert_batch_ready_for_837p_generation
from apps.edi.utils.service import (
    allocate_control_numbers,
    build_colorado_837p_filename,
    create_edi_file_for_batch,
    mark_edi_file_uploaded,
)
from apps.long_distance_rule.models import LongDistanceRule
from apps.nemt_trip.models import NemtTrip
from apps.patient.models import Patient
from apps.provider_billing_profile.models import ProviderBillingProfile
from apps.trading_partner.models import TradingPartner


class EDIFixturesMixin:
    def setUp(self):
        super().setUp()
        LongDistanceRule.objects.update_or_create(
            county_type="STANDARD",
            defaults={
                "review_threshold": 52,
                "verification_threshold": 25,
                "is_active": True,
            },
        )
        self.partner = TradingPartner.objects.create(
            name="Colorado Medicaid",
            sender_id="TP123456",
            receiver_id="COMEDASSISTPROG",
            environment="TEST",
        )
        self.patient = Patient.objects.create(
            first_name="Ali",
            last_name="Khan",
            date_of_birth=date(1995, 5, 12),
            gender="M",
            medicaid_member_id="MEDI001",
            county="Denver",
            address_line_1="100 Main St",
            city="Denver",
            state="CO",
            zip="80202",
            phone="3035550100",
        )
        self.provider = ProviderBillingProfile.objects.create(
            legal_name="Test Transport LLC",
            billing_name="Test Transport LLC",
            npi="1234567890",
            tax_id="123456789",  # required for REF*EI in 837P
            taxonomy_code="343900000X",
            address_line_1="100 Main St",
            city="Denver",
            state="CO",
            zip="80202",
            phone="3035550199",
        )
        self.trip = NemtTrip.objects.create(
            patient=self.patient,
            provider=self.provider,
            service_date=date(2026, 8, 30),
            pickup="Home",
            dropoff="Clinic",
            one_way_miles=Decimal("78.00"),
            mileage_units=78,
            charge=Decimal("150.00"),
        )
        self.claim, _ = create_claim_from_trip(
            trip_id=self.trip.id,
            claim_number="C-EDI-1",
            diagnosis_code="R68.89",
            place_of_service="41",
            procedure_code="A0130",
            create_service_line=True,
        )
        for doc_type, name, digest in (
            (DocumentType.STANDARD_TRIP_LOG, "trip.pdf", "H1"),
            (DocumentType.MILE_25_VERIFICATION, "v25.pdf", "H2"),
        ):
            ClaimDocument.objects.create(
                claim=self.claim,
                document_type=doc_type,
                file_name=name,
                document_hash=digest,
                blob_ref=f"claim-documents/{self.claim.id}/{doc_type}/{name}",
                is_signed=True,
                status=DocumentStatus.COMPLETE,
            )
        sync_claim_document_status(self.claim)
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.status, ClaimStatus.READY_FOR_837P)

        self.batch = SubmissionBatch.objects.create(
            batch_number="RB-EDI-10048",
            trading_partner=self.partner,
            environment="TEST",
            status=BatchStatus.READY,
        )
        BatchClaim.objects.create(
            batch=self.batch,
            claim=self.claim,
            st02="0001",
        )
        self.batch.claim_count = 1
        self.batch.total_amount = Decimal("150.00")
        self.batch.save(update_fields=["claim_count", "total_amount", "updated_at"])


class EDIServiceTests(EDIFixturesMixin, TestCase):
    def test_allocate_control_numbers_and_filename(self):
        row, created = allocate_control_numbers(batch_id=self.batch.id)
        self.assertTrue(created)
        self.assertEqual(row.isa13, "000000001")
        self.assertEqual(row.gs06, "1")
        self.assertEqual(row.environment, "TEST")

        again, created_again = allocate_control_numbers(batch_id=self.batch.id)
        self.assertFalse(created_again)
        self.assertEqual(again.id, row.id)

        name = build_colorado_837p_filename(sender_id="TP123456")
        self.assertTrue(name.startswith("TP123456-837P-"))
        self.assertTrue(name.endswith("-1of1.txt"))

        envelope = get_edi_envelope_config("TEST")
        self.assertEqual(envelope["isa15"], "T")
        self.assertEqual(envelope["gs08"], "005010X222A1")
        self.assertEqual(get_edi_envelope_config("PRODUCTION")["isa15"], "P")

    def test_create_edi_file_and_mark_uploaded(self):
        assert_batch_ready_for_837p_generation(self.batch)
        edi_file = create_edi_file_for_batch(
            batch_id=self.batch.id,
            file_hash="FILEHASH123",
            path_or_blob_ref="s3://edi/001.txt",
        )
        self.assertEqual(edi_file.transaction_type, "837P")
        self.assertEqual(edi_file.status, EDIFileStatus.GENERATED)
        self.assertIsNotNone(edi_file.control_number_id)
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.status, BatchStatus.GENERATED)

        uploaded = mark_edi_file_uploaded(
            edi_file.id,
            path_or_blob_ref="s3://edi/001.txt",
            file_hash="FILEHASH123",
        )
        self.assertEqual(uploaded.status, EDIFileStatus.UPLOADED)
        self.assertIsNotNone(uploaded.uploaded_at)
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.status, BatchStatus.SUBMITTED)

    def test_missing_medicaid_member_id_blocks_edi_file(self):
        """Missing medicaid_member_id must block 837P generation (not silently fabricated)."""
        self.patient.medicaid_member_id = ""
        self.patient.save(update_fields=["medicaid_member_id", "updated_at"])
        with self.assertRaises(ValueError) as ctx:
            create_edi_file_for_batch(batch_id=self.batch.id)
        self.assertIn("medicaid_member_id", str(ctx.exception).lower())


class EDIAPITests(EDIFixturesMixin, AuthAPITestCase):
    def test_allocate_and_from_batch_apis(self):
        alloc = self.client.post(
            reverse("edi-control-number-allocate"),
            {"batch_id": self.batch.id, "environment": "TEST"},
            format="json",
        )
        self.assertEqual(alloc.status_code, status.HTTP_201_CREATED)
        self.assertEqual(alloc.data["data"]["isa13"], "000000001")

        created = self.client.post(
            reverse("edi-file-from-batch"),
            {
                "batch_id": self.batch.id,
                "transaction_type": "837P",
                "file_hash": "FILEHASH123",
                "path_or_blob_ref": "s3://edi/001.txt",
                "status": "GENERATED",
            },
            format="json",
        )
        self.assertEqual(created.status_code, status.HTTP_201_CREATED)
        file_id = created.data["data"]["id"]
        self.assertTrue(
            created.data["data"]["filename"].startswith("TP123456-837P-")
        )

        marked = self.client.post(
            reverse("edi-file-mark-uploaded", kwargs={"pk": file_id}),
            {"path_or_blob_ref": "s3://edi/001.txt", "file_hash": "FILEHASH123"},
            format="json",
        )
        self.assertEqual(marked.status_code, status.HTTP_200_OK)
        self.assertEqual(marked.data["data"]["status"], "UPLOADED")

        listed = self.client.get(
            reverse("edi-file-list-create"), {"batch_id": self.batch.id}
        )
        self.assertEqual(listed.status_code, status.HTTP_200_OK)
        self.assertEqual(listed.data["count"], 1)

        controls = self.client.get(
            reverse("edi-control-number-list-create"),
            {"batch_id": self.batch.id},
        )
        self.assertEqual(controls.status_code, status.HTTP_200_OK)
        self.assertEqual(controls.data["count"], 1)
        self.assertEqual(EDIControlNumber.objects.count(), 1)
        self.assertEqual(EDIFile.objects.count(), 1)

    def test_empty_batch_cannot_create_file(self):
        empty = SubmissionBatch.objects.create(
            batch_number="RB-EMPTY",
            trading_partner=self.partner,
            environment="TEST",
            status=BatchStatus.READY,
            claim_count=0,
        )
        response = self.client.post(
            reverse("edi-file-from-batch"),
            {"batch_id": empty.id},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class Generate837PTests(EDIFixturesMixin, AuthAPITestCase):
    def test_handler_and_generate_api(self):
        from apps.edi.utils.handler import Generate837PHandler
        from apps.edi.utils.schema import build_edi_content, render_edi_file

        # tax_id is now required on the provider record (no settings fallback).
        self.provider.tax_id = "123456789"
        self.provider.save(update_fields=["tax_id", "updated_at"])

        handler = Generate837PHandler(self.batch.id)
        payload = handler.build_payload_dict()
        self.assertEqual(payload["trading_partner"]["sender_id"], "TP123456")
        self.assertEqual(len(payload["claims"]), 1)
        segments = build_edi_content(payload)
        body = render_edi_file(segments)
        self.assertIn("ISA*", body)
        self.assertIn("COMEDASSISTPROG", body)
        self.assertIn("CO_TXIX", body)
        self.assertIn("ST*837*", body)
        # Client-approved sample: HL → NM1*85 (no PRV); REF*EI after N4
        hl_i = body.index("HL*1**20*1~")
        nm1_i = body.index("NM1*85*")
        ref_i = body.index("REF*EI*")
        self.assertLess(hl_i, nm1_i)
        self.assertLess(nm1_i, ref_i)
        self.assertNotIn("PRV*BI*", body)
        self.assertTrue(body.strip().endswith("IEA*1*000000001~") or "IEA*1*" in body)

        edi_file, _, _ = handler.generate()
        self.assertEqual(edi_file.status, EDIFileStatus.GENERATED)
        self.assertTrue(edi_file.filename.startswith("TP123456-837P-"))
        self.assertTrue(edi_file.file_hash)
        # After generation, claim status advances to EDI_GENERATED (not yet uploaded).
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.status, ClaimStatus.EDI_GENERATED)

        # Second generate via API on same batch still allowed (new file)
        response = self.client.post(
            reverse("edi-file-generate-837p"),
            {"batch_id": self.batch.id},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["data"]["status"], "GENERATED")

    def test_queue_upload_writes_transfer_logs(self):
        from unittest.mock import patch

        from apps.edi.choices import TransferLogStatus
        from apps.edi.models import EDIFileTransferLog, SFTPCredentials, SFTPDirectory
        from apps.edi.utils.handler import Generate837PHandler

        edi_file, _, _ = Generate837PHandler(self.batch.id).generate()
        cred = SFTPCredentials.objects.create(
            name="TEST-SFTP",
            trading_partner=self.partner,
            environment="TEST",
            host="127.0.0.1",
            port=22,
            username="user",
            auth_type="PASSWORD",
            password="secret",
            is_active=True,
        )
        SFTPDirectory.objects.create(
            credentials=cred,
            name="outbound",
            purpose="OUTBOUND_837P",
            sending_path="/send",
            receiving_path="/recv",
            is_active=True,
        )

        with self.settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_STORE_EAGER_RESULT=True):
            with patch(
                "apps.edi.utils.upload.upload_bytes_via_sftp",
                return_value="/send/" + edi_file.filename,
            ), patch(
                "apps.edi.utils.upload.upload_bytes_to_s3",
                return_value="s3://edi-files/edi/837p/1/" + edi_file.filename,
            ):
                response = self.client.post(
                    reverse("edi-file-queue-upload", kwargs={"pk": edi_file.id}),
                    {},
                    format="json",
                )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        edi_file.refresh_from_db()
        self.assertEqual(edi_file.status, EDIFileStatus.UPLOADED)
        logs = EDIFileTransferLog.objects.filter(edi_file=edi_file)
        self.assertEqual(logs.count(), 2)
        self.assertTrue(
            logs.filter(channel="SFTP", status=TransferLogStatus.SUCCESS).exists()
        )
        self.assertTrue(
            logs.filter(channel="S3", status=TransferLogStatus.SUCCESS).exists()
        )

        listed = self.client.get(
            reverse("edi-file-transfer-log-list"),
            {"edi_file_id": edi_file.id},
        )
        self.assertEqual(listed.status_code, status.HTTP_200_OK)
        self.assertEqual(listed.data["count"], 2)

        # Resend allowed after UPLOADED
        with self.settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_STORE_EAGER_RESULT=True):
            with patch(
                "apps.edi.utils.upload.upload_bytes_via_sftp",
                return_value="/send/" + edi_file.filename,
            ), patch(
                "apps.edi.utils.upload.upload_bytes_to_s3",
                return_value="s3://edi-files/edi/837p/1/" + edi_file.filename,
            ):
                again = self.client.post(
                    reverse("edi-file-queue-upload", kwargs={"pk": edi_file.id}),
                    {"credentials_id": cred.id},
                    format="json",
                )
        self.assertEqual(again.status_code, status.HTTP_202_ACCEPTED)
        edi_file.refresh_from_db()
        self.assertEqual(edi_file.status, EDIFileStatus.UPLOADED)
        self.assertEqual(
            EDIFileTransferLog.objects.filter(edi_file=edi_file).count(),
            4,
        )


class EDIAcknowledgementAPITests(EDIFixturesMixin, AuthAPITestCase):
    def test_apply_999_sets_edi_accepted_not_paid(self):
        edi = create_edi_file_for_batch(
            batch_id=self.batch.id,
            file_hash="ACKHASH1",
            path_or_blob_ref="media/edi/demo.txt",
        )
        # After create_edi_file_for_batch, claim is EDI_GENERATED (not yet uploaded).
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.status, ClaimStatus.EDI_GENERATED)

        mark_edi_file_uploaded(edi.id)
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.status, ClaimStatus.EDI_SENT)

        response = self.client.post(
            reverse("edi-acknowledgement-apply"),
            {
                "batch_id": self.batch.id,
                "ack_type": "999",
                "status": "ACCEPTED",
                "affected_st02": "0001",
                "raw_file_ref": "s3://edi/999_001.edi",
                "apply_claim_status": True,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["data"]["status"], "ACCEPTED")
        self.assertIn(self.claim.id, response.data["data"]["updated_claim_ids"])

        self.claim.refresh_from_db()
        self.assertEqual(self.claim.status, ClaimStatus.EDI_ACCEPTED)
        self.assertNotEqual(self.claim.status, ClaimStatus.PAID)

        edi.refresh_from_db()
        self.assertEqual(edi.status, EDIFileStatus.ACKNOWLEDGED)
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.status, BatchStatus.ACKNOWLEDGED)

        listed = self.client.get(
            reverse("edi-acknowledgement-list-create"),
            {"batch_id": self.batch.id},
        )
        self.assertEqual(listed.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(listed.data["count"], 1)

    def test_import_999_parses_client_sample(self):
        edi = create_edi_file_for_batch(
            batch_id=self.batch.id,
            file_hash="ACKHASH2",
            path_or_blob_ref="media/edi/demo2.txt",
        )
        mark_edi_file_uploaded(edi.id)
        # After upload, claim is EDI_SENT — ready for 999 ack.
        self.claim.status = ClaimStatus.EDI_SENT
        self.claim.save(update_fields=["status", "updated_at"])
        content = (
            "ISA*00*          *00*          *ZZ*COMEDASSISTPROG*ZZ*89513013       "
            "*260817*1947*^*00501*000000001*0*T*:~"
            "GS*FA*COMEDASSISTPROG*89513013*20260817*1947*1*X*005010X231A1~"
            "ST*999*0001*005010X231A1~"
            "AK1*HC*1*005010X222A1~"
            "AK2*837*0001*005010X222A1~"
            "IK5*A~"
            "AK9*A*1*1*1~"
            "SE*6*0001~"
            "GE*1*1~"
            "IEA*1*000000001~"
        )
        response = self.client.post(
            reverse("edi-acknowledgement-import-999"),
            {
                "batch_id": self.batch.id,
                "edi_file_id": edi.id,
                "raw_file_ref": "s3://edi/999_001.edi",
                "content": content,
                "apply_claim_status": True,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["data"]["status"], "ACCEPTED")
        self.assertEqual(response.data["data"]["affected_st02"], "0001")
        self.assertEqual(response.data["data"]["parsed"]["ik5_code"], "A")
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.status, ClaimStatus.EDI_ACCEPTED)

    def test_driver_nm1_dn_not_emitted(self):
        """T5: NM1*DN is the referring-provider qualifier — must NOT appear for drivers."""
        from apps.edi.utils.handler import Generate837PHandler
        from apps.edi.utils.schema import build_edi_content, render_edi_file

        self.trip.driver_first_name = "CHRIS"
        self.trip.driver_last_name = "TESTDRIVER"
        self.trip.save(
            update_fields=["driver_first_name", "driver_last_name", "updated_at"]
        )
        payload = Generate837PHandler(self.batch.id).build_payload_dict()
        body = render_edi_file(build_edi_content(payload))
        # DN = referring provider qualifier; must never appear for NEMT drivers.
        self.assertNotIn("NM1*DN*", body)


class T2RejectionFileTests(TestCase):
    """T2: .rjct/.rsp/.description/.html files must not be silently skipped."""

    def test_is_report_file_detects_rjct_rsp_description_html(self):
        from apps.edi.utils.import_999 import _is_report_file
        for name in ["file.rjct", "file.rsp", "file.description", "report.html", "rpt.htm"]:
            self.assertTrue(_is_report_file(name), f"Should detect: {name}")

    def test_is_report_file_ignores_x12_and_txt(self):
        from apps.edi.utils.import_999 import _is_report_file
        for name in ["999.x12", "ack.txt", "data.edi", "file.tmp"]:
            self.assertFalse(_is_report_file(name), f"Should not detect: {name}")

    def test_candidate_filename_accepts_999_x12(self):
        from apps.edi.utils.import_999 import _candidate_filename
        self.assertTrue(_candidate_filename("tp123-999-20260901120000000-1of1.x12"))
        self.assertFalse(_candidate_filename("tp123.rjct"))   # routed to save_rejection, not 999
        self.assertFalse(_candidate_filename("file.tmp"))


class EDI999ImportAPITests(EDIFixturesMixin, AuthAPITestCase):
    def test_poll_import_999_async_returns_celery_task(self):
        from unittest.mock import patch

        with patch("apps.edi.import_999_views.poll_edi_999_imports") as mock_poll:
            mock_poll.delay.return_value.id = "task-import-999-1"
            response = self.client.post(
                reverse("edi-999-import-poll"),
                {"async_mode": True},
                format="json",
            )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(response.data["data"]["message"], "Importing started.")
        self.assertEqual(response.data["data"]["celery_task_id"], "task-import-999-1")
        mock_poll.delay.assert_called_once()

    def test_list_edi_999_imports(self):
        from apps.edi.choices import EDI999ImportStatus
        from apps.edi.models import EDI999Import, SFTPCredentials

        cred = SFTPCredentials.objects.create(
            name="TEST-SFTP-999",
            trading_partner=self.partner,
            environment="TEST",
            host="127.0.0.1",
            port=22,
            username="user",
            auth_type="PASSWORD",
            password="secret",
            is_active=True,
        )
        EDI999Import.objects.create(
            credentials=cred,
            filename="ack_0001.999",
            remote_path="/recv/ack_0001.999",
            status=EDI999ImportStatus.QUEUED,
            message="Importing started (Celery task queued).",
        )
        response = self.client.get(reverse("edi-999-import-list"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(response.data["count"], 1)

    def test_process_import_999_parses_and_marks_imported(self):
        from unittest.mock import patch

        from apps.edi.choices import EDI999ImportStatus, SFTPDirectoryPurpose
        from apps.edi.models import EDI999Import, SFTPCredentials, SFTPDirectory
        from apps.edi.utils.import_999 import process_edi_999_import

        edi = create_edi_file_for_batch(
            batch_id=self.batch.id,
            file_hash="HASH999IMP",
            path_or_blob_ref="media/edi/demo999.txt",
        )
        mark_edi_file_uploaded(edi.id)
        # Ensure control gs06 matches sample AK1 group control "1"
        ctrl = edi.control_number
        if ctrl:
            ctrl.gs06 = "1"
            ctrl.isa13 = "000000001"
            ctrl.save(update_fields=["gs06", "isa13", "updated_at"])

        cred = SFTPCredentials.objects.create(
            name="TEST-SFTP-999-PROC",
            trading_partner=self.partner,
            environment="TEST",
            host="127.0.0.1",
            port=22,
            username="user",
            auth_type="PASSWORD",
            password="secret",
            is_active=True,
        )
        directory = SFTPDirectory.objects.create(
            credentials=cred,
            name="inbound-999",
            purpose=SFTPDirectoryPurpose.INBOUND_999,
            sending_path="/send",
            receiving_path="/recv",
            is_active=True,
        )
        row = EDI999Import.objects.create(
            credentials=cred,
            directory=directory,
            batch=self.batch,
            filename="client_999.edi",
            remote_path="/recv/client_999.edi",
            status=EDI999ImportStatus.QUEUED,
        )
        content = (
            "ISA*00*          *00*          *ZZ*COMEDASSISTPROG*ZZ*89513013       "
            "*260817*1947*^*00501*000000001*0*T*:~"
            "GS*FA*COMEDASSISTPROG*89513013*20260817*1947*1*X*005010X231A1~"
            "ST*999*0001*005010X231A1~"
            "AK1*HC*1*005010X222A1~"
            "AK2*837*0001*005010X222A1~"
            "IK5*A~"
            "AK9*A*1*1*1~"
            "SE*6*0001~"
            "GE*1*1~"
            "IEA*1*000000001~"
        ).encode("utf-8")

        # Advance claim to EDI_SENT first (simulating upload) so the 999
        # ACCEPTED ack can advance it to EDI_ACCEPTED.
        self.claim.status = ClaimStatus.EDI_SENT
        self.claim.save(update_fields=["status", "updated_at"])

        with patch(
            "apps.edi.utils.import_999.download_bytes_via_sftp",
            return_value=content,
        ):
            result = process_edi_999_import(row.id, batch_id=self.batch.id)

        self.assertEqual(result["status"], EDI999ImportStatus.IMPORTED)
        row.refresh_from_db()
        self.assertEqual(row.status, EDI999ImportStatus.IMPORTED)
        self.assertIsNotNone(row.acknowledgement_id)
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.status, ClaimStatus.EDI_ACCEPTED)

    def test_generate_matches_approved_sample_shape(self):
        """
        Verify 837P segment shape against the client-approved sample.

        All values below are synthetic test data — no real company, person,
        address, NPI, or credential should appear in source code.
        """
        from apps.claim_service_line.models import ClaimServiceLine
        from apps.edi.utils.handler import Generate837PHandler
        from apps.edi.utils.schema import build_edi_content, render_edi_file

        SENDER = "SMPLSENDER1"
        PROVIDER_NPI = "1999999999"
        PROVIDER_TAX_ID = "000000000"

        self.partner.name = "SAMPLE TRANSPORT LLC"
        self.partner.sender_id = SENDER
        self.partner.contact_name = "BILLING CONTACT"
        self.partner.contact_phone = "0000000000"
        self.partner.save()

        self.provider.billing_name = "SAMPLE TRANSPORT LLC"
        self.provider.npi = PROVIDER_NPI
        self.provider.tax_id = PROVIDER_TAX_ID
        self.provider.address_line_1 = "100 SAMPLE ST"
        self.provider.city = "DENVER"
        self.provider.state = "CO"
        self.provider.zip = "80000"
        self.provider.save()

        self.patient.first_name = "SAMPLE"
        self.patient.last_name = "PATIENT"
        self.patient.medicaid_member_id = "SMPLMEMBER001"
        self.patient.address_line_1 = "200 SAMPLE AVE"
        self.patient.city = "DENVER"
        self.patient.zip = "80001"
        self.patient.date_of_birth = date(1970, 1, 1)
        self.patient.gender = "F"
        self.patient.save()

        self.trip.driver_first_name = "DRIVER"
        self.trip.driver_last_name = "SAMPLE"
        self.trip.service_date = date(2026, 8, 5)
        self.trip.charge = Decimal("14.90")
        self.trip.save()

        self.claim.claim_number = "SAMPLECLAIM001"
        self.claim.diagnosis_code = "R69"
        self.claim.place_of_service = "03"
        self.claim.total_charge = Decimal("14.90")
        self.claim.save()

        ClaimServiceLine.objects.filter(claim=self.claim).delete()
        ClaimServiceLine.objects.create(
            claim=self.claim,
            procedure_code="A0120",
            from_date=date(2026, 8, 5),
            to_date=date(2026, 8, 5),
            units=1,
            charge=Decimal("12.15"),
            is_active=True,
        )
        ClaimServiceLine.objects.create(
            claim=self.claim,
            procedure_code="S0215",
            from_date=date(2026, 8, 5),
            to_date=date(2026, 8, 5),
            units=1,
            charge=Decimal("2.75"),
            is_active=True,
        )
        bc = BatchClaim.objects.get(batch=self.batch, claim=self.claim)
        bc.st02 = "0001"
        bc.save(update_fields=["st02", "updated_at"])

        payload = Generate837PHandler(self.batch.id).build_payload_dict()
        body = render_edi_file(build_edi_content(payload))

        # Organisation entity (NM102=2): NM103+4 empty (NM104-NM107)+qualifier+id → 5 asterisks
        self.assertIn(f"NM1*41*2*SAMPLE TRANSPORT LLC*****46*{SENDER}~", body)
        self.assertIn("PER*IC*BILLING CONTACT*TE*0000000000~", body)
        self.assertIn(f"NM1*85*2*SAMPLE TRANSPORT LLC*****XX*{PROVIDER_NPI}~", body)
        self.assertIn(f"REF*EI*{PROVIDER_TAX_ID}~", body)
        # Person entity (NM102=1): NM103+NM104+3 empty (NM105-NM107)+qualifier+id → 4 asterisks
        self.assertIn("NM1*IL*1*PATIENT*SAMPLE****MI*SMPLMEMBER001~", body)
        self.assertIn("CLM*SAMPLECLAIM001*14.90***03:B:1*Y*A*Y*Y~", body)
        self.assertIn("HI*ABK:R69~", body)
        self.assertNotIn("NM1*DN*", body)  # T5: DN = referring provider, not driver
        self.assertIn("SV1*HC:A0120*12.15*UN*1*03**1~", body)
        self.assertIn("SV1*HC:S0215*2.75*UN*1*03**1~", body)
        self.assertIn("BHT*0019*00*0001*", body)
        self.assertNotIn("PRV*BI*", body)
        # ISA must be exactly 106 chars
        isa_line = [seg for seg in body.split("\n") if seg.startswith("ISA*")][0]
        self.assertEqual(len(isa_line), 106, f"ISA length mismatch: {len(isa_line)}")


class ReceiverUploadGuardTests(TestCase):
    def test_invalid_saved_file_cannot_be_queued_or_create_transfer_logs(self):
        """Upload must fail closed on missing billing N403 (zip), not on optional DMG."""
        from apps.edi.tests_pyx12_preflight import _payload
        from apps.edi.utils.schema import build_edi_content, render_edi_file
        from apps.edi.utils.upload import queue_edi_file_upload

        body = render_edi_file(build_edi_content(_payload())).replace(
            "N4*DENVER*CO*80202~", "N4*DENVER*CO~"
        )
        edi_file = EDIFile.objects.create(
            filename="synthetic-invalid.x12",
            content=body,
            status=EDIFileStatus.GENERATED,
        )
        with self.assertRaisesRegex(ValueError, "N403|zip|pyx12"):
            queue_edi_file_upload(edi_file_id=edi_file.id)
        edi_file.refresh_from_db()
        self.assertEqual(edi_file.status, EDIFileStatus.GENERATED)
        self.assertFalse(EDIFileTransferLog.objects.filter(edi_file=edi_file).exists())

    def test_file_without_dmg_can_still_be_queued_preflight(self):
        """Subscriber DMG is optional — missing DMG alone must not block upload guard."""
        from apps.edi.tests_pyx12_preflight import _payload
        from apps.edi.utils.schema import build_edi_content, render_edi_file
        from apps.edi.utils.pyx12_preflight import assert_pyx12_valid

        segments = build_edi_content(_payload())
        segments = [s for s in segments if not s.startswith("DMG*")]
        start = next(i for i, s in enumerate(segments) if s.startswith("ST*"))
        end = next(i for i, s in enumerate(segments) if s.startswith("SE*"))
        segments[end] = f"SE*{end - start + 1}*0001~"
        # Should not raise — DMG is optional.
        assert_pyx12_valid(render_edi_file(segments))


class HcpfSftpPathSwapTests(TestCase):
    """
    Outgoing claims and incoming acknowledgments use separate MFT folders.
    """

    def test_path_constants_confirmed(self):
        from apps.edi.utils.upload import HCPF_837P_SEND_PATH, HCPF_ACK_RECEIVE_PATH

        confirmed = "Organizational/Incoming/fromedifecs/edifecs.stco.hosted"
        self.assertEqual(HCPF_837P_SEND_PATH, "Organizational/Outgoing/edifecs.stco.hosted/toedifecs")
        self.assertEqual(HCPF_ACK_RECEIVE_PATH, confirmed)
        self.assertNotEqual(HCPF_837P_SEND_PATH, HCPF_ACK_RECEIVE_PATH)

    def test_sync_updates_stale_edifecs_directory_paths(self):
        from apps.edi.models import SFTPCredentials, SFTPDirectory
        from apps.edi.utils.upload import (
            HCPF_837P_SEND_PATH,
            HCPF_ACK_RECEIVE_PATH,
            sync_hcpf_directory_paths,
        )

        partner = TradingPartner.objects.create(
            name="HCPF Path Test",
            sender_id="89513013",
            receiver_id="COMEDASSISTPROG",
            environment="PRODUCTION",
            is_active=True,
        )
        cred = SFTPCredentials.objects.create(
            name="HCPF-MFT-PATH-TEST",
            trading_partner=partner,
            environment="PRODUCTION",
            host="sftp.mft.edifecsfedcloud.com",
            port=22,
            username="user",
            auth_type="PASSWORD",
            password="secret",
            is_active=True,
        )
        directory = SFTPDirectory.objects.create(
            credentials=cred,
            name="stale outbound",
            purpose="OUTBOUND_837P",
            sending_path="Organizational/Incoming/fromedifecs/edifecs.stco.hosted",
            receiving_path="Organizational/Incoming/fromedifecs/edifecs.stco.hosted",
            is_active=True,
        )
        updated = sync_hcpf_directory_paths(credentials=cred)
        self.assertEqual(updated, 1)
        directory.refresh_from_db()
        self.assertEqual(directory.sending_path, HCPF_837P_SEND_PATH)
        self.assertEqual(directory.receiving_path, HCPF_ACK_RECEIVE_PATH)

    def test_sync_skips_non_edifecs_hosts(self):
        from apps.edi.models import SFTPCredentials, SFTPDirectory
        from apps.edi.utils.upload import sync_hcpf_directory_paths

        partner = TradingPartner.objects.create(
            name="Local Path Test",
            sender_id="TPLOCAL",
            receiver_id="RECV",
            environment="TEST",
            is_active=True,
        )
        cred = SFTPCredentials.objects.create(
            name="LOCAL-SFTP",
            trading_partner=partner,
            environment="TEST",
            host="127.0.0.1",
            port=22,
            username="user",
            auth_type="PASSWORD",
            password="secret",
            is_active=True,
        )
        directory = SFTPDirectory.objects.create(
            credentials=cred,
            name="local outbound",
            purpose="OUTBOUND_837P",
            sending_path="/send",
            receiving_path="/recv",
            is_active=True,
        )
        updated = sync_hcpf_directory_paths(credentials=cred)
        self.assertEqual(updated, 0)
        directory.refresh_from_db()
        self.assertEqual(directory.sending_path, "/send")
        self.assertEqual(directory.receiving_path, "/recv")
