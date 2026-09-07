from __future__ import annotations

import os
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.claim.choices import AttachmentRoute, AttachmentStatus, BatchStatus, ClaimStatus
from apps.claim.models import BatchClaim, Claim, SubmissionBatch
from apps.claim_service_line.models import ClaimServiceLine
from apps.edi.choices import EDIFileStatus, TransactionType, TransferChannel
from apps.edi.models import EDIFile, EDIFileTransferLog
from apps.edi.utils.handler import Generate837PHandler
from apps.edi.utils.pyx12_preflight import validate_with_pyx12
from apps.edi.utils.required_claim_data import billing_address_errors, subscriber_errors
from apps.nemt_trip.models import NemtTrip
from apps.patient.models import Patient
from apps.provider_billing_profile.models import ProviderBillingProfile
from apps.trading_partner.choices import Environment
from apps.trading_partner.models import TradingPartner

TWOPLACES = Decimal("0.01")


def _env(name: str, *, required: bool = True, default: str = "") -> str:
    value = os.environ.get(name, default).strip()
    if required and not value:
        raise CommandError(f"Missing required environment variable: {name}")
    return value


def _int(name: str) -> int:
    try:
        return int(_env(name))
    except ValueError as exc:
        raise CommandError(f"{name} must be an integer") from exc


def _money(name: str) -> Decimal:
    try:
        return Decimal(_env(name)).quantize(TWOPLACES)
    except Exception as exc:
        raise CommandError(f"{name} must be a decimal number") from exc


def _date(name: str) -> date:
    try:
        return date.fromisoformat(_env(name))
    except ValueError as exc:
        raise CommandError(f"{name} must be YYYY-MM-DD") from exc


def _digits(value: str) -> str:
    return "".join(ch for ch in (value or "") if ch.isdigit())


