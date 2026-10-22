"""Détection d'anomalies de consommation (jour par jour), explicable et en deux temps.

1. Valeur attendue : médiane des mêmes jours de semaine des 8 semaines précédentes (un mardi se compare
   aux mardis, pas aux dimanches). La médiane résiste aux anomalies passées.
2. Écart robuste : (réel - attendu) / (1,4826 × MAD). Au-delà de ±3,5 → anomalie (seuil classique de
   Iglewicz et Hoaglin). Avec un écart minimal de 5 % pour ne pas alerter sur du bruit sur des courbes très régulières.
3. Confirmation : un Isolation Forest (apprentissage non supervisé) est entraîné sur les mêmes jours ;
   s'il isole aussi le jour, la gravité monte. Les deux méthodes en accord = alerte à examiner en priorité.

Rien n'est inventé : sans assez de jours valides, la détection est refusée avec un message.
"""
import math
from collections import defaultdict
from datetime import date, timedelta

import numpy as np
from django.db import transaction
from sklearn.ensemble import IsolationForest

from energy.models import EnergyAnomaly, EnergyAsset
from .forecasting import InsufficientData, build_daily_series

LOOKBACK_WEEKS = 8
MIN_HISTORY_DAYS = 28       # jours valides minimum pour estimer ce qui est « normal »
Z_THRESHOLD = 3.5
Z_HIGH = 6.0
MIN_RELATIVE_GAP = 0.05     # écart d'au moins 5 % de la valeur attendue
ZERO_RATIO = 0.05           # consommation < 5 % du normal = quasi nulle
SEED = 42


class AnomalyError(Exception):
    """Détection impossible (message affichable)."""


def _expected_by_weekday(values: dict[date, float], d: date):
    prev = [values[x] for x in sorted(k for k in values if k < d and k.weekday() == d.weekday())][-LOOKBACK_WEEKS:]
    return float(np.median(prev)) if len(prev) >= 3 else None


def analyse_series(values: dict[date, float]):
    """Retourne la liste d'anomalies (dicts) d'une série {jour: kWh}. Fonction pure, testable."""
    days = sorted(values)
    rows = []
    for d in days:
        exp = _expected_by_weekday(values, d)
        if exp is not None and exp > 0:
            rows.append((d, values[d], exp))
    if len(rows) < MIN_HISTORY_DAYS // 2:
        raise AnomalyError("Pas assez de jours comparables pour définir ce qui est normal.")

    resid = np.array([a - e for _, a, e in rows])
    mad = float(np.median(np.abs(resid - np.median(resid))))
    scale = 1.4826 * mad
    if scale < 1e-9:  # série parfaitement régulière : on prend un écart minimal pour éviter la division par zéro
        scale = max(1e-6, 0.01 * float(np.median([e for _, _, e in rows])))

    # Isolation Forest sur (écart relatif, écart robuste, jour de la semaine)
    X = np.array([[(a - e) / e, (a - e) / scale, d.weekday()] for d, a, e in rows])
    iso_flags = np.zeros(len(rows), dtype=bool)
    if len(rows) >= 30:
        iso = IsolationForest(n_estimators=200, contamination="auto", random_state=SEED).fit(X)
        iso_flags = iso.predict(X) == -1

    out = []
    for (d, a, e), iso_flag in zip(rows, iso_flags):
        z = (a - e) / scale
        gap = (a - e) / e
        if abs(z) < Z_THRESHOLD or abs(gap) < MIN_RELATIVE_GAP:
            continue
        if a < ZERO_RATIO * e:
            kind = EnergyAnomaly.Kind.ZERO
        else:
            kind = EnergyAnomaly.Kind.SPIKE if z > 0 else EnergyAnomaly.Kind.DROP
        high = abs(z) >= Z_HIGH or (bool(iso_flag) and abs(z) >= Z_THRESHOLD + 1)
        wd = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"][d.weekday()]
        sign = "au-dessus" if gap > 0 else "en dessous"
        out.append({
            "date": d, "actual": a, "expected": e, "z": z, "kind": kind,
            "severity": EnergyAnomaly.Severity.HIGH if high else EnergyAnomaly.Severity.MEDIUM,
            "iforest": bool(iso_flag),
            "explanation": (f"{abs(gap) * 100:.0f} % {sign} de la normale d'un {wd} : {a:.0f} kWh mesurés "
                            f"pour {e:.0f} kWh attendus."
                            + (" Consommation quasi nulle : compteur arrêté ou donnée manquante ?" if kind == EnergyAnomaly.Kind.ZERO else "")),
        })
    return out


def detect_for_asset(asset: EnergyAsset, since_days: int = 180) -> dict:
    """Détecte et enregistre les anomalies des `since_days` derniers jours. Conserve le suivi déjà saisi."""
    try:
        series = build_daily_series(asset)
    except InsufficientData as exc:
        raise AnomalyError(str(exc))
    valid = series.dropna()
    if len(valid) < MIN_HISTORY_DAYS:
        raise AnomalyError(f"Données insuffisantes : {len(valid)} jour(s) valide(s), il en faut au moins {MIN_HISTORY_DAYS}.")
    values = {d: float(v) for d, v in valid.items()}
    found = analyse_series(values)
    cutoff = max(values) - timedelta(days=since_days)
    found = [f for f in found if f["date"] >= cutoff]
    is_demo = asset.readings.filter(is_demo=True).exists()

    created = updated = 0
    with transaction.atomic():
        keep = set()
        for f in found:
            keep.add(f["date"])
            obj, was_created = EnergyAnomaly.objects.update_or_create(
                asset=asset, date=f["date"],
                defaults=dict(actual_kwh=round(f["actual"], 2), expected_kwh=round(f["expected"], 2), z_score=round(f["z"], 2),
                              kind=f["kind"], severity=f["severity"], iforest_agrees=f["iforest"],
                              explanation=f["explanation"][:300], is_demo=is_demo))
            created += was_created
            updated += not was_created
        # Les alertes jamais examinées qui ne sont plus anormales disparaissent ; les examinées sont conservées.
        removed, _ = (EnergyAnomaly.objects.filter(asset=asset, status=EnergyAnomaly.Status.NEW, date__gte=cutoff)
                      .exclude(date__in=keep).delete())
    return {"created": created, "updated": updated, "removed": removed, "analysed_days": len(values)}
