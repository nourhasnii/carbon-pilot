"""Moteur de prévision de consommation (module 4).

Principes (cahier des charges) :
- AUCUNE valeur inventée : les jours manquants/incomplets restent manquants (NaN) ; les
  lignes d'apprentissage qui en dépendent sont simplement écartées.
- Données insuffisantes -> statut « insufficient_data » et message clair, pas de fausse prévision.
- Évaluation honnête : découpage CHRONOLOGIQUE (les 20 % les plus récents servent de test),
  backtest à l'horizon demandé (prévision récursive par blocs), métriques MAE et RMSE.
- Comparaison de modèles : baseline saisonnière naïve (valeur d'il y a 7 jours), RandomForest,
  GradientBoosting. Le meilleur (RMSE puis MAE) est ré-entraîné sur toutes les données.
- Convention : les prévisions sont journalières, en kWh ; seuls les jours COMPLETS (< aujourd'hui) comptent.
"""
from __future__ import annotations

import math
import time
from datetime import date, timedelta

import numpy as np
import pandas as pd
from django.utils import timezone
from sklearn.ensemble import GradientBoostingRegressor, RandomForestRegressor
from sklearn.inspection import permutation_importance

from apps.energy.models import EnergyForecast, EnergyReading, ForecastPoint

MIN_DAYS = {7: 60, 30: 120}  # jours valides minimum selon l'horizon
TEST_FRACTION = 0.2
MIN_BLOCK = 7
LAGS = (1, 7, 14)
COMPLETENESS = 0.8  # un jour est valide s'il a >= 80 % du nombre médian de mesures par jour
SEED = 42
INTERVAL_LEVEL = 80  # la fourchette contient ~80 % des écarts observés pendant le test
MIN_RESIDUALS = 20
TO_KWH = {"kWh": 1.0, "MWh": 1000.0}


class InsufficientData(Exception):
    pass


# ---------- Série journalière ----------
def build_daily_series(asset, today: date | None = None) -> pd.Series:
    """kWh par jour local, jours incomplets/manquants = NaN, index continu jusqu'au dernier jour valide."""
    today = today or timezone.localdate()
    rows = list(
        EnergyReading.objects.filter(asset=asset, unit__in=TO_KWH.keys()).values_list("timestamp", "value", "unit")
    )
    if not rows:
        raise InsufficientData("Aucune mesure en kWh/MWh pour cet asset.")
    df = pd.DataFrame(rows, columns=["ts", "value", "unit"])
    df["kwh"] = df["value"].astype(float) * df["unit"].map(TO_KWH)
    df["day"] = pd.to_datetime(df["ts"], utc=True).dt.tz_convert(timezone.get_current_timezone()).dt.date
    df = df[df["day"] < today]  # on exclut le jour en cours (incomplet)
    if df.empty:
        raise InsufficientData("Aucun jour complet disponible.")
    g = df.groupby("day").agg(total=("kwh", "sum"), n=("kwh", "size"))
    median_n = g["n"].median()
    g.loc[g["n"] < COMPLETENESS * median_n, "total"] = np.nan  # jour incomplet : non utilisé
    full_index = pd.date_range(g.index.min(), g.index.max(), freq="D").date
    series = g["total"].reindex(full_index)
    return series


# ---------- Features ----------
def _features(d: date, hist: dict):
    lags = []
    for L in LAGS:
        v = hist.get(d - timedelta(days=L))
        if v is None or (isinstance(v, float) and math.isnan(v)):
            return None
        lags.append(v)
    window = [hist.get(d - timedelta(days=k)) for k in range(1, 8)]
    window = [w for w in window if w is not None and not (isinstance(w, float) and math.isnan(w))]
    if len(window) < 5:
        return None
    doy = d.timetuple().tm_yday
    return [
        d.weekday(), 1 if d.weekday() >= 5 else 0, d.month,
        math.sin(2 * math.pi * doy / 365.25), math.cos(2 * math.pi * doy / 365.25),
        *lags, float(np.mean(window)),
    ]


def _training_matrix(hist: dict, until: date | None = None):
    X, y = [], []
    for d, v in hist.items():
        if v is None or math.isnan(v) or (until and d >= until):
            continue
        f = _features(d, hist)
        if f is not None:
            X.append(f)
            y.append(v)
    return np.array(X), np.array(y)


