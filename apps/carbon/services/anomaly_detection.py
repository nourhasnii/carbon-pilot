"""Machine-learning carbon anomaly detection (scikit-learn IsolationForest).

CarbonPilot uses scikit-learn IsolationForest to identify unusual carbon
emission patterns from historical approved activities. This is a real
unsupervised model fitted on the tenant's own data — not a random or simulated
score. See ``docs/anomaly_detection.md`` for the full technical write-up.

What it does, step by step
--------------------------
1. **Input data.** Only activities that are ``APPROVED``, not archived and
   belonging to a *single* company are ever read. Draft, submitted, rejected
   and archived rows are excluded, and data from different companies is never
   mixed.
2. **Grouping.** Activities are grouped by ``department + category + reporting
   year`` so that, for example, electricity is compared with electricity of the
   same department and period — never with unrelated waste or transport rows.
3. **Minimum data.** A group needs at least ``MIN_GROUP_OBSERVATIONS`` values
   before the model runs. Smaller groups are skipped so no false anomaly is
   invented from thin data.
4. **Preprocessing.** Each observation is described by two numeric features —
   ``co2e_kg`` and ``quantity`` — standardised (zero mean, unit variance) with
   ``StandardScaler`` so no single feature dominates purely by scale.
5. **Model.** ``IsolationForest`` (an ensemble of random isolation trees) is
   fitted per group with a fixed ``random_state`` so the pipeline is
   deterministic: identical data always yields identical verdicts.
6. **Anomaly score.** The forest's ``decision_function`` output is min-max
   normalised to ``[0, 1]`` (higher = more unusual) and stored as the log's
   ``score``.
7. **Severity & type.** The score is combined with the ratio of the value to
   the group's historical median to derive a deterministic LOW/MEDIUM/HIGH
   severity and an OUTLIER/SPIKE/TREND_INCREASE type. SPIKE and TREND_INCREASE
   are only used when the data actually supports them, otherwise OUTLIER.

Limitations
-----------
This model only flags observations that are *statistically* unusual relative to
the tenant's own history. It does not know the business cause and it makes no
legal, regulatory or scientific claim — a human reviewer must confirm every
finding.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

import numpy as np
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler

from django.db.models import Count, Q
from django.utils import timezone

from apps.analytics.models import ModelKind, ModelMetrics
from apps.carbon.models import (
	ActivityCategory,
	ActivityStatus,
	AnomalyLog,
	AnomalySeverity,
	AnomalyStatus,
	AnomalyType,
	CarbonActivity,
)

# --- Tunable, documented constants ----------------------------------------- #
# A group needs this many approved observations before the model is trusted.
# Below it the service refuses to guess, so no false anomaly is created.
MIN_GROUP_OBSERVATIONS = 8

# IsolationForest hyper-parameters. The fixed seed makes the whole pipeline
# deterministic: the same data always produces the same verdicts.
FOREST_ESTIMATORS = 200
FOREST_RANDOM_STATE = 42

# Scoring thresholds used to turn the model output into an explainable label.
SPIKE_RATIO = 2.0      # ≥ 2× the group median is treated as a spike
MEDIUM_RATIO = 1.5     # ≥ 1.5× the group median raises severity to MEDIUM
HIGH_SCORE = 0.75      # normalised score above this is highly unusual
MEDIUM_SCORE = 0.5     # normalised score above this is moderately unusual
TREND_CORRELATION = 0.5  # minimum |r| for an increasing trend to be "supported"

INSUFFICIENT_DATA_MESSAGE = "Insufficient historical data for anomaly detection."

MODEL_NAME = "IsolationForest"
MODEL_VERSION = "1.0.0"

_FEATURE_CO2E = "co2e_kg"
_FEATURE_QUANTITY = "quantity"


@dataclass
class DetectionResult:
	"""Outcome of a single detection run, safe to surface in the UI."""

	ok: bool = True
	message: str = ""
	insufficient: bool = False
	detected: int = 0
	created: int = 0
	updated: int = 0
	activities_analyzed: int = 0
	groups_analyzed: int = 0
	groups_skipped: int = 0

	@property
	def has_insufficient_groups(self):
		return self.groups_skipped > 0


def _approved_activity_queryset(company):
	"""Approved, non-archived, CO2e-bearing activities of one company only."""
	return (
		CarbonActivity.objects.filter(
			company=company,
			status=ActivityStatus.APPROVED,
			is_archived=False,
			co2e_kg__isnull=False,
		)
		.select_related("department")
		.order_by("period_start", "pk")
	)


def _group_key(activity):
	"""Meaningful comparison group: department + category + reporting year."""
	return (activity.department_id, activity.category, activity.period_start.year)


def _normalised_scores(decision_values):
	"""Map ``decision_function`` output to [0, 1] where 1 = most unusual."""
	low = float(np.min(decision_values))
	high = float(np.max(decision_values))
	if high - low < 1e-12:
		return np.zeros(len(decision_values), dtype=float)
	# decision_function is high for normal points, so invert it.
	return (high - decision_values) / (high - low)


def _has_increasing_trend(co2e_values):
	"""True when the series rises over time with a supported correlation."""
	if len(co2e_values) < 3:
		return False
	values = np.asarray(co2e_values, dtype=float)
	if float(np.std(values)) < 1e-12:
		return False
	steps = np.arange(len(values), dtype=float)
	slope = float(np.polyfit(steps, values, 1)[0])
	correlation = float(np.corrcoef(steps, values)[0, 1])
	return slope > 0 and correlation >= TREND_CORRELATION


def _classify(ratio, score, trend_supported, is_latest):
	"""Derive a deterministic anomaly type and severity. Documented in module docstring."""
	if ratio >= SPIKE_RATIO:
		anomaly_type = AnomalyType.SPIKE
	elif trend_supported and is_latest:
		anomaly_type = AnomalyType.TREND_INCREASE
	else:
		anomaly_type = AnomalyType.OUTLIER

	if score >= HIGH_SCORE and ratio >= SPIKE_RATIO:
		severity = AnomalySeverity.HIGH
	elif score >= MEDIUM_SCORE or ratio >= MEDIUM_RATIO:
		severity = AnomalySeverity.MEDIUM
	else:
		severity = AnomalySeverity.LOW
	return anomaly_type, severity


def _build_message(anomaly_type, ratio, department_name, category_label):
	"""Plain-language reason. Never asserts a business cause the data can't prove."""
	where = f"{department_name} · {category_label}"
	if anomaly_type == AnomalyType.SPIKE:
		reason = (
			f"CO2e is {ratio:.1f}× the historical median for {where} — "
			"significantly higher than the usual pattern."
		)
	elif anomaly_type == AnomalyType.TREND_INCREASE:
		reason = (
			f"CO2e for {where} has been rising across recent periods and the "
			"latest value sits above the historical trend."
		)
	else:
		reason = (
			f"CO2e for {where} is unusual compared with other approved "
			"activities in the same group."
		)
	return (
		reason
		+ " Flagged statistically by the model; the underlying business cause is not determined."
	)


