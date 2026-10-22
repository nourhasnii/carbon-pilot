"""Sérialiseurs de l'API. La validation métier réutilise les formulaires Django de l'interface web :
les règles de contrôle de saisie (code, dates, unités, bornes…) sont donc strictement identiques."""
from django.forms.models import model_to_dict
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from energy import forms
from energy.models import (
    EnergyAnomaly, EnergyAsset, EnergyForecast, EnergyReading, ForecastPoint, OptimizationScenario,
)
from energy.services.optimization import ScenarioError, compute_scenario


def _flatten(errors):
    """Erreurs de formulaire Django -> format DRF {champ: [messages]}."""
    out = {}
    for field, errs in errors.items():
        key = "non_field_errors" if field == "__all__" else field
        out[key] = [str(e) for e in errs]
    return out


class FormBackedSerializer(serializers.ModelSerializer):
    """ModelSerializer dont la validation métier est déléguée à un formulaire Django."""

    form_class = None

    def _company(self):
        return self.context["company"]

    def validate(self, attrs):
        instance = self.instance or self.Meta.model()
        form_fields = self.form_class.base_fields.keys()
        data = model_to_dict(instance, fields=list(form_fields))
        data.update({k: v for k, v in self.initial_data.items() if k in form_fields})
        form = self.form_class(data, instance=self.instance, company=self._company())
        if not form.is_valid():
            raise serializers.ValidationError(_flatten(form.errors))
        self._form = form
        return attrs

    def create(self, validated_data):
        obj = self._form.save(commit=False)
        if hasattr(obj, "company_id") and obj.company_id is None:
            obj.company = self._company()  # toujours côté serveur
        obj.save()
        return obj

    def update(self, instance, validated_data):
        return self._form.save()

    def get_fields(self):
        fields = super().get_fields()
        company = self.context.get("company")
        if company is not None:  # une clé étrangère ne peut viser que l'entreprise de l'utilisateur
            if "asset" in fields and hasattr(fields["asset"], "queryset"):
                fields["asset"].queryset = EnergyAsset.objects.filter(company=company)
            if "department" in fields:
                from core.models import Department
                fields["department"].queryset = Department.objects.filter(company=company)
        return fields


class AssetSerializer(FormBackedSerializer):
    form_class = forms.AssetForm

    class Meta:
        model = EnergyAsset
        fields = ["id", "name", "code", "asset_type", "energy_type", "department", "is_active"]
        read_only_fields = ["id"]
        extra_kwargs = {"department": {"required": False, "allow_null": True}}
        validators = []


class ReadingSerializer(FormBackedSerializer):
    form_class = forms.ReadingForm
    asset_code = serializers.CharField(source="asset.code", read_only=True)

    class Meta:
        model = EnergyReading
        fields = ["id", "asset", "asset_code", "timestamp", "value", "unit"]
        read_only_fields = ["id"]
        validators = []


class ForecastPointSerializer(serializers.ModelSerializer):
    class Meta:
        model = ForecastPoint
        fields = ["date", "predicted_kwh", "lower_kwh", "upper_kwh", "actual_kwh"]


class ForecastSerializer(serializers.ModelSerializer):
    asset_code = serializers.CharField(source="asset.code", read_only=True)
    status_label = serializers.CharField(source="get_status_display", read_only=True)
    model_label = serializers.CharField(read_only=True)
    baseline_mae = serializers.SerializerMethodField()

    class Meta:
        model = EnergyForecast
        fields = ["id", "asset", "asset_code", "horizon_days", "status", "status_label", "message", "model_name",
                  "model_label", "mae", "rmse", "baseline_mae", "n_training_days", "training_start", "training_end",
                  "duration_seconds", "created_at", "finished_at"]
        read_only_fields = fields

    @extend_schema_field(serializers.FloatField(allow_null=True))
    def get_baseline_mae(self, obj):
        cand = (obj.metrics or {}).get("candidates", {}).get("seasonal_naive", {})
        return cand.get("mae")