FEATURE_GROUPS = [  # (libellé lisible, indices des colonnes de _features)
    ("Jour de la semaine", [0, 1]),
    ("Mois / saison", [2, 3, 4]),
    ("Consommation de la veille", [5]),
    ("Consommation il y a 7 jours", [6]),
    ("Consommation il y a 14 jours", [7]),
    ("Moyenne des 7 derniers jours", [8]),
]


# ---------- Modèles ----------
def _make_model(name):
    if name == "random_forest":
        return RandomForestRegressor(n_estimators=200, min_samples_leaf=2, random_state=SEED, n_jobs=1)
    if name == "gradient_boosting":
        return GradientBoostingRegressor(n_estimators=200, max_depth=3, learning_rate=0.05, random_state=SEED)
    raise ValueError(name)


MODEL_LABELS = {
    "seasonal_naive": "Baseline saisonnière (valeur J-7)",
    "random_forest": "RandomForestRegressor",
    "gradient_boosting": "GradientBoostingRegressor",
}


def _recursive_forecast(predict_one, hist: dict, start: date, steps: int):
    """Prévision récursive : chaque prédiction devient disponible comme lag pour les suivantes."""
    work = dict(hist)
    out = {}
    for i in range(steps):
        d = start + timedelta(days=i)
        v = predict_one(d, work)
        if v is None:
            return None
        v = max(0.0, float(v))
        work[d] = v
        out[d] = v
    return out


def _predictor(name, model=None):
    if name == "seasonal_naive":
        def f(d, hist):
            v = hist.get(d - timedelta(days=7))
            return None if v is None or (isinstance(v, float) and math.isnan(v)) else v
        return f

    def f(d, hist):
        feats = _features(d, hist)
        return None if feats is None else model.predict(np.array([feats]))[0]
    return f


def _metrics(pairs):
    a = np.array([p[0] for p in pairs])
    p = np.array([p[1] for p in pairs])
    err = p - a
    return {"mae": float(np.mean(np.abs(err))), "rmse": float(math.sqrt(np.mean(err ** 2))), "n": int(len(a))}


def evaluate_models(hist: dict, dates: list, horizon: int, detail: dict | None = None):
    """Backtest chronologique à l'horizon demandé. Retourne {modèle: {mae, rmse, n}}."""
    split = dates[int(len(dates) * (1 - TEST_FRACTION))]
    X, y = _training_matrix(hist, until=split)
    if len(y) < 30:
        raise InsufficientData("Pas assez de lignes d'apprentissage valides (jours consécutifs manquants).")

    blocks = []
    o, end = split, dates[-1]
    while o <= end:
        steps = min(horizon, (end - o).days + 1)
        if steps >= MIN_BLOCK:
            blocks.append((o, steps))
        o += timedelta(days=horizon)
    if not blocks:
        raise InsufficientData("Période de test trop courte pour évaluer le modèle.")

    results = {}
    for name in MODEL_LABELS:
        model = None if name == "seasonal_naive" else _make_model(name).fit(X, y)
        predict = _predictor(name, model)
        pairs, rows = [], []
        for origin, steps in blocks:
            seed_hist = {d: v for d, v in hist.items() if d < origin}  # uniquement le passé réel
            pred = _recursive_forecast(predict, seed_hist, origin, steps)
            if pred is None:
                continue
            for d, pv in pred.items():
                av = hist.get(d)
                if av is not None and not math.isnan(av):
                    pairs.append((av, pv))
                    rows.append((d, av, pv))
        if pairs:
            results[name] = _metrics(pairs)
            if detail is not None:
                detail[name] = rows
    if "random_forest" not in results or "seasonal_naive" not in results:
        raise InsufficientData("Évaluation impossible : trop de jours manquants dans la période de test.")
    return results