class Command(BaseCommand):
    help = "Prepare and locally validate one explicitly authorized HCPF 837P; does not upload."

    def handle(self, *args, **options):
        if _env("OPS_REAL_SUBMIT_ENABLED") != "YES_ONE_HCPF_CLAIM":
            raise CommandError("One-shot guard is not enabled")

        marker = _env("OPS_EXTERNAL_MARKER")
        claim_number = _env("OPS_CLAIM_NUMBER")
        batch_number = _env("OPS_BATCH_NUMBER")
        provider = ProviderBillingProfile.objects.filter(pk=_int("OPS_PROVIDER_PROFILE_ID"), is_active=True).first()
        partner = TradingPartner.objects.filter(pk=_int("OPS_TRADING_PARTNER_ID"), is_active=True).first()
        expected_provider_id = _env("OPS_EXPECTED_MEDICAID_PROVIDER_ID")
        member_first = _env("OPS_MEMBER_FIRST")
        member_last = _env("OPS_MEMBER_LAST")
        member_id = _env("OPS_MEMBER_ID")
        service_date = _date("OPS_SERVICE_DATE")
        outbound = _int("OPS_OUTBOUND_MILES")
        return_miles = _int("OPS_RETURN_MILES")
        a0120_charge = _money("OPS_A0120_CHARGE")
        rate = _money("OPS_S0215_RATE")
        diagnosis = _env("OPS_DIAGNOSIS", default="R68.89")
        pos = _env("OPS_POS", default="41")
        tin = _digits(_env("OPS_BILLING_TIN"))

        if provider is None or not provider.is_atypical or (provider.npi or "").strip():
            raise CommandError("Provider atypical configuration mismatch")
        if (provider.medicaid_provider_id or "").strip() != expected_provider_id:
            raise CommandError("Provider Medicaid ID mismatch")
        if partner is None or (partner.environment or "").upper() != Environment.PRODUCTION:
            raise CommandError("Trading partner is not active PRODUCTION")
        if (partner.receiver_id or "").strip() != "COMEDASSISTPROG":
            raise CommandError("Trading partner receiver mismatch")
        if len(tin) != 9:
            raise CommandError("Billing TIN must contain exactly 9 digits")
        member_dob = _date("OPS_MEMBER_DOB")
        member_gender = _env("OPS_MEMBER_GENDER").upper()
        data_errors = subscriber_errors(member_dob, member_gender) + billing_address_errors({
            field: getattr(provider, field, None)
            for field in ("address_line_1", "city", "state", "zip")
        })
        if data_errors:
            raise CommandError("; ".join(data_errors))
        existing_patient = Patient.objects.filter(medicaid_member_id=member_id).first()
        if existing_patient is not None and (
            existing_patient.date_of_birth != member_dob
            or (existing_patient.gender or "").upper() != member_gender
        ):
            raise CommandError("Existing member demographics differ; verify and update the patient record before preparation")
        if outbound < 1 or return_miles < 0 or outbound > 52 or return_miles > 52:
            raise CommandError("Per-leg mileage must be between 0 and 52, with outbound at least 1")

        existing_tin = _digits(provider.tax_id or "")
        if existing_tin and existing_tin != tin:
            raise CommandError("Provider already has a different Tax ID")
        if not existing_tin:
            provider.tax_id = tin
            provider.save(update_fields=["tax_id", "updated_at"])

        total_miles = outbound + return_miles
        mileage_charge = (Decimal(total_miles) * rate).quantize(TWOPLACES, rounding=ROUND_HALF_UP)
        total_charge = (a0120_charge + mileage_charge).quantize(TWOPLACES)

        claim = Claim.objects.filter(external_id=marker, is_active=True).first()
        if claim is None:
            duplicate = Claim.objects.filter(
                is_active=True,
                trip__is_active=True,
                trip__service_date=service_date,
                trip__patient__medicaid_member_id__iexact=member_id,
            ).first()
            if duplicate is not None:
                raise CommandError("Duplicate claim protection triggered; no new claim prepared")

            with transaction.atomic():
                patient, created = Patient.objects.get_or_create(
                    medicaid_member_id=member_id,
                    defaults={
                        "first_name": member_first, "last_name": member_last,
                        "date_of_birth": member_dob, "gender": member_gender, "is_active": True,
                    },
                )
                if not created and (
                    (patient.first_name or "").strip().casefold() != member_first.casefold()
                    or (patient.last_name or "").strip().casefold() != member_last.casefold()
                ):
                    raise CommandError("Existing member ID has a different name")

                trip = NemtTrip.objects.create(
                    patient=patient,
                    provider=provider,
                    service_date=service_date,
                    pickup=_env("OPS_PICKUP", required=False) or None,
                    dropoff=_env("OPS_DROPOFF", required=False) or None,
                    one_way_miles=Decimal(outbound),
                    mileage_units=total_miles,
                    driver_first_name=_env("OPS_DRIVER_FIRST", required=False) or None,
                    driver_last_name=_env("OPS_DRIVER_LAST", required=False) or None,
                    charge=total_charge,
                    is_active=True,
                )
                claim = Claim.objects.create(
                    claim_number=claim_number,
                    external_id=marker,
                    trip=trip,
                    diagnosis_code=diagnosis,
                    place_of_service=pos,
                    total_charge=total_charge,
                    status=ClaimStatus.READY_FOR_837P,
                    attachment_required=False,
                    attachment_route=AttachmentRoute.NONE,
                    attachment_status=AttachmentStatus.NOT_REQUIRED,
                    is_active=True,
                )
                ClaimServiceLine.objects.create(
                    claim=claim, procedure_code="A0120", from_date=service_date, to_date=service_date,
                    units=2, charge=a0120_charge, is_active=True,
                )
                ClaimServiceLine.objects.create(
                    claim=claim, procedure_code="S0215", from_date=service_date, to_date=service_date,
                    units=total_miles, mileage=Decimal(total_miles), charge=mileage_charge, is_active=True,
                )
                batch = SubmissionBatch.objects.create(
                    batch_number=batch_number,
                    trading_partner=partner,
                    environment=Environment.PRODUCTION,
                    claim_count=1,
                    total_amount=total_charge,
                    status=BatchStatus.READY,
                    is_active=True,
                )
                BatchClaim.objects.create(batch=batch, claim=claim, st02="0001", is_active=True)
        else:
            row = BatchClaim.objects.select_related("batch").filter(claim=claim, is_active=True).order_by("-id").first()
            if row is None or row.batch is None:
                raise CommandError("Existing prepared claim has no batch")
            batch = row.batch

        edi_file = EDIFile.objects.filter(
            batch=batch, transaction_type=TransactionType.X837P, is_active=True
        ).order_by("-id").first()
        if edi_file is not None and EDIFileTransferLog.objects.filter(
            edi_file=edi_file, channel=TransferChannel.SFTP, is_active=True
        ).exists():
            raise CommandError("SFTP attempt already exists; preparation will not create a new file")
        if edi_file is None:
            edi_file, _payload, _body = Generate837PHandler(batch.id).generate()
        elif edi_file.status != EDIFileStatus.GENERATED:
            raise CommandError("Existing EDI file is not in GENERATED state")

        validation = validate_with_pyx12(edi_file.content or "")
        if not validation["valid"]:
            raise CommandError(f"pyx12 validation failed: {validation.get('errors')!r}")

        body = edi_file.content or ""
        required = (
            "COMEDASSISTPROG", "PI*CO_TXIX", f"REF*G2*{expected_provider_id}~",
            f"SV1*HC:A0120*{a0120_charge:.2f}*UN*2*{pos}",
            f"SV1*HC:S0215*{mileage_charge:.2f}*UN*{total_miles}*{pos}",
        )
        if any(item not in body for item in required):
            raise CommandError("Generated 837P failed safety assertions")
        if "*P*:~" not in body.split("~", 1)[0] + "~":
            raise CommandError("ISA15 is not production P")

        control = edi_file.control_number
        self.stdout.write(self.style.SUCCESS(
            "OPS_PREPARED "
            f"claim_id={claim.id} batch_id={batch.id} edi_file_id={edi_file.id} "
            f"filename={edi_file.filename} isa13={getattr(control, 'isa13', None)} "
            f"gs06={getattr(control, 'gs06', None)} total_miles={total_miles} "
            f"total_charge={total_charge:.2f} pyx12_valid=true"
        ))
