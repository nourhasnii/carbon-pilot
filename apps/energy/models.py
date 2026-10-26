"""Module 4 — Energy Forecasting & Resource Optimization : données de base.

Règles du cahier des charges appliquées ici :
- multi-tenant strict : chaque asset appartient à une Company, les readings
  héritent de la company via l'asset (manager `for_company`) ;
- aucune donnée inventée : le flag `is_demo` marque explicitement les données DEMO ;
- pas de doublons : unicité (asset, timestamp) ;
- valeurs impossibles refusées : consommation >= 0.
"""
from django.conf import settings
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinLengthValidator, MinValueValidator
from django.db import models
from django.db.models import Q

from apps.enterprise.models import Company, Department

from .validation import (
    MAX_READING_VALUE, validate_code, validate_has_alnum, validate_multiline, validate_not_future,
    validate_not_too_old, validate_single_line, unit_error,
)


class EnergyAsset(models.Model):
    class AssetType(models.TextChoices):
        BUILDING = "building", "Bâtiment"
        METER = "meter", "Compteur"
        BOILER = "boiler", "Chaudière"
        HVAC = "hvac", "HVAC / Climatisation"
        OTHER = "other", "Autre"

    class EnergyType(models.TextChoices):
        ELECTRICITY = "electricity", "Électricité"
        GAS = "gas", "Gaz"
        FUEL = "fuel", "Fioul / Carburant"
        OTHER = "other", "Autre"

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="energy_assets")
    department = models.ForeignKey(
        Department, null=True, blank=True, on_delete=models.SET_NULL, related_name="energy_assets"
    )
    name = models.CharField("nom", max_length=120, validators=[MinLengthValidator(2), validate_single_line, validate_has_alnum])
    code = models.CharField("code interne", max_length=40, validators=[validate_code])
    asset_type = models.CharField(max_length=20, choices=AssetType.choices, default=AssetType.METER)
    energy_type = models.CharField(max_length=20, choices=EnergyType.choices, default=EnergyType.ELECTRICITY)
    is_active = models.BooleanField(default=True)
    is_demo = models.BooleanField("données DEMO", default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["company", "code"], name="uniq_energy_asset_code_per_company"),
        ]

    def clean(self):
        if self.department_id and self.company_id and self.department.company_id != self.company_id:
            raise ValidationError({"department": "Ce département n'appartient pas à l'entreprise de l'asset."})

    def __str__(self):
        return f"{self.name} [{self.code}]"


class EnergyReadingQuerySet(models.QuerySet):
    def for_company(self, company):
        """Point d'entrée obligatoire des vues : filtre toujours par entreprise."""
        return self.filter(asset__company=company)


class EnergyReading(models.Model):
    class Unit(models.TextChoices):
        KWH = "kWh", "kWh"
        MWH = "MWh", "MWh"
        M3 = "m3", "m³ (gaz)"
        LITRE = "L", "Litre"

    asset = models.ForeignKey(EnergyAsset, on_delete=models.CASCADE, related_name="readings")
    timestamp = models.DateTimeField(db_index=True, validators=[validate_not_too_old])
    value = models.DecimalField("consommation", max_digits=14, decimal_places=3,
                                validators=[MinValueValidator(Decimal("0")), MaxValueValidator(MAX_READING_VALUE)])
    unit = models.CharField(max_length=5, choices=Unit.choices, default=Unit.KWH)
    is_demo = models.BooleanField("donnée DEMO", default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = EnergyReadingQuerySet.as_manager()

    class Meta:
        ordering = ["asset", "timestamp"]
        constraints = [
            models.UniqueConstraint(fields=["asset", "timestamp"], name="uniq_reading_per_asset_timestamp"),
            models.CheckConstraint(condition=Q(value__gte=0), name="reading_value_non_negative"),
        ]
        indexes = [models.Index(fields=["asset", "timestamp"], name="reading_asset_ts_idx")]

    def clean(self):
        if self.value is not None and self.value < 0:
            raise ValidationError({"value": "La consommation ne peut pas être négative."})
        if self.timestamp:
            validate_not_future(self.timestamp)
        if self.asset_id and self.unit:
            msg = unit_error(self.asset.energy_type, self.unit)
            if msg:
                raise ValidationError({"unit": msg})

    def __str__(self):
        return f"{self.asset.code} {self.timestamp:%Y-%m-%d %H:%M} = {self.value} {self.unit}"


class ImportBatch(models.Model):
    """Trace d'un import : qui, quand, quel fichier, combien de lignes acceptées/rejetées."""

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="energy_import_batches")
    imported_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    filename = models.CharField(max_length=255)
    total_rows = models.PositiveIntegerField(default=0)
    imported_rows = models.PositiveIntegerField(default=0)
    rejected_rows = models.PositiveIntegerField(default=0)
    errors = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.filename} ({self.imported_rows}/{self.total_rows})"


