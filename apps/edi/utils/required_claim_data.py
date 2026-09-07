"""Required data for the self-subscriber Colorado 837P flow.

Edifecs rejects a missing 2010BA DMG when SBR02 is 18, even when
the generic pyx12 map allows that situational segment to be absent.
"""

from datetime import date, datetime
import re


def subscriber_errors(dob, gender):
    errors = []
    if not dob:
        errors.append("Subscriber date_of_birth is required for 2010BA DMG02 (SBR02=18).")
    else:
        try:
            if not isinstance(dob, date) and not re.fullmatch(r"[0-9]{8}", str(dob)):
                raise ValueError()
            value = dob if isinstance(dob, date) else datetime.strptime(str(dob), "%Y%m%d").date()
            if value > date.today():
                raise ValueError()
        except (ValueError, TypeError):
            errors.append("Subscriber date_of_birth must be a valid, non-future date.")
    if (gender or "").strip().upper() not in {"M", "F", "U"}:
        errors.append("Subscriber gender is required for DMG03: M, F, or explicitly recorded U (unknown).")
    return errors


def billing_address_errors(provider):
    errors = []
    for field in ("address_line_1", "city", "state", "zip"):
        if not str(provider.get(field) or "").strip():
            errors.append(f"Billing provider {field} is required for 2010AA N3/N4.")
    postal = str(provider.get("zip") or "").strip()
    if postal and not re.fullmatch(r"[0-9]{5}(?:[0-9]{4}|-[0-9]{4})?", postal):
        errors.append("Billing provider zip must be a real 5-digit ZIP or ZIP+4; never pad or invent digits.")
    return errors


def x12_required_data_errors(raw):
    """Apply the receiver's situational checks to the exact bytes to send."""
    text = (raw or "").lstrip("\ufeff\r\n\t ")
    if not text.startswith("ISA") or len(text) < 106:
        return []  # pyx12 reports malformed envelopes.
    separator, terminator = text[3], text[105]
    errors = []
    loop = None
    self_subscriber = False
    has_dmg = False
    has_billing_n4 = False
    for segment in text.split(terminator):
        fields = segment.strip().split(separator)
        tag = fields[0]
        value = lambda index: fields[index] if len(fields) > index else ""
        if tag == "ST":
            self_subscriber = False
            has_dmg = False
            has_billing_n4 = False
            loop = None
        if tag == "NM1":
            if loop == "85" and not has_billing_n4:
                errors.append("Billing provider 2010AA N4 is missing.")
            loop = value(1)
        if tag == "N4" and loop == "85":
            has_billing_n4 = True
            if not value(3).strip():
                errors.append("Billing provider zip is missing in 2010AA N403.")
        if tag == "SBR":
            self_subscriber = self_subscriber or value(2) == "18"
        if tag == "DMG" and loop == "IL":
            has_dmg = True
            errors.extend(subscriber_errors(value(2), value(3)))
        if tag == "SE" and self_subscriber and not has_dmg:
            errors.append("2010BA DMG is required when 2000B SBR02=18; supply verified subscriber demographics.")
    return errors
