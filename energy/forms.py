from datetime import timedelta
from decimal import Decimal

from django import forms
from django.utils import timezone

from core.models import Department
from .models import EnergyAnomaly, EnergyAsset, EnergyForecast, EnergyReading, OptimizationScenario
from .validation import (
    CODE_PATTERN, MAX_READING_VALUE, MIN_TIMESTAMP, unit_error, validate_not_future, validate_not_too_old,
)

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_PENDING_FORECASTS = 3  # par entreprise : évite de saturer le serveur
PENDING_EXPIRY = timedelta(minutes=15)  # une prévision « en cours » plus ancienne est considérée bloquée (worker arrêté)


class AssetForm(forms.ModelForm):
    class Meta:
        model = EnergyAsset
        fields = ["name", "code", "asset_type", "energy_type", "department", "is_active"]
        labels = {"name": "Nom", "code": "Code interne", "asset_type": "Type d'asset", "energy_type": "Type d'énergie",
                  "department": "Département", "is_active": "Asset actif"}

    def __init__(self, *args, company, **kwargs):
        super().__init__(*args, **kwargs)
        self.company = company
        self.fields["department"].queryset = Department.objects.filter(company=company)
        self.fields["department"].required = False
        for name, f in self.fields.items():
            f.widget.attrs.setdefault("class", "form-check-input" if name == "is_active" else "form-control")
        for name in ("asset_type", "energy_type", "department"):
            self.fields[name].widget.attrs["class"] = "form-select"
        self.fields["name"].widget.attrs.update({"minlength": "2", "autocomplete": "off"})
        self.fields["name"].help_text = "2 à 120 caractères."
        self.fields["code"].widget.attrs.update({
            "pattern": CODE_PATTERN, "autocomplete": "off", "spellcheck": "false", "autocapitalize": "characters",
            "title": "Lettres, chiffres, point, tiret ou tiret bas, sans espace (40 caractères maximum)",
        })
        self.fields["code"].help_text = "Lettres, chiffres, . - _ sans espace. Exemple : HVAC-01. Utilisé dans les imports."

    def clean_code(self):
        code = self.cleaned_data["code"].strip()
        qs = EnergyAsset.objects.filter(company=self.company, code__iexact=code)  # HV1 et hv1 = même code
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError("Ce code existe déjà pour votre entreprise.")
        return code

    def clean(self):
        data = super().clean()
        # Changer le type d'énergie d'un asset ayant des mesures rendrait leurs unités incohérentes.
        if self.instance.pk and data.get("energy_type") and data["energy_type"] != self.instance.energy_type:
            bad = {r.unit for r in self.instance.readings.only("unit").distinct()
                   if unit_error(data["energy_type"], r.unit)}
            if bad:
                self.add_error("energy_type", f"Impossible : cet asset a déjà des mesures en {', '.join(sorted(bad))}, "
                                              "incompatibles avec ce type d'énergie.")
        return data


class ReadingForm(forms.ModelForm):
    timestamp = forms.DateTimeField(
        label="Date et heure",
        widget=forms.DateTimeInput(attrs={"type": "datetime-local", "class": "form-control"}),
        input_formats=["%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S"],
        help_text="Pas de date future ni antérieure à 2000.",
    )

    class Meta:
        model = EnergyReading
        fields = ["asset", "timestamp", "value", "unit"]
        labels = {"asset": "Asset", "value": "Consommation", "unit": "Unité"}

    def __init__(self, *args, company, **kwargs):
        super().__init__(*args, **kwargs)
        assets = EnergyAsset.objects.filter(company=company, is_active=True)
        if self.instance.pk:  # l'asset d'une mesure existante reste choisissable même s'il a été désactivé
            assets = EnergyAsset.objects.filter(company=company, is_active=True) | EnergyAsset.objects.filter(pk=self.instance.asset_id)
        self.fields["asset"].queryset = assets.distinct()
        self.fields["asset"].widget.attrs["class"] = "form-select"
        self.fields["unit"].widget.attrs["class"] = "form-select"
        self.fields["value"].widget.attrs.update({
            "class": "form-control", "min": "0", "max": str(MAX_READING_VALUE), "step": "0.001", "inputmode": "decimal"})
        self.fields["value"].help_text = "Nombre positif ou nul, 3 décimales au maximum."
        self.fields["timestamp"].widget.attrs["min"] = MIN_TIMESTAMP.strftime("%Y-%m-%dT%H:%M")
        self.fields["timestamp"].widget.attrs["max"] = timezone.localtime().strftime("%Y-%m-%dT%H:%M")

    def clean_timestamp(self):
        ts = self.cleaned_data["timestamp"]
        validate_not_future(ts)
        validate_not_too_old(ts)
        return ts

    def clean(self):
        data = super().clean()
        asset, ts, unit = data.get("asset"), data.get("timestamp"), data.get("unit")
        if asset and ts and EnergyReading.objects.filter(asset=asset, timestamp=ts).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("Une mesure existe déjà pour cet asset à cette date/heure.")
        if asset and unit:
            msg = unit_error(asset.energy_type, unit)
            if msg:
                self.add_error("unit", msg)
        return data


