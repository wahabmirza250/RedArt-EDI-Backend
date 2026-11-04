from django.db import models

from apps.core.models import BaseModel
from apps.edi.choices import (
    AcknowledgementStatus,
    AcknowledgementType,
    EDI999ImportStatus,
    EDI835ImportStatus,
    EDIFileStatus,
    RemittanceClaimOutcome,
    SFTPAuthType,
    SFTPDirectoryPurpose,
    TransactionType,
    TransferChannel,
    TransferLogStatus,
    ValidationReportStatus,
    ValidationReportType,
)
from apps.trading_partner.choices import Environment


class EDIControlNumberQuerySet(models.QuerySet):
    def with_relations(self):
        return self.select_related(
            "batch",
            "batch__trading_partner",
        )


class EDIControlNumber(BaseModel):
    """
    X12 interchange / functional-group control numbers for a submission batch.
    Reconcile with BatchClaim.st02 (transaction set control number).
    """

    batch = models.ForeignKey(
        "claim.SubmissionBatch",
        on_delete=models.PROTECT,
        related_name="edi_control_numbers",
        null=True,
        blank=True,
    )
    environment = models.CharField(
        max_length=20,
        choices=Environment.choices,
        default=Environment.TEST,
    )
    isa13 = models.CharField(
        max_length=9,
        null=True,
        blank=True,
        help_text="ISA13 interchange control number (typically 9 digits).",
    )
    gs06 = models.CharField(
        max_length=9,
        null=True,
        blank=True,
        help_text="GS06 group control number.",
    )
    is_active = models.BooleanField(default=True)

    objects = EDIControlNumberQuerySet.as_manager()

    class Meta:
        verbose_name = "EDI Control Number"
        verbose_name_plural = "EDI Control Numbers"
        ordering = ("-id",)
        constraints = [
            models.UniqueConstraint(
                fields=["environment", "isa13"],
                condition=models.Q(
                    environment__isnull=False,
                    isa13__isnull=False,
                    is_active=True,
                ),
                name="uniq_active_isa13_per_environment",
            ),
            models.UniqueConstraint(
                fields=["environment", "gs06"],
                condition=models.Q(
                    environment__isnull=False,
                    gs06__isnull=False,
                    is_active=True,
                ),
                name="uniq_active_gs06_per_environment",
            ),
        ]
        indexes = [
            models.Index(
                fields=["environment", "is_active"],
                name="edi_ctrl_env_active_idx",
            ),
        ]

    def __str__(self):
        return f"ISA13={self.isa13} GS06={self.gs06} ({self.environment})"


class EDIFileQuerySet(models.QuerySet):
    def with_relations(self):
        return self.select_related(
            "batch",
            "batch__trading_partner",
            "control_number",
        )


class EDIFile(BaseModel):
    """
    Generated / uploaded X12 file for a batch.
    Transport status only — do not mirror Claim.status here.
    """

    batch = models.ForeignKey(
        "claim.SubmissionBatch",
        on_delete=models.PROTECT,
        related_name="edi_files",
        null=True,
        blank=True,
    )
    control_number = models.ForeignKey(
        EDIControlNumber,
        on_delete=models.SET_NULL,
        related_name="edi_files",
        null=True,
        blank=True,
    )
    transaction_type = models.CharField(
        max_length=16,
        choices=TransactionType.choices,
        default=TransactionType.X837P,
        null=True,
        blank=True,
    )
    filename = models.CharField(max_length=255, null=True, blank=True)
    file_hash = models.CharField(max_length=128, null=True, blank=True)
    path_or_blob_ref = models.CharField(max_length=1024, null=True, blank=True)
    content = models.TextField(null=True, blank=True)
    status = models.CharField(
        max_length=32,
        choices=EDIFileStatus.choices,
        default=EDIFileStatus.GENERATED,
        null=True,
        blank=True,
    )
    uploaded_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    objects = EDIFileQuerySet.as_manager()

    class Meta:
        verbose_name = "EDI File"
        verbose_name_plural = "EDI Files"
        ordering = ("-id",)
        constraints = [
            models.UniqueConstraint(
                fields=["filename"],
                condition=models.Q(filename__isnull=False),
                name="uniq_edi_file_filename_not_null",
            ),
        ]
        indexes = [
            models.Index(fields=["status"], name="edi_file_status_idx"),
            models.Index(
                fields=["batch", "transaction_type"],
                name="edi_file_batch_txn_idx",
            ),
        ]

    def __str__(self):
        return self.filename or f"EDIFile {self.pk}"