def _interval_offsets(residuals):
    """Décalages (bas, haut) en kWh autour d'une prévision, d'après les écarts réels du backtest.

    Quantiles empiriques 10 % / 90 % de (réel - prévu) : la fourchette couvre ~80 % des cas observés.
    Avec trop peu d'écarts, on retombe sur une approximation gaussienne (±1,28 écart-type).
    """
    r = np.array(residuals, dtype=float)
    if len(r) >= MIN_RESIDUALS:
        lo, hi = float(np.quantile(r, 0.10)), float(np.quantile(r, 0.90))
        method = "quantiles"
    else:
        sd = float(np.std(r)) if len(r) > 1 else 0.0
        lo, hi, method = -1.2816 * sd, 1.2816 * sd, "gaussienne"
    return {"level": INTERVAL_LEVEL, "low": round(lo, 3), "high": round(hi, 3), "n": int(len(r)), "method": method}


def _validation_series(detail, best):
    """Prévu/réel sur la période de test pour le modèle retenu et la baseline (même dates)."""
    best_rows = {d: (a, p) for d, a, p in detail.get(best, [])}
    naive_rows = {d: p for d, a, p in detail.get("seasonal_naive", [])}
    dates = sorted(d for d in best_rows if d in naive_rows)
    return {
        "dates": [d.isoformat() for d in dates],
        "actual": [round(best_rows[d][0], 2) for d in dates],
        "best": [round(best_rows[d][1], 2) for d in dates],
        "naive": [round(naive_rows[d], 2) for d in dates],
    }


def _feature_importance(best, hist, dates):
    """Importance par permutation sur la période de test : de combien l'erreur grimpe si on brouille une variable.

    Évalué sur des données que le modèle n'a pas vues (réentraîné sur le seul passé), en kWh de RMSE.
    """
    if best == "seasonal_naive":
        return None
    split = dates[int(len(dates) * (1 - TEST_FRACTION))]
    X, y = _training_matrix(hist, until=split)
    rows = [(d, _features(d, hist)) for d in dates if d >= split and hist.get(d) is not None and not math.isnan(hist[d])]
    rows = [(d, f) for d, f in rows if f is not None]
    if len(y) < 30 or len(rows) < 10:
        return None
    Xt = np.array([f for _, f in rows])
    yt = np.array([hist[d] for d, _ in rows])
    model = _make_model(best).fit(X, y)
    imp = {}
    for label, cols in FEATURE_GROUPS:
        # on brouille ensemble les colonnes d'un même groupe (ex. sin/cos de la saison)
        rng = np.random.default_rng(SEED)
        base = math.sqrt(np.mean((model.predict(Xt) - yt) ** 2))
        scores = []
        for _ in range(5):
            Xp = Xt.copy()
            perm = rng.permutation(len(Xp))
            Xp[:, cols] = Xt[perm][:, cols]
            scores.append(math.sqrt(np.mean((model.predict(Xp) - yt) ** 2)) - base)
        imp[label] = max(0.0, float(np.mean(scores)))
    total = sum(imp.values())
    if total <= 0:
        return None
    return sorted(({"feature": k, "share": round(100 * v / total, 1)} for k, v in imp.items()), key=lambda r: -r["share"])


