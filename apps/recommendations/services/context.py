"""STEP A — build the factual context the recommendation engine reasons over.

Everything in here is read from the database for **one** company, resolved from
the authenticated user. The context is the only thing Qwen ever sees: the
candidate evidence (open anomalies, at-risk objectives, approved-emission
summaries) plus an explicit allowlist of numeric facts and their provenance.

Rules honoured here:
* only real database values — unknown values stay ``None`` (rendered as JSON null);
* approved, non-archived activities are the only source of official emissions;
* a prediction/probability is included *only* when a stored
  :class:`~apps.objectives.models.PredictionRun` exists — never fabricated;
* data from another company can never enter the payload.
"""

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Case, IntegerField, Q, Value, When
from django.utils import timezone

from apps.carbon.models import ActivityStatus, AnomalySeverity, AnomalyStatus
from apps.carbon.services import analytics
from apps.carbon.services.tenancy import visible_activities, visible_anomalies
from apps.objectives.models import ObjectiveStatus
from apps.objectives.services import objective_prediction, visible_objectives
from apps.enterprise.models import Department

from .access import visible_recommendations

# Caps keep the prompt concise and the run bounded.
MAX_ANOMALIES = 15
MAX_OBJECTIVES = 15
MAX_BREAKDOWN = 6
MAX_EXISTING = 20
MAX_FACTS = 160

# A draft activity left untouched for this many days is flagged as stale.
STALE_DRAFT_DAYS = 30

_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")

# Structural numbers that are always legitimate in prose ("Scope 1/2/3", "CO2e").
_BASE_NUMBERS = (0.0, 1.0, 2.0, 3.0)


# --------------------------------------------------------------------------- #
# Numeric grounding helpers
# --------------------------------------------------------------------------- #
def _to_float(token):
	if token is None:
		return None
	if isinstance(token, bool):
		return None
	text = str(token).strip()
	if not text:
		return None
	if "," in text and "." in text:
		text = text.replace(",", "")
	else:
		text = text.replace(",", ".")
	try:
		return float(text)
	except ValueError:
		return None


def _forms(value):
	"""Accepted representations of a number (0, 1 or 2 decimal places)."""
	return {round(value, 0), round(value, 1), round(value, 2)}


def _iter_numbers(value):
	if isinstance(value, bool):
		return
	if isinstance(value, (int, float, Decimal)):
		parsed = _to_float(value)
		if parsed is not None:
			yield parsed
	elif isinstance(value, str):
		for token in _NUMBER_RE.findall(value):
			parsed = _to_float(token)
			if parsed is not None:
				yield parsed
	elif isinstance(value, dict):
		for item in value.values():
			yield from _iter_numbers(item)
	elif isinstance(value, (list, tuple)):
		for item in value:
			yield from _iter_numbers(item)


def collect_numbers(value):
	"""Every number (and every textual number) in ``value`` as grounded forms."""
	forms = set(_BASE_NUMBERS)
	for number in _iter_numbers(value):
		forms |= _forms(number)
	return forms


def collect_facts(value, path=""):
	"""Curated ``{value, source}`` allowlist with provenance paths."""
	facts = []
	if isinstance(value, bool):
		return facts
	if isinstance(value, (int, float, Decimal)):
		parsed = _to_float(value)
		if parsed is not None:
			facts.append({"value": parsed, "source": path})
		return facts
	if isinstance(value, dict):
		for key, item in value.items():
			child = f"{path}.{key}" if path else str(key)
			facts.extend(collect_facts(item, child))
	elif isinstance(value, (list, tuple)):
		for index, item in enumerate(value):
			facts.extend(collect_facts(item, f"{path}[{index}]"))
	return facts


def ungrounded_number(text, allowed):
	"""Return the first numeric token in ``text`` with no grounded counterpart."""
	for token in _NUMBER_RE.findall(text or ""):
		parsed = _to_float(token)
		if parsed is None:
			continue
		if not (_forms(parsed) & allowed):
			return token
	return None