class EDIFileTransferLogQuerySet(models.QuerySet):
    def with_relations(self):
        return self.select_related(
            "edi_file",
            "edi_file__batch",
            "edi_file__batch__trading_partner",
        )


class EDIFileTransferLog(BaseModel):
    """
    Per-channel upload attempt trail (SFTP / S3) for FE and ops.
    One EDIFile can have many log rows across retries.
    """

    edi_file = models.ForeignKey(
        EDIFile,
        on_delete=models.CASCADE,
        related_name="transfer_logs",
    )
    channel = models.CharField(
        max_length=16,
        choices=TransferChannel.choices,
    )
    status = models.CharField(
        max_length=32,
        choices=TransferLogStatus.choices,
        default=TransferLogStatus.PENDING,
    )
    attempt = models.PositiveIntegerField(default=1)
    remote_path = models.CharField(max_length=1024, null=True, blank=True)
    message = models.CharField(max_length=500, null=True, blank=True)
    detail = models.TextField(null=True, blank=True)
    celery_task_id = models.CharField(max_length=255, null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    objects = EDIFileTransferLogQuerySet.as_manager()

    class Meta:
        verbose_name = "EDI File Transfer Log"
        verbose_name_plural = "EDI File Transfer Logs"
        ordering = ("-id",)
        indexes = [
            models.Index(
                fields=["edi_file", "channel"],
                name="edi_xfer_file_channel_idx",
            ),
            models.Index(
                fields=["status", "is_active"],
                name="edi_xfer_status_active_idx",
            ),
        ]

    def __str__(self):
        return f"{self.channel} {self.status} #{self.pk}"


class EDIAcknowledgementQuerySet(models.QuerySet):
    def with_relations(self):
        return self.select_related(
            "batch",
            "batch__trading_partner",
            "edi_file",
            "edi_file__batch",
        )


class EDIAcknowledgement(BaseModel):
    """
    Inbound acknowledgement (typically 999) for a submission batch / ST02.
    ACCEPTED means structural accept of the EDI — not payment.
    """

    batch = models.ForeignKey(
        "claim.SubmissionBatch",
        on_delete=models.PROTECT,
        related_name="edi_acknowledgements",
        null=True,
        blank=True,
    )
    edi_file = models.ForeignKey(
        EDIFile,
        on_delete=models.SET_NULL,
        related_name="acknowledgements",
        null=True,
        blank=True,
    )
    ack_type = models.CharField(
        max_length=16,
        choices=AcknowledgementType.choices,
        default=AcknowledgementType.X999,
    )
    status = models.CharField(
        max_length=32,
        choices=AcknowledgementStatus.choices,
        default=AcknowledgementStatus.ACCEPTED,
    )
    affected_st02 = models.CharField(
        max_length=16,
        null=True,
        blank=True,
        help_text="Transaction set control number (ST02) affected by this ack.",
    )
    raw_file_ref = models.CharField(max_length=1024, null=True, blank=True)
    message = models.CharField(max_length=500, null=True, blank=True)
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    objects = EDIAcknowledgementQuerySet.as_manager()

    class Meta:
        verbose_name = "EDI Acknowledgement"
        verbose_name_plural = "EDI Acknowledgements"
        ordering = ("-id",)
        indexes = [
            models.Index(
                fields=["batch", "ack_type"],
                name="edi_ack_batch_type_idx",
            ),
            models.Index(
                fields=["status", "is_active"],
                name="edi_ack_status_active_idx",
            ),
            models.Index(
                fields=["affected_st02"],
                name="edi_ack_st02_idx",
            ),
        ]

    def __str__(self):
        return f"{self.ack_type} {self.status} ST02={self.affected_st02} #{self.pk}"


class EDI999ImportQuerySet(models.QuerySet):
    def with_relations(self):
        return self.select_related(
            "credentials",
            "credentials__trading_partner",
            "directory",
            "acknowledgement",
            "acknowledgement__batch",
            "batch",
            "edi_file",
        )


class EDI999Import(BaseModel):
    """
    Track one inbound 999 file discovered on SFTP through import outcome.
    Idempotent on credentials + remote_path (and file_hash when set).
    """

    credentials = models.ForeignKey(
        "edi.SFTPCredentials",
        on_delete=models.PROTECT,
        related_name="edi_999_imports",
        null=True,
        blank=True,
    )
    directory = models.ForeignKey(
        "edi.SFTPDirectory",
        on_delete=models.SET_NULL,
        related_name="edi_999_imports",
        null=True,
        blank=True,
    )
    batch = models.ForeignKey(
        "claim.SubmissionBatch",
        on_delete=models.SET_NULL,
        related_name="edi_999_imports",
        null=True,
        blank=True,
    )
    edi_file = models.ForeignKey(
        EDIFile,
        on_delete=models.SET_NULL,
        related_name="edi_999_imports",
        null=True,
        blank=True,
    )
    acknowledgement = models.ForeignKey(
        EDIAcknowledgement,
        on_delete=models.SET_NULL,
        related_name="import_rows",
        null=True,
        blank=True,
    )
    filename = models.CharField(max_length=255)
    remote_path = models.CharField(max_length=1024)
    file_hash = models.CharField(max_length=128, null=True, blank=True)
    status = models.CharField(
        max_length=32,
        choices=EDI999ImportStatus.choices,
        default=EDI999ImportStatus.DISCOVERED,
    )
    attempt = models.PositiveIntegerField(default=0)
    celery_task_id = models.CharField(max_length=255, null=True, blank=True)
    message = models.CharField(max_length=500, null=True, blank=True)
    detail = models.TextField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    objects = EDI999ImportQuerySet.as_manager()

    class Meta:
        verbose_name = "EDI 999 Import"
        verbose_name_plural = "EDI 999 Imports"
        ordering = ("-id",)
        constraints = [
            models.UniqueConstraint(
                fields=["credentials", "remote_path"],
                condition=models.Q(
                    credentials__isnull=False,
                    remote_path__isnull=False,
                    is_active=True,
                ),
                name="uniq_active_edi_999_import_remote_path",
            ),
        ]
        indexes = [
            models.Index(
                fields=["status", "is_active"],
                name="edi_999_imp_status_active_idx",
            ),
            models.Index(
                fields=["file_hash"],
                name="edi_999_imp_hash_idx",
            ),
            models.Index(
                fields=["filename"],
                name="edi_999_imp_filename_idx",
            ),
        ]

    def __str__(self):
        return f"{self.filename} {self.status} #{self.pk}"


class EDI277ImportQuerySet(models.QuerySet):
    def with_relations(self):
        return self.select_related(
            "credentials",
            "credentials__trading_partner",
            "directory",
            "acknowledgement",
            "acknowledgement__batch",
            "batch",
            "edi_file",
        )


class EDI277Import(BaseModel):
    """
    Track one inbound 277 file discovered on SFTP through import outcome.
    Idempotent on credentials + remote_path (and file_hash when set).
    """

    credentials = models.ForeignKey(
        "edi.SFTPCredentials",
        on_delete=models.PROTECT,
        related_name="edi_277_imports",
        null=True,
        blank=True,
    )
    directory = models.ForeignKey(
        "edi.SFTPDirectory",
        on_delete=models.SET_NULL,
        related_name="edi_277_imports",
        null=True,
        blank=True,
    )
    batch = models.ForeignKey(
        "claim.SubmissionBatch",
        on_delete=models.SET_NULL,
        related_name="edi_277_imports",
        null=True,
        blank=True,
    )
    edi_file = models.ForeignKey(
        EDIFile,
        on_delete=models.SET_NULL,
        related_name="edi_277_imports",
        null=True,
        blank=True,
    )
    acknowledgement = models.ForeignKey(
        EDIAcknowledgement,
        on_delete=models.SET_NULL,
        related_name="import_277_rows",
        null=True,
        blank=True,
    )
    filename = models.CharField(max_length=255)
    remote_path = models.CharField(max_length=1024)
    file_hash = models.CharField(max_length=128, null=True, blank=True)
    status = models.CharField(
        max_length=32,
        choices=EDI999ImportStatus.choices,
        default=EDI999ImportStatus.DISCOVERED,
    )
    attempt = models.PositiveIntegerField(default=0)
    celery_task_id = models.CharField(max_length=255, null=True, blank=True)
    message = models.CharField(max_length=500, null=True, blank=True)
    detail = models.TextField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    objects = EDI277ImportQuerySet.as_manager()

    class Meta:
        verbose_name = "EDI 277 Import"
        verbose_name_plural = "EDI 277 Imports"
        ordering = ("-id",)
        constraints = [
            models.UniqueConstraint(
                fields=["credentials", "remote_path"],
                condition=models.Q(
                    credentials__isnull=False,
                    remote_path__isnull=False,
                    is_active=True,
                ),
                name="uniq_active_edi_277_import_remote_path",
            ),
        ]
        indexes = [
            models.Index(
                fields=["status", "is_active"],
                name="edi_277_imp_status_active_idx",
            ),
            models.Index(
                fields=["file_hash"],
                name="edi_277_imp_hash_idx",
            ),
            models.Index(
                fields=["filename"],
                name="edi_277_imp_filename_idx",
            ),
        ]

    def __str__(self):
        return f"{self.filename} {self.status} #{self.pk}"


class EDI835ImportQuerySet(models.QuerySet):
    def with_relations(self):
        return self.select_related(
            "credentials",
            "credentials__trading_partner",
            "directory",
            "remittance",
        )


class EDI835Import(BaseModel):
    """
    Track one inbound 835 file discovered on SFTP through import outcome.
    Idempotent on credentials + remote_path (and file_hash when set).
    """

    credentials = models.ForeignKey(
        "edi.SFTPCredentials",
        on_delete=models.PROTECT,
        related_name="edi_835_imports",
        null=True,
        blank=True,
    )
    directory = models.ForeignKey(
        "edi.SFTPDirectory",
        on_delete=models.SET_NULL,
        related_name="edi_835_imports",
        null=True,
        blank=True,
    )
    remittance = models.ForeignKey(
        "edi.EDI835Remittance",
        on_delete=models.SET_NULL,
        related_name="import_rows",
        null=True,
        blank=True,
    )
    filename = models.CharField(max_length=255)
    remote_path = models.CharField(max_length=1024)
    file_hash = models.CharField(max_length=128, null=True, blank=True)
    status = models.CharField(
        max_length=32,
        choices=EDI835ImportStatus.choices,
        default=EDI835ImportStatus.DISCOVERED,
    )
    attempt = models.PositiveIntegerField(default=0)
    celery_task_id = models.CharField(max_length=255, null=True, blank=True)
    message = models.CharField(max_length=500, null=True, blank=True)
    detail = models.TextField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    objects = EDI835ImportQuerySet.as_manager()

    class Meta:
        verbose_name = "EDI 835 Import"
        verbose_name_plural = "EDI 835 Imports"
        ordering = ("-id",)
        constraints = [
            models.UniqueConstraint(
                fields=["credentials", "remote_path"],
                condition=models.Q(
                    credentials__isnull=False,
                    remote_path__isnull=False,
                    is_active=True,
                ),
                name="uniq_active_edi_835_import_remote_path",
            ),
        ]
        indexes = [
            models.Index(
                fields=["status", "is_active"],
                name="edi_835_imp_status_active_idx",
            ),
            models.Index(fields=["file_hash"], name="edi_835_imp_hash_idx"),
            models.Index(fields=["filename"], name="edi_835_imp_filename_idx"),
        ]

    def __str__(self):
        return f"{self.filename} {self.status} #{self.pk}"


class SFTPCredentialsQuerySet(models.QuerySet):
    def with_relations(self):
        return self.select_related("trading_partner")


class SFTPCredentials(BaseModel):
    """
    SFTP/MFT login used to push 837P and pull acknowledgements.
    Secrets are write-only on APIs — never echo password/key material on GET.
    """

    name = models.CharField(max_length=255)
    trading_partner = models.ForeignKey(
        "trading_partner.TradingPartner",
        on_delete=models.PROTECT,
        related_name="sftp_credentials",
        null=True,
        blank=True,
    )
    environment = models.CharField(
        max_length=20,
        choices=Environment.choices,
        default=Environment.TEST,
    )
    host = models.CharField(max_length=255)
    port = models.PositiveIntegerField(default=22)
    username = models.CharField(max_length=255)
    auth_type = models.CharField(
        max_length=32,
        choices=SFTPAuthType.choices,
        default=SFTPAuthType.PASSWORD,
    )
    password = models.CharField(max_length=255, null=True, blank=True)
    private_key_pem = models.TextField(null=True, blank=True)
    private_key_passphrase = models.CharField(max_length=255, null=True, blank=True)
    host_fingerprint = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        help_text="Optional expected host key fingerprint.",
    )
    timeout_seconds = models.PositiveIntegerField(default=30)
    notes = models.CharField(max_length=500, null=True, blank=True)
    is_active = models.BooleanField(default=True)

    objects = SFTPCredentialsQuerySet.as_manager()

    class Meta:
        verbose_name = "SFTP Credentials"
        verbose_name_plural = "SFTP Credentials"
        ordering = ("-id",)
        constraints = [
            models.UniqueConstraint(
                fields=["name", "environment"],
                name="uniq_sftp_credentials_name_environment",
            ),
        ]
        indexes = [
            models.Index(
                fields=["environment", "is_active"],
                name="sftp_cred_env_active_idx",
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.environment})"


