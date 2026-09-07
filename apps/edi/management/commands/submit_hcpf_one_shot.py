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
from apps.edi.utils.upload import queue_edi_file_upload, run_edi_file_upload
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


def _int_env(name: str) -> int:
    raw = _env(name)
    try:
        return int(raw)
    except ValueError as exc:
        raise CommandError(f"{name} must be an integer") from exc


def _decimal_env(name: str) -> Decimal:
    raw = _env(name)
    try:
        return Decimal(raw)
    except Exception as exc:
        raise CommandError(f"{name} must be a decimal number") from exc


def _date_env(name: str) -> date:
    raw = _env(name)
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise CommandError(f"{name} must be YYYY-MM-DD") from exc


def _digits(value: str) -> str:
    return "".join(ch for ch in (value or "") if ch.isdigit())


class Command(BaseCommand):
    help = (
        "Submit exactly one explicitly-authorized Colorado HCPF 837P claim. "
        "This command is guarded and idempotent: after any SFTP attempt is "
        "recorded it will refuse to resend automatically."
    )

    def handle(self, *args, **options):
        guard = _env("OPS_REAL_SUBMIT_ENABLED")
        if guard != "YES_ONE_HCPF_CLAIM":
            raise CommandError(
                "Real submission guard is not enabled. Set "
                "OPS_REAL_SUBMIT_ENABLED=YES_ONE_HCPF_CLAIM only for the "
                "single approved production run."
            )

        marker = _env("OPS_EXTERNAL_MARKER")
        claim_number = _env("OPS_CLAIM_NUMBER")
        batch_number = _env("OPS_BATCH_NUMBER")
        provider_id = _int_env("OPS_PROVIDER_PROFILE_ID")
        partner_id = _int_env("OPS_TRADING_PARTNER_ID")
        expected_provider_id = _env("OPS_EXPECTED_MEDICAID_PROVIDER_ID")
        member_first = _env("OPS_MEMBER_FIRST")
        member_last = _env("OPS_MEMBER_LAST")
        member_id = _env("OPS_MEMBER_ID")
        service_date = _date_env("OPS_SERVICE_DATE")
        outbound_miles = _int_env("OPS_OUTBOUND_MILES")
        return_miles = _int_env("OPS_RETURN_MILES")
        a0120_charge = _decimal_env("OPS_A0120_CHARGE").quantize(TWOPLACES)
        mileage_rate = _decimal_env("OPS_S0215_RATE").quantize(TWOPLACES)
        diagnosis = _env("OPS_DIAGNOSIS", default="R68.89")
        pos = _env("OPS_POS", default="41")
        billing_tin = _digits(_env("OPS_BILLING_TIN"))
        driver_first = _env("OPS_DRIVER_FIRST", required=False)
        driver_last = _env("OPS_DRIVER_LAST", required=False)
        pickup = _env("OPS_PICKUP", required=False)
        dropoff = _env("OPS_DROPOFF", required=False)

        if len(billing_tin) != 9:
            raise CommandError("OPS_BILLING_TIN must contain exactly 9 digits")
        if outbound_miles < 1 or return_miles < 0:
            raise CommandError("Mileage must be positive")
        if outbound_miles > 52 or return_miles > 52:
            raise CommandError(
                "Submission blocked: no individual trip leg may exceed 52 miles."
            )

        total_miles = outbound_miles + return_miles
        mileage_charge = (Decimal(total_miles) * mileage_rate).quantize(
            TWOPLACES, rounding=ROUND_HALF_UP
        )
        total_charge = (a0120_charge + mileage_charge).quantize(TWOPLACES)

        provider = ProviderBillingProfile.objects.filter(
            pk=provider_id, is_active=True
        ).first()
        if provider is None:
            raise CommandError("Configured provider profile not found or inactive")
        if not provider.is_atypical:
            raise CommandError("Configured provider is not marked atypical")
        if (provider.npi or "").strip():
            raise CommandError("Atypical provider unexpectedly has an NPI; refusing send")
        actual_provider_id = (provider.medicaid_provider_id or "").strip()
        if actual_provider_id != expected_provider_id:
            raise CommandError("Configured provider Medicaid ID does not match expected value")

        partner = TradingPartner.objects.filter(pk=partner_id, is_active=True).first()
        if partner is None:
            raise CommandError("Configured trading partner not found or inactive")
        if (partner.environment or "").upper() != Environment.PRODUCTION:
            raise CommandError("Trading partner is not configured for PRODUCTION")
        if (partner.receiver_id or "").strip() != "COMEDASSISTPROG":
            raise CommandError("Trading partner receiver is not COMEDASSISTPROG")
        if not (partner.sender_id or "").strip():
            raise CommandError("Trading partner sender TPID is missing")

        existing_tin = _digits(provider.tax_id or "")
        if existing_tin and existing_tin != billing_tin:
            raise CommandError(
                "Provider already has a different Tax ID; refusing to overwrite it"
            )
        if not existing_tin:
            provider.tax_id = billing_tin
            provider.save(update_fields=["tax_id", "updated_at"])

        # Strong duplicate check before creating anything. The explicit marker is
        # our idempotency key, while member + DOS catches an independently-created
        # duplicate claim for the same bill.
        existing_claim = Claim.objects.filter(
            external_id=marker, is_active=True
        ).first()
        if existing_claim is None:
            duplicate = (
                Claim.objects.filter(
                    is_active=True,
                    trip__is_active=True,
                    trip__service_date=service_date,
                    trip__patient__medicaid_member_id__iexact=member_id,
                )
                .exclude(external_id=marker)
                .first()
            )
            if duplicate is not None:
                raise CommandError(
                    "Duplicate protection: another active Django claim already "
                    "exists for this member and service date. No send attempted."
                )

            with transaction.atomic():
                patient, created = Patient.objects.get_or_create(
                    medicaid_member_id=member_id,
                    defaults={
                        "first_name": member_first,
                        "last_name": member_last,
                        "is_active": True,
                    },
                )
                if not created:
                    if (
                        (patient.first_name or "").strip().casefold()
                        != member_first.casefold()
                        or (patient.last_name or "").strip().casefold()
                        != member_last.casefold()
                    ):
                        raise CommandError(
                            "Existing Medicaid member ID has a different name; refusing send"
                        )

                trip = NemtTrip.objects.create(
                    patient=patient,
                    provider=provider,
                    service_date=service_date,
                    pickup=pickup or None,
                    dropoff=dropoff or None,
                    one_way_miles=Decimal(outbound_miles),
                    mileage_units=total_miles,
                    driver_first_name=driver_first or None,
                    driver_last_name=driver_last or None,
                    charge=total_charge,
                    is_active=True,
                )

                existing_number = Claim.objects.filter(
                    claim_number=claim_number, is_active=True
                ).exists()
                if existing_number:
                    raise CommandError("OPS_CLAIM_NUMBER is already in use")

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
                    claim=claim,
                    procedure_code="A0120",
                    from_date=service_date,
                    to_date=service_date,
                    units=2,
                    mileage=None,
                    charge=a0120_charge,
                    is_active=True,
                )
                ClaimServiceLine.objects.create(
                    claim=claim,
                    procedure_code="S0215",
                    from_date=service_date,
                    to_date=service_date,
                    units=total_miles,
                    mileage=Decimal(total_miles),
                    charge=mileage_charge,
                    is_active=True,
                )

                if SubmissionBatch.objects.filter(batch_number=batch_number).exists():
                    raise CommandError("OPS_BATCH_NUMBER is already in use")
                batch = SubmissionBatch.objects.create(
                    batch_number=batch_number,
                    trading_partner=partner,
                    environment=Environment.PRODUCTION,
                    claim_count=1,
                    total_amount=total_charge,
                    status=BatchStatus.READY,
                    is_active=True,
                )
                BatchClaim.objects.create(
                    batch=batch,
                    claim=claim,
                    st02="0001",
                    is_active=True,
                )
        else:
            row = (
                BatchClaim.objects.select_related("batch")
                .filter(claim=existing_claim, is_active=True)
                .order_by("-id")
                .first()
            )
            if row is None or row.batch is None:
                raise CommandError(
                    "Existing one-shot claim has no tracked batch; manual review required"
                )
            claim = existing_claim
            batch = row.batch

        if batch.environment != Environment.PRODUCTION:
            raise CommandError("One-shot batch is not PRODUCTION; refusing upload")
        if batch.trading_partner_id != partner.id:
            raise CommandError("One-shot batch trading partner mismatch")

        edi_file = (
            EDIFile.objects.filter(
                batch=batch,
                transaction_type=TransactionType.X837P,
                is_active=True,
            )
            .order_by("-id")
            .first()
        )

        if edi_file is not None:
            prior_sftp = (
                EDIFileTransferLog.objects.filter(
                    edi_file=edi_file,
                    channel=TransferChannel.SFTP,
                    is_active=True,
                )
                .order_by("-id")
                .first()
            )
            if prior_sftp is not None:
                control = edi_file.control_number
                self.stdout.write(
                    "OPS_NO_RESEND "
                    f"claim_id={claim.id} batch_id={batch.id} edi_file_id={edi_file.id} "
                    f"edi_status={edi_file.status} sftp_status={prior_sftp.status} "
                    f"attempt={prior_sftp.attempt} isa13={getattr(control, 'isa13', None)} "
                    f"gs06={getattr(control, 'gs06', None)}"
                )
                return
            if edi_file.status != EDIFileStatus.GENERATED:
                raise CommandError(
                    f"Existing EDI file is status={edi_file.status}; manual review required"
                )
        else:
            handler = Generate837PHandler(batch.id)
            edi_file, _payload, _body = handler.generate()

        validation = validate_with_pyx12(edi_file.content or "")
        if not validation["valid"]:
            raise CommandError(
                "pyx12 4.0 rejected generated 837P; SFTP upload blocked. "
                f"errors={validation.get('errors')!r}"
            )

        body = edi_file.content or ""
        required_markers = (
            "COMEDASSISTPROG",
            "PI*CO_TXIX",
            f"REF*G2*{expected_provider_id}~",
            f"SV1*HC:A0120*{a0120_charge:.2f}*UN*2*{pos}",
            f"SV1*HC:S0215*{mileage_charge:.2f}*UN*{total_miles}*{pos}",
        )
        missing = [marker_text for marker_text in required_markers if marker_text not in body]
        if missing:
            raise CommandError(
                "Generated 837P failed one-shot safety assertions; upload blocked"
            )
        if "*P*:~" not in body.split("~", 1)[0] + "~":
            raise CommandError("Generated ISA15 is not production P; upload blocked")

        self.stdout.write(
            "OPS_PREUPLOAD_OK "
            f"claim_id={claim.id} batch_id={batch.id} edi_file_id={edi_file.id} "
            f"filename={edi_file.filename} pyx12_valid=true total_miles={total_miles} "
            f"total_charge={total_charge:.2f}"
        )

        queued_file, attempt, _sftp_log, _s3_log = queue_edi_file_upload(
            edi_file_id=edi_file.id,
            async_mode=False,
        )
        result = run_edi_file_upload(
            edi_file_id=queued_file.id,
            attempt=attempt,
            task_id="ops-one-shot",
        )

        queued_file.refresh_from_db()
        control = queued_file.control_number
        if queued_file.status != EDIFileStatus.UPLOADED:
            raise CommandError(
                "SFTP upload did not complete successfully. No automatic retry will occur. "
                f"edi_file_id={queued_file.id} attempt={attempt} status={queued_file.status}"
            )

        self.stdout.write(
            self.style.SUCCESS(
                "OPS_SFTP_UPLOADED "
                f"claim_id={claim.id} batch_id={batch.id} edi_file_id={queued_file.id} "
                f"attempt={attempt} filename={queued_file.filename} "
                f"isa13={getattr(control, 'isa13', None)} "
                f"gs06={getattr(control, 'gs06', None)} "
                f"remote_path={result.get('sftp_path')}"
            )
        )
        self.stdout.write(
            "OPS_NEXT_WAIT_FOR_HCPF_999 local_pyx12_999_is_not_state_ack=true"
        )
