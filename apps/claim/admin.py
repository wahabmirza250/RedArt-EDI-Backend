from django.contrib import admin

from apps.claim.models import (
    AttachmentSubmission,
    BatchClaim,
    Claim,
    ClaimDocument,
    SubmissionBatch,
)


@admin.register(Claim)
class ClaimAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "claim_number",
        "external_id",
        "trip",
        "diagnosis_code",
        "place_of_service",
        "total_charge",
        "status",
        "attachment_required",
        "attachment_status",
        "is_active",
        "created_at",
    )
    list_filter = (
        "status",
        "attachment_required",
        "attachment_status",
        "is_active",
        "created_at",
    )
    search_fields = (
        "claim_number",
        "external_id",
        "diagnosis_code",
        "trip__pickup",
        "trip__dropoff",
        "trip__patient__medicaid_member_id",
    )
    ordering = ("-id",)
    list_per_page = 50
    autocomplete_fields = ("trip",)
    readonly_fields = ("id", "created_at", "updated_at")
    fieldsets = (
        (
            None,
            {
                "fields": (
                    "id",
                    "claim_number",
                    "external_id",
                    "trip",
                    "diagnosis_code",
                    "place_of_service",
                    "total_charge",
                    "status",
                    "is_active",
                )
            },
        ),
        (
            "Attachments",
            {
                "fields": (
                    "attachment_required",
                    "attachment_route",
                    "attachment_status",
                )
            },
        ),
        ("Timestamps", {"fields": ("created_at", "updated_at")}),
    )


@admin.register(ClaimDocument)
class ClaimDocumentAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "claim",
        "document_type",
        "file_name",
        "document_hash",
        "blob_ref",
        "file_size",
        "is_signed",
        "status",
        "is_active",
        "created_at",
    )
    list_filter = ("document_type", "status", "is_signed", "is_active")
    search_fields = ("file_name", "document_hash", "claim__claim_number")
    ordering = ("-id",)
    autocomplete_fields = ("claim",)
    readonly_fields = ("id", "created_at", "updated_at")


@admin.register(SubmissionBatch)
class SubmissionBatchAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "batch_number",
        "trading_partner",
        "environment",
        "claim_count",
        "total_amount",
        "status",
        "is_active",
        "created_at",
    )
    list_filter = ("status", "environment", "is_active")
    search_fields = ("batch_number", "trading_partner__name")
    ordering = ("-id",)
    autocomplete_fields = ("trading_partner",)
    readonly_fields = (
        "id",
        "claim_count",
        "total_amount",
        "created_at",
        "updated_at",
    )


@admin.register(BatchClaim)
class BatchClaimAdmin(admin.ModelAdmin):
    list_display = ("id", "batch", "claim", "st02", "is_active", "created_at")
    list_filter = ("is_active",)
    search_fields = ("st02", "batch__batch_number", "claim__claim_number")
    ordering = ("-id",)
    autocomplete_fields = ("batch", "claim")
    readonly_fields = ("id", "created_at", "updated_at")


@admin.register(AttachmentSubmission)
class AttachmentSubmissionAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "claim",
        "channel",
        "submission_reference",
        "payload_hash",
        "status",
        "submitted_at",
        "confirmed_at",
        "is_active",
        "created_at",
    )
    list_filter = ("channel", "status", "is_active")
    search_fields = (
        "submission_reference",
        "notes",
        "claim__claim_number",
    )
    ordering = ("-id",)
    autocomplete_fields = ("claim",)
    readonly_fields = ("id", "created_at", "updated_at")
