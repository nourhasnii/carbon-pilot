"""AI governance analysis decided by the local Qwen model (Ollama).

Two features, both decided by Qwen — Django only supplies facts:

* **Feature A — missing-policy detector.** Qwen is handed the tenant's own
  governance facts (policies and their coverage, latest risk assessment,
  data-quality backlog, open anomalies, objectives, supplier ESG gaps) and
  decides which internal policies are missing, with a priority and an
  evidence-based justification.
* **Feature B — governance summary.** Qwen writes the executive narrative from
  the same facts plus the detector's own findings.

Django never invents a finding. The factual context is built exclusively from
one company's rows, the model's JSON is validated value by value (unknown
categories and priorities are rejected), and when the model is unreachable the
analysis degrades to a clear "AI unavailable" message instead of raising.
"""

import json
from dataclasses import dataclass, field
from datetime import timedelta

from django.db.models import Count
from django.utils import timezone

from apps.carbon.models import (
	ActivityStatus,
	AnomalyLog,
	AnomalySeverity,
	AnomalyStatus,
	CarbonActivity,
)
from apps.objectives.models import Objective, ObjectiveStatus
from apps.recommendations.services import ollama_client
from apps.suppliers.models import ESGStatus, Supplier

from ..models import PolicyCategory, PolicyStatus

# Local CPU inference of a 7B model is slow; the spec requires >= 120s.
MIN_TIMEOUT_SECONDS = 120.0

UNAVAILABLE_MESSAGE = (
	"AI assistance is temporarily unavailable, so no governance analysis could "
	"be generated. Nothing was changed — please try again in a moment."
)
INVALID_RESPONSE_MESSAGE = (
	"The AI returned an answer that could not be read, so no governance analysis "
	"is shown. Please try running the analysis again."
)

PRIORITIES = ("HIGH", "MEDIUM", "LOW")

MAX_MISSING_POLICIES = 6
MAX_TITLE = 255
MIN_TEXT = 8
MAX_TEXT = 4000
MAX_ITEM = 500
MAX_LIST_ITEMS = 6
MAX_SIGNALS = 6
MAX_EXISTING_POLICIES = 30
MAX_DRIVERS = 5
MAX_ANOMALY_TYPES = 8
MAX_OBJECTIVES = 12
MAX_SUMMARY_TEXT = 4000

# A draft activity left untouched for this many days is flagged as stale.
STALE_DRAFT_DAYS = 30


# --------------------------------------------------------------------------- #
# Ollama structured-output schemas
# --------------------------------------------------------------------------- #
MISSING_POLICIES_SCHEMA = {
	"type": "object",
	"properties": {
		"missing_policies": {
			"type": "array",
			"items": {
				"type": "object",
				"properties": {
					"suggested_title": {"type": "string"},
					"category": {"type": "string", "enum": list(PolicyCategory.values)},
					"priority": {"type": "string", "enum": list(PRIORITIES)},
					"why": {"type": "string"},
					"related_signals": {"type": "array", "items": {"type": "string"}},
				},
				"required": [
					"suggested_title",
					"category",
					"priority",
					"why",
					"related_signals",
				],
				"additionalProperties": False,
			},
		},
		"note": {"type": "string"},
	},
	"required": ["missing_policies"],
	"additionalProperties": False,
}

GOVERNANCE_SUMMARY_SCHEMA = {
	"type": "object",
	"properties": {
		"executive_summary": {"type": "string"},
		"strengths": {"type": "array", "items": {"type": "string"}},
		"gaps": {"type": "array", "items": {"type": "string"}},
		"management_attention": {"type": "array", "items": {"type": "string"}},
	},
	"required": ["executive_summary", "strengths", "gaps", "management_attention"],
	"additionalProperties": False,
}


@dataclass
class GovernanceAnalysis:
	"""Outcome of one AI governance analysis. ``ai_available=False`` never raises."""

	ok: bool
	company_id: int | None = None
	ai_available: bool = False
	model: str = ""
	generated_at: str = ""
	message: str = ""
	missing_policies: list = field(default_factory=list)
	summary: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Factual context (tenant-scoped, read-only)