class ForecastDetailSerializer(ForecastSerializer):
    points = ForecastPointSerializer(many=True, read_only=True)
    candidates = serializers.SerializerMethodField()

    class Meta(ForecastSerializer.Meta):
        fields = ForecastSerializer.Meta.fields + ["candidates", "points"]
        read_only_fields = fields

    @extend_schema_field(serializers.DictField())
    def get_candidates(self, obj):
        return (obj.metrics or {}).get("candidates", {})


class ForecastCreateSerializer(serializers.Serializer):
    """Demande de prévision : l'API répond tout de suite (202) ; le calcul est asynchrone (Celery)."""

    asset = serializers.PrimaryKeyRelatedField(queryset=EnergyAsset.objects.none())
    horizon_days = serializers.ChoiceField(choices=[7, 30], default=7)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        company = self.context.get("company")
        if company is not None:
            self.fields["asset"].queryset = EnergyAsset.objects.filter(company=company, is_active=True)

    def validate(self, attrs):
        form = forms.ForecastForm({"asset": attrs["asset"].pk, "horizon_days": attrs["horizon_days"]},
                                  company=self.context["company"])
        if not form.is_valid():
            raise serializers.ValidationError(_flatten(form.errors))
        return attrs


class ScenarioSerializer(FormBackedSerializer):
    form_class = forms.ScenarioForm
    asset_code = serializers.CharField(source="asset.code", read_only=True)

    class Meta:
        model = OptimizationScenario
        fields = ["id", "name", "asset", "asset_code", "action", "horizon_days", "reduction_percent",
                  "affected_share_percent", "price_per_kwh", "emission_factor", "investment_cost", "notes",
                  "baseline_source", "baseline_kwh", "optimized_kwh", "saved_kwh", "cost_saved", "co2_saved_kg",
                  "payback_months", "result_message", "computed_at"]
        read_only_fields = ["id", "baseline_source", "baseline_kwh", "optimized_kwh", "saved_kwh", "cost_saved",
                            "co2_saved_kg", "payback_months", "result_message", "computed_at"]
        validators = []

    def _save(self, form, creating):
        scenario = form.save(commit=False)
        if creating:
            scenario.created_by = self.context["request"].user
        scenario.save()
        try:
            compute_scenario(scenario)
        except ScenarioError as exc:
            OptimizationScenario.objects.filter(pk=scenario.pk).update(
                baseline_kwh=None, optimized_kwh=None, saved_kwh=None, cost_saved=None, co2_saved_kg=None,
                payback_months=None, daily=[], result_message=str(exc))
        scenario.refresh_from_db()
        return scenario

    def create(self, validated_data):
        return self._save(self._form, creating=True)

    def update(self, instance, validated_data):
        return self._save(self._form, creating=False)


class AnomalySerializer(serializers.ModelSerializer):
    asset_code = serializers.CharField(source="asset.code", read_only=True)
    deviation_pct = serializers.FloatField(read_only=True)
    reviewed_by = serializers.StringRelatedField(read_only=True)

    class Meta:
        model = EnergyAnomaly
        fields = ["id", "asset", "asset_code", "date", "actual_kwh", "expected_kwh", "deviation_pct", "z_score",
                  "kind", "severity", "iforest_agrees", "explanation", "status", "note", "reviewed_by", "reviewed_at"]
        read_only_fields = [f for f in fields if f not in ("status", "note")]

    def validate(self, attrs):
        form = forms.AnomalyReviewForm({"status": attrs.get("status", self.instance.status),
                                        "note": attrs.get("note", self.instance.note)}, instance=self.instance)
        if not form.is_valid():
            raise serializers.ValidationError(_flatten(form.errors))
        return attrs


class AnomalyDetectSerializer(serializers.Serializer):
    asset = serializers.PrimaryKeyRelatedField(queryset=EnergyAsset.objects.none(), required=False, allow_null=True,
                                               help_text="Vide = tous les assets actifs.")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        company = self.context.get("company")
        if company is not None:
            self.fields["asset"].queryset = EnergyAsset.objects.filter(company=company, is_active=True)