class EnergyForecastQuerySet(models.QuerySet):
    def for_company(self, company):
        return self.filter(asset__company=company)


class EnergyForecast(models.Model):
    """Résultat (traçable) d'une exécution de prévision pour un asset."""

    class Status(models.TextChoices):
        PENDING = "pending", "En attente"
        RUNNING = "running", "En cours"
        SUCCESS = "success", "Terminée"
        INSUFFICIENT = "insufficient_data", "Données insuffisantes"
        FAILED = "failed", "Échec"

    HORIZON_CHOICES = [(7, "7 jours"), (30, "30 jours")]

    asset = models.ForeignKey(EnergyAsset, on_delete=models.CASCADE, related_name="forecasts")
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    horizon_days = models.PositiveSmallIntegerField(choices=HORIZON_CHOICES, default=7)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    message = models.TextField(blank=True)

    # Traçabilité (cahier des charges §5) : modèle retenu, métriques, période de référence
    model_name = models.CharField(max_length=40, blank=True)
    mae = models.FloatField(null=True, blank=True)
    rmse = models.FloatField(null=True, blank=True)
    metrics = models.JSONField(default=dict, blank=True)  # candidats : {nom: {mae, rmse}}, baseline, etc.
    n_training_days = models.PositiveIntegerField(default=0)
    training_start = models.DateField(null=True, blank=True)
    training_end = models.DateField(null=True, blank=True)
    duration_seconds = models.FloatField(null=True, blank=True)
    is_demo = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    objects = EnergyForecastQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at"]

    @property
    def model_label(self):
        """Nom lisible du modèle retenu (ex. « RandomForestRegressor »)."""
        return (self.metrics or {}).get("candidates", {}).get(self.model_name, {}).get("label", self.model_name)

    def __str__(self):
        return f"Prévision {self.asset.code} +{self.horizon_days}j ({self.get_status_display()})"


class ForecastPoint(models.Model):
    forecast = models.ForeignKey(EnergyForecast, on_delete=models.CASCADE, related_name="points")
    date = models.DateField()
    predicted_kwh = models.FloatField()
    lower_kwh = models.FloatField(null=True, blank=True)  # bas de la fourchette (80 %)
    upper_kwh = models.FloatField(null=True, blank=True)  # haut de la fourchette (80 %)
    actual_kwh = models.FloatField(null=True, blank=True)  # rempli quand la réalité arrive

    class Meta:
        ordering = ["date"]
        constraints = [models.UniqueConstraint(fields=["forecast", "date"], name="uniq_forecast_point_date")]

    @property
    def error(self):
        return None if self.actual_kwh is None else self.predicted_kwh - self.actual_kwh


