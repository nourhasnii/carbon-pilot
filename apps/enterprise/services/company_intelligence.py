"""Company Intelligence: one tenant-scoped view of a company's ESG posture.

Everything shown here is read from data CarbonPilot already produces — approved
carbon activities, data-quality scoring, reduction objectives and their ML
predictions, anomaly logs, recommendations and the latest risk assessment. No
new model and no invented figure is introduced.

The company-wide snapshot (approved emissions, top departments/categories,
quality/evidence, objectives with their latest prediction, anomaly counts, open
recommendations and the latest risk assessment with its ranked drivers) is the
same snapshot the ESG report is built from, so the two are always consistent:
this service reuses :func:`apps.reports.services.esg_report.build_report_data`
and only reshapes it for the Company Intelligence page. Every read starts from
the given ``company``, so no other tenant's rows can ever be reached.
"""

from __future__ import annotations

from django.utils import timezone

from apps.carbon.models import ActivityStatus, CarbonActivity
from apps.carbon.services import analytics
from apps.objectives.models import ObjectiveStatus, RiskLevel
from apps.recommendations.models import (
	OPEN_STATUSES,
	RecommendationLog,
	RecommendationPriority,
)
from apps.reports.services import esg_report

# How many open recommendations are surfaced in the priority actions list.
TOP_ACTIONS = 6

# Severities used to type a priority action in the UI (derived, never invented).
SEVERITY_RISK = "risk"
SEVERITY_HIGH = "high"
SEVERITY_MEDIUM = "medium"
SEVERITY_LOW = "low"

_PRIORITY_SEVERITY = {
	RecommendationPriority.HIGH: SEVERITY_HIGH,
	RecommendationPriority.MEDIUM: SEVERITY_MEDIUM,
	RecommendationPriority.LOW: SEVERITY_LOW,
}
_PRIORITY_RANK = {
	RecommendationPriority.HIGH: 0,
	RecommendationPriority.MEDIUM: 1,
	RecommendationPriority.LOW: 2,
}


def available_years(company):
	"""Reporting years that have approved activity data, newest first."""
	approved = CarbonActivity.objects.filter(
		company=company, status=ActivityStatus.APPROVED, is_archived=False
	)
	return analytics.available_years(approved, timezone.localdate().year)


def resolve_year(company, requested=None):
	"""Pick the reporting year to show: the requested one if valid, else newest."""
	years = available_years(company)
	try:
		requested = int(requested) if requested not in (None, "") else None
	except (TypeError, ValueError):
		requested = None
	if requested is not None and requested in years:
		return requested
	return years[0]


def _objectives_at_risk(objectives):
	"""Objectives that are missed or predicted (by the ML model) to be at risk."""
	items = []
	for item in objectives.get("items", []):
		prediction = item.get("prediction")
		is_risky = item["status"] == ObjectiveStatus.MISSED or (
			prediction is not None
			and prediction["risk_level"] in (RiskLevel.HIGH, RiskLevel.MEDIUM)
		)
		if is_risky:
			items.append(item)
	return {
		"total": objectives.get("total", 0),
		"achieved": objectives.get("achieved", 0),
		"missed": objectives.get("missed", 0),
		"at_risk": objectives.get("at_risk", 0),
		"items": items,
	}


def _priority_actions(company, risk):
	"""Priority actions from the risk drivers and the company's open recommendations."""
	actions = []
	if risk:
		for driver in risk.get("drivers") or []:
			actions.append({
				"kind": "risk_driver",
				"title": driver.get("label", "Risk driver"),
				"detail": driver.get("detail", ""),
				"severity": SEVERITY_RISK,
				"points": round(float(driver.get("points", 0) or 0), 2),
				"max_points": driver.get("max_points"),
			})

	open_items = list(
		RecommendationLog.objects.filter(company=company, status__in=OPEN_STATUSES)
	)
	open_items.sort(
		key=lambda rec: (_PRIORITY_RANK.get(rec.priority, 3), -rec.created_at.timestamp())
	)
	for rec in open_items[:TOP_ACTIONS]:
		actions.append({
			"kind": "recommendation",
			"title": rec.title,
			"detail": rec.recommended_action or rec.message,
			"severity": _PRIORITY_SEVERITY.get(rec.priority, SEVERITY_LOW),
			"priority_label": rec.get_priority_display(),
			"status_label": rec.get_status_display(),
		})
	return actions


def build_intelligence(company, *, period_year=None):
	"""Build the Company Intelligence snapshot for one company (and year).

	Only the given ``company``'s own data is read; ``period_year`` is resolved
	from the company's approved activity years. Raises ``ValueError`` without a
	company so a caller can never build an unscoped view.
	"""
	if company is None:
		raise ValueError("A company is required to build company intelligence.")

	year = resolve_year(company, period_year)
	metrics, summary = esg_report.build_report_data(company, period_year=year)
	risk = metrics["risk"]
	return {
		"period_year": year,
		"years": available_years(company),
		"generated_at": timezone.now(),
		"summary": summary,
		"company": metrics["company"],
		"emissions": metrics["emissions"],
		"quality": metrics["quality"],
		"objectives": _objectives_at_risk(metrics["objectives"]),
		"anomalies": metrics["anomalies"],
		"recommendations": metrics["recommendations"],
		"risk": risk,
		"priority_actions": _priority_actions(company, risk),
	}