# ---------- Orchestration ----------
def run_forecast(forecast: EnergyForecast) -> EnergyForecast:
    """Exécute le forecast, met à jour l'objet (statut, métriques, points). Ne lève jamais."""
    t0 = time.monotonic()
    forecast.status = EnergyForecast.Status.RUNNING
    forecast.save(update_fields=["status"])
    try:
        series = build_daily_series(forecast.asset)
        valid_days = int(series.notna().sum())
        need = MIN_DAYS[forecast.horizon_days]
        if valid_days < need:
            raise InsufficientData(
                f"Données insuffisantes : {valid_days} jour(s) valide(s), il en faut au moins {need} "
                f"pour une prévision à {forecast.horizon_days} jours. Consultez l'historique (analyse descriptive)."
            )
        hist = {d: (None if pd.isna(v) else float(v)) for d, v in series.items()}
        dates = list(series.index)
        horizon = forecast.horizon_days

        detail = {}
        results = evaluate_models(hist, dates, horizon, detail)
        best = min(results, key=lambda n: (results[n]["rmse"], results[n]["mae"]))

        # Si la baseline gagne, on explique quand même le meilleur modèle à arbres : ce qui pèse sur la consommation.
        trees = [n for n in results if n != "seasonal_naive"]
        explain = best if best != "seasonal_naive" else min(trees, key=lambda n: (results[n]["rmse"], results[n]["mae"]))
        X, y = _training_matrix(hist)
        model = None if best == "seasonal_naive" else _make_model(best).fit(X, y)
        origin = dates[-1] + timedelta(days=1)
        pred = _recursive_forecast(_predictor(best, model), hist, origin, horizon)
        if pred is None:
            raise InsufficientData("Les 14 derniers jours contiennent des mesures manquantes : prévision impossible.")

        naive_rmse = results["seasonal_naive"]["rmse"]
        forecast.model_name = best
        forecast.mae, forecast.rmse = results[best]["mae"], results[best]["rmse"]
        forecast.metrics = {
            "candidates": {k: {**v, "label": MODEL_LABELS[k]} for k, v in results.items()},
            "best": best,
            "improvement_vs_naive_pct": round(100 * (1 - results[best]["rmse"] / naive_rmse), 1) if naive_rmse else None,
            "validation": _validation_series(detail, best),
            "interval": _interval_offsets([a - p for _, a, p in detail.get(best, [])]),
            "importance": _feature_importance(explain, hist, dates),
            "importance_model": MODEL_LABELS[explain],
            "importance_is_best": explain == best,
            "evaluation": "backtest chronologique (20 % les plus récents), prévision récursive à l'horizon demandé",
        }
        forecast.n_training_days = valid_days
        forecast.training_start, forecast.training_end = dates[0], dates[-1]
        forecast.is_demo = forecast.asset.readings.filter(is_demo=True).exists()
        forecast.status = EnergyForecast.Status.SUCCESS
        forecast.message = ""
        if origin <= timezone.localdate() - timedelta(days=1):
            # Données non récentes : on prévient clairement au lieu de présenter une « prévision d'avenir ».
            forecast.message = (
                f"Attention : les dernières mesures valides datent du {dates[-1]:%d/%m/%Y}. La prévision commence le "
                f"{origin:%d/%m/%Y}, une période déjà écoulée. Importez les mesures récentes pour prévoir l'avenir."
            )
        forecast.points.all().delete()
        iv = forecast.metrics["interval"]
        ForecastPoint.objects.bulk_create(
            ForecastPoint(forecast=forecast, date=d, predicted_kwh=v,
                          lower_kwh=round(max(0.0, v + iv["low"]), 3), upper_kwh=round(v + iv["high"], 3))
            for d, v in pred.items())
    except InsufficientData as exc:
        forecast.status = EnergyForecast.Status.INSUFFICIENT
        forecast.message = str(exc)
    except Exception as exc:  # erreur technique : ne jamais exposer une trace à l'utilisateur
        forecast.status = EnergyForecast.Status.FAILED
        forecast.message = "Une erreur est survenue pendant le calcul. Réessayez ou contactez un administrateur."
        import logging
        logging.getLogger(__name__).exception("Forecast %s failed: %s", forecast.pk, exc)
    forecast.duration_seconds = round(time.monotonic() - t0, 3)
    forecast.finished_at = timezone.now()
    forecast.save()
    return forecast


def update_actuals(forecast: EnergyForecast):
    """Compare prévu/réel : renseigne actual_kwh pour les jours désormais complets, retourne métriques réalisées."""
    if forecast.status != EnergyForecast.Status.SUCCESS:
        return None
    try:
        series = build_daily_series(forecast.asset)
    except InsufficientData:
        return None
    pts = list(forecast.points.all())
    changed, pairs = [], []
    for p in pts:
        v = series.get(p.date)
        if v is not None and not pd.isna(v):
            if p.actual_kwh != float(v):
                p.actual_kwh = float(v)
                changed.append(p)
            pairs.append((float(v), p.predicted_kwh))
    if changed:
        ForecastPoint.objects.bulk_update(changed, ["actual_kwh"])
    if not pairs:
        return None
    m = _metrics(pairs)
    a = np.array([x[0] for x in pairs])
    p = np.array([x[1] for x in pairs])
    m["mape_pct"] = float(np.mean(np.abs(p - a) / np.where(a == 0, np.nan, a)) * 100) if np.any(a != 0) else None
    return m
