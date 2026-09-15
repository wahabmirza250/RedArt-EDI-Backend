from django.contrib import admin

from apps.provider_billing_profile.models import ProviderBillingProfile


@admin.register(ProviderBillingProfile)
class ProviderBillingProfileAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "legal_name",
        "billing_name",
        "npi",
        "location_id",
        "medicaid_provider_id",
        "revalidation_date",
        "city",
        "state",
        "is_active",
        "created_at",
        "updated_at",
    )
    list_filter = ("is_active", "state", "country", "revalidation_date", "created_at")
    search_fields = (
        "legal_name",
        "billing_name",
        "npi",
        "location_id",
        "medicaid_provider_id",
        "city",
        "email",
        "phone",
    )
    ordering = ("-id",)
    list_per_page = 50
    readonly_fields = ("id", "created_at", "updated_at")
    fieldsets = (
        (
            None,
            {
                "fields": (
                    "id",
                    "legal_name",
                    "billing_name",
                    "npi",
                    "taxonomy_code",
                    "location_id",
                    "medicaid_provider_id",
                    "revalidation_date",
                    "is_active",
                )
            },
        ),
        (
            "Address",
            {
                "fields": (
                    "address_line_1",
                    "address_line_2",
                    "city",
                    "state",
                    "zip",
                    "country",
                )
            },
        ),
        (
            "Pay-to address (2010AB)",
            {
                "fields": (
                    "pay_to_name",
                    "pay_to_address_line_1",
                    "pay_to_address_line_2",
                    "pay_to_city",
                    "pay_to_state",
                    "pay_to_zip",
                ),
                "description": (
                    "Optional. Used only when payment should go to a different "
                    "address than the physical/service address."
                ),
            },
        ),
        (
            "Contact",
            {"fields": ("phone", "email")},
        ),
        (
            "Timestamps",
            {"fields": ("created_at", "updated_at")},
        ),
    )
