from django.contrib import admin

from apps.nemt_trip.models import NemtTrip


@admin.register(NemtTrip)
class NemtTripAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "patient",
        "provider",
        "service_date",
        "pickup",
        "dropoff",
        "one_way_miles",
        "mileage_units",
        "driver_last_name",
        "charge",
        "is_active",
        "created_at",
    )
    list_filter = ("is_active", "service_date", "created_at")
    search_fields = (
        "pickup",
        "dropoff",
        "driver_first_name",
        "driver_last_name",
        "patient__first_name",
        "patient__last_name",
        "patient__medicaid_member_id",
        "provider__legal_name",
        "provider__billing_name",
        "provider__npi",
    )
    ordering = ("-id",)
    list_per_page = 50
    autocomplete_fields = ("patient", "provider")
    readonly_fields = ("id", "created_at", "updated_at")
    fieldsets = (
        (
            None,
            {
                "fields": (
                    "id",
                    "patient",
                    "provider",
                    "service_date",
                    "pickup",
                    "dropoff",
                    "one_way_miles",
                    "mileage_units",
                    "driver_first_name",
                    "driver_last_name",
                    "charge",
                    "is_active",
                )
            },
        ),
        (
            "Timestamps",
            {"fields": ("created_at", "updated_at")},
        ),
    )
