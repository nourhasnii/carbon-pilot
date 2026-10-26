"""Évaluation de la détection d'anomalies sur des séries synthétiques à vérité connue.

Les données réelles n'ont pas d'étiquettes « anomalie / normal ». On fabrique donc des séries réalistes
(rythme hebdomadaire, tendance lente, bruit gaussien), on y injecte des anomalies à des jours connus, puis on
compare ce que le détecteur trouve à ce qu'on a injecté : précision, rappel, F1 et matrice de confusion.
Tout est déterministe (graines fixes) : mêmes chiffres à chaque exécution.
"""
import random
from datetime import date, timedelta

from .anomalies import analyse_series

START = date(2026, 1, 5)  # un lundi
INJECTION_FACTORS = [1.8, 1.5, 0.55, 0.4, 0.02]  # pic fort, pic modéré, chute modérée, chute forte, quasi nul


def make_series(seed: int, days: int = 140, noise: float = 0.03, n_anomalies: int = 6):
    """Retourne (série {jour: kWh}, ensemble des jours réellement anormaux)."""
    rng = random.Random(seed)
    values = {}
    for i in range(days):
        d = START + timedelta(days=i)
        base = (100.0 + 0.05 * i) * (1.0 if d.weekday() < 5 else 0.6)
        values[d] = base * (1 + rng.gauss(0, noise))
    candidates = list(range(40, days))  # laisser 5 semaines d'historique avant la première anomalie
    rng.shuffle(candidates)
    truth, chosen = set(), []
    for i in candidates:
        if len(chosen) == n_anomalies:
            break
        if all(abs(i - j) > 3 for j in chosen):  # anomalies isolées (pas collées l'une à l'autre)
            chosen.append(i)
    for i in chosen:
        d = START + timedelta(days=i)
        values[d] *= rng.choice(INJECTION_FACTORS)
        truth.add(d)
    return values, truth


def evaluate(seeds=range(30), **kw):
    """Compare détecteur et vérité sur plusieurs séries. Retourne matrice de confusion et métriques."""
    tp = fp = fn = tn = 0
    for seed in seeds:
        values, truth = make_series(seed, **kw)
        found = {a["date"] for a in analyse_series(values)}
        # On n'évalue que les jours où le détecteur dispose d'un historique (au moins 3 mêmes jours de semaine)
        scored = {d for d in values if d >= START + timedelta(days=21)}
        for d in scored:
            if d in truth and d in found:
                tp += 1
            elif d in truth:
                fn += 1
            elif d in found:
                fp += 1
            else:
                tn += 1
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn, "precision": precision, "recall": recall, "f1": f1,
            "series": len(list(seeds)) if hasattr(seeds, "__len__") else None}
