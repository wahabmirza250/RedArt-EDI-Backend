from copy import deepcopy

import pytest

from apps.edi.tests_pyx12_preflight import _payload
from apps.edi.utils.schema import build_edi_content, render_edi_file
from apps.edi.utils.pyx12_preflight import validate_with_pyx12


@pytest.mark.parametrize("field,value", [("date_of_birth", ""), ("date_of_birth", "19800230"), ("gender", "")])
def test_generation_blocks_missing_or_invalid_demographics(field, value):
    payload = deepcopy(_payload())
    payload["claims"][0]["patient"][field] = value
    with pytest.raises(ValueError, match="Subscriber"):
        build_edi_content(payload)


def test_generation_blocks_missing_billing_postal_code():
    payload = _payload()
    payload["claims"][0]["provider"]["zip"] = ""
    with pytest.raises(ValueError, match="zip"):
        build_edi_content(payload)


def test_old_file_without_dmg_fails_even_if_generic_pyx12_allows_it():
    segments = build_edi_content(_payload())
    segments = [s for s in segments if not s.startswith("DMG*")]
    st = next(i for i, s in enumerate(segments) if s.startswith("ST*"))
    se = next(i for i, s in enumerate(segments) if s.startswith("SE*"))
    segments[se] = f"SE*{se - st + 1}*0001~"
    result = validate_with_pyx12(render_edi_file(segments))
    assert not result["valid"]
    assert any("DMG" in error for error in result["errors"]["colorado_required_data"])
    assert result["local_999_is_state_acknowledgment"] is False


def test_existing_file_without_provider_zip_is_blocked():
    body = render_edi_file(build_edi_content(_payload())).replace("N4*DENVER*CO*80202~", "N4*DENVER*CO~")
    result = validate_with_pyx12(body)
    assert not result["valid"]
    assert any("N403" in error for error in result["errors"]["colorado_required_data"])


def test_recorded_unknown_gender_is_preserved():
    payload = _payload()
    payload["claims"][0]["patient"]["gender"] = "U"
    body = render_edi_file(build_edi_content(payload))
    assert "DMG*D8*19800101*U~" in body
    assert validate_with_pyx12(body)["valid"]
