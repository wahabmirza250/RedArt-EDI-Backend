from rest_framework import serializers

from apps.claim.models import Claim
from apps.claim.utils.validators import clean_optional_text, ensure_non_negative
from apps.claim_service_line.models import ClaimServiceLine


class ClaimServiceLineSerializer(serializers.ModelSerializer):
    claim_number = serializers.CharField(
        source="claim.claim_number", read_only=True, allow_null=True
    )

    class Meta:
        model = ClaimServiceLine
        fields = (
            "id",
            "claim",
            "claim_number",
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
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "claim_number", "created_at", "updated_at")

    def validate_procedure_code(self, value):
        return clean_optional_text(value)

    def validate_modifier_1(self, value):
        return self._clean_modifier(value)

    def validate_modifier_2(self, value):
        return self._clean_modifier(value)

    def validate_modifier_3(self, value):
        return self._clean_modifier(value)

    def validate_modifier_4(self, value):
        return self._clean_modifier(value)

    @staticmethod
    def _clean_modifier(value):
        value = clean_optional_text(value)
        if value is None:
            return value
        value = value.upper()
        if len(value) > 2:
            raise serializers.ValidationError(
                "Modifier must be at most 2 characters."
            )
        return value

    def validate_units(self, value):
        if value is None:
            return value
        if value < 0:
            raise serializers.ValidationError("units cannot be negative.")
        return value

    def validate_mileage(self, value):
        return ensure_non_negative(value, "mileage")

    def validate_charge(self, value):
        return ensure_non_negative(value, "charge")

    def validate_claim(self, value):
        if value is None:
            return value
        if not value.is_active:
            raise serializers.ValidationError("Claim not found or inactive.")
        return value

    def validate(self, attrs):
        from_date = attrs.get("from_date", getattr(self.instance, "from_date", None))
        to_date = attrs.get("to_date", getattr(self.instance, "to_date", None))
        if from_date and to_date and from_date > to_date:
            raise serializers.ValidationError(
                {"to_date": ["Must be on or after from_date."]}
            )
        claim = attrs.get("claim", getattr(self.instance, "claim", None))
        if claim is None:
            raise serializers.ValidationError({"claim": ["This field is required."]})
        return attrs


class ClaimServiceLineListSerializer(serializers.ModelSerializer):
    class Meta:
        model = ClaimServiceLine
        fields = (
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
            "created_at",
        )
        read_only_fields = fields


class ClaimServiceLineIdSerializer(serializers.Serializer):
    id = serializers.IntegerField(read_only=True)
