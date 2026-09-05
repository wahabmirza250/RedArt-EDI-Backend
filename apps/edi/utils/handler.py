"""
837P generate handler: load batch → payload dict → X12 schema → EDIFile on disk.
"""

from __future__ import annotations

import hashlib
import logging
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.db.models import Prefetch
from django.utils import timezone

from apps.claim.choices import BatchStatus, ClaimStatus
from apps.claim.models import BatchClaim, Claim
from apps.claim_service_line.models import ClaimServiceLine
from apps.edi.choices import EDIFileStatus, TransactionType
from apps.edi.models import EDIFile
from apps.edi.utils.envelope import get_edi_envelope_config
from apps.edi.utils.readiness import load_batch_for_837p
from apps.edi.utils.schema import build_edi_content, render_edi_file
from apps.edi.utils.service import allocate_control_numbers, build_colorado_837p_filename

logger = logging.getLogger(__name__)


def _money(value):
    if value is None:
        return "0"
    return f"{Decimal(value):.2f}"


def _date_ymd(value):
    if value is None:
        return ""
    return value.strftime("%Y%m%d")


class Generate837PHandler:
    """
    Constructor sets up batch, envelope, and control numbers.
    build_payload_dict() is pure JSON-shaped data.
    generate() writes the X12 file and EDIFile row (no upload).
    """

    def __init__(self, batch_id, *, allocate_controls=True):
        self.batch = load_batch_for_837p(batch_id)
        self.partner = self.batch.trading_partner
        self.environment = (self.batch.environment or self.partner.environment or "TEST").upper()
        self.envelope = get_edi_envelope_config(self.environment)
        self.generated_at = timezone.now()
        self.allocate_controls = allocate_controls
        self.control = None
        if allocate_controls:
            self.control, _ = allocate_control_numbers(
                batch_id=self.batch.id,
                environment=self.environment,
            )

        self.batch_claims = list(
            BatchClaim.objects.with_relations()
            .filter(batch_id=self.batch.id, is_active=True)
            .prefetch_related(
                Prefetch(
                    "claim__service_lines",
                    queryset=ClaimServiceLine.objects.filter(is_active=True).order_by(
                        "id"
                    ),
                )
            )
            .order_by("id")
        )

    def build_payload_dict(self):
        """Pure dict payload used by the X12 schema builder."""
        if self.control is None:
            self.control, _ = allocate_control_numbers(
                batch_id=self.batch.id,
                environment=self.environment,
            )

        claims = []
        for index, row in enumerate(self.batch_claims, start=1):
            claim = row.claim
            trip = claim.trip
            patient = trip.patient
            provider = trip.provider
            st02 = (row.st02 or f"{index:04d}").zfill(4)[:9]
            if not row.st02:
                row.st02 = st02
                row.save(update_fields=["st02", "updated_at"])

            lines = []
            line_total = Decimal("0")
            for line in claim.service_lines.all():
                line_charge = Decimal(line.charge if line.charge is not None else (trip.charge or 0))
                line_total += line_charge
                lines.append(
                    {
                        "procedure_code": line.procedure_code,
                        "from_date": _date_ymd(line.from_date or trip.service_date),
                        "to_date": _date_ymd(line.to_date or trip.service_date),
                        "units": line.units or trip.mileage_units or 1,
                        "mileage": str(line.mileage) if line.mileage is not None else None,
                        "charge": _money(line_charge),
                    }
                )

            # CLM02 must equal sum of SV1 amounts when service lines exist.
            total_charge = line_total if lines else Decimal(claim.total_charge or trip.charge or 0)

            claims.append(
                {
                    "claim_id": claim.id,
                    "claim_number": claim.claim_number or f"CLM{claim.id}",
                    "st02": st02,
                    "diagnosis_code": claim.diagnosis_code,
                    "place_of_service": claim.place_of_service,
                    "total_charge": _money(total_charge),
                    "patient": {
                        "first_name": patient.first_name,
                        "last_name": patient.last_name,
                        "date_of_birth": _date_ymd(patient.date_of_birth),
                        "gender": patient.gender,
                        "medicaid_member_id": patient.medicaid_member_id,
                        "address_line_1": patient.address_line_1,
                        "city": patient.city,
                        "state": patient.state,
                        "zip": patient.zip,
                        "phone": patient.phone,
                    },
                    "provider": {
                        "legal_name": provider.legal_name,
                        "billing_name": provider.billing_name,
                        # is_atypical controls NM108/NM109 in the 837P schema builder.
                        "is_atypical": bool(getattr(provider, "is_atypical", False)),
                        "npi": (provider.npi or "").strip(),
                        # Atypical providers use medicaid_provider_id as NM109 (NM108=XX).
                        "medicaid_provider_id": (
                            getattr(provider, "medicaid_provider_id", None) or ""
                        ).strip(),
                        "taxonomy_code": provider.taxonomy_code,
                        # tax_id (EIN/TIN) — from provider record only; never from
                        # settings or any default.  Validation rejects if missing.
                        "tax_id": (getattr(provider, "tax_id", None) or "").strip(),
                        "address_line_1": provider.address_line_1,
                        "city": provider.city,
                        "state": provider.state,
                        "zip": provider.zip,
                        "phone": provider.phone,
                    },
                    "driver": {
                        "first_name": getattr(trip, "driver_first_name", None) or "",
                        "last_name": getattr(trip, "driver_last_name", None) or "",
                    },
                    "service_lines": lines,
                }
            )

        return {
            "batch_id": self.batch.id,
            "batch_number": self.batch.batch_number,
            "environment": self.environment,
            "generated_at": self.generated_at,
            "envelope": self.envelope,
            "trading_partner": {
                "id": self.partner.id,
                "name": self.partner.name,
                "sender_id": self.partner.sender_id,
                "receiver_id": self.partner.receiver_id,
                "contact_name": getattr(self.partner, "contact_name", None) or "",
                "contact_phone": getattr(self.partner, "contact_phone", None) or "",
            },
            "control": {
                "id": self.control.id,
                "isa13": self.control.isa13,
                "gs06": self.control.gs06,
            },
            "claims": claims,
        }

    @transaction.atomic
    def generate(self):
        """Build X12, write local file, persist EDIFile as GENERATED."""
        payload = self.build_payload_dict()
        segments = build_edi_content(payload)
        body = render_edi_file(segments)
        raw = body.encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()

        filename = build_colorado_837p_filename(
            sender_id=self.partner.sender_id,
            generated_at=self.generated_at,
        )
        relative_dir = Path("edi") / "837p" / str(self.batch.id)
        abs_dir = Path(settings.MEDIA_ROOT) / relative_dir
        abs_dir.mkdir(parents=True, exist_ok=True)
        abs_path = abs_dir / filename
        abs_path.write_bytes(raw)

        relative_path = str(relative_dir / filename).replace("\\", "/")

        edi_file = EDIFile.objects.create(
            batch=self.batch,
            control_number=self.control,
            transaction_type=TransactionType.X837P,
            filename=filename,
            file_hash=digest,
            path_or_blob_ref=relative_path,
            content=body,
            status=EDIFileStatus.GENERATED,
            is_active=True,
        )

        if self.batch.status in (BatchStatus.DRAFT, BatchStatus.READY, None, ""):
            self.batch.status = BatchStatus.GENERATED
            self.batch.save(update_fields=["status", "updated_at"])

        # Advance claim status: READY_FOR_837P → EDI_GENERATED
        # "837P generated ≠ uploaded" — per client requirement.
        claim_ids = [
            bc.claim_id for bc in self.batch_claims if bc.claim_id is not None
        ]
        if claim_ids:
            from django.utils import timezone as _tz
            Claim.objects.filter(
                id__in=claim_ids,
                is_active=True,
                status__in=(
                    ClaimStatus.READY_FOR_837P,
                    ClaimStatus.DOCUMENTS_COMPLETE,
                ),
            ).update(status=ClaimStatus.EDI_GENERATED, updated_at=_tz.now())

        logger.info(
            "Generated 837P edi_file_id=%s batch_id=%s path=%s segments=%s sha256=%s",
            edi_file.id,
            self.batch.id,
            relative_path,
            len(segments),
            digest[:16],
        )
        return edi_file, payload, body