class OptimizationScenario(models.Model):
    """Simulation « et si ? » : effet d'une réduction de consommation sur un asset.

    Les résultats sont calculés à l'enregistrement (voir services/optimization.py) puis conservés,
    avec les hypothèses utilisées, pour rester traçables.
    """

    class Action(models.TextChoices):
        HVAC = "hvac", "Climatisation / chauffage (HVAC)"
        LIGHTING = "lighting", "Éclairage"
        SCHEDULE = "schedule", "Horaires de fonctionnement"
        EQUIPMENT = "equipment", "Équipement plus efficace"
        OTHER = "other", "Autre action"

    class Source(models.TextChoices):
        FORECAST = "forecast", "Dernière prévision"
        HISTORY = "history", "Moyenne par jour de semaine (historique)"

    asset = models.ForeignKey(EnergyAsset, on_delete=models.CASCADE, related_name="scenarios")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    name = models.CharField("Nom du scénario", max_length=120, validators=[MinLengthValidator(3), validate_single_line, validate_has_alnum])
    action = models.CharField("Type d'action", max_length=12, choices=Action.choices, default=Action.HVAC)
    horizon_days = models.PositiveSmallIntegerField("Période simulée", choices=[(7, "7 jours"), (30, "30 jours")], default=30)
    reduction_percent = models.DecimalField(
        "Réduction visée (%)", max_digits=5, decimal_places=2,
        validators=[MinValueValidator(Decimal("0.1")), MaxValueValidator(Decimal("80"))],
        help_text="Baisse de consommation obtenue sur la part concernée (0,1 à 80 %).",
    )
    affected_share_percent = models.DecimalField(
        "Part de l'asset concernée (%)", max_digits=5, decimal_places=2, default=Decimal("100"),
        validators=[MinValueValidator(Decimal("1")), MaxValueValidator(Decimal("100"))],
        help_text="Ex. : si le HVAC pèse 40 % de la consommation de ce site, saisir 40.",
    )
    # Hypothèses économiques / environnementales : modifiables et à confirmer avec des sources officielles.
    price_per_kwh = models.DecimalField(
        "Prix du kWh (DT)", max_digits=8, decimal_places=4, default=Decimal("0.3500"),
        validators=[MinValueValidator(Decimal("0.0001")), MaxValueValidator(Decimal("100"))],
        help_text="Hypothèse par défaut, à confirmer avec votre facture STEG (0,0001 à 100 DT).",
    )
    emission_factor = models.DecimalField(
        "Facteur d'émission (kg CO₂e/kWh)", max_digits=6, decimal_places=4, default=Decimal("0.5000"),
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("5"))],
        help_text="Hypothèse par défaut, à confirmer avec une source officielle (réseau électrique tunisien), de 0 à 5.",
    )
    investment_cost = models.DecimalField(
        "Investissement (DT, facultatif)", max_digits=12, decimal_places=2, null=True, blank=True,
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("1000000000"))],
    )
    notes = models.TextField("Notes", blank=True, max_length=2000, validators=[validate_multiline])

    # Résultats calculés
    baseline_source = models.CharField(max_length=10, choices=Source.choices, blank=True)
    baseline_kwh = models.FloatField(null=True, blank=True)
    optimized_kwh = models.FloatField(null=True, blank=True)
    saved_kwh = models.FloatField(null=True, blank=True)
    cost_saved = models.FloatField(null=True, blank=True)
    co2_saved_kg = models.FloatField(null=True, blank=True)
    payback_months = models.FloatField(null=True, blank=True)
    daily = models.JSONField(default=list, blank=True)  # [[date, baseline_kwh, optimized_kwh], ...]
    result_message = models.TextField(blank=True)
    computed_at = models.DateTimeField(null=True, blank=True)
    is_demo = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = EnergyForecastQuerySet.as_manager()  # même filtre for_company (asset__company)

    class Meta:
        ordering = ["-created_at"]

    @property
    def has_result(self):
        return self.baseline_kwh is not None

    def __str__(self):
        return f"{self.name} ({self.asset.code})"


class EnergyAnomaly(models.Model):
    """Jour de consommation anormale détecté automatiquement, avec son explication et son suivi."""

    class Kind(models.TextChoices):
        SPIKE = "spike", "Pic de consommation"
        DROP = "drop", "Chute de consommation"
        ZERO = "zero", "Consommation quasi nulle"

    class Severity(models.TextChoices):
        MEDIUM = "medium", "Moyenne"
        HIGH = "high", "Élevée"

    class Status(models.TextChoices):
        NEW = "new", "À examiner"
        ACKNOWLEDGED = "acknowledged", "Confirmée"
        DISMISSED = "dismissed", "Écartée (fausse alerte)"

    asset = models.ForeignKey(EnergyAsset, on_delete=models.CASCADE, related_name="anomalies")
    date = models.DateField()
    actual_kwh = models.FloatField()
    expected_kwh = models.FloatField()
    z_score = models.FloatField()
    kind = models.CharField(max_length=8, choices=Kind.choices)
    severity = models.CharField(max_length=8, choices=Severity.choices)
    iforest_agrees = models.BooleanField(default=False)  # un 2e algorithme (Isolation Forest) confirme
    explanation = models.CharField(max_length=300)
    status = models.CharField(max_length=14, choices=Status.choices, default=Status.NEW)
    note = models.CharField(max_length=500, blank=True, validators=[validate_single_line])
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    detected_at = models.DateTimeField(auto_now=True)
    is_demo = models.BooleanField(default=False)

    objects = EnergyForecastQuerySet.as_manager()  # filtre for_company (asset__company)

    class Meta:
        ordering = ["-date", "asset__name"]
        constraints = [models.UniqueConstraint(fields=["asset", "date"], name="uniq_anomaly_per_asset_date")]

    @property
    def deviation_pct(self):
        return None if not self.expected_kwh else round(100 * (self.actual_kwh - self.expected_kwh) / self.expected_kwh, 1)

    def __str__(self):
        return f"{self.asset.code} {self.date} ({self.get_kind_display()})"
