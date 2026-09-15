from django.db import models

from apps.core.models import BaseModel


class ClaimServiceLineQuerySet(models.QuerySet):
    def with_relations(self):
        return self.select_related(
            "claim",
            "claim__trip",
            "claim__trip__patient",
            "claim__trip__provider",
        )


class ClaimServiceLine(BaseModel):
    claim = models.ForeignKey(
        "claim.Claim",
        on_delete=models.CASCADE,
        related_name="service_lines",
        null=True,
        blank=True,
    )
    procedure_code = models.CharField(max_length=32, null=True, blank=True)
    # X12 SV101 modifiers (e.g. 76 same rendering, 77 different rendering).
    modifier_1 = models.CharField(max_length=2, null=True, blank=True)
    modifier_2 = models.CharField(max_length=2, null=True, blank=True)
    modifier_3 = models.CharField(max_length=2, null=True, blank=True)
    modifier_4 = models.CharField(max_length=2, null=True, blank=True)
    from_date = models.DateField(null=True, blank=True)
    to_date = models.DateField(null=True, blank=True)
    units = models.PositiveIntegerField(null=True, blank=True)
    mileage = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
    )
    charge = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
    )
    is_active = models.BooleanField(default=True)

    def active_modifiers(self):
        """Return non-blank modifiers in SV101 order."""
        return [
            value.strip().upper()
            for value in (
                self.modifier_1,
                self.modifier_2,
                self.modifier_3,
                self.modifier_4,
            )
            if (value or "").strip()
        ]

    objects = ClaimServiceLineQuerySet.as_manager()

    class Meta:
        verbose_name = "Claim Service Line"
        verbose_name_plural = "Claim Service Lines"
        ordering = ("claim_id", "id")
        indexes = [
            # FK already indexed; composite helps "lines for claim + code" lookups
            models.Index(
                fields=["claim", "procedure_code"],
                name="csl_claim_proc_idx",
            ),
        ]

    def __str__(self):
        return f"{self.procedure_code or 'LINE'} #{self.pk}"