class SFTPDirectoryQuerySet(models.QuerySet):
    def with_relations(self):
        return self.select_related(
            "credentials",
            "credentials__trading_partner",
        )


class SFTPDirectory(BaseModel):
    """Remote folders for outbound claims and inbound acknowledgements."""

    credentials = models.ForeignKey(
        SFTPCredentials,
        on_delete=models.PROTECT,
        related_name="directories",
    )
    name = models.CharField(max_length=255, null=True, blank=True)
    purpose = models.CharField(
        max_length=32,
        choices=SFTPDirectoryPurpose.choices,
        default=SFTPDirectoryPurpose.GENERAL,
    )
    sending_path = models.CharField(
        max_length=500,
        help_text="Remote path for outbound uploads (837P).",
    )
    receiving_path = models.CharField(
        max_length=500,
        help_text="Remote path for inbound downloads (999/277/835).",
    )
    is_active = models.BooleanField(default=True)

    objects = SFTPDirectoryQuerySet.as_manager()

    class Meta:
        verbose_name = "SFTP Directory"
        verbose_name_plural = "SFTP Directories"
        ordering = ("-id",)
        constraints = [
            models.UniqueConstraint(
                fields=["credentials", "purpose", "sending_path", "receiving_path"],
                condition=models.Q(is_active=True),
                name="uniq_active_sftp_directory_paths",
            ),
        ]
        indexes = [
            models.Index(
                fields=["purpose", "is_active"],
                name="sftp_dir_purpose_active_idx",
            ),
        ]

    def __str__(self):
        return self.name or f"{self.purpose} #{self.pk}"


