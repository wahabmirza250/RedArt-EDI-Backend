from django.db import models

from apps.core.models import BaseModel
from apps.patient.choices import Gender


class Patient(BaseModel):
    first_name = models.CharField(max_length=255)
    last_name = models.CharField(max_length=255)
    email = models.EmailField(max_length=255, blank=True, null=True)
    # Draft records may omit DOB. Self-subscriber 837P generation requires
    # verified demographics for DMG; never fabricate a value.
    date_of_birth = models.DateField(null=True, blank=True)
    gender = models.CharField(
        max_length=1,
        choices=Gender.choices,
        null=True,
        blank=True,
    )
    medicaid_member_id = models.CharField(max_length=255, unique=True)
    county = models.CharField(max_length=255, blank=True, null=True)
    address_line_1 = models.CharField(max_length=500, blank=True, null=True)
    address_line_2 = models.CharField(max_length=500, blank=True, null=True)
    city = models.CharField(max_length=100, blank=True, null=True)
    state = models.CharField(max_length=2, blank=True, null=True)
    zip = models.CharField(max_length=20, blank=True, null=True)
    phone = models.CharField(max_length=50, blank=True, null=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name = "Patient"
        verbose_name_plural = "Patients"

    def __str__(self):
        return f"{self.first_name} {self.last_name}"

    def has_837p_demographics(self):
        """
        Return True when the patient has the minimum data for a Colorado 837P.

        Draft storage is permissive; generation requires subscriber DMG data.
        """
        from apps.edi.utils.required_claim_data import subscriber_errors

        return bool((self.medicaid_member_id or "").strip()) and not subscriber_errors(
            self.date_of_birth, self.gender
        )