# --------------------------------------------------------------------------- #
# Context object
# --------------------------------------------------------------------------- #
@dataclass
class FactualContext:
	"""Tenant-scoped evidence plus the allowlist used to validate Qwen's answer."""

	company_id: int
	company_name: str
	as_of: date
	payload: dict
	existing: list
	allowed_numbers: set = field(default_factory=set)
	anomaly_ids: set = field(default_factory=set)
	objective_ids: set = field(default_factory=set)
	company_department_ids: set = field(default_factory=set)

	def is_empty(self):
		"""True when there is no evidence worth sending to the model."""
		return not self.payload.get("open_anomalies") and not self.payload.get(
			"at_risk_objectives"
		)

	def resolve_evidence(self, value):
		"""Resolve an ``evidence_ids`` entry to ``(kind, id)`` or ``None``.

		Accepts a bare integer (matched against supplied anomalies/objectives) or
		a prefixed string such as ``"anomaly:45"`` / ``"objective:12"``.
		"""
		if isinstance(value, bool):
			return None
		if isinstance(value, int):
			if value in self.anomaly_ids:
				return ("anomaly", value)
			if value in self.objective_ids:
				return ("objective", value)
			return None
		if isinstance(value, str):
			match = re.fullmatch(r"\s*(anomaly|objective|department)\s*[:_-]?\s*(\d+)\s*", value.lower())
			if not match:
				return None
			kind, raw_id = match.group(1), int(match.group(2))
			if kind == "anomaly" and raw_id in self.anomaly_ids:
				return (kind, raw_id)
			if kind == "objective" and raw_id in self.objective_ids:
				return (kind, raw_id)
			if kind == "department" and raw_id in self.company_department_ids:
				return (kind, raw_id)
		return None


# --------------------------------------------------------------------------- #
# Row builders (all tenant- and role-scoped)
# --------------------------------------------------------------------------- #
def _tco2e(kilograms):
	if kilograms is None:
		return None
	return float(analytics.to_tonnes(kilograms))


def _number(value):
	if value is None:
		return None
	return float(value)


_SEVERITY_ORDER = Case(
	When(severity=AnomalySeverity.HIGH, then=Value(0)),
	When(severity=AnomalySeverity.MEDIUM, then=Value(1)),
	default=Value(2),
	output_field=IntegerField(),
)


def _open_anomaly_rows(user, company):
	queryset = (
		visible_anomalies(user, company)
		.filter(status=AnomalyStatus.NEW)
		.select_related("department", "activity")
		.order_by(_SEVERITY_ORDER, "-detected_at")[:MAX_ANOMALIES]
	)
	rows = []
	for anomaly in queryset:
		activity = anomaly.activity if anomaly.activity_id else None
		rows.append(
			{
				"id": anomaly.pk,
				"severity": anomaly.severity,
				"anomaly_type": anomaly.anomaly_type,
				"activity_id": activity.pk if activity is not None else None,
				"activity_name": activity.display_name if activity is not None else None,
				"category": activity.category if activity is not None else None,
				"scope": activity.scope if activity is not None else None,
				"department_id": anomaly.department_id,
				"department_name": anomaly.department.name if anomaly.department_id else None,
				"period_start": anomaly.period_start.isoformat() if anomaly.period_start else None,
				"period_end": anomaly.period_end.isoformat() if anomaly.period_end else None,
				"co2e_tco2e": _tco2e(activity.co2e_kg) if activity is not None else None,
				"score": _number(anomaly.score),
				"status": anomaly.status,
				"message": (anomaly.message or "").strip() or None,
			}
		)
	return rows


def _at_risk_objective_rows(user, company, as_of):
	queryset = (
		visible_objectives(user, company)
		.exclude(status=ObjectiveStatus.ARCHIVED)
		.select_related("department")
		.order_by("year", "name")
	)
	rows = []
	for objective in queryset:
		gap = objective.gap_tco2e
		if gap <= 0:
			# Already met (or no reduction committed) — nothing to recommend.
			continue
		_, end = objective.period_bounds()
		remaining = (end - as_of).days
		prediction = objective_prediction.latest_prediction(objective)
		rows.append(
			{
				"id": objective.pk,
				"name": objective.name,
				"department_id": objective.department_id,
				"department_name": objective.department.name if objective.department_id else None,
				"year": objective.year,
				"period_label": objective.period_label,
				"baseline_tco2e": _number(objective.baseline_tco2e),
				"target_tco2e": _number(objective.target_tco2e),
				"current_tco2e": _number(objective.current_tco2e),
				"gap_tco2e": _number(gap),
				"progress_percent": _number(objective.progress_percent),
				"remaining_days": remaining if remaining > 0 else 0,
				"deadline": end.isoformat(),
				"prediction": (
					{
						"probability_achievement": _number(prediction.probability_achievement),
						"risk_level": prediction.risk_level,
						"expected_outcome": prediction.expected_outcome,
						"model_name": prediction.model_name,
						"created_at": prediction.created_at.isoformat(),
					}
					if prediction is not None
					else None
				),
			}
		)
		if len(rows) >= MAX_OBJECTIVES:
			break
	return rows


