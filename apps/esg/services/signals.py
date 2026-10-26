"""Read-only summaries aggregated from the other CarbonPilot modules.

The ESG hub never recomputes or mutates another module's figures — it reads
them, states their **source** and **reporting period**, and reports honestly
when there is nothing to show. A missing figure is surfaced as *unavailable*
with an explanation; it is never silently replaced by zero.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.urls import reverse
from django.utils import timezone

from apps.audits.models import Finding
from apps.carbon.models import ActivityStatus, CarbonActivity
from apps.carbon.services import analytics as carbon_analytics
from apps.governance.models import Policy
from apps.objectives.models import Objective, ObjectiveStatus, PredictionRun
from apps.objectives.models import RiskLevel as ObjectiveRiskLevel
from apps.risk.services.risk_assessment import latest_assessment as latest_risk_assessment


@dataclass
class SignalCard:
	"""One read-only summary card sourced from an existing module."""

	key: str
	label: str
	value: str
	detail: str
	source: str
	period: str
	available: bool = True
	url: str = ""

	def as_dict(self):
		return {
			"key": self.key,
			"label": self.label,
			"value": self.value,
			"detail": self.detail,
			"source": self.source,
			"period": self.period,
			"available": self.available,
			"url": self.url,
		}


def _carbon_signal(company):
	queryset = CarbonActivity.objects.filter(
		company=company,
		status=ActivityStatus.APPROVED,
		is_archived=False,
	)
	years = carbon_analytics.available_years(queryset, timezone.localdate().year)
	year = years[0]
	scoped = queryset.filter(period_start__year=year)
	has_data = scoped.exists()
	url = reverse("carbon:footprint")
	if not has_data:
		return SignalCard(
			key="emissions",
			label="Approved emissions",
			value="—",
			detail="No approved carbon activity is on record, so no footprint can be shown.",
			source="Carbon management",
			period=str(year),
			available=False,
			url=url,
		)
	total = carbon_analytics.total_tonnes(scoped)
	scopes = {row["key"]: row["tonnes"] for row in carbon_analytics.scope_breakdown(scoped)}
	return SignalCard(
		key="emissions",
		label="Approved emissions",
		value=f"{total:,.2f} tCO2e",
		detail=(
			f"Scope 1 {scopes.get('SCOPE_1', 0):,.2f} · "
			f"Scope 2 {scopes.get('SCOPE_2', 0):,.2f} · "
			f"Scope 3 {scopes.get('SCOPE_3', 0):,.2f} tCO2e"
		),
		source="Carbon management · approved activities only",
		period=str(year),
		available=True,
		url=url,
	)


def _objectives_signal(company):
	live = Objective.objects.filter(company=company).exclude(
		status=ObjectiveStatus.ARCHIVED
	)
	total = live.count()
	url = reverse("objectives:objective_list")
	if total == 0:
		return SignalCard(
			key="objectives",
			label="Objectives at risk",
			value="—",
			detail="No reduction objective has been set, so no objective risk can be shown.",
			source="Objectives",
			period="—",
			available=False,
			url=url,
		)
	missed = live.filter(status=ObjectiveStatus.MISSED).count()
	active_ids = list(
		live.filter(status=ObjectiveStatus.ACTIVE).values_list("pk", flat=True)
	)
	latest_runs = {}
	if active_ids:
		for run in PredictionRun.objects.filter(objective_id__in=active_ids).order_by(
			"objective_id", "-created_at", "-id"
		):
			latest_runs.setdefault(run.objective_id, run)
	at_risk = sum(
		1
		for run in latest_runs.values()
		if run.risk_level in (ObjectiveRiskLevel.HIGH, ObjectiveRiskLevel.MEDIUM)
	)
	behind = len(active_ids) - len(latest_runs) + at_risk
	return SignalCard(
		key="objectives",
		label="Objectives at risk",
		value=str(behind),
		detail=(
			f"{at_risk} active objective(s) predicted at risk, {missed} missed, "
			f"{total} on record."
		),
		source="Objectives · latest achievement predictions",
		period=str(timezone.localdate().year),
		available=True,
		url=url,
	)


def _risk_signal(company):
	assessment = latest_risk_assessment(company)
	url = reverse("risk:risk_assessment")
	if assessment is None:
		return SignalCard(
			key="risk",
			label="Latest risk assessment",
			value="—",
			detail="No risk assessment has been run yet.",
			source="Risk assessment",
			period="—",
			available=False,
			url=url,
		)
	return SignalCard(
		key="risk",
		label="Latest risk assessment",
		value=f"{assessment.score}/100",
		detail=f"{assessment.get_level_display()} risk · {assessment.method.replace('_', ' ').title()}",
		source="Risk assessment · rule-based",
		period=assessment.period_label,
		available=True,
		url=url,
	)


def _policies_signal(company):
	live = company.policies.live()
	total = live.count()
	due = live.due_for_review().count()
	url = reverse("governance:policy_list")
	if total == 0:
		return SignalCard(
			key="policies",
			label="Policies due for review",
			value="—",
			detail="No policy is on record, so no review is due.",
			source="Governance",
			period="—",
			available=False,
			url=url,
		)
	return SignalCard(
		key="policies",
		label="Policies due for review",
		value=str(due),
		detail=f"{due} of {total} live polic{'y' if total == 1 else 'ies'} past their review date.",
		source="Governance · approved policies",
		period=str(timezone.localdate().year),
		available=True,
		url=url,
	)


def _findings_signal(company):
	findings = Finding.objects.filter(company=company)
	total = findings.count()
	open_count = findings.open().count()
	overdue = findings.overdue().count()
	url = reverse("audits:audit_list")
	if total == 0:
		return SignalCard(
			key="findings",
			label="Open audit findings",
			value="—",
			detail="No audit finding is on record.",
			source="Internal audit",
			period="—",
			available=False,
			url=url,
		)
	return SignalCard(
		key="findings",
		label="Open audit findings",
		value=str(open_count),
		detail=f"{open_count} open or in progress, {overdue} overdue, {total} on record.",
		source="Internal audit",
		period=str(timezone.localdate().year),
		available=True,
		url=url,
	)


def company_signals(company):
	"""Every read-only source-module summary card for one company."""
	if company is None:
		return []
	return [
		_carbon_signal(company),
		_objectives_signal(company),
		_risk_signal(company),
		_policies_signal(company),
		_findings_signal(company),
	]