class EDI835RemittanceQuerySet(models.QuerySet):
    def with_relations(self):
        return self.prefetch_related("claim_payments", "claim_payments__claim")


class EDI835Remittance(BaseModel):
    """
    One inbound 835 Electronic Remittance Advice (ERA).
    Payment/denial is driven by CLP rows — never by 999.
    """

    file_hash = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        unique=True,
        help_text="SHA-256 of normalized content for idempotent re-import.",
    )
    raw_file_ref = models.CharField(max_length=1024, null=True, blank=True)
    isa13 = models.CharField(max_length=16, null=True, blank=True)
    gs06 = models.CharField(max_length=16, null=True, blank=True)
    st02 = models.CharField(max_length=16, null=True, blank=True)
    trace_number = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        help_text="TRN02 check / EFT trace number when present.",
    )
    payment_method = models.CharField(
        max_length=8,
        null=True,
        blank=True,
        help_text="BPR04 payment method code when present.",
    )
    total_payment = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="BPR02 total provider payment amount.",
    )
    payment_date = models.DateField(null=True, blank=True)
    message = models.CharField(max_length=500, null=True, blank=True)
    claim_line_count = models.PositiveIntegerField(default=0)
    applied_claim_count = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    objects = EDI835RemittanceQuerySet.as_manager()

    class Meta:
        verbose_name = "EDI 835 Remittance"
        verbose_name_plural = "EDI 835 Remittances"
        ordering = ("-id",)
        indexes = [
            models.Index(fields=["file_hash"], name="edi_835_file_hash_idx"),
            models.Index(
                fields=["trace_number", "is_active"],
                name="edi_835_trace_active_idx",
            ),
            models.Index(
                fields=["isa13", "is_active"],
                name="edi_835_isa13_active_idx",
            ),
        ]

    def __str__(self):
        return f"835 remittance #{self.pk} trace={self.trace_number}"


