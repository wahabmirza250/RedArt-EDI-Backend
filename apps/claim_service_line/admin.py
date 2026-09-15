from django.contrib import admin

from apps.claim_service_line.models import ClaimServiceLine


@admin.register(ClaimServiceLine)
class ClaimServiceLineAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "claim",
        "procedure_code",
        "modifier_1",
        "from_date",
        "to_date",
        "units",
        "mileage",
        "charge",
        "is_active",
        "created_at",
    )
    list_filter = ("is_active", "from_date", "created_at")
    search_fields = (
        "procedure_code",
        "modifier_1",
        "modifier_2",
        "claim__claim_number",
        "claim__external_id",
    )
    ordering = ("-id",)
    list_per_page = 50
    autocomplete_fields = ("claim",)
    readonly_fields = ("id", "created_at", "updated_at")
    fieldsets = (
        (
            None,
            {
                "fields": (
                    "id",
                    "claim",
                    "procedure_code",
                    "modifier_1",
                    "modifier_2",
                    "modifier_3",
                    "modifier_4",
                    "from_date",
                    "to_date",
                    "units",
                    "mileage",
                    "charge",
                    "is_active",
                )
            },
        ),
        ("Timestamps", {"fields": ("created_at", "updated_at")}),
    )
