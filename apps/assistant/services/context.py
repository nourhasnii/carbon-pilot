"""Factual, tenant-scoped context for the "Ask CarbonPilot" assistant.

The assistant is a conversational reader of data CarbonPilot already produces —
it is deliberately **not** a second recommendations engine. Every value in the
payload is read for the authenticated user's own company only, and the company
snapshot is taken from the ESG report service
(:func:`apps.reports.services.esg_report.build_report_data`), so the assistant
can never disagree with the dashboard, the Company Intelligence view or the PDF
report. Suppliers are added from the supplier module.

No figure is invented: missing data stays absent, and a tenant's rows can never
enter another tenant's payload because every read starts from ``company``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from django.utils import timezone

from apps.enterprise.services import company_intelligence
from apps.recommendations.services.context import collect_numbers
from apps.reports.services import esg_report
from apps.suppliers.models import ESGStatus, RiskLevel, Supplier
from apps.suppliers.services import priority_suppliers

# Source labels shown in the UI ("sources used") and offered to the model.
SOURCE_ACTIVITIES = "Activities"
SOURCE_OBJECTIVES = "Objectives"
SOURCE_ANOMALIES = "Anomalies"
SOURCE_RISK = "Risk Assessment"
SOURCE_QUALITY = "Data Quality"
SOURCE_SUPPLIERS = "Suppliers"
SOURCE_RECOMMENDATIONS = "Recommendations"

# How many priority suppliers / objectives / breakdown rows are included.
MAX_OBJECTIVES = 20
MAX_SUPPLIERS = 8
MAX_BREAKDOWN = 5


def _scopes(emissions):
	return [
		{"scope": row["label"], "tco2e": row["tonnes"], "share_percent": row["percent"]}
		for row in emissions.get("scopes", [])
	]


def _breakdown(rows):
	return [
		{"label": row["label"], "tco2e": row["tonnes"], "share_percent": row["percent"]}
		for row in (rows or [])[:MAX_BREAKDOWN]
	]


def _objectives(objectives):
	items = []
	for item in objectives.get("items", []):
		entry = {
			"name": item["name"],
			"scope": item["scope"],
			"status": item["status_label"],
			"progress_percent": item["progress_percent"],
			"current_tco2e": item["current_tonnes"],
			"target_tco2e": item["target_tonnes"],
		}
		prediction = item.get("prediction")
		if prediction is not None:
			entry["prediction"] = {
				"probability_percent": prediction["probability"],
				"risk": prediction["risk_label"],
				"outcome": prediction["outcome"],
			}
		items.append(entry)
	return {
		"total": objectives.get("total", 0),
		"achieved": objectives.get("achieved", 0),
		"at_risk": objectives.get("at_risk", 0),
		"missed": objectives.get("missed", 0),
		"items": items[:MAX_OBJECTIVES],
	}


def _suppliers(company):
	"""Supplier posture for the company, or ``None`` when it has no suppliers."""
	queryset = Supplier.objects.filter(company=company)
	total = queryset.count()
	if not total:
		return None
	return {
		"total": total,
		"active": queryset.filter(is_active=True).count(),
		"by_esg_status": {
			status: queryset.filter(esg_status=status).count() for status in ESGStatus.values
		},
		"high_risk": queryset.filter(is_active=True, risk_level=RiskLevel.HIGH).count(),
		"priority": [
			{
				"name": row["supplier"].name,
				"reasons": row["reasons"],
				"scope3_tco2e": float(row["scope3_tonnes"]),
			}
			for row in priority_suppliers(company, limit=MAX_SUPPLIERS)
		],
	}


@dataclass
class AssistantContext:
	"""Tenant-scoped payload plus the numeric allowlist used to ground answers."""

	company_id: int
	company_name: str
	period_year: int
	as_of: date
	payload: dict
	sources: list = field(default_factory=list)
	allowed_numbers: set = field(default_factory=set)

	@property
	def has_data(self):
		"""True when there is at least one real fact worth answering from."""
		emissions = self.payload.get("approved_emissions", {})
		objectives = self.payload.get("objectives", {})
		anomalies = self.payload.get("anomalies", {})
		return bool(
			emissions.get("approved_activities")
			or objectives.get("total")
			or anomalies.get("total")
			or self.payload.get("risk_assessment")
			or self.payload.get("suppliers")
		)


def _available_sources(payload):
	sources = []
	if payload["approved_emissions"]["approved_activities"]:
		sources.append(SOURCE_ACTIVITIES)
	if payload["data_quality"]["scored_activities"]:
		sources.append(SOURCE_QUALITY)
	if payload["objectives"]["total"]:
		sources.append(SOURCE_OBJECTIVES)
	if payload["anomalies"]["total"]:
		sources.append(SOURCE_ANOMALIES)
	if payload["risk_assessment"] is not None:
		sources.append(SOURCE_RISK)
	if payload["suppliers"] is not None:
		sources.append(SOURCE_SUPPLIERS)
	if payload["recommendations_summary"]["open"]:
		sources.append(SOURCE_RECOMMENDATIONS)
	return sources


def build_context(company, *, period_year=None, as_of=None):
	"""Build the assistant context for one company, or ``None`` without a tenant."""
	if company is None:
		return None
	as_of = as_of or timezone.localdate()
	year = company_intelligence.resolve_year(company, period_year)
	metrics, summary = esg_report.build_report_data(company, period_year=year)

	emissions = metrics["emissions"]
	objectives = _objectives(metrics["objectives"])
	suppliers = _suppliers(company)
	risk = metrics["risk"]

	payload = {
		"company": {"name": metrics["company"]["name"]},
		"reporting_year": year,
		"executive_summary": summary,
		"approved_emissions": {
			"total_tco2e": emissions["total_tonnes"],
			"approved_activities": emissions["approved_activities"],
			"by_scope": _scopes(emissions),
			"top_departments": _breakdown(emissions.get("top_departments")),
			"top_categories": _breakdown(emissions.get("top_categories")),
		},
		"data_quality": {
			"average_percent": metrics["quality"]["average"],
			"scored_activities": metrics["quality"]["scored"],
			"evidence_coverage_percent": metrics["quality"]["evidence_coverage"],
			"missing_evidence": metrics["quality"]["missing_evidence"],
		},
		"objectives": objectives,
		"anomalies": {
			"total": metrics["anomalies"].get("total", 0),
			"high": metrics["anomalies"].get("high", 0),
			"medium": metrics["anomalies"].get("medium", 0),
			"low": metrics["anomalies"].get("low", 0),
		},
		"risk_assessment": (
			{
				"score": risk["score"],
				"level": risk["level_label"],
				"summary": risk["summary"],
				"assessed_on": risk["assessed_on"],
				"drivers": [
					{"label": driver.get("label"), "points": driver.get("points")}
					for driver in risk.get("drivers") or []
				],
			}
			if risk is not None
			else None
		),
		"recommendations_summary": {
			"open": metrics["recommendations"]["open"],
			"high_priority": metrics["recommendations"]["high"],
		},
		"suppliers": suppliers,
	}

	return AssistantContext(
		company_id=company.pk,
		company_name=company.name,
		period_year=year,
		as_of=as_of,
		payload=payload,
		sources=_available_sources(payload),
		allowed_numbers=collect_numbers(payload),
	)
