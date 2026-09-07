from __future__ import annotations

import io
import json
from typing import Any

import pyx12.params
import pyx12.x12n_document


def validate_with_pyx12(raw_x12: str) -> dict[str, Any]:
    """Validate an 837P locally with pyx12 4.0.

    The generated local 999 is diagnostic only. It is NOT an HCPF
    acknowledgement and must never be treated as evidence of state receipt.
    """
    ack_stream = io.StringIO()
    json_stream = io.StringIO()
    source_stream = io.StringIO((raw_x12 or "").lstrip("\ufeff\r\n\t "))
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
    except Exception as exc:
        return {
            "valid": False,
            "local_999": ack_stream.getvalue() or None,
            "local_999_is_state_acknowledgment": False,
            "errors": {
                "validator_exception": {
                    "type": type(exc).__name__,
                    "message": str(exc),
                }
            },
        }

    structured: Any = None
    raw_json = json_stream.getvalue().strip()
    if raw_json:
        try:
            structured = json.loads(raw_json)
        except json.JSONDecodeError:
            structured = {"raw": raw_json}

    return {
        "valid": bool(valid),
        "local_999": ack_stream.getvalue() or None,
        "local_999_is_state_acknowledgment": False,
        "errors": structured,
    }


def assert_pyx12_valid(raw_x12: str) -> None:
    """Fail closed before an invalid 837P can be persisted or transmitted."""
    result = validate_with_pyx12(raw_x12)
    if result["valid"]:
        return

    # Do not include the raw X12 or local 999 in the exception: both may contain PHI.
    errors = result.get("errors")
    raise ValueError(f"pyx12 4.0 validation failed: {errors!r}")