class EDI835ClaimPaymentQuerySet(models.QuerySet):
    def with_relations(self):
        return self.select_related("remittance", "claim")


class EDI835ClaimPayment(BaseModel):
    """
    One CLP claim payment/denial line from an 835.
    Links to Claim when claim_number matches an active claim.
    """

    remittance = models.ForeignKey(
        EDI835Remittance,
        on_delete=models.CASCADE,
        related_name="claim_payments",
    )
    claim = models.ForeignKey(
        "claim.Claim",
        on_delete=models.SET_NULL,
        related_name="edi_835_payments",
        null=True,
        blank=True,
    )
    claim_number = models.CharField(
        max_length=64,
        help_text="CLP01 patient control / claim number.",
    )
    clp_status_code = models.CharField(
        max_length=8,
        help_text="CLP02 claim status code (1=paid primary, 4=denied, …).",
    )
    outcome = models.CharField(
        max_length=32,
        choices=RemittanceClaimOutcome.choices,
        default=RemittanceClaimOutcome.IGNORED,
    )
    charge_amount = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )
    payment_amount = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )
    patient_responsibility = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )
    payer_claim_control = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        help_text="CLP07 payer claim control number when present.",
    )
    adjustment_codes = models.CharField(
        max_length=500,
        null=True,
        blank=True,
        help_text="Compact CAS group/reason codes from following CAS segments.",
    )
    prior_claim_status = models.CharField(max_length=32, null=True, blank=True)
    status_applied = models.BooleanField(
        default=False,
        help_text="True when Claim.status was updated from this line.",
    )
    skip_reason = models.CharField(max_length=255, null=True, blank=True)
    is_active = models.BooleanField(default=True)

    objects = EDI835ClaimPaymentQuerySet.as_manager()

    class Meta:
        verbose_name = "EDI 835 Claim Payment"
        verbose_name_plural = "EDI 835 Claim Payments"
        ordering = ("id",)
        constraints = [
            models.UniqueConstraint(
                fields=["remittance", "claim_number", "clp_status_code"],
                condition=models.Q(is_active=True),
                name="uniq_active_835_claim_line",
            ),
        ]
        indexes = [
            models.Index(
                fields=["claim_number", "is_active"],
                name="edi_835_clm_num_active_idx",
            ),
            models.Index(
                fields=["outcome", "status_applied"],
                name="edi_835_outcome_applied_idx",
            ),
        ]

    def __str__(self):
        return f"835 CLP {self.claim_number} {self.outcome} #{self.pk}"


