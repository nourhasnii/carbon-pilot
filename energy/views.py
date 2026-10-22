from datetime import timedelta

from django.contrib import messages
from django.db.models import Case, Count, F, Max, Min, Sum, When, DecimalField, Avg
from django.db.models.functions import TruncDate, TruncWeek
import csv
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse_lazy
from django.utils import timezone
from django.utils.text import slugify
from django.views import View
from django.views.generic import CreateView, DeleteView, DetailView, FormView, ListView, TemplateView, UpdateView

from .forms import (AnomalyDetectForm, AnomalyReviewForm, AssetForm, ForecastForm, ImportForm, ReadingForm,
                    ScenarioForm)
from .mixins import CompanyRequiredMixin
from .validation import clean_search, parse_int_param
from .models import EnergyAnomaly, EnergyAsset, EnergyForecast, EnergyReading, ImportBatch, OptimizationScenario
from .services.forecasting import update_actuals
from .services.importer import ImportFileError, import_readings
from .services.anomalies import AnomalyError, detect_for_asset
from .services.forecasting import build_daily_series
from .services.optimization import ScenarioError, compute_scenario
from .tasks import run_forecast_task


# ---------- Assets ----------
class AssetList(CompanyRequiredMixin, ListView):
    permission_required = "energy.view_energyasset"
    template_name = "energy/asset_list.html"
    paginate_by = 20

    def get_queryset(self):
        qs = EnergyAsset.objects.filter(company=self.company).annotate(n_readings=Count("readings")).order_by("name")
        q = clean_search(self.request.GET.get("q"))
        return qs.filter(name__icontains=q) if q else qs


class AssetCreate(CompanyRequiredMixin, CreateView):
    permission_required = "energy.add_energyasset"
    form_class = AssetForm
    template_name = "energy/form.html"
    success_url = reverse_lazy("energy:asset_list")
    extra_context = {"title": "Nouvel asset énergétique", "parent_label": "Assets", "parent_url": reverse_lazy("energy:asset_list")}

    def get_form_kwargs(self):
        return {**super().get_form_kwargs(), "company": self.company}

    def form_valid(self, form):
        form.instance.company = self.company  # jamais issu du navigateur
        messages.success(self.request, "Asset créé.")
        return super().form_valid(form)


class AssetUpdate(CompanyRequiredMixin, UpdateView):
    permission_required = "energy.change_energyasset"
    form_class = AssetForm
    template_name = "energy/form.html"
    success_url = reverse_lazy("energy:asset_list")
    extra_context = {"title": "Modifier l'asset", "parent_label": "Assets", "parent_url": reverse_lazy("energy:asset_list")}

    def get_queryset(self):
        return EnergyAsset.objects.filter(company=self.company)

    def get_form_kwargs(self):
        return {**super().get_form_kwargs(), "company": self.company}

    def form_valid(self, form):
        messages.success(self.request, "Asset modifié.")
        return super().form_valid(form)


class AssetDelete(CompanyRequiredMixin, DeleteView):
    permission_required = "energy.delete_energyasset"
    template_name = "energy/confirm_delete.html"
    success_url = reverse_lazy("energy:asset_list")

    def get_queryset(self):
        return EnergyAsset.objects.filter(company=self.company)

    def form_valid(self, form):
        messages.success(self.request, "Asset supprimé avec ses mesures.")
        return super().form_valid(form)


# ---------- Readings ----------
def filtered_readings(request, company):
    """Mesures de l'entreprise, filtrées par asset si demandé (partagé par la liste et l'export)."""
    qs = EnergyReading.objects.for_company(company).select_related("asset").order_by("-timestamp")
    asset = parse_int_param(request.GET.get("asset"))
    if asset:
        qs = qs.filter(asset_id=asset)
    return qs


