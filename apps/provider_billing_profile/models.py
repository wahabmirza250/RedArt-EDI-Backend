from django.core.exceptions import ValidationError
from django.db import models

from apps.core.models import BaseModel


class ProviderBillingProfile(BaseModel):
    """
    One row per transportation company / billing entity that RedArt onboards.

    NPI providers (standard):
        is_atypical = False, npi required, tax_id required (for REF*EI in 837P).

    Atypical providers (Colorado Medicaid atypical — no NPI assigned):
        is_atypical = True, medicaid_provider_id required, npi must be blank.
        NM108 = XX, NM109 = medicaid_provider_id in the 837P
        (same XX qualifier as the HCPF-accepted sample; never invent an NPI).

    Adding a company never requires editing source code — update via API.
    """

    legal_name = models.CharField(max_length=300, blank=True, null=True)
    billing_name = models.CharField(max_length=300, blank=True, null=True)

    # Standard NPI provider.
    npi = models.CharField(max_length=10, blank=True, null=True)

    # EIN / TIN — required for REF*EI in 837P 2010AA when NM108=XX.
    # Never hard-coded; always supplied by the company during onboarding.
    tax_id = models.CharField(
        max_length=20,
        blank=True,
        null=True,
        help_text="EIN or TIN (digits only). Required for NPI providers in 837P REF*EI.",
    )

    # Atypical provider (Colorado Medicaid — no NPI).
    is_atypical = models.BooleanField(
        default=False,
        help_text=(
            "True = atypical Colorado Medicaid provider without NPI. "
            "Uses medicaid_provider_id as NM109 with NM108=XX "
            "(never invent an NPI)."
        ),
    )

    taxonomy_code = models.CharField(max_length=50, blank=True, null=True)
    location_id = models.CharField(max_length=50, blank=True, null=True)

    # Colorado Medicaid provider identifier — required when is_atypical=True.
    medicaid_provider_id = models.CharField(max_length=50, blank=True, null=True)

    revalidation_date = models.DateField(blank=True, null=True)
    city = models.CharField(max_length=50, blank=True, null=True)
    zip = models.CharField(max_length=50, blank=True, null=True)
    state = models.CharField(max_length=50, blank=True, null=True)
    country = models.CharField(max_length=50, blank=True, null=True)
    address_line_1 = models.CharField(max_length=500, blank=True, null=True)
    address_line_2 = models.CharField(max_length=500, blank=True, null=True)
    phone = models.CharField(max_length=50, blank=True, null=True)
    email = models.EmailField(blank=True, null=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name = "Provider Billing Profile"
        verbose_name_plural = "Provider Billing Profiles"

    def __str__(self):
        return self.billing_name or self.legal_name or str(self.pk)

    def clean(self):
        """Enforce NPI vs atypical invariant at the model level."""
        if self.is_atypical:
            if not (self.medicaid_provider_id or "").strip():
                raise ValidationError(
                    "Atypical providers must have a medicaid_provider_id."
                )
        else:
            if (self.npi or "").strip() and not str(self.npi).isdigit():
                raise ValidationError("NPI must contain digits only.")

    @property
    def billing_qualifier(self):
        """Return X12 NM108 qualifier (always XX; NM109 carries NPI or Medicaid ID)."""
        return "XX"

    @property
    def billing_id(self):
        """Return the X12 NM109 value appropriate to this provider type."""
        if self.is_atypical:
            return (self.medicaid_provider_id or "").strip()
        return (self.npi or "").strip()