class ImportForm(forms.Form):
    file = forms.FileField(
        label="Fichier CSV ou Excel (.xlsx)",
        help_text="Colonnes : asset_code, timestamp, value, unit (facultative). 10 Mo et 200 000 lignes au maximum.",
        widget=forms.ClearableFileInput(attrs={"class": "form-control", "accept": ".csv,.xlsx", "data-dropzone": "1", "data-max-bytes": str(MAX_UPLOAD_BYTES)}),
    )

    def clean_file(self):
        f = self.cleaned_data["file"]
        name = f.name.lower()
        if len(f.name) > 200:
            raise forms.ValidationError("Nom de fichier trop long.")
        if not name.endswith((".csv", ".xlsx")):
            raise forms.ValidationError("Format non supporté : .csv ou .xlsx uniquement.")
        if f.size == 0:
            raise forms.ValidationError("Le fichier est vide.")
        if f.size > MAX_UPLOAD_BYTES:
            raise forms.ValidationError("Fichier trop volumineux (10 Mo maximum).")
        # On vérifie le contenu réel, pas seulement l'extension (un .exe renommé en .csv est refusé).
        head = f.read(4096)
        f.seek(0)
        if name.endswith(".xlsx") and not head.startswith(b"PK\x03\x04"):
            raise forms.ValidationError("Ce fichier n'est pas un vrai classeur Excel (.xlsx).")
        if name.endswith(".csv") and b"\x00" in head:
            raise forms.ValidationError("Ce fichier n'est pas un CSV texte valide.")
        return f


class ForecastForm(forms.Form):
    asset = forms.ModelChoiceField(queryset=EnergyAsset.objects.none(), label="Asset")
    horizon_days = forms.TypedChoiceField(
        choices=[(7, "7 jours"), (30, "30 jours")], coerce=int, label="Horizon de prévision",
        help_text="30 jours demande un historique plus long (au moins 120 jours valides).",
    )

    def __init__(self, *args, company, **kwargs):
        super().__init__(*args, **kwargs)
        self.company = company
        self.fields["asset"].queryset = EnergyAsset.objects.filter(company=company, is_active=True)
        self.fields["asset"].widget.attrs["class"] = "form-select"
        self.fields["horizon_days"].widget.attrs["class"] = "form-select"

    def clean(self):
        data = super().clean()
        asset, horizon = data.get("asset"), data.get("horizon_days")
        active = EnergyForecast.objects.for_company(self.company).filter(
            status__in=[EnergyForecast.Status.PENDING, EnergyForecast.Status.RUNNING],
            created_at__gte=timezone.now() - PENDING_EXPIRY)
        if asset and horizon and active.filter(asset=asset, horizon_days=horizon).exists():
            raise forms.ValidationError("Une prévision identique est déjà en cours pour cet asset. Attendez sa fin.")
        if active.count() >= MAX_PENDING_FORECASTS:
            raise forms.ValidationError("Trop de prévisions en cours pour votre entreprise. Réessayez dans un instant.")
        return data


class ScenarioForm(forms.ModelForm):
    class Meta:
        model = OptimizationScenario
        fields = ["name", "asset", "action", "horizon_days", "reduction_percent", "affected_share_percent",
                  "price_per_kwh", "emission_factor", "investment_cost", "notes"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 3, "maxlength": "2000"})}

    BOUNDS = {  # champ : (min, max, pas)
        "reduction_percent": ("0.1", "80", "0.1"),
        "affected_share_percent": ("1", "100", "0.1"),
        "price_per_kwh": ("0.0001", "100", "0.0001"),
        "emission_factor": ("0", "5", "0.0001"),
        "investment_cost": ("0", "1000000000", "0.01"),
    }

    def __init__(self, *args, company, **kwargs):
        super().__init__(*args, **kwargs)
        assets = EnergyAsset.objects.filter(company=company, is_active=True)
        if self.instance.pk:
            assets = assets | EnergyAsset.objects.filter(pk=self.instance.asset_id)
        self.fields["asset"].queryset = assets.distinct()
        self.company = company
        for name, f in self.fields.items():
            f.widget.attrs["class"] = "form-select" if name in ("asset", "action", "horizon_days") else "form-control"
        for name, (lo, hi, step) in self.BOUNDS.items():
            self.fields[name].widget.attrs.update({"inputmode": "decimal", "min": lo, "max": hi, "step": step})
        self.fields["name"].widget.attrs.update({"minlength": "3", "autocomplete": "off"})

    def clean_reduction_percent(self):
        v = self.cleaned_data["reduction_percent"]
        if v is not None and not Decimal("0.1") <= v <= Decimal("80"):
            raise forms.ValidationError("La réduction doit être comprise entre 0,1 % et 80 %.")
        return v

    def clean(self):
        data = super().clean()
        name, asset = data.get("name"), data.get("asset")
        if name and asset:
            dup = OptimizationScenario.objects.filter(asset=asset, name__iexact=name.strip())
            if self.instance.pk:
                dup = dup.exclude(pk=self.instance.pk)
            if dup.exists():
                self.add_error("name", "Un scénario de même nom existe déjà pour cet asset.")
        return data


class AnomalyDetectForm(forms.Form):
    asset = forms.ModelChoiceField(queryset=EnergyAsset.objects.none(), required=False, empty_label="Tous les assets actifs", label="Asset à analyser")

    def __init__(self, *args, company, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["asset"].queryset = EnergyAsset.objects.filter(company=company, is_active=True)
        self.fields["asset"].widget.attrs["class"] = "form-select"


class AnomalyReviewForm(forms.ModelForm):
    class Meta:
        model = EnergyAnomaly
        fields = ["status", "note"]
        labels = {"status": "Décision", "note": "Commentaire (cause, action menée)"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["status"].widget.attrs["class"] = "form-select"
        self.fields["note"].widget.attrs.update({"class": "form-control", "maxlength": "500", "autocomplete": "off"})
        self.fields["note"].required = False
        self.fields["note"].help_text = "500 caractères au maximum."
