"""Scénarios d'optimisation : combien d'énergie, d'argent et de CO₂ une réduction permettrait d'éviter.

Principe (volontairement simple et explicable) :
    économie_jour = référence_jour × part_concernée × réduction
La « référence » est la prévision la plus récente de l'asset si elle existe pour le même horizon,
sinon la moyenne des 8 dernières semaines par jour de semaine. Rien n'est inventé : sans historique
suffisant, le calcul est refusé avec un message clair.
"""
from datetime import timedelta
from decimal import Decimal

import numpy as np
from django.utils import timezone

from ..models import EnergyForecast, OptimizationScenario
from .forecasting import InsufficientData, build_daily_series

HISTORY_WEEKS = 8
MIN_VALID_DAYS = 14


class ScenarioError(Exception):
    """Calcul impossible (message affichable à l'utilisateur)."""


def _baseline_from_forecast(asset, horizon):
    f = (
        EnergyForecast.objects.filter(asset=asset, status=EnergyForecast.Status.SUCCESS, horizon_days=horizon)
        .order_by("-created_at").first()
    )
    if not f:
        return None
    pts = list(f.points.all())
    if len(pts) < horizon or pts[-1].date < timezone.localdate():
        return None  # prévision incomplète ou déjà passée : elle ne décrit plus la période à venir
    return [(p.date, float(p.predicted_kwh)) for p in pts[:horizon]]


def _baseline_from_history(asset, horizon):
    try:
        series = build_daily_series(asset)
    except InsufficientData as e:
        raise ScenarioError(str(e))
    recent = series.dropna().tail(HISTORY_WEEKS * 7)
    if len(recent) < MIN_VALID_DAYS:
        raise ScenarioError(
            f"Données insuffisantes : {len(recent)} jour(s) valide(s), il en faut au moins {MIN_VALID_DAYS} "
            "pour estimer une consommation de référence."
        )
    by_wd = {}
    for d, v in recent.items():
        by_wd.setdefault(d.weekday(), []).append(float(v))
    overall = float(np.mean(recent.values))
    start = series.index[-1] + timedelta(days=1)
    return [(start + timedelta(days=i), float(np.mean(by_wd.get((start + timedelta(days=i)).weekday(), [overall]))))
            for i in range(horizon)]


def compute_scenario(scenario: OptimizationScenario) -> OptimizationScenario:
    """Calcule et enregistre les résultats. Lève ScenarioError si la référence est impossible."""
    base = _baseline_from_forecast(scenario.asset, scenario.horizon_days)
    source = OptimizationScenario.Source.FORECAST
    if base is None:
        base = _baseline_from_history(scenario.asset, scenario.horizon_days)
        source = OptimizationScenario.Source.HISTORY

    factor = float(Decimal(scenario.reduction_percent) / 100 * Decimal(scenario.affected_share_percent) / 100)
    daily = [[d.isoformat(), round(v, 2), round(v * (1 - factor), 2)] for d, v in base]
    baseline = sum(r[1] for r in daily)
    saved = baseline * factor
    cost = saved * float(scenario.price_per_kwh)
    co2 = saved * float(scenario.emission_factor)

    payback = None
    if scenario.investment_cost and cost > 0:
        yearly = cost / scenario.horizon_days * 365  # projection linéaire de la période simulée
        payback = float(scenario.investment_cost) / yearly * 12

    scenario.baseline_source = source
    scenario.baseline_kwh = round(baseline, 2)
    scenario.optimized_kwh = round(baseline - saved, 2)
    scenario.saved_kwh = round(saved, 2)
    scenario.cost_saved = round(cost, 2)
    scenario.co2_saved_kg = round(co2, 2)
    scenario.payback_months = None if payback is None else round(payback, 1)
    scenario.daily = daily
    scenario.result_message = ""
    scenario.computed_at = timezone.now()
    scenario.save()
    return scenario