def _approved_emission_summary(user, company, year):
	approved = visible_activities(user, company).filter(
		status=ActivityStatus.APPROVED, is_archived=False
	)
	current = approved.filter(period_start__year=year)
	previous = approved.filter(period_start__year=year - 1)
	return {
		"year": year,
		"total_tco2e": float(analytics.total_tonnes(current)),
		"previous_year_total_tco2e": float(analytics.total_tonnes(previous)),
		"by_scope": [
			{
				"scope": row["key"],
				"label": row["label"],
				"tco2e": float(row["tonnes"]),
				"percent": row["percent"],
			}
			for row in analytics.scope_breakdown(current)
		],
		"by_category": [
			{
				"category": row["key"],
				"label": row["label"],
				"tco2e": float(row["tonnes"]),
				"percent": row["percent"],
				"activities": row["activities"],
			}
			for row in analytics.category_breakdown(current)[:MAX_BREAKDOWN]
		],
		"by_department": [
			{
				"department_name": row["label"],
				"tco2e": float(row["tonnes"]),
				"percent": row["percent"],
				"activities": row["activities"],
			}
			for row in analytics.department_breakdown(current)[:MAX_BREAKDOWN]
		],
	}


def _existing_open_rows(user, company):
	queryset = (
		visible_recommendations(user, company)
		.open()
		.select_related("department")
		.order_by("-created_at")[:MAX_EXISTING]
	)
	return [
		{
			"id": rec.pk,
			"title": rec.title,
			"priority": rec.priority,
			"status": rec.status,
			"source": rec.source,
			"department_id": rec.department_id,
			"objective_id": rec.objective_id,
			"anomaly_id": rec.anomaly_id,
			"generation_method": rec.generation_method,
		}
		for rec in queryset
	]


def _data_quality_signals(user, company, as_of):
	"""Tenant-scoped data-quality counts used as evidence and by the rules.

	Only real, countable rows are reported — nothing is inferred. A zero means
	the check found nothing, which keeps the signals honest rather than
	alarmist. These feed both the EVIDENCE section and the deterministic
	DATA_QUALITY recommendation.
	"""
	activities = visible_activities(user, company).filter(is_archived=False)
	signals = {
		"unvalidated_activities": activities.filter(
			status__in=(ActivityStatus.SUBMITTED, ActivityStatus.UNDER_REVIEW)
		).count(),
		"activities_missing_emission_factor": activities.exclude(
			status=ActivityStatus.REJECTED
		)
		.filter(emission_factor__isnull=True)
		.count(),
		"stale_draft_activities": activities.filter(
			status=ActivityStatus.DRAFT,
			created_at__date__lt=as_of - timedelta(days=STALE_DRAFT_DAYS),
		).count(),
		"submitted_without_evidence": activities.filter(
			status=ActivityStatus.SUBMITTED, evidence=""
		).count(),
	}
	signals["total_issues"] = sum(
		signals[key]
		for key in (
			"unvalidated_activities",
			"activities_missing_emission_factor",
			"stale_draft_activities",
			"submitted_without_evidence",
		)
	)
	return signals


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def build_context(user, company, *, as_of=None):
	"""Build the tenant-scoped factual context, or ``None`` when no tenant."""
	if company is None:
		return None
	as_of = as_of or timezone.localdate()

	anomalies = _open_anomaly_rows(user, company)
	objectives = _at_risk_objective_rows(user, company, as_of)
	emissions = _approved_emission_summary(user, company, as_of.year)
	existing = _existing_open_rows(user, company)
	data_quality = _data_quality_signals(user, company, as_of)

	payload = {
		"company": {"id": company.pk, "name": company.name},
		"as_of": as_of.isoformat(),
		"open_anomalies": anomalies,
		"at_risk_objectives": objectives,
		"approved_emissions": emissions,
		"data_quality": data_quality,
	}

	context = FactualContext(
		company_id=company.pk,
		company_name=company.name,
		as_of=as_of,
		payload=payload,
		existing=existing,
		allowed_numbers=collect_numbers(payload),
		anomaly_ids={row["id"] for row in anomalies},
		objective_ids={row["id"] for row in objectives},
		company_department_ids=set(
			Department.objects.filter(company=company).values_list("pk", flat=True)
		),
	)
	return context


def numeric_facts(context):
	"""Curated numeric allowlist for evidence the AI is allowed to consider."""
	payload = {key: value for key, value in context.payload.items() if key != "data_quality"}
	return collect_facts(payload)[:MAX_FACTS]
