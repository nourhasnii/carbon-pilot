"""Deterministic, database-grounded evidence for a recommendation.

Every value returned here is read from the tenant-scoped factual context
(:mod:`context`), which is itself built from real, company-scoped rows. When a
metric is not available the builder emits the explicit sentinel
``"Data unavailable"`` rather than guessing, so a fabricated figure can never
reach the screen.

These items are ALWAYS produced on the server — never by the model. That is
what lets the EVIDENCE section honesty-hold even for an AI-generated
recommendation: Qwen decides the *narrative* (problem / analysis /
recommendation / action plan), CarbonPilot supplies the *facts*.
"""

from datetime import date

from apps.carbon.models import (
	ActivityCategory,
	AnomalySeverity,
	AnomalyType,
	Scope,
)

from ..models import RecommendationSource

DATA_UNAVAILABLE = "Data unavailable"


# --------------------------------------------------------------------------- #
# Formatting helpers (never raise — fall back to the sentinel)
# --------------------------------------------------------------------------- #
def _tco2e(value):
	if value is None:
		return DATA_UNAVAILABLE
	try:
		return f"{value:.2f} tCO2e"
	except (TypeError, ValueError):
		return DATA_UNAVAILABLE


def _percent(value):
	if value is None:
		return DATA_UNAVAILABLE
	try:
		return f"{value:.1f} %"
	except (TypeError, ValueError):
		return DATA_UNAVAILABLE


def _score(value):
	if value is None:
		return DATA_UNAVAILABLE
	try:
		return f"{value:.3f}"
	except (TypeError, ValueError):
		return DATA_UNAVAILABLE


def _count(value):
	if value is None:
		return DATA_UNAVAILABLE
	return str(value)


def _date(value):
	if not value:
		return DATA_UNAVAILABLE
	try:
		return date.fromisoformat(str(value)).strftime("%b %d, %Y")
	except (ValueError, TypeError):
		return DATA_UNAVAILABLE


def _text(value):
	value = (value or "").strip() if isinstance(value, str) else value
	return value or DATA_UNAVAILABLE


def _choice(choices, value):
	if value in dict(choices):
		return dict(choices)[value]
	return DATA_UNAVAILABLE


def _period(start, end):
	left, right = _date(start), _date(end)
	if left == DATA_UNAVAILABLE and right == DATA_UNAVAILABLE:
		return DATA_UNAVAILABLE
	return f"{left} → {right}"


def _item(label, value, source):
	return {"label": label, "value": value, "source": source}


def _find(rows, row_id):
	for row in rows:
		if row.get("id") == row_id:
			return row
	return None


# --------------------------------------------------------------------------- #
# Per-entity builders
# --------------------------------------------------------------------------- #
def anomaly_evidence(row):
	"""Facts that support an anomaly-based recommendation."""
	source = f"anomaly:{row['id']}"
	return [
		_item("Severity", _choice(AnomalySeverity.choices, row.get("severity")), source),
		_item("Anomaly type", _choice(AnomalyType.choices, row.get("anomaly_type")), source),
		_item("Anomaly score", _score(row.get("score")), source),
		_item("Linked activity", _text(row.get("activity_name")), source),
		_item("Category", _choice(ActivityCategory.choices, row.get("category")), source),
		_item("Scope", _choice(Scope.choices, row.get("scope")), source),
		_item("Activity CO2e", _tco2e(row.get("co2e_tco2e")), source),
		_item("Department", _text(row.get("department_name")), source),
		_item("Period", _period(row.get("period_start"), row.get("period_end")), source),
		_item("Detection note", _text(row.get("message")), source),
	]


def _trend(emissions):
	"""Year-over-year company emission trend, or the sentinel when unavailable."""
	if not emissions:
		return DATA_UNAVAILABLE
	current = emissions.get("total_tco2e")
	previous = emissions.get("previous_year_total_tco2e")
	if current is None or previous is None or previous == 0:
		return DATA_UNAVAILABLE
	direction = "up" if current > previous else "down" if current < previous else "flat"
	return (
		f"{_tco2e(current)} this year vs {_tco2e(previous)} the previous year ({direction})"
	)


def objective_evidence(row, emissions=None):
	"""Facts that support an objective-based recommendation."""
	source = f"objective:{row['id']}"
	items = [
		_item("Objective", _text(row.get("name")), source),
		_item("Period", _text(row.get("period_label")), source),
		_item("Baseline", _tco2e(row.get("baseline_tco2e")), source),
		_item("Target", _tco2e(row.get("target_tco2e")), source),
		_item("Current emissions", _tco2e(row.get("current_tco2e")), source),
		_item("Remaining reduction", _tco2e(row.get("gap_tco2e")), source),
		_item("Progress", _percent(row.get("progress_percent")), source),
		_item("Remaining days", _count(row.get("remaining_days")), source),
		_item("Deadline", _date(row.get("deadline")), source),
		_item("Department", _text(row.get("department_name")), source),
	]
	prediction = row.get("prediction")
	if prediction:
		items.append(
			_item(
				"AI prediction",
				f"{_percent(prediction.get('probability_achievement'))} · "
				f"{_text(prediction.get('expected_outcome'))}",
				source,
			)
		)
	else:
		items.append(_item("AI prediction", DATA_UNAVAILABLE, source))
	items.append(_item("Company emission trend", _trend(emissions), "approved_emissions"))
	return items


def data_quality_evidence(signals):
	"""Counts behind a data-quality recommendation (all real query counts)."""
	source = "data_quality"
	return [
		_item("Unvalidated activities", _count(signals.get("unvalidated_activities")), source),
		_item(
			"Activities missing an emission factor",
			_count(signals.get("activities_missing_emission_factor")),
			source,
		),
		_item("Stale drafts (30+ days)", _count(signals.get("stale_draft_activities")), source),
		_item(
			"Submitted without evidence",
			_count(signals.get("submitted_without_evidence")),
			source,
		),
		_item("Total data-quality issues", _count(signals.get("total_issues")), source),
	]


# --------------------------------------------------------------------------- #
# Dispatcher
# --------------------------------------------------------------------------- #
def evidence_for(context, *, source, anomaly_id=None, objective_id=None):
	"""Build the evidence items for one recommendation, grounded in real rows."""
	payload = context.payload
	items = []
	if anomaly_id is not None:
		row = _find(payload.get("open_anomalies", []), anomaly_id)
		if row is not None:
			items.extend(anomaly_evidence(row))
	if objective_id is not None:
		row = _find(payload.get("at_risk_objectives", []), objective_id)
		if row is not None:
			items.extend(objective_evidence(row, payload.get("approved_emissions")))
	if source == RecommendationSource.DATA_QUALITY:
		items.extend(data_quality_evidence(payload.get("data_quality", {})))
	return items
