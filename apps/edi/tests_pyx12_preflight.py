from datetime import datetime, timezone

from apps.edi.utils.pyx12_preflight import validate_with_pyx12
from apps.edi.utils.schema import build_edi_content, render_edi_file


def _payload(*, tax_id="123456789", outbound=50, return_miles=50):
    total_miles = outbound + return_miles
    mileage_charge = total_miles * 2.74
    return {
        "envelope": {
            "isa05": "ZZ",
            "isa07": "ZZ",
            "isa15": "P",
            "gs01": "HC",
            "gs08": "005010X222A1",
            "element_separator": "*",
            "component_separator": ":",
            "segment_terminator": "~",
            "repetition_separator": "^",
        },
        "trading_partner": {
            "name": "TEST EDI SUBMITTER",
            "sender_id": "12345678",
            "receiver_id": "COMEDASSISTPROG",
            "contact_name": "EDI TEST",
            "contact_phone": "3035550100",
        },
        "control": {"isa13": "123456789", "gs06": "123456789"},
        "generated_at": datetime(2026, 9, 7, 20, 30, tzinfo=timezone.utc),
        "claims": [
            {
                "claim_id": 1,
                "claim_number": "TESTCLAIM1",
                "st02": "0001",
                "diagnosis_code": "R68.89",
                "place_of_service": "41",
                "total_charge": f"{24.30 + mileage_charge:.2f}",
                "patient": {
                    "first_name": "Jane",
                    "last_name": "Doe",
                    "date_of_birth": "",
                    "gender": "",
                    "medicaid_member_id": "A1234567",
                    "address_line_1": "",
                    "city": "",
                    "state": "",
                    "zip": "",
                    "phone": "",
                },
                "provider": {
                    "legal_name": "TEST TRANSPORTATION LLC",
                    "billing_name": "TEST TRANSPORTATION LLC",
                    "is_atypical": True,
                    "npi": "",
                    "medicaid_provider_id": "9000000001",
                    "taxonomy_code": "",
                    "tax_id": tax_id,
                    "address_line_1": "100 TEST AVE",
                    "city": "DENVER",
                    "state": "CO",
                    "zip": "80202",
                    "phone": "",
                },
                "driver": {"first_name": "", "last_name": ""},
                "service_lines": [
                    {
                        "procedure_code": "A0120",
                        "from_date": "20260821",
                        "to_date": "20260821",
                        "units": 2,
                        "mileage": None,
                        "charge": "24.30",
                    },
                    {
                        "procedure_code": "S0215",
                        "from_date": "20260821",
                        "to_date": "20260821",
                        "units": total_miles,
                        "mileage": str(total_miles),
                        "charge": f"{mileage_charge:.2f}",
                    },
                ],
            }
        ],
    }


def test_atypical_provider_shape_passes_pyx12():
    x12 = render_edi_file(build_edi_content(_payload()))
    result = validate_with_pyx12(x12)
    assert result["valid"] is True, result["errors"]

    assert "NM1*85*2*TEST TRANSPORTATION LLC~" in x12
    assert "REF*EI*123456789~" in x12
    assert "NM1*PR*2*COLORADO MEDICAL ASSISTANCE PROGRAM*****PI*CO_TXIX~" in x12
    assert x12.index("REF*G2*9000000001~") > x12.index("NM1*PR*")
    assert "NM1*85*2*TEST TRANSPORTATION LLC*****XX*9000000001" not in x12
    assert "SV1*HC:S0215*274.00*UN*100*41**1~" in x12
    assert "CLM*TESTCLAIM1*298.30***41:B:1*Y*A*Y*Y~" in x12


def test_atypical_provider_without_real_tax_id_is_blocked():
    try:
        build_edi_content(_payload(tax_id=""))
    except ValueError as exc:
        assert "real 9-digit billing Tax ID/EIN" in str(exc)
    else:
        raise AssertionError("Missing atypical Tax ID must block generation")


def test_52_plus_52_round_trip_x12_is_structurally_valid():
    x12 = render_edi_file(build_edi_content(_payload(outbound=52, return_miles=52)))
    result = validate_with_pyx12(x12)
    assert result["valid"] is True, result["errors"]
    assert "SV1*HC:S0215*284.96*UN*104*41**1~" in x12