def _to_score(value):
	return Decimal(str(value)).quantize(Decimal("0.00001"), rounding=ROUND_HALF_UP)


def _fingerprint(company, activity):
	"""Deterministic identity so re-running detection never duplicates a row."""
	period = activity.period_start.isoformat() if activity.period_start else "na"
	return f"{company.pk}:{activity.pk}:{period}"


def _analyse_group(group):
	"""Fit an IsolationForest on one group and return its flagged findings."""
	co2e = np.array([float(activity.co2e_kg) for activity in group], dtype=float)
	quantity = np.array([float(activity.quantity) for activity in group], dtype=float)
	features = np.column_stack([co2e, quantity])

	scaled = StandardScaler().fit_transform(features)
	forest = IsolationForest(
		n_estimators=FOREST_ESTIMATORS,
		random_state=FOREST_RANDOM_STATE,
		contamination="auto",
	)
	forest.fit(scaled)
	labels = forest.predict(scaled)
	normalised = _normalised_scores(forest.decision_function(scaled))

	median = float(np.median(co2e))
	trend_supported = _has_increasing_trend(co2e.tolist())
	latest_pk = group[-1].pk

	findings = []
	for index, activity in enumerate(group):
		if labels[index] != -1:
			continue  # the forest considers this observation normal
		value = co2e[index]
		ratio = (value / median) if median > 0 else 1.0
		score = round(float(normalised[index]), 5)
		anomaly_type, severity = _classify(
			ratio, score, trend_supported, activity.pk == latest_pk
		)
		department_name = activity.department.name if activity.department else "Unassigned"
		category_label = dict(ActivityCategory.choices).get(
			activity.category, activity.category
		)
		findings.append(
			(
				activity,
				anomaly_type,
				severity,
				_to_score(score),
				_build_message(anomaly_type, ratio, department_name, category_label),
			)
		)
	return findings