# --------------------------------------------------------------------------- #
def _policy_rows(company):
	live = company.policies.live()
	rows = [
		{
			"id": policy.pk,
			"title": policy.title,
			"category": policy.category,
			"status": policy.status,
			"next_review_date": (
				policy.next_review_date.isoformat() if policy.next_review_date else None
			),
			"due_for_review": policy.is_due_for_review,
		}
		for policy in live.order_by("title")[:MAX_EXISTING_POLICIES]
	]
	covered = sorted(set(live.values_list("category", flat=True)))
	return {
		"policies": rows,
		"policy_coverage": {
			"total": live.count(),
			"approved": live.filter(status=PolicyStatus.APPROVED).count(),
			"in_review": live.filter(status=PolicyStatus.IN_REVIEW).count(),
			"draft": live.filter(status=PolicyStatus.DRAFT).count(),
			"due_for_review": live.due_for_review().count(),
			"covered_categories": covered,
			"uncovered_categories": [
				value for value, _label in PolicyCategory.choices if value not in covered
			],
		},
	}


def _risk_context(company):
	"""Latest assessment of this company, or ``None`` when none exists yet."""
	assessment = company.risk_assessments.order_by("-created_at", "-id").first()
	if assessment is None:
		return None
	return {
		"score": float(assessment.score),
		"level": assessment.level,
		"summary": assessment.summary,
		"period": assessment.period_label,
		"assessed_at": assessment.created_at.isoformat(),
		# Passed through as stored: Django never recomputes the drivers.
		"drivers": [
			driver
			for driver in assessment.drivers[:MAX_DRIVERS]
			if isinstance(driver, dict)
		],
	}


