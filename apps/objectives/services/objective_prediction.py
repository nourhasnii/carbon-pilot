"""Real machine-learning objective achievement prediction (scikit-learn).

CarbonPilot uses scikit-learn ``RandomForestClassifier`` to estimate whether a
reduction objective is likely to be achieved. The classifier is trained on the
tenant's own *resolved* historical objectives (those whose final outcome is
already known: ACHIEVED or MISSED) and their approved carbon activities. It is a
genuine supervised model — the ACHIEVED/AT RISK verdict and the probability come
from ``model.predict()`` and ``model.predict_proba()``, not from hardcoded
progress thresholds, random values or a simulated AI score.

What it does, step by step
--------------------------
1. **Input data.** Training reads only objectives that belong to a *single*
   company and already have a known final outcome. Data from different companies
   is never mixed, and a prediction never trains on the objective being scored
   (no leakage of its own outcome).
2. **Features.** Each objective is described by real CarbonPilot data:
   ``progress_percent``, ``remaining_days``, ``emissions_trend``,
   ``approved_activities_count`` and the Scope 1/2/3 breakdown (tCO2e) computed
   from *approved, non-archived* activities only. See ``FEATURE_NAMES``.
3. **Labels.** ``1`` for a historical objective that ended ACHIEVED, ``0`` for
   one that ended MISSED. Both classes must be present before the model is
   trained.
4. **Minimum data.** Below ``MIN_TRAINING_OBJECTIVES`` resolved objectives — or
   if either class has fewer than ``MIN_CLASS_SAMPLES`` examples — the service
   refuses to guess and returns ``INSUFFICIENT_DATA_MESSAGE``. No fake
   prediction is stored.
5. **No future leakage.** The target objective is scored using only activities
   up to the run date; historical objectives are scored at their final period
   end (their outcome is already known).
6. **Model.** ``RandomForestClassifier`` with a fixed ``random_state`` so the
   same data always yields the same verdict.
7. **Output.** ``predict_proba`` gives ``P(ACHIEVED)``, stored as the run's
   ``probability_achievement`` (0–100). ``predict`` gives the LIKELY TO ACHIEVE
   / AT RISK verdict. ``risk_level`` is a documented bucketing of that
   probability.

Limitations
-----------
The model only estimates probability from the patterns in the tenant's own
history. It is not a guarantee, and it makes no legal, regulatory or scientific
claim — a human should confirm any decision. Small tenants usually will not have
enough resolved objectives to train a model, in which case no prediction is
produced.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import numpy as np
from sklearn.ensemble import RandomForestClassifier

from django.db.models import Sum
from django.db.models.functions import TruncMonth
from django.utils import timezone

from apps.carbon.models import Scope

from ..models import (
	ExpectedOutcome,
	Objective,
	ObjectiveStatus,
	PredictionRun,
	RiskLevel,
)

# --- Tunable, documented constants ----------------------------------------- #
# A model is only trusted once enough resolved history exists. Both classes
# must be represented, otherwise the classifier cannot learn the distinction.
MIN_TRAINING_OBJECTIVES = 8
MIN_CLASS_SAMPLES = 3

# RandomForestClassifier hyper-parameters. The fixed seed makes the pipeline
# deterministic: identical data always produces identical verdicts.
FOREST_ESTIMATORS = 200
FOREST_RANDOM_STATE = 42

# Probability buckets used to label risk. This is presentation of the model's
# probability — the ACHIEVED/AT RISK verdict itself comes from predict().
RISK_LOW_MIN = 70.0     # ≥ 70% likely to achieve → LOW risk
RISK_MEDIUM_MIN = 40.0  # ≥ 40% likely to achieve → MEDIUM risk, below → HIGH

INSUFFICIENT_DATA_MESSAGE = "Insufficient historical data for reliable AI prediction."

MODEL_NAME = "RandomForestClassifier"
MODEL_VERSION = "1.0.0"

# The exact, ordered feature vector handed to the model. Documented so the audit
# trail is unambiguous about what the model actually sees.
FEATURE_NAMES = (
	"progress_percent",
	"remaining_days",
	"emissions_trend",
	"approved_activities_count",
	"scope1_tco2e",
	"scope2_tco2e",
	"scope3_tco2e",
)

# Supervised labels.
_LABEL_MISSED = 0
_LABEL_ACHIEVED = 1

_KG_PER_TONNE = 1000.0


@dataclass
class PredictionResult:
	"""Outcome of a single prediction run, safe to surface in the UI."""

	ok: bool = True
	message: str = ""
	insufficient: bool = False
	run: "PredictionRun | None" = None


def _scoped_activity_queryset(objective, as_of):
	"""Approved, non-archived activities in scope, never past ``as_of``."""
	queryset = objective.scoped_activities()
	if as_of is not None:
		# No future leakage: ignore anything dated after the moment we score.
		queryset = queryset.filter(period_start__lte=as_of)
	return queryset


def _scope_tonnes(queryset):
	"""Scope 1/2/3 totals (tCO2e) from approved activities."""
	totals = {
		row["scope"]: float(row["total"] or 0)
		for row in queryset.values("scope").annotate(total=Sum("co2e_kg"))
	}
	return (
		totals.get(Scope.SCOPE_1, 0.0) / _KG_PER_TONNE,
		totals.get(Scope.SCOPE_2, 0.0) / _KG_PER_TONNE,
		totals.get(Scope.SCOPE_3, 0.0) / _KG_PER_TONNE,
	)


def _emissions_trend(queryset):
	"""Signed relative change: recent monthly emissions vs earlier ones.

	Computed from the monthly CO2e totals of the in-scope approved activities.
	Positive means emissions are rising (bad for a reduction target).
	"""
	rows = (
		queryset.annotate(month=TruncMonth("period_start"))
		.values("month")
		.annotate(total=Sum("co2e_kg"))
		.order_by("month")
	)
	monthly = [float(row["total"] or 0) for row in rows if row["month"] is not None]
	if len(monthly) < 2:
		return 0.0
	half = len(monthly) // 2
	earlier = monthly[:half]
	recent = monthly[half:]
	earlier_avg = sum(earlier) / len(earlier)
	if earlier_avg <= 0:
		return 0.0
	recent_avg = sum(recent) / len(recent)
	return round((recent_avg - earlier_avg) / earlier_avg, 6)


def _feature_vector(objective, *, as_of=None):
	"""Build one feature row from real CarbonPilot data (see FEATURE_NAMES)."""
	as_of = as_of or timezone.localdate()
	scoped = _scoped_activity_queryset(objective, as_of)
	period_end = objective.period_bounds()[1]
	remaining_days = max(0, (period_end - as_of).days)
	scope1, scope2, scope3 = _scope_tonnes(scoped)
	return [
		float(objective.progress_percent),
		float(remaining_days),
		_emissions_trend(scoped),
		float(scoped.count()),
		scope1,
		scope2,
		scope3,
	]


def _training_rows(company, *, exclude_pk=None):
	"""Resolved historical objectives of one company (known outcome only)."""
	queryset = Objective.objects.filter(
		company=company,
		status__in=(ObjectiveStatus.ACHIEVED, ObjectiveStatus.MISSED),
	)
	if exclude_pk is not None:
		# Never train the model on the objective it is about to score.
		queryset = queryset.exclude(pk=exclude_pk)
	return list(queryset.select_related("department"))


def _build_training_data(company, *, exclude_pk=None):
	"""Return (X, y) built from the company's resolved objectives."""
	rows = _training_rows(company, exclude_pk=exclude_pk)
	features: list[list[float]] = []
	labels: list[int] = []
	for objective in rows:
		# Historical objectives are evaluated at their final, known outcome.
		period_end = objective.period_bounds()[1]
		features.append(_feature_vector(objective, as_of=period_end))
		labels.append(
			_LABEL_ACHIEVED
			if objective.status == ObjectiveStatus.ACHIEVED
			else _LABEL_MISSED
		)
	return features, labels