def _persist(company, activity, anomaly_type, severity, score, message, actor):
	"""Create or update one log, preserving a reviewer's status on re-runs."""
	fingerprint = _fingerprint(company, activity)
	fields = {
		"company": company,
		"department": activity.department,
		"activity": activity,
		"anomaly_type": anomaly_type,
		"score": score,
		"severity": severity,
		"message": message,
		"period_start": activity.period_start,
		"period_end": activity.period_end,
		"detected_at": timezone.now(),
		"detected_by": actor if getattr(actor, "is_authenticated", False) else None,
	}
	existing = AnomalyLog.objects.filter(fingerprint=fingerprint).first()
	if existing is None:
		AnomalyLog.objects.create(
			fingerprint=fingerprint, status=AnomalyStatus.NEW, **fields
		)
		return True
	for name, value in fields.items():
		setattr(existing, name, value)
	existing.save(update_fields=[*fields.keys(), "updated_at"])
	return False


def model_params():
	"""Hyper-parameters this service actually uses, for the metrics panel."""
	return {
		"contamination": "auto",
		"min_observations": MIN_GROUP_OBSERVATIONS,
		"n_estimators": FOREST_ESTIMATORS,
		"random_state": FOREST_RANDOM_STATE,
	}


def _record_metrics(company, result):
	"""Store one append-only ``ModelMetrics`` snapshot of this detection run."""
	ModelMetrics.objects.create(
		company=company,
		model_kind=ModelKind.ANOMALY_DETECTION,
		model_name=MODEL_NAME,
		model_version=MODEL_VERSION,
		activities_analyzed=result.activities_analyzed,
		groups_analyzed=result.groups_analyzed,
		groups_skipped=result.groups_skipped,
		anomalies_found=result.detected,
		params=model_params(),
		insufficient_data=result.insufficient,
		note=result.message,
	)


def detect_anomalies(company, *, actor=None):
	"""Run IsolationForest on one company's approved activities.

	Always scoped to ``company``: tenant isolation is enforced here, not by the
	caller. Returns a :class:`DetectionResult`.
	"""
	if company is None:
		return DetectionResult(
			ok=False, message="No company selected for anomaly detection."
		)

	activities = list(_approved_activity_queryset(company))

	groups = {}
	for activity in activities:
		groups.setdefault(_group_key(activity), []).append(activity)

	if not groups:
		result = DetectionResult(
			ok=True,
			message=INSUFFICIENT_DATA_MESSAGE,
			insufficient=True,
		)
		_record_metrics(company, result)
		return result

	result = DetectionResult(activities_analyzed=len(activities))
	for group in groups.values():
		group.sort(key=lambda item: (item.period_start, item.pk))
		if len(group) < MIN_GROUP_OBSERVATIONS:
			result.groups_skipped += 1
			continue
		result.groups_analyzed += 1
		for finding in _analyse_group(group):
			activity, anomaly_type, severity, score, message = finding
			created = _persist(
				company, activity, anomaly_type, severity, score, message, actor
			)
			result.detected += 1
			if created:
				result.created += 1
			else:
				result.updated += 1

	if result.groups_analyzed == 0:
		result.insufficient = True
		result.message = INSUFFICIENT_DATA_MESSAGE
	elif result.detected == 0:
		result.message = "No anomalies detected in the current approved activities."
	else:
		result.message = (
			f"{result.detected} anomal"
			f"{'y' if result.detected == 1 else 'ies'} detected."
		)
	_record_metrics(company, result)
	return result


def anomaly_counts(queryset):
	"""Aggregate totals/high/medium/low/new counts for any anomaly queryset."""
	return queryset.aggregate(
		total=Count("id"),
		high=Count("id", filter=Q(severity=AnomalySeverity.HIGH)),
		medium=Count("id", filter=Q(severity=AnomalySeverity.MEDIUM)),
		low=Count("id", filter=Q(severity=AnomalySeverity.LOW)),
		new=Count("id", filter=Q(status=AnomalyStatus.NEW)),
	)