def _data_quality_context(company, as_of):
	activities = CarbonActivity.objects.filter(company=company, is_archived=False)
	return {
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


def _anomaly_context(company):
	open_anomalies = AnomalyLog.objects.filter(
		company=company, status=AnomalyStatus.NEW
	)
	by_type = (
		open_anomalies.values("anomaly_type")
		.annotate(count=Count("id"))
		.order_by("-count")[:MAX_ANOMALY_TYPES]
	)
	return {
		"open": open_anomalies.count(),
		"high": open_anomalies.filter(severity=AnomalySeverity.HIGH).count(),
		"medium": open_anomalies.filter(severity=AnomalySeverity.MEDIUM).count(),
		"low": open_anomalies.filter(severity=AnomalySeverity.LOW).count(),
		"by_type": [
			{"type": row["anomaly_type"], "count": row["count"]} for row in by_type
		],
	}


def _objective_context(company):
	objectives = Objective.objects.filter(company=company)
	return {
		"total": objectives.count(),
		"active": objectives.filter(status=ObjectiveStatus.ACTIVE).count(),
		"achieved": objectives.filter(status=ObjectiveStatus.ACHIEVED).count(),
		"missed": objectives.filter(status=ObjectiveStatus.MISSED).count(),
		"missed_objectives": [
			{"name": objective.name, "year": objective.year}
			for objective in objectives.filter(status=ObjectiveStatus.MISSED).order_by(
				"-year", "name"
			)[:MAX_OBJECTIVES]
		],
		"active_objectives": [
			{"name": objective.name, "year": objective.year}
			for objective in objectives.filter(status=ObjectiveStatus.ACTIVE).order_by(
				"year", "name"
			)[:MAX_OBJECTIVES]
		],
	}


def _supplier_context(company):
	suppliers = Supplier.objects.filter(company=company, is_active=True)
	return {
		"active": suppliers.count(),
		"esg_complete": suppliers.filter(esg_status=ESGStatus.COMPLETE).count(),
		"esg_partial": suppliers.filter(esg_status=ESGStatus.PARTIAL).count(),
		"esg_missing": suppliers.filter(esg_status=ESGStatus.MISSING).count(),
	}


def build_context(company, *, as_of=None):
	"""Factual governance state of one company. ``None`` when there is no tenant."""
	if company is None:
		return None
	as_of = as_of or timezone.localdate()
	context = {
		"company": {"id": company.pk, "name": company.name},
		"as_of": as_of.isoformat(),
	}
	context.update(_policy_rows(company))
	context["risk"] = _risk_context(company)
	context["data_quality"] = _data_quality_context(company, as_of)
	context["anomalies"] = _anomaly_context(company)
	context["objectives"] = _objective_context(company)
	context["suppliers"] = _supplier_context(company)
	return context


# --------------------------------------------------------------------------- #
# Prompt construction
# --------------------------------------------------------------------------- #
_MISSING_SYSTEM = """You are CarbonPilot's corporate governance analyst.
You are given the factual governance state of ONE company, collected by CarbonPilot from its own database.
Decide which internal policies this company is MISSING, if any.
Base every decision only on the supplied facts.
Never invent figures, incidents, regulations, laws or policy names that the facts do not support.
A policy category that is already covered is not missing.
If the supplied facts do not justify a missing policy, return an empty list."""

_MISSING_RULES = """HARD RULES
- Reply with ONLY a JSON object. No prose, no markdown, no code fences.
- Return at most {limit} missing policies. Prefer fewer; never pad the list to reach the limit.
- "category" must be exactly one of: {categories}.
- "priority" must be exactly one of: HIGH, MEDIUM, LOW.
- "why" must justify the finding from the supplied facts and name the signals it relies on.
- "related_signals" must list only signal names present in the facts, for example: coverage, risk, evidence, anomaly, objective, supplier.
- Never write a number that is not present in the supplied facts.
- If nothing is clearly missing, return exactly {{"missing_policies": [], "note": "why nothing is missing"}}"""

_MISSING_SCHEMA_TEXT = """OUTPUT JSON SCHEMA (exact keys, exact spelling)
{{"missing_policies": [
  {{"suggested_title": "short title of the missing policy",
    "category": "one category from the allowed list",
    "priority": "HIGH" | "MEDIUM" | "LOW",
    "why": "evidence-based justification citing the supplied facts",
    "related_signals": ["coverage", "risk", "evidence", "anomaly", "objective", "supplier"]}}
],
 "note": "optional short explanation, required when missing_policies is empty"}"""

_SUMMARY_SYSTEM = """You are CarbonPilot's corporate governance analyst.
Write an executive governance summary for ONE company.
Use only the supplied factual governance state and the missing-policy findings.
Never invent figures, incidents, regulations or policy names.
Write for a board reader: factual, concise and actionable."""

_SUMMARY_RULES = """HARD RULES
- Reply with ONLY a JSON object. No prose, no markdown, no code fences.
- "executive_summary": 2 to 5 sentences describing the company's governance position from the facts.
- "strengths": what the facts show is working (may be an empty array).
- "gaps": weaknesses and missing coverage shown by the facts (may be an empty array).
- "management_attention": at most {limit} concrete items the board should act on.
- Never write a number that is not present in the supplied facts.
- Do not restate the raw JSON; write prose."""

_SUMMARY_SCHEMA_TEXT = """OUTPUT JSON SCHEMA (exact keys, exact spelling)
{{"executive_summary": "2 to 5 sentences",
  "strengths": ["..."],
  "gaps": ["..."],
  "management_attention": ["..."]}}"""

_FINAL_INSTRUCTION = (
	"Now write your answer: a single JSON object using ONLY the exact keys from "
	"the schema. Output starts with {{ and ends with }}."
)


def build_missing_policies_prompt(context):
	"""Prompt for Feature A — Qwen decides which policies are missing."""
	return (
		f"{_MISSING_SYSTEM}\n\n"
		f"{_MISSING_RULES.format(limit=MAX_MISSING_POLICIES, categories=', '.join(PolicyCategory.values))}\n\n"
		"MISSING-POLICY OUTPUT SCHEMA (exact keys, exact spelling)\n"
		f"{_MISSING_SCHEMA_TEXT}\n\n"
		"CARBONPILOT GOVERNANCE FACTS (JSON)\n"
		f"{json.dumps(context, ensure_ascii=False, indent=2)}\n\n"
		f"{_FINAL_INSTRUCTION}"
	)


def build_summary_prompt(context, missing_policies):
	"""Prompt for Feature B — Qwen writes the governance summary."""
	data = {
		"governance_facts": context,
		"missing_policy_findings": missing_policies,
	}
	return (
		f"{_SUMMARY_SYSTEM}\n\n"
		f"{_SUMMARY_RULES.format(limit=MAX_LIST_ITEMS)}\n\n"
		"GOVERNANCE SUMMARY OUTPUT SCHEMA (exact keys, exact spelling)\n"
		f"{_SUMMARY_SCHEMA_TEXT}\n\n"
		"CARBONPILOT GOVERNANCE FACTS AND FINDINGS (JSON)\n"
		f"{json.dumps(data, ensure_ascii=False, indent=2)}\n\n"
		f"{_FINAL_INSTRUCTION}"
	)


# --------------------------------------------------------------------------- #
# Parsing and validation
# --------------------------------------------------------------------------- #
def _loads(text):
	"""Tolerant JSON parse; also finds an object embedded in surrounding prose."""
	if not isinstance(text, str):
		return None
	decoder = json.JSONDecoder()
	try:
		return json.loads(text)
	except (ValueError, TypeError):
		pass
	for index, character in enumerate(text):
		if character != "{":
			continue
		try:
			data, _ = decoder.raw_decode(text[index:])
		except (ValueError, TypeError):
			continue
		return data
	return None


def _text(value, limit=MAX_TEXT):
	if not isinstance(value, str):
		return ""
	return value.strip()[:limit]


def parse_missing_policies(text):
	"""Return the ``missing_policies`` list from a model reply, or ``None``."""
	data = _loads(text)
	if not isinstance(data, dict):
		return None
	for key, value in data.items():
		if str(key).lower() not in ("missing_policies", "missing_policy"):
			continue
		if isinstance(value, list):
			return value
		if isinstance(value, dict):
			return [value]
	return None


def validate_missing_item(item):
	"""Validate one suggestion. Returns a cleaned dict, or ``None`` to reject it."""
	if not isinstance(item, dict):
		return None
	title = _text(item.get("suggested_title"), MAX_TITLE)
	if not title:
		return None
	category = str(item.get("category") or "").strip().upper()
	if category not in PolicyCategory.values:
		return None
	priority = str(item.get("priority") or "").strip().upper()
	if priority not in PRIORITIES:
		return None
	why = _text(item.get("why"))
	if len(why) < MIN_TEXT:
		return None
	signals = item.get("related_signals")
	cleaned_signals = []
	if isinstance(signals, list):
		for value in signals:
			token = _text(value, 64).lower()
			if token and token not in cleaned_signals:
				cleaned_signals.append(token)
			if len(cleaned_signals) >= MAX_SIGNALS:
				break
	return {
		"suggested_title": title,
		"category": category,
		"category_label": PolicyCategory(category).label,
		"priority": priority,
		"why": why,
		"related_signals": cleaned_signals,
	}


def validate_missing_policies(items):
	"""Validate the whole list. Returns ``(cleaned, rejected_count)``."""
	cleaned, rejected = [], 0
	if not isinstance(items, list):
		return cleaned, rejected
	seen = set()
	for item in items:
		candidate = validate_missing_item(item)
		if candidate is None:
			rejected += 1
			continue
		key = candidate["suggested_title"].lower()
		if key in seen:
			rejected += 1
			continue
		seen.add(key)
		cleaned.append(candidate)
		if len(cleaned) >= MAX_MISSING_POLICIES:
			break
	return cleaned, rejected


def parse_summary(text):
	"""Return the governance-summary object from a model reply, or ``None``."""
	data = _loads(text)
	if not isinstance(data, dict):
		return None
	if "executive_summary" in data:
		return data
	for key, value in data.items():
		if str(key).lower() in ("summary", "governance_summary") and isinstance(
			value, dict
		):
			return value
	return None


def validate_summary(data):
	"""Validate the narrative summary. Returns a cleaned dict, or ``None``."""
	if not isinstance(data, dict):
		return None
	executive = _text(data.get("executive_summary"), MAX_SUMMARY_TEXT)
	if len(executive) < MIN_TEXT:
		return None

	def _items(key):
		values = data.get(key)
		if not isinstance(values, list):
			return []
		cleaned = []
		for value in values:
			text = _text(value, MAX_ITEM)
			if text:
				cleaned.append(text)
			if len(cleaned) >= MAX_LIST_ITEMS:
				break
		return cleaned

	return {
		"executive_summary": executive,
		"strengths": _items("strengths"),
		"gaps": _items("gaps"),
		"management_attention": _items("management_attention"),
	}


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def _ask(prompt, schema):
	"""One Qwen call with the governance timeout floor. Never raises."""
	return ollama_client.generate(
		prompt,
		timeout=max(MIN_TIMEOUT_SECONDS, ollama_client.timeout_seconds()),
		schema=schema,
	)


def run_analysis(company, *, as_of=None):
	"""Run Feature A then Feature B for one company. Never raises."""
	generated_at = timezone.now().isoformat()
	model = ollama_client.model_name()
	if company is None:
		return GovernanceAnalysis(
			ok=False, model=model, generated_at=generated_at,
			message="No company is associated with this account.",
		)

	context = build_context(company, as_of=as_of)
	if not context["policy_coverage"]["uncovered_categories"] and not any(
		(
			context["anomalies"]["open"],
			context["objectives"]["missed"],
			context["suppliers"]["esg_missing"] + context["suppliers"]["esg_partial"],
			context["risk"],
		)
	):
		# Nothing for the model to reason over: no coverage gap and no signal.
		return GovernanceAnalysis(
			ok=True,
			company_id=company.pk,
			ai_available=True,
			model=model,
			generated_at=generated_at,
			message=(
				"No governance signal is on record yet — add policies and carbon data, "
				"then run the analysis again."
			),
		)

	# Feature A — Qwen decides which policies are missing.
	missing_result = _ask(build_missing_policies_prompt(context), MISSING_POLICIES_SCHEMA)
	if not missing_result.ok:
		return GovernanceAnalysis(
			ok=True,
			company_id=company.pk,
			ai_available=False,
			model=model,
			generated_at=generated_at,
			message=UNAVAILABLE_MESSAGE,
		)
	raw_missing = parse_missing_policies(missing_result.text)
	if raw_missing is None:
		return GovernanceAnalysis(
			ok=True,
			company_id=company.pk,
			ai_available=False,
			model=model,
			generated_at=generated_at,
			message=INVALID_RESPONSE_MESSAGE,
		)
	missing, rejected = validate_missing_policies(raw_missing)

	# Feature B — Qwen writes the summary from the facts and Feature A's findings.
	summary = {}
	summary_note = ""
	summary_result = _ask(
		build_summary_prompt(context, missing), GOVERNANCE_SUMMARY_SCHEMA
	)
	if summary_result.ok:
		summary = validate_summary(parse_summary(summary_result.text)) or {}
	if not summary:
		summary_note = (
			" The governance summary could not be retrieved from the model."
		)

	parts = [
		f"AI · Qwen analysed your governance data and identified {len(missing)} "
		f"missing policy suggestion(s)."
		if missing
		else "AI · Qwen found no missing policy from the supplied governance data."
	]
	if rejected:
		parts.append(f"{rejected} suggestion(s) failed validation and were discarded.")
	message = " ".join(parts) + summary_note

	return GovernanceAnalysis(
		ok=True,
		company_id=company.pk,
		ai_available=True,
		model=model,
		generated_at=generated_at,
		message=message,
		missing_policies=missing,
		summary=summary,
	)