def csv_safe(value):
    """Neutralise l'injection de formules (Excel/LibreOffice) : une cellule texte commençant par = + - @ est préfixée."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def csv_response(filename):
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response.write("\ufeff")  # BOM : Excel lit correctement les accents
    return response


class ReadingList(CompanyRequiredMixin, ListView):
    permission_required = "energy.view_energyreading"
    template_name = "energy/reading_list.html"
    paginate_by = 25

    def get_queryset(self):
        return filtered_readings(self.request, self.company)

    def get_context_data(self, **kw):
        ctx = super().get_context_data(**kw)
        ctx["assets"] = EnergyAsset.objects.filter(company=self.company)
        asset = parse_int_param(self.request.GET.get("asset"))
        ctx["selected_asset"] = str(asset) if asset else ""
        return ctx


class ReadingCreate(CompanyRequiredMixin, CreateView):
    permission_required = "energy.add_energyreading"
    form_class = ReadingForm
    template_name = "energy/form.html"
    success_url = reverse_lazy("energy:reading_list")
    extra_context = {"title": "Nouvelle mesure", "parent_label": "Mesures", "parent_url": reverse_lazy("energy:reading_list")}

    def get_form_kwargs(self):
        return {**super().get_form_kwargs(), "company": self.company}

    def form_valid(self, form):
        messages.success(self.request, "Mesure enregistrée.")
        return super().form_valid(form)


class ReadingUpdate(CompanyRequiredMixin, UpdateView):
    permission_required = "energy.change_energyreading"
    form_class = ReadingForm
    template_name = "energy/form.html"
    success_url = reverse_lazy("energy:reading_list")
    extra_context = {"title": "Modifier la mesure", "parent_label": "Mesures", "parent_url": reverse_lazy("energy:reading_list")}

    def get_queryset(self):
        return EnergyReading.objects.for_company(self.company)

    def get_form_kwargs(self):
        return {**super().get_form_kwargs(), "company": self.company}


class ReadingDelete(CompanyRequiredMixin, DeleteView):
    permission_required = "energy.delete_energyreading"
    template_name = "energy/confirm_delete.html"
    success_url = reverse_lazy("energy:reading_list")

    def get_queryset(self):
        return EnergyReading.objects.for_company(self.company)


# ---------- Import ----------
class ImportView(CompanyRequiredMixin, FormView):
    permission_required = "energy.add_importbatch"
    form_class = ImportForm
    template_name = "energy/import.html"

    def form_valid(self, form):
        try:
            result = import_readings(form.cleaned_data["file"], self.company, self.request.user)
        except ImportFileError as exc:
            form.add_error("file", str(exc))
            return self.form_invalid(form)
        level = messages.SUCCESS if not result.errors else messages.WARNING
        messages.add_message(
            self.request, level,
            f"{result.imported_rows} ligne(s) importée(s) sur {result.total_rows}; {result.rejected_rows} rejetée(s).",
        )
        return redirect("energy:import_detail", pk=result.batch.pk)


class ImportList(CompanyRequiredMixin, ListView):
    permission_required = "energy.view_importbatch"
    template_name = "energy/import_list.html"
    paginate_by = 20

    def get_queryset(self):
        return ImportBatch.objects.filter(company=self.company)


class ImportDetail(CompanyRequiredMixin, DetailView):
    permission_required = "energy.view_importbatch"
    template_name = "energy/import_detail.html"

    def get_queryset(self):
        return ImportBatch.objects.filter(company=self.company)


# ---------- Historique / tendances ----------
KWH = Case(
    When(unit="kWh", then=F("value")),
    When(unit="MWh", then=F("value") * 1000),
    default=None,
    output_field=DecimalField(max_digits=18, decimal_places=3),
)


class HistoryView(CompanyRequiredMixin, TemplateView):
    permission_required = "energy.view_energyreading"
    template_name = "energy/history.html"

    def get_context_data(self, **kw):
        ctx = super().get_context_data(**kw)
        g = self.request.GET
        assets = EnergyAsset.objects.filter(company=self.company)
        qs = EnergyReading.objects.for_company(self.company)

        asset_pk = parse_int_param(g.get("asset"))
        asset_id = str(asset_pk) if asset_pk else ""
        if asset_pk:
            qs = qs.filter(asset_id=asset_pk)
        period = g.get("period") if g.get("period") in ("day", "week") else "day"
        days = parse_int_param(g.get("days"), default=90, lo=1, hi=3650)
        since = timezone.now() - timedelta(days=days)
        qs = qs.filter(timestamp__gte=since)

        trunc = TruncWeek("timestamp") if period == "week" else TruncDate("timestamp")
        series = list(
            qs.annotate(bucket=trunc, kwh=KWH).values("bucket").annotate(total=Sum("kwh"), n=Count("id")).order_by("bucket")
        )
        stats = qs.annotate(kwh=KWH).aggregate(total=Sum("kwh"), avg=Avg("kwh"), peak=Max("kwh"), first=Min("timestamp"), last=Max("timestamp"))
        by_asset = list(
            qs.annotate(kwh=KWH).values("asset__name").annotate(total=Sum("kwh"), n=Count("id")).order_by("-total")
        )
        ctx.update(
            assets=assets, selected_asset=asset_id, period=period, days=days, stats=stats,
            by_asset=by_asset, series=series,
            excluded=qs.exclude(unit__in=["kWh", "MWh"]).count(),
            chart_data={
                "labels": [s["bucket"].strftime("%Y-%m-%d") for s in series],
                "values": [float(s["total"] or 0) for s in series],
            },
            is_demo=qs.filter(is_demo=True).exists(),
            quick_ranges=[(7, "7 jours"), (30, "30 jours"), (90, "90 jours"), (365, "1 an")],
        )
        return ctx


class DashboardView(HistoryView):
    template_name = "energy/dashboard.html"

    def get_context_data(self, **kw):
        ctx = super().get_context_data(**kw)
        if self.request.user.has_perm("energy.view_energyforecast"):
            f = (EnergyForecast.objects.for_company(self.company)
                 .filter(status=EnergyForecast.Status.SUCCESS).select_related("asset").first())
            if f:
                total = sum(p.predicted_kwh for p in f.points.all())
                ctx.update(latest_forecast=f, latest_total=total)
        if self.request.user.has_perm("energy.view_energyanomaly"):
            new = EnergyAnomaly.objects.for_company(self.company).filter(status=EnergyAnomaly.Status.NEW)
            ctx.update(new_anomalies=new.count(), high_anomalies=new.filter(severity=EnergyAnomaly.Severity.HIGH).count())
        return ctx


# ---------- Prévisions ----------
class ForecastList(CompanyRequiredMixin, ListView):
    permission_required = "energy.view_energyforecast"
    template_name = "energy/forecast_list.html"
    paginate_by = 20

    def get_queryset(self):
        return EnergyForecast.objects.for_company(self.company).select_related("asset", "requested_by")


class ForecastCreate(CompanyRequiredMixin, FormView):
    permission_required = "energy.add_energyforecast"
    form_class = ForecastForm
    template_name = "energy/form.html"
    extra_context = {"title": "Nouvelle prévision de consommation", "parent_label": "Prévisions", "parent_url": reverse_lazy("energy:forecast_list")}

    def get_form_kwargs(self):
        return {**super().get_form_kwargs(), "company": self.company}

    def form_valid(self, form):
        forecast = EnergyForecast.objects.create(
            asset=form.cleaned_data["asset"], horizon_days=form.cleaned_data["horizon_days"],
            requested_by=self.request.user,
        )
        # Asynchrone via Celery (exécution immédiate en mode eager / dev, worker en production)
        run_forecast_task.delay(forecast.pk)
        return redirect("energy:forecast_detail", pk=forecast.pk)


class ForecastDetail(CompanyRequiredMixin, DetailView):
    permission_required = "energy.view_energyforecast"
    template_name = "energy/forecast_detail.html"

    def get_queryset(self):
        return EnergyForecast.objects.for_company(self.company).select_related("asset")

    def get_context_data(self, **kw):
        ctx = super().get_context_data(**kw)
        f = self.object
        ctx["realized"] = update_actuals(f)
        points = list(f.points.all())
        ctx["points"] = points
        ctx["candidates"] = (f.metrics or {}).get("candidates", {}) if f.metrics else {}
        # Historique récent (60 derniers jours complets) pour le graphique
        from .services.forecasting import build_daily_series, InsufficientData
        history = []
        if f.status == EnergyForecast.Status.SUCCESS:
            try:
                s = build_daily_series(f.asset).dropna().tail(60)
                history = [(d.isoformat(), float(v)) for d, v in s.items()]
            except InsufficientData:
                pass
        ctx["chart_data"] = {
            "history": history,
            "forecast": [(p.date.isoformat(), round(p.predicted_kwh, 2)) for p in points],
            "actual": [(p.date.isoformat(), round(p.actual_kwh, 2)) for p in points if p.actual_kwh is not None],
            "interval": [(p.date.isoformat(), round(p.lower_kwh, 2), round(p.upper_kwh, 2))
                         for p in points if p.lower_kwh is not None and p.upper_kwh is not None],
        }
        m = f.metrics or {}
        ctx["interval"] = m.get("interval")
        ctx["importance"] = m.get("importance")
        ctx["importance_model"] = m.get("importance_model")
        ctx["importance_is_best"] = m.get("importance_is_best")
        validation = m.get("validation") or {}
        ctx["has_validation"] = bool(validation.get("dates"))
        ctx["validation_rows"] = list(zip(validation.get("dates", []), validation.get("actual", []),
                                          validation.get("best", []), validation.get("naive", [])))
        ctx["fx_data"] = {
            "validation": validation,
            "candidates": [{"label": c.get("label", k), "mae": round(c["mae"], 2), "rmse": round(c["rmse"], 2), "best": k == f.model_name}
                           for k, c in ctx["candidates"].items()],
            "importance": m.get("importance") or [],
            "best_label": f.model_label,
        }
        return ctx


class ForecastStatus(CompanyRequiredMixin, DetailView):
    """Endpoint JSON léger interrogé par la page pendant le calcul."""
    permission_required = "energy.view_energyforecast"

    def get_queryset(self):
        return EnergyForecast.objects.for_company(self.company)

    def render_to_response(self, context, **kw):
        f = self.object
        return JsonResponse({"status": f.status, "message": f.message, "done": f.status not in ("pending", "running")})


# ---------- Scénarios d'optimisation ----------
SCENARIO_PARENT = {"parent_label": "Scénarios", "parent_url": reverse_lazy("energy:scenario_list")}


class ScenarioList(CompanyRequiredMixin, ListView):
    permission_required = "energy.view_optimizationscenario"
    template_name = "energy/scenario_list.html"
    paginate_by = 20

    def get_queryset(self):
        return OptimizationScenario.objects.for_company(self.company).select_related("asset")


class ScenarioSaveMixin:
    """Enregistre puis calcule ; si le calcul est impossible, le scénario reste enregistré avec le message."""
    form_class = ScenarioForm
    template_name = "energy/form.html"

    def get_form_kwargs(self):
        return {**super().get_form_kwargs(), "company": self.company}

    def form_valid(self, form):
        scenario = form.save(commit=False)
        if not scenario.pk:
            scenario.created_by = self.request.user
        scenario.save()
        try:
            compute_scenario(scenario)
            messages.success(self.request, "Scénario calculé.")
        except ScenarioError as e:
            OptimizationScenario.objects.filter(pk=scenario.pk).update(
                baseline_kwh=None, optimized_kwh=None, saved_kwh=None, cost_saved=None, co2_saved_kg=None,
                payback_months=None, daily=[], result_message=str(e))
            messages.warning(self.request, "Scénario enregistré, mais le calcul est impossible pour l'instant.")
        return redirect("energy:scenario_detail", pk=scenario.pk)


class ScenarioCreate(ScenarioSaveMixin, CompanyRequiredMixin, FormView):
    permission_required = "energy.add_optimizationscenario"
    extra_context = {"title": "Nouveau scénario d'optimisation", **SCENARIO_PARENT}


class ScenarioUpdate(ScenarioSaveMixin, CompanyRequiredMixin, UpdateView):
    permission_required = "energy.change_optimizationscenario"
    extra_context = {"title": "Modifier le scénario", **SCENARIO_PARENT}

    def get_queryset(self):
        return OptimizationScenario.objects.for_company(self.company)


class ScenarioDetail(CompanyRequiredMixin, DetailView):
    permission_required = "energy.view_optimizationscenario"
    template_name = "energy/scenario_detail.html"

    def get_queryset(self):
        return OptimizationScenario.objects.for_company(self.company).select_related("asset")

    def get_context_data(self, **kw):
        ctx = super().get_context_data(**kw)
        ctx["chart_data"] = {"daily": self.object.daily}
        ctx["daily_rows"] = [(d, b, o, round(b - o, 2)) for d, b, o in self.object.daily]
        ctx["reduction_total_percent"] = (
            round(100 * self.object.saved_kwh / self.object.baseline_kwh, 1)
            if self.object.has_result and self.object.baseline_kwh else None)
        return ctx


class ScenarioDelete(CompanyRequiredMixin, DeleteView):
    permission_required = "energy.delete_optimizationscenario"
    template_name = "energy/confirm_delete.html"
    success_url = reverse_lazy("energy:scenario_list")

    def get_queryset(self):
        return OptimizationScenario.objects.for_company(self.company)

    def form_valid(self, form):
        messages.success(self.request, "Scénario supprimé.")
        return super().form_valid(form)


# ---------- Anomalies ----------
class AnomalyList(CompanyRequiredMixin, ListView):
    permission_required = "energy.view_energyanomaly"
    template_name = "energy/anomaly_list.html"
    paginate_by = 20

    def filters(self):
        g = self.request.GET
        status = g.get("status") if g.get("status") in EnergyAnomaly.Status.values else ""
        severity = g.get("severity") if g.get("severity") in EnergyAnomaly.Severity.values else ""
        return {"asset": parse_int_param(g.get("asset")), "status": status, "severity": severity}

    def get_queryset(self):
        f = self.filters()
        qs = EnergyAnomaly.objects.for_company(self.company).select_related("asset")
        if f["asset"]:
            qs = qs.filter(asset_id=f["asset"])
        if f["status"]:
            qs = qs.filter(status=f["status"])
        if f["severity"]:
            qs = qs.filter(severity=f["severity"])
        return qs

    def get_context_data(self, **kw):
        ctx = super().get_context_data(**kw)
        f = self.filters()
        base = EnergyAnomaly.objects.for_company(self.company)
        ctx.update(
            assets=EnergyAsset.objects.filter(company=self.company), f=f, selected_asset=str(f["asset"] or ""),
            statuses=EnergyAnomaly.Status.choices, severities=EnergyAnomaly.Severity.choices,
            n_new=base.filter(status=EnergyAnomaly.Status.NEW).count(),
            n_high=base.filter(status=EnergyAnomaly.Status.NEW, severity=EnergyAnomaly.Severity.HIGH).count(),
            detect_form=AnomalyDetectForm(company=self.company),
        )
        return ctx


class AnomalyDetect(CompanyRequiredMixin, View):
    permission_required = "energy.add_energyanomaly"
    http_method_names = ["post"]

    def post(self, request):
        form = AnomalyDetectForm(request.POST, company=self.company)
        if not form.is_valid():
            messages.error(request, "Asset invalide.")
            return redirect("energy:anomaly_list")
        asset = form.cleaned_data["asset"]
        assets = [asset] if asset else list(EnergyAsset.objects.filter(company=self.company, is_active=True))
        created = skipped = 0
        for a in assets:
            try:
                created += detect_for_asset(a)["created"]
            except AnomalyError as exc:
                skipped += 1
                if asset:
                    messages.warning(request, f"{a.name} : {exc}")
        total = EnergyAnomaly.objects.for_company(self.company).filter(status=EnergyAnomaly.Status.NEW).count()
        messages.success(request, f"Analyse terminée sur {len(assets) - skipped} asset(s) : {created} nouvelle(s) alerte(s), {total} à examiner au total.")
        if skipped and not asset:
            messages.warning(request, f"{skipped} asset(s) ignoré(s) faute de données suffisantes.")
        return redirect("energy:anomaly_list")


class AnomalyDetail(CompanyRequiredMixin, UpdateView):
    permission_required = "energy.view_energyanomaly"
    form_class = AnomalyReviewForm
    template_name = "energy/anomaly_detail.html"

    def get_queryset(self):
        return EnergyAnomaly.objects.for_company(self.company).select_related("asset", "reviewed_by")

    def post(self, request, *args, **kwargs):
        if not request.user.has_perm("energy.change_energyanomaly"):
            raise PermissionDenied
        return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        obj = form.save(commit=False)
        obj.reviewed_by, obj.reviewed_at = self.request.user, timezone.now()
        obj.save()
        messages.success(self.request, "Décision enregistrée.")
        return redirect("energy:anomaly_detail", pk=obj.pk)

    def get_context_data(self, **kw):
        ctx = super().get_context_data(**kw)
        a = self.object
        series = []
        try:
            s = build_daily_series(a.asset).dropna()
            lo, hi = a.date - timedelta(days=30), a.date + timedelta(days=30)
            series = [(d.isoformat(), round(float(v), 2)) for d, v in s.items() if lo <= d <= hi]
        except Exception:
            series = []
        ctx["chart_data"] = {"series": series, "date": a.date.isoformat(), "actual": a.actual_kwh, "expected": a.expected_kwh}
        ctx["can_review"] = self.request.user.has_perm("energy.change_energyanomaly")
        return ctx


class AnomalyExportCSV(CompanyRequiredMixin, View):
    permission_required = "energy.view_energyanomaly"

    def get(self, request):
        response = csv_response("anomalies.csv")
        w = csv.writer(response)
        w.writerow(["date", "asset", "type", "gravite", "reel_kwh", "attendu_kwh", "ecart_pct", "confirme_par_isolation_forest", "statut", "commentaire", "donnee_demo"])
        for a in EnergyAnomaly.objects.for_company(self.company).select_related("asset")[:EXPORT_MAX_ROWS]:
            w.writerow([a.date.isoformat(), csv_safe(a.asset.code), a.get_kind_display(), a.get_severity_display(), a.actual_kwh,
                        a.expected_kwh, a.deviation_pct, "oui" if a.iforest_agrees else "non", a.get_status_display(),
                        csv_safe(a.note), "oui" if a.is_demo else "non"])
        return response


# ---------- Exports CSV ----------
EXPORT_MAX_ROWS = 100_000


class ReadingExportCSV(CompanyRequiredMixin, View):
    permission_required = "energy.view_energyreading"

    def get(self, request):
        response = csv_response(f"mesures_{timezone.localdate():%Y%m%d}.csv")
        w = csv.writer(response)
        w.writerow(["asset_code", "asset_nom", "horodatage", "valeur", "unite", "donnee_demo"])
        qs = filtered_readings(request, self.company)[:EXPORT_MAX_ROWS]
        for r in qs.iterator():
            w.writerow([csv_safe(r.asset.code), csv_safe(r.asset.name), timezone.localtime(r.timestamp).strftime("%Y-%m-%d %H:%M"),
                        r.value, r.unit, "oui" if r.is_demo else "non"])
        return response


class ForecastExportCSV(CompanyRequiredMixin, View):
    permission_required = "energy.view_energyforecast"

    def get(self, request, pk):
        forecast = get_object_or_404(EnergyForecast.objects.for_company(self.company).select_related("asset"), pk=pk)
        update_actuals(forecast)
        response = csv_response(f"prevision_{slugify(forecast.asset.code) or 'asset'}_{forecast.pk}.csv")
        w = csv.writer(response)
        w.writerow(["date", "prevu_kwh", "bas_80pct_kwh", "haut_80pct_kwh", "reel_kwh", "ecart_kwh", "modele", "donnee_demo"])
        for p in forecast.points.all():
            w.writerow([p.date.isoformat(), round(p.predicted_kwh, 3),
                        "" if p.lower_kwh is None else round(p.lower_kwh, 3),
                        "" if p.upper_kwh is None else round(p.upper_kwh, 3),
                        "" if p.actual_kwh is None else round(p.actual_kwh, 3),
                        "" if p.error is None else round(p.error, 3),
                        forecast.model_name, "oui" if forecast.is_demo else "non"])
        return response


class ScenarioExportCSV(CompanyRequiredMixin, View):
    permission_required = "energy.view_optimizationscenario"

    def get(self, request, pk):
        sc = get_object_or_404(OptimizationScenario.objects.for_company(self.company).select_related("asset"), pk=pk)
        response = csv_response(f"scenario_{slugify(sc.name) or 'scenario'}_{sc.pk}.csv")
        w = csv.writer(response)
        w.writerow(["scenario", "asset", "reduction_pct", "part_concernee_pct", "prix_kwh_dt", "facteur_kgco2e_kwh", "source_reference", "donnee_demo"])
        w.writerow([csv_safe(sc.name), csv_safe(sc.asset.code), sc.reduction_percent, sc.affected_share_percent,
                    sc.price_per_kwh, sc.emission_factor, sc.baseline_source, "oui" if sc.is_demo else "non"])
        w.writerow([])
        w.writerow(["date", "reference_kwh", "avec_action_kwh", "economie_kwh"])
        for d, base, opt in sc.daily:
            w.writerow([d, base, opt, round(base - opt, 2)])
        return response
