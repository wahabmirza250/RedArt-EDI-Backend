"""HIPAA + Colorado preflight validation for outbound 837P files.

This module validates the exact X12 body before the SFTP transport is allowed
to send it to HCPF.  pyx12 performs the base 005010X222A1 implementation-guide
validation; the small Colorado layer below checks payer routing / companion
rules that a generic X12 validator cannot know.

IMPORTANT: pyx12 can generate a local 999 while validating.  That 999 is a
local diagnostic artifact only.  It MUST NEVER be persisted as an HCPF
acknowledgement or used to advance claim status.  Real HCPF TA1/999/277CA
responses still have to arrive through the configured trading-partner SFTP.
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass
from typing import Any

import pyx12.params
import pyx12.x12n_document


COLORADO_RECEIVER_ID = "COMEDASSISTPROG"
COLORADO_RECEIVER_NAME = "COLORADO MEDICAL ASSISTANCE PROGRAM"
COLORADO_PAYER_ID = "CO_TXIX"
IMPLEMENTATION_VERSION = "005010X222A1"


@dataclass(frozen=True)
class ParsedX12:
    element_separator: str
    component_separator: str
    segment_terminator: str
    segments: list[list[str]]


def _clean_x12(raw: str) -> str:
    return (raw or "").lstrip("\ufeff\r\n\t ")


def parse_x12(raw: str) -> ParsedX12:
    """Parse delimiters from the fixed-width ISA and split the interchange."""
    text = _clean_x12(raw)
    if not text.startswith("ISA"):
        raise ValueError("X12 document must begin with ISA.")
    if len(text) < 106:
        raise ValueError("X12 document is too short to contain a valid ISA envelope.")

    element_separator = text[3]
    component_separator = text[104]
    segment_terminator = text[105]

    if segment_terminator.isalnum() or segment_terminator in {" ", "\r", "\n", "\t"}:
        raise ValueError("ISA segment terminator is invalid or ISA is not fixed-width.")

    segments: list[list[str]] = []
    for raw_segment in text.split(segment_terminator):
        segment = raw_segment.strip("\r\n\t ")
        if segment:
            segments.append(segment.split(element_separator))

    return ParsedX12(
        element_separator=element_separator,
        component_separator=component_separator,
        segment_terminator=segment_terminator,
        segments=segments,
    )


def _first(parsed: ParsedX12, tag: str) -> list[str] | None:
    return next((seg for seg in parsed.segments if seg and seg[0] == tag), None)


def _all(parsed: ParsedX12, tag: str) -> list[list[str]]:
    return [seg for seg in parsed.segments if seg and seg[0] == tag]


def _nm1(parsed: ParsedX12, entity_code: str) -> list[list[str]]:
    return [
        seg
        for seg in parsed.segments
        if len(seg) > 1 and seg[0] == "NM1" and seg[1] == entity_code
    ]


def _value(segment: list[str] | None, index: int) -> str:
    if not segment or len(segment) <= index:
        return ""
    return (segment[index] or "").strip()


def _error(
    code: str,
    segment: str,
    field: str,
    message: str,
    *,
    expected: str | None = None,
    actual: str | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "code": code,
        "segment": segment,
        "field": field,
        "message": message,
    }
    if expected is not None:
        item["expected"] = expected
    if actual is not None:
        item["actual"] = actual
    return item


def validate_with_pyx12(raw_x12: str) -> dict[str, Any]:
    """Run pyx12 4.x and return validation result + locally generated 999."""
    ack_stream = io.StringIO()
    json_stream = io.StringIO()
    source_stream = io.StringIO(_clean_x12(raw_x12))
    param = pyx12.params.params()

    try:
        valid = pyx12.x12n_document.x12n_document(
            param=param,
            src_file=source_stream,
            fd_997=ack_stream,
            fd_html=None,
            fd_xmldoc=None,
            fd_json=json_stream,
            xslt_files=None,
        )
    except Exception as exc:  # pyx12 exposes several parser/map exception types.
        return {
            "valid": False,
            "errors": {
                "validator_exception": {
                    "type": type(exc).__name__,
                    "message": str(exc),
                }
            },
            "local_999": ack_stream.getvalue() or None,
            "local_999_is_state_acknowledgment": False,
        }

    structured_errors: Any = None
    raw_json = json_stream.getvalue().strip()
    if raw_json:
        try:
            structured_errors = json.loads(raw_json)
        except json.JSONDecodeError:
            structured_errors = {"raw": raw_json}

    return {
        "valid": bool(valid),
        "errors": structured_errors,
        "local_999": ack_stream.getvalue() or None,
        "local_999_is_state_acknowledgment": False,
    }


def validate_colorado_rules(
    raw_x12: str,
    *,
    expected_tpid: str,
    expected_usage: str,
) -> dict[str, Any]:
    """Apply Colorado Medicaid routing/companion checks after X12 parsing."""
    errors: list[dict[str, Any]] = []
    try:
        parsed = parse_x12(raw_x12)
    except ValueError as exc:
        return {
            "valid": False,
            "errors": [
                _error(
                    "CO-X12-ENVELOPE",
                    "ISA",
                    "envelope",
                    str(exc),
                )
            ],
            "metadata": {},
        }

    isa = _first(parsed, "ISA")
    gs = _first(parsed, "GS")
    st = _first(parsed, "ST")
    bht = _first(parsed, "BHT")

    isa_count = len(_all(parsed, "ISA"))
    if isa_count != 1:
        errors.append(
            _error(
                "CO-ISA-COUNT",
                "ISA",
                "ISA",
                "Colorado outbound 837P file must contain exactly one ISA interchange.",
                expected="1",
                actual=str(isa_count),
            )
        )

    if isa is None:
        errors.append(_error("CO-ISA-MISSING", "ISA", "ISA", "ISA segment is required."))
    else:
        for index, field, expected, code in (
            (5, "ISA05", "ZZ", "CO-ISA05"),
            (7, "ISA07", "ZZ", "CO-ISA07"),
            (8, "ISA08", COLORADO_RECEIVER_ID, "CO-ISA08"),
            (12, "ISA12", "00501", "CO-ISA12"),
        ):
            actual = _value(isa, index)
            if actual != expected:
                errors.append(
                    _error(
                        code,
                        "ISA",
                        field,
                        f"{field} does not match the Colorado Medicaid requirement.",
                        expected=expected,
                        actual=actual,
                    )
                )

        sender_tpid = _value(isa, 6)
        expected_tpid = (expected_tpid or "").strip()
        if not expected_tpid:
            errors.append(
                _error(
                    "CO-TPID-CONFIG-MISSING",
                    "ISA",
                    "ISA06",
                    "The submitting Trading Partner configuration has no sender TPID.",
                )
            )
        elif sender_tpid != expected_tpid:
            errors.append(
                _error(
                    "CO-ISA06-TPID",
                    "ISA",
                    "ISA06",
                    "ISA06 does not match the submitting company's configured Colorado TPID.",
                    expected=expected_tpid,
                    actual=sender_tpid,
                )
            )

        usage = _value(isa, 15)
        expected_usage = (expected_usage or "").strip().upper()
        if usage not in {"T", "P"}:
            errors.append(
                _error(
                    "CO-ISA15",
                    "ISA",
                    "ISA15",
                    "ISA15 must be T for test or P for production.",
                    expected="T or P",
                    actual=usage,
                )
            )
        elif expected_usage and usage != expected_usage:
            errors.append(
                _error(
                    "CO-ISA15-MODE",
                    "ISA",
                    "ISA15",
                    "ISA15 does not match the batch submission environment.",
                    expected=expected_usage,
                    actual=usage,
                )
            )

    if gs is None:
        errors.append(_error("CO-GS-MISSING", "GS", "GS", "GS segment is required."))
    else:
        for index, field, expected, code in (
            (1, "GS01", "HC", "CO-GS01"),
            (3, "GS03", COLORADO_RECEIVER_ID, "CO-GS03"),
            (8, "GS08", IMPLEMENTATION_VERSION, "CO-GS08"),
        ):
            actual = _value(gs, index)
            if actual != expected:
                errors.append(
                    _error(
                        code,
                        "GS",
                        field,
                        f"{field} does not match the Colorado Medicaid requirement.",
                        expected=expected,
                        actual=actual,
                    )
                )

        gs_sender = _value(gs, 2)
        expected_sender = (expected_tpid or _value(isa, 6)).strip()
        if gs_sender != expected_sender:
            errors.append(
                _error(
                    "CO-GS02-TPID",
                    "GS",
                    "GS02",
                    "GS02 must match the configured Colorado TPID used in ISA06.",
                    expected=expected_sender,
                    actual=gs_sender,
                )
            )

    if st is None:
        errors.append(_error("CO-ST-MISSING", "ST", "ST", "ST segment is required."))
    else:
        if _value(st, 1) != "837":
            errors.append(
                _error(
                    "CO-ST01",
                    "ST",
                    "ST01",
                    "Transaction set must be 837.",
                    expected="837",
                    actual=_value(st, 1),
                )
            )
        for txn_st in _all(parsed, "ST"):
            if _value(txn_st, 1) != "837" or _value(txn_st, 3) != IMPLEMENTATION_VERSION:
                errors.append(
                    _error(
                        "CO-ST03",
                        "ST",
                        "ST03",
                        "Every Colorado professional claim transaction must use 005010X222A1.",
                        expected=IMPLEMENTATION_VERSION,
                        actual=_value(txn_st, 3),
                    )
                )

    for txn_bht in _all(parsed, "BHT"):
        bht06 = _value(txn_bht, 6)
        if bht06 not in {"CH", "RP"}:
            errors.append(
                _error(
                    "CO-BHT06",
                    "BHT",
                    "BHT06",
                    "Colorado accepts CH for fee-for-service or RP for encounter claims.",
                    expected="CH or RP",
                    actual=bht06,
                )
            )
    if bht is None:
        errors.append(_error("CO-BHT-MISSING", "BHT", "BHT", "BHT segment is required."))

    receivers = _nm1(parsed, "40")
    if not receivers:
        errors.append(_error("CO-1000B-MISSING", "NM1", "1000B", "Receiver NM1*40 is required."))
    for receiver in receivers:
        if _value(receiver, 3) != COLORADO_RECEIVER_NAME:
            errors.append(
                _error(
                    "CO-1000B-NM103",
                    "NM1",
                    "NM103",
                    "Receiver name does not match Colorado Medicaid.",
                    expected=COLORADO_RECEIVER_NAME,
                    actual=_value(receiver, 3),
                )
            )
        if _value(receiver, 9) != COLORADO_RECEIVER_ID:
            errors.append(
                _error(
                    "CO-1000B-NM109",
                    "NM1",
                    "NM109",
                    "Receiver identifier does not match Colorado Medicaid.",
                    expected=COLORADO_RECEIVER_ID,
                    actual=_value(receiver, 9),
                )
            )

    subscribers = _nm1(parsed, "IL")
    if not subscribers:
        errors.append(_error("CO-2010BA-MISSING", "NM1", "2010BA", "Subscriber NM1*IL is required."))
    for subscriber in subscribers:
        qualifier = _value(subscriber, 8)
        member_id = _value(subscriber, 9)
        if qualifier != "MI":
            errors.append(
                _error(
                    "CO-2010BA-NM108",
                    "NM1",
                    "NM108",
                    "Colorado subscriber identification qualifier must be MI.",
                    expected="MI",
                    actual=qualifier,
                )
            )
        if not member_id:
            errors.append(
                _error(
                    "CO-2010BA-NM109",
                    "NM1",
                    "NM109",
                    "Colorado Medical Assistance Program Client ID is required.",
                )
            )

    payers = _nm1(parsed, "PR")
    if not payers:
        errors.append(_error("CO-2010BB-MISSING", "NM1", "2010BB", "Payer NM1*PR is required."))
    for payer in payers:
        if _value(payer, 8) != "PI":
            errors.append(
                _error(
                    "CO-2010BB-NM108",
                    "NM1",
                    "NM108",
                    "Colorado payer identification qualifier must be PI.",
                    expected="PI",
                    actual=_value(payer, 8),
                )
            )
        if _value(payer, 9) != COLORADO_PAYER_ID:
            errors.append(
                _error(
                    "CO-2010BB-NM109",
                    "NM1",
                    "NM109",
                    "Colorado Medicaid payer identifier must be CO_TXIX.",
                    expected=COLORADO_PAYER_ID,
                    actual=_value(payer, 9),
                )
            )

    for sbr in _all(parsed, "SBR"):
        filing_code = _value(sbr, 9)
        if filing_code not in {"MC", "16", "MA", "MB"}:
            errors.append(
                _error(
                    "CO-SBR09",
                    "SBR",
                    "SBR09",
                    "Use MC for standard Medicaid or an allowed Medicare crossover code.",
                    expected="MC, 16, MA, or MB",
                    actual=filing_code,
                )
            )

    if _all(parsed, "PWK"):
        errors.append(
            _error(
                "CO-PWK-UNSUPPORTED",
                "PWK",
                "PWK",
                "PWK is not supported by the current Colorado 837P companion guide.",
            )
        )

    clm_positions = [i for i, seg in enumerate(parsed.segments) if seg and seg[0] == "CLM"]
    for position_index, position in enumerate(clm_positions):
        clm = parsed.segments[position]
        clm05 = _value(clm, 5)
        components = clm05.split(parsed.component_separator) if clm05 else []
        frequency = components[2].strip() if len(components) > 2 else ""
        if frequency not in {"1", "7", "8"}:
            errors.append(
                _error(
                    "CO-CLM05-3",
                    "CLM",
                    "CLM05-3",
                    "Colorado recognizes claim frequency codes 1, 7, and 8.",
                    expected="1, 7, or 8",
                    actual=frequency,
                )
            )
            continue

        if frequency in {"7", "8"}:
            next_position = (
                clm_positions[position_index + 1]
                if position_index + 1 < len(clm_positions)
                else len(parsed.segments)
            )
            claim_segments = parsed.segments[position + 1 : next_position]
            has_payer_control = any(
                len(seg) > 2 and seg[0] == "REF" and seg[1] == "F8" and bool(seg[2].strip())
                for seg in claim_segments
            )
            if not has_payer_control:
                errors.append(
                    _error(
                        "CO-CLM-ADJUSTMENT-REF",
                        "REF",
                        "REF*F8",
                        "Claim frequency 7 or 8 requires the payer claim control number/Colorado ICN.",
                    )
                )

    metadata = {
        "implementation_version": _value(gs, 8) or _value(st, 3),
        "sender_tpid": _value(isa, 6),
        "receiver_id": _value(isa, 8),
        "usage": _value(isa, 15),
        "claim_count": len(clm_positions),
        "transaction_count": len(_all(parsed, "ST")),
    }
    return {"valid": not errors, "errors": errors, "metadata": metadata}


def validate_colorado_837p_preflight(
    raw_x12: str,
    *,
    expected_tpid: str,
    expected_usage: str,
) -> dict[str, Any]:
    """Return one fail-closed decision for the exact outbound X12 body."""
    pyx12_result = validate_with_pyx12(raw_x12)
    colorado_result = validate_colorado_rules(
        raw_x12,
        expected_tpid=expected_tpid,
        expected_usage=expected_usage,
    )
    return {
        "valid": bool(pyx12_result["valid"] and colorado_result["valid"]),
        "pyx12": pyx12_result,
        "colorado": colorado_result,
    }


def compact_preflight_error(result: dict[str, Any], *, max_codes: int = 12) -> str:
    """Build a PHI-free transfer-log summary suitable for production logs."""
    parts: list[str] = []
    pyx12_result = result.get("pyx12") or {}
    colorado_result = result.get("colorado") or {}

    if not pyx12_result.get("valid"):
        pyx12_errors = pyx12_result.get("errors") or {}
        exception = pyx12_errors.get("validator_exception") if isinstance(pyx12_errors, dict) else None
        if exception:
            parts.append(f"pyx12={exception.get('type', 'validator_exception')}")
        else:
            parts.append("pyx12=invalid")

    state_errors = colorado_result.get("errors") or []
    codes = [str(item.get("code")) for item in state_errors if item.get("code")]
    if codes:
        shown = codes[:max_codes]
        suffix = f"+{len(codes) - len(shown)}" if len(codes) > len(shown) else ""
        parts.append("colorado=" + ",".join(shown) + suffix)

    return "; ".join(parts) or "preflight validation failed"
