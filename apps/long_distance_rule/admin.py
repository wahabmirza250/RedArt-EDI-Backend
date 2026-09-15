from django.contrib import admin

from apps.long_distance_rule.models import LongDistanceRule


@admin.register(LongDistanceRule)
class LongDistanceRuleAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "county_type",
        "review_threshold",
        "verification_threshold",
        "is_active",
        "created_at",
        "updated_at",
    )
    list_filter = ("county_type", "is_active", "created_at")
    search_fields = ("county_type",)
    ordering = ("-id",)
    list_per_page = 50
    readonly_fields = ("id", "created_at", "updated_at")
    fieldsets = (
        (
            None,
            {
                "fields": (
                    "id",
                    "county_type",
                    "review_threshold",
                    "verification_threshold",
                    "is_active",
                )
            },
        ),
        (
            "Timestamps",
            {"fields": ("created_at", "updated_at")},
        ),
    )
