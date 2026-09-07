from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest import TestCase, mock

from apps.edi.utils.pyx12_validation import (
    validate_colorado_rules,
    validate_with_pyx12,
)
from apps.edi.utils.schema import build_edi_content, render_edi_file
from apps.edi.utils.sftp_client import EDI837PPreflightError, upload_bytes_via_sftp


TPID = "89513013"


def _generated_redart_837p() -> str:
    payload = {
        "environment": "TEST",
        "generated_at": datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc),
        "envelope": {
            "isa05": "ZZ",
            "isa07": "ZZ",
            "isa15": "T",
            "gs01": "HC",
            "gs08": "005010X222A1",
            "element_separator": "*",
            "component_separator": ":",
            "segment_terminator": "~",
            "repetition_separator": "^",
        },
        "trading_partner": {
            "id": 1,
            "name": "REDART TEST SUBMITTER",
            "sender_id": TPID,
            "receiver_id": "COMEDASSISTPROG",
            "contact_name": "EDI SUPPORT",
            "contact_phone": "7195550100",
        },
        "control": {
            "id": 1,
            "isa13": "000000001",
            "gs06": "1",
        },
        "claims": [
            {
                "claim_id": 1,
                "claim_number": "TEST0001",
                "st02": "0001",
                "diagnosis_code": "Z0289",
                "place_of_service": "41",
                "total_charge": "25.00",
                "patient": {
                    "first_name": "JANE",
                    "last_name": "DOE",
                    "date_of_birth": "19800101",
                    "gender": "F",
                    "medicaid_member_id": "A1234567",
                    "address_line_1": "100 TEST ST",
                    "city": "DENVER",
                    "state": "CO",
                    "zip": "80202",
                    "phone": "3035550100",
                },
                "provider": {
                    "legal_name": "TEST TRANSPORT LLC",
                    "billing_name": "TEST TRANSPORT LLC",
                    "is_atypical": False,
                    "npi": "1234567893",
                    "medicaid_provider_id": "",
                    "taxonomy_code": "343900000X",
                    "tax_id": "123456789",
                    "address_line_1": "200 TEST AVE",
                    "city": "DENVER",
                    "state": "CO",
                    "zip": "80203",
                    "phone": "3035550111",
                },
                "driver": {"first_name": "TEST", "last_name": "DRIVER"},
                "service_lines": [
                    {
                        "procedure_code": "T2003",
                        "from_date": "20260907",
                        "to_date": "20260907",
                        "units": 1,
                        "mileage": None,
                        "charge": "25.00",
                    }
                ],
            }
        ],
    }
    return render_edi_file(build_edi_content(payload))


class ColoradoPyx12PreflightTests(TestCase):
    def test_redart_generated_837p_passes_colorado_routing_rules(self):
        body = _generated_redart_837p()
        result = validate_colorado_rules(
            body,
            expected_tpid=TPID,
            expected_usage="T",
        )
        self.assertTrue(result["valid"], result["errors"])
        self.assertEqual(result["metadata"]["sender_tpid"], TPID)
        self.assertEqual(result["metadata"]["receiver_id"], "COMEDASSISTPROG")

    def test_redart_generated_837p_passes_pyx12_x222a1(self):
        body = _generated_redart_837p()
        result = validate_with_pyx12(body)
        self.assertTrue(
            result["valid"],
            "RedArt generated 837P failed pyx12 4.0 validation:\n"
            + json.dumps(result.get("errors"), indent=2, default=str),
        )
        self.assertFalse(result["local_999_is_state_acknowledgment"])
        self.assertIn("ST*999*", result.get("local_999") or "")

    def test_company_tpid_mismatch_is_blocked(self):
        body = _generated_redart_837p()
        result = validate_colorado_rules(
            body,
            expected_tpid="99999999",
            expected_usage="T",
        )
        self.assertFalse(result["valid"])
        codes = {item["code"] for item in result["errors"]}
        self.assertIn("CO-ISA06-TPID", codes)
        self.assertIn("CO-GS02-TPID", codes)

    @mock.patch("apps.edi.utils.sftp_client.open_sftp")
    def test_invalid_837p_is_blocked_before_sftp_network_connection(self, open_sftp):
        filename = "tp89513013-837P-20260907120000000-1of1.x12"
        with self.assertRaises(EDI837PPreflightError) as ctx:
            upload_bytes_via_sftp(
                credentials=object(),
                remote_dir="/outbound",
                filename=filename,
                data=b"not an x12 interchange",
            )
        open_sftp.assert_not_called()
        self.assertIn("file was not sent", str(ctx.exception))