def _has_enough_data(labels):
	"""True only when both outcome classes are represented well enough."""
	if len(labels) < MIN_TRAINING_OBJECTIVES:
		return False
	if labels.count(_LABEL_ACHIEVED) < MIN_CLASS_SAMPLES:
		return False
	if labels.count(_LABEL_MISSED) < MIN_CLASS_SAMPLES:
		return False
	return True


def _risk_level(probability):
	"""Deterministic bucketing of the model probability into a risk band."""
	if probability >= RISK_LOW_MIN:
		return RiskLevel.LOW
	if probability >= RISK_MEDIUM_MIN:
		return RiskLevel.MEDIUM
	return RiskLevel.HIGH


def predict_objective(objective, *, as_of=None):
	"""Train a RandomForestClassifier and predict one objective's outcome.

	Always scoped to ``objective.company``: tenant isolation is enforced here,
	not by the caller. Returns a :class:`PredictionResult`. When there is not
	enough resolved history, no model is trained and no run is stored.
	"""
	if objective is None:
		return PredictionResult(ok=False, message="No objective selected for prediction.")

	company = objective.company
	as_of = as_of or timezone.localdate()

	features, labels = _build_training_data(company, exclude_pk=objective.pk)
	if not _has_enough_data(labels):
		return PredictionResult(
			ok=False, message=INSUFFICIENT_DATA_MESSAGE, insufficient=True
		)

	model = RandomForestClassifier(
		n_estimators=FOREST_ESTIMATORS,
		random_state=FOREST_RANDOM_STATE,
		class_weight="balanced",
	)
	model.fit(np.asarray(features, dtype=float), np.asarray(labels, dtype=int))

	x_current = np.asarray([_feature_vector(objective, as_of=as_of)], dtype=float)
	predicted_label = int(model.predict(x_current)[0])
	probabilities = model.predict_proba(x_current)[0]
	achieved_index = list(model.classes_).index(_LABEL_ACHIEVED)
	probability = float(probabilities[achieved_index]) * 100.0
	probability = round(max(0.0, min(probability, 100.0)), 2)

	expected_outcome = (
		ExpectedOutcome.LIKELY_TO_ACHIEVE
		if predicted_label == _LABEL_ACHIEVED
		else ExpectedOutcome.AT_RISK
	)

	run = PredictionRun.objects.create(
		objective=objective,
		probability_achievement=Decimal(str(probability)),
		risk_level=_risk_level(probability),
		expected_outcome=expected_outcome,
		current_progress_percent=Decimal(str(objective.progress_percent)),
		model_name=MODEL_NAME,
		model_version=MODEL_VERSION,
	)
	return PredictionResult(
		ok=True,
		message=(
			f"AI prediction generated from {len(labels)} historical objectives."
		),
		run=run,
	)


def latest_prediction(objective):
	"""The most recent stored prediction for an objective, or ``None``."""
	return objective.predictions.first()
