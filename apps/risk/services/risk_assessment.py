"""Deterministic, rule-based ESG / carbon risk assessment.

CarbonPilot scores a company's carbon/ESG risk from the data it already holds.
This is deliberately **not** a machine-learning model: it is a transparent,
reproducible rule-based sum of five capped components, built from *real*
database counts and values. The two existing models are reused rather than
re-trained — open anomalies come from
:mod:`apps.carbon.services.anomaly_detection` (IsolationForest) and objective
risk comes from :mod:`apps.objectives.services.objective_prediction`
(RandomForestClassifier). No synthetic, random or hardcoded AI value is ever
invented here.

Scoring model (total = 100)
---------------------------
1. **Open anomalies** (max 30) — NEW HIGH/MEDIUM severity anomaly logs.
2. **Objective risk** (max 30) — missed objectives and objectives whose latest
   prediction is at medium/high risk (low achievement probability).
3. **Data quality** (max 15) — the inverse of the average data-quality score of
   approved activities.
4. **Missing evidence** (max 10) — approved activities with no evidence file.
5. **Review backlog** (max 15) — activities still awaiting review.

Risk levels
-----------
``score < 35`` → LOW; ``35 <= score < 65`` → MEDIUM; ``score >= 65`` → HIGH.

Tenant isolation is enforced by the caller (views resolve the company from the
authenticated user); every queryset here starts from the given ``company`` so
no other tenant's data can ever contribute to a score.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from django.db.models import Q

from apps.carbon.models import (
	ActivityStatus,
	AnomalyLog,
	AnomalySeverity,
	AnomalyStatus,
	CarbonActivity,
)
from apps.carbon.services import data_quality
from apps.objectives.models import (
	Objective,
	ObjectiveStatus,
	PredictionRun,
)
from apps.objectives.models import (
	RiskLevel as ObjectiveRiskLevel,
)

from ..models import RiskAssessment, RiskLevel, RiskMethod

# --- Component weights (points). The five weights sum to 100. --------------- #
ANOMALY_WEIGHT = 30
OBJECTIVE_WEIGHT = 30
DATA_QUALITY_WEIGHT = 15
EVIDENCE_WEIGHT = 10
BACKLOG_WEIGHT = 15

TOTAL_WEIGHT = (
	ANOMALY_WEIGHT
	+ OBJECTIVE_WEIGHT
	+ DATA_QUALITY_WEIGHT
	+ EVIDENCE_WEIGHT
	+ BACKLOG_WEIGHT
)

# Fixed component catalogue shown in the UI scoring breakdown (display order).
SCORING_WEIGHTS = (
	{
		"key": "anomalies",
		"label": "Open anomalies",
		"max_points": ANOMALY_WEIGHT,
		"description": "Open high/medium anomalies flagged by the detector.",
	},
	{
		"key": "objectives",
		"label": "Objective risk",
		"max_points": OBJECTIVE_WEIGHT,
		"description": "Missed objectives and at-risk objective predictions.",
	},
	{
		"key": "data_quality",
		"label": "Data quality",
		"max_points": DATA_QUALITY_WEIGHT,
		"description": "Average data quality of approved activities.",
	},
	{
		"key": "evidence",
		"label": "Missing evidence",
		"max_points": EVIDENCE_WEIGHT,
		"description": "Approved activities without supporting evidence.",
	},
	{
		"key": "backlog",
		"label": "Review backlog",
		"max_points": BACKLOG_WEIGHT,
		"description": "Activities still awaiting review.",
	},
)

# --- Per-item points inside a component (each capped by the weight). -------- #
# Anomalies: one open HIGH anomaly is worth more than a MEDIUM one.
POINTS_PER_HIGH_ANOMALY = 8
POINTS_PER_MEDIUM_ANOMALY = 4

# Objectives: a missed target is the strongest signal, then at-risk predictions.
POINTS_PER_MISSED_OBJECTIVE = 12
POINTS_PER_HIGH_RISK_PREDICTION = 8
POINTS_PER_MEDIUM_RISK_PREDICTION = 4

# Predicted achievement probability below this signals objective risk.
LOW_PROBABILITY_THRESHOLD = Decimal("40.00")

# Review backlog: points per activity still awaiting review.
POINTS_PER_PENDING_ACTIVITY = 3

# Risk-level thresholds (score is 0-100).
LEVEL_LOW_MAX = Decimal("35")     # score < 35 → LOW
LEVEL_MEDIUM_MAX = Decimal("65")  # 35 <= score < 65 → MEDIUM, else HIGH

# Activities that are waiting on a reviewer.
PENDING_STATUSES = (ActivityStatus.SUBMITTED, ActivityStatus.UNDER_REVIEW)


@dataclass
class Driver:
	"""One ranked reason behind the score. ``points`` is what it contributed."""

	key: str
	label: str
	points: Decimal
	max_points: int
	detail: str

	def as_dict(self):
		return {
			"key": self.key,
			"label": self.label,
			"points": float(self.points),
			"max_points": self.max_points,
			"detail": self.detail,
		}


@dataclass
class RiskResult:
	"""Outcome of one assessment run, safe to surface in the UI or persist."""

	score: Decimal
	level: str
	summary: str
	method: str
	drivers: list          # ranked, only what contributed (points > 0)
	breakdown: list        # every component, including zero contributors
	insufficient_data: bool = False
	period_year: int | None = None

	def as_dict(self):
		return {
			"score": self.score,
			"level": self.level,
			"summary": self.summary,
			"method": self.method,
			"drivers": [driver.as_dict() for driver in self.drivers],
			"breakdown": [driver.as_dict() for driver in self.breakdown],
			"insufficient_data": self.insufficient_data,
			"period_year": self.period_year,
		}


def _q(value):
	"""Quantise a Decimal to two places, the storage precision of the score."""
	return Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _cap(points, weight):
	return points if points < weight else Decimal(weight)


def level_for(score):
	"""Map a 0-100 score to its LOW / MEDIUM / HIGH level."""
	score = Decimal(score)
	if score < LEVEL_LOW_MAX:
		return RiskLevel.LOW
	if score < LEVEL_MEDIUM_MAX:
		return RiskLevel.MEDIUM
	return RiskLevel.HIGH


def _approved_activities(company, period_year=None):
	queryset = CarbonActivity.objects.filter(
		company=company,
		status=ActivityStatus.APPROVED,
		is_archived=False,
	)
	if period_year is not None:
		queryset = queryset.filter(period_start__year=period_year)
	return queryset


def _anomaly_component(company, period_year=None):
	"""Open (NEW) HIGH/MEDIUM anomalies — the strongest single signal."""
	queryset = AnomalyLog.objects.filter(
		company=company,
		status=AnomalyStatus.NEW,
		severity__in=(AnomalySeverity.HIGH, AnomalySeverity.MEDIUM),
	)
	if period_year is not None:
		queryset = queryset.filter(period_start__year=period_year)
	high = queryset.filter(severity=AnomalySeverity.HIGH).count()
	medium = queryset.filter(severity=AnomalySeverity.MEDIUM).count()
	points = _cap(
		Decimal(high * POINTS_PER_HIGH_ANOMALY + medium * POINTS_PER_MEDIUM_ANOMALY),
		ANOMALY_WEIGHT,
	)
	detail = (
		f"{high} open high-severity and {medium} open medium-severity anomal"
		f"{'y' if (high + medium) == 1 else 'ies'} awaiting review."
	)
	return Driver("anomalies", "Open anomalies", points, ANOMALY_WEIGHT, detail)


def _objective_component(company, period_year=None):
	"""Missed objectives and objectives predicted to be at risk."""
	queryset = Objective.objects.filter(company=company).exclude(
		status=ObjectiveStatus.ARCHIVED
	)
	if period_year is not None:
		queryset = queryset.filter(year=period_year)
	objectives = list(queryset)
	objective_ids = [objective.pk for objective in objectives]

	# One query, latest run per objective (PredictionRun is ordered newest first).
	latest_runs = {}
	if objective_ids:
		runs = PredictionRun.objects.filter(objective_id__in=objective_ids).order_by(
			"objective_id", "-created_at", "-id"
		)
		for run in runs:
			latest_runs.setdefault(run.objective_id, run)

	missed = 0
	high_risk = 0
	medium_risk = 0
	low_probability = 0
	for objective in objectives:
		if objective.status == ObjectiveStatus.MISSED:
			missed += 1
		run = latest_runs.get(objective.pk)
		if run is None:
			continue
		if run.risk_level == ObjectiveRiskLevel.HIGH:
			high_risk += 1
		elif run.risk_level == ObjectiveRiskLevel.MEDIUM:
			medium_risk += 1
		if run.probability_achievement < LOW_PROBABILITY_THRESHOLD:
			low_probability += 1

	points = _cap(
		Decimal(
			missed * POINTS_PER_MISSED_OBJECTIVE
			+ high_risk * POINTS_PER_HIGH_RISK_PREDICTION
			+ medium_risk * POINTS_PER_MEDIUM_RISK_PREDICTION
		),
		OBJECTIVE_WEIGHT,
	)
	detail = (
		f"{missed} missed objective(s), {high_risk} predicted high risk and "
		f"{medium_risk} predicted medium risk ({low_probability} with a low "
		"achievement probability)."
	)
	return Driver("objectives", "Objective risk", points, OBJECTIVE_WEIGHT, detail)


def _data_quality_component(company, period_year=None):
	"""The worse the average data quality, the more risk it contributes."""
	activities = list(
		_approved_activities(company, period_year).select_related("emission_factor")
	)
	if not activities:
		return Driver(
			"data_quality",
			"Data quality",
			Decimal("0"),
			DATA_QUALITY_WEIGHT,
			"No approved activities to assess data quality.",
		)
	average = data_quality.average_score(activities)
	points = _q(
		Decimal(str((100 - average) / 100)) * Decimal(DATA_QUALITY_WEIGHT)
	)
	points = _cap(points, DATA_QUALITY_WEIGHT)
	detail = (
		f"Average data quality is {average}% across {len(activities)} approved "
		"activity(ies)."
	)
	return Driver("data_quality", "Data quality", points, DATA_QUALITY_WEIGHT, detail)


def _evidence_component(company, period_year=None):
	"""Approved activities that are missing their supporting evidence."""
	queryset = _approved_activities(company, period_year)
	total = queryset.count()
	if not total:
		return Driver(
			"evidence",
			"Missing evidence",
			Decimal("0"),
			EVIDENCE_WEIGHT,
			"No approved activities, so no evidence can be missing.",
		)
	missing = queryset.filter(
		Q(evidence__isnull=True) | Q(evidence="")
	).count()
	points = _cap(
		Decimal(str(missing / total)) * Decimal(EVIDENCE_WEIGHT),
		EVIDENCE_WEIGHT,
	)
	points = _q(points)
	detail = (
		f"{missing} of {total} approved activity(ies) lack supporting evidence."
	)
	return Driver("evidence", "Missing evidence", points, EVIDENCE_WEIGHT, detail)


def _backlog_component(company, period_year=None):
	"""Activities still waiting in the review queue."""
	queryset = CarbonActivity.objects.filter(
		company=company,
		is_archived=False,
		status__in=PENDING_STATUSES,
	)
	if period_year is not None:
		queryset = queryset.filter(period_start__year=period_year)
	pending = queryset.count()
	points = _cap(Decimal(pending * POINTS_PER_PENDING_ACTIVITY), BACKLOG_WEIGHT)
	detail = f"{pending} activity(ies) awaiting review."
	return Driver("backlog", "Review backlog", points, BACKLOG_WEIGHT, detail)


def _rank(breakdown):
	"""Rank drivers by contribution; ties break by weight then key."""
	return sorted(
		breakdown,
		key=lambda driver: (-driver.points, -driver.max_points, driver.key),
	)


def _build_summary(company_name, level, score, ranked, insufficient):
	level_label = RiskLevel(level).label
	base = f"{company_name}: {level_label} risk (score {score}/100)."
	if insufficient:
		base += (
			" No approved activity data is available yet, so the assessment may "
			"understate risk."
		)
	top = [driver for driver in ranked if driver.points > 0][:2]
	if top:
		base += " " + " ".join(driver.detail for driver in top)
	else:
		base += " No material risk factors were detected in the current data."
	return base


def assess_company(company, *, period_year=None):
	"""Compute the current rule-based risk for one company (no persistence).

	Only the given ``company``'s data is read. Returns a :class:`RiskResult`
	with the score, level, summary, ranked drivers and full breakdown.
	"""
	if company is None:
		raise ValueError("A company is required to assess risk.")

	components = [
		_anomaly_component(company, period_year),
		_objective_component(company, period_year),
		_data_quality_component(company, period_year),
		_evidence_component(company, period_year),
		_backlog_component(company, period_year),
	]
	total = sum((component.points for component in components), Decimal("0"))
	score = _q(min(max(total, Decimal("0")), Decimal("100")))
	level = level_for(score)
	ranked = _rank(components)
	# Ranked drivers explain why: only the components that actually contributed.
	drivers = [driver for driver in ranked if driver.points > 0]

	approved_count = _approved_activities(company, period_year).count()
	insufficient = approved_count == 0

	summary = _build_summary(company.name, level, score, ranked, insufficient)
	return RiskResult(
		score=score,
		level=level,
		summary=summary,
		method=RiskMethod.RULE_BASED,
		drivers=drivers,
		breakdown=components,
		insufficient_data=insufficient,
		period_year=period_year,
	)


def run_assessment(company, *, actor=None, period_year=None):
	"""Run an assessment and persist it as an immutable :class:`RiskAssessment`."""
	result = assess_company(company, period_year=period_year)
	return RiskAssessment.objects.create(
		company=company,
		score=result.score,
		level=result.level,
		summary=result.summary,
		drivers_json=[driver.as_dict() for driver in result.drivers],
		period_year=period_year,
		method=result.method,
		created_by=actor if getattr(actor, "is_authenticated", False) else None,
	)


def latest_assessment(company):
	"""The most recent stored assessment for a company, or ``None``."""
	if company is None:
		return None
	return RiskAssessment.objects.filter(company=company).latest_first().first()