class EDIValidationReportQuerySet(models.QuerySet):
    def with_relations(self):
        return self.select_related(
            "batch",
            "batch__trading_partner",
            "edi_file",
        )


class EDIValidationReport(BaseModel):
    """
    Edifecs STCO validation report (Summary / Audit / LDNS XML).
    Complements X12 999 for batch acceptance visibility.
    """

    batch = models.ForeignKey(
        "claim.SubmissionBatch",
        on_delete=models.SET_NULL,
        related_name="validation_reports",
        null=True,
        blank=True,
    )
    edi_file = models.ForeignKey(
        EDIFile,
        on_delete=models.SET_NULL,
        related_name="validation_reports",
        null=True,
        blank=True,
    )
    report_type = models.CharField(
        max_length=16,
        choices=ValidationReportType.choices,
        default=ValidationReportType.SUMMARY,
    )
    status = models.CharField(
        max_length=32,
        choices=ValidationReportStatus.choices,
        default=ValidationReportStatus.UNKNOWN,
    )
    task_id = models.CharField(max_length=128, null=True, blank=True)
    report_guid = models.CharField(max_length=64, null=True, blank=True)
    file_name = models.CharField(max_length=255, null=True, blank=True)
    file_hash = models.CharField(max_length=128, null=True, blank=True)
    error_count = models.PositiveIntegerField(default=0, null=True, blank=True)
    accepted_claims = models.PositiveIntegerField(null=True, blank=True)
    accepted_charge = models.DecimalField(
        max_digits=14, decimal_places=2, null=True, blank=True
    )
    raw_file_ref = models.CharField(max_length=1024, null=True, blank=True)
    message = models.CharField(max_length=500, null=True, blank=True)
    parsed_summary = models.JSONField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    objects = EDIValidationReportQuerySet.as_manager()

    class Meta:
        verbose_name = "EDI Validation Report"
        verbose_name_plural = "EDI Validation Reports"
        ordering = ("-id",)
        indexes = [
            models.Index(fields=["task_id"], name="edi_valrep_task_idx"),
            models.Index(
                fields=["report_type", "status"],
                name="edi_valrep_type_status_idx",
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["file_hash"],
                condition=models.Q(file_hash__isnull=False, is_active=True),
                name="uniq_active_validation_report_hash",
            ),
        ]

    def __str__(self):
        return f"{self.report_type} {self.status} task={self.task_id} #{self.pk}"
