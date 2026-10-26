"""Qwen "ESG Completeness Coach".

Django computes every score, gap and signal; Qwen only **prioritises** the gaps
and explains what to do next. The model is handed one company's own facts
(assessment results, concrete gaps, tracked indicators and the read-only
source-module signals) and returns a validated JSON document.

Qwen must never invent KPIs, measurements or compliance claims: the prompt
forbids it, the JSON is validated value by value (unknown pillars and
priorities are rejected), and when Ollama is unreachable or returns something
unreadable the coach degrades to a clear message — the deterministic dashboard
keeps working without AI.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from django.utils import timezone

from apps.recommendations.services import ollama_client

from ..models import ESGPillar, Indicator
from . import assessment as assessment_service
from . import signals as signals_service

# Local CPU inference of a 7B model is slow; the spec requires >= 120s.
MIN_TIMEOUT_SECONDS = 120.0

UNAVAILABLE_MESSAGE = (
	"AI assistance is temporarily unavailable, so no coaching could be generated. "
	"Nothing was changed — please try again in a moment."
)
INVALID_RESPONSE_MESSAGE = (
	"The AI returned an answer that could not be read, so no coaching is shown. "
	"Please try running it again."
)

PRIORITIES = ("HIGH", "MEDIUM", "LOW")

MAX_GAPS = 6
MAX_RECOMMENDATIONS = 6
MAX_STEPS = 5
MAX_TEXT = 4000
MAX_ITEM = 500
MAX_SUMMARY = 4000
MIN_TEXT = 8
MAX_INDICATOR_ROWS = 30

COACH_SCHEMA = {
	"type": "object",
	"properties": {
		"executive_summary": {"type": "string"},
		"priority_gaps": {
			"type": "array",
			"items": {
				"type": "object",
				"properties": {
					"pillar": {"type": "string", "enum": list(ESGPillar.values)},
					"issue": {"type": "string"},
					"priority": {"type": "string", "enum": list(PRIORITIES)},
					"explanation": {"type": "string"},
				},
				"required": ["pillar", "issue", "priority", "explanation"],
				"additionalProperties": False,
			},
		},
		"recommended_indicators": {
			"type": "array",
			"items": {
				"type": "object",
				"properties": {
					"name": {"type": "string"},
					"pillar": {"type": "string", "enum": list(ESGPillar.values)},
					"justification": {"type": "string"},
				},
				"required": ["name", "pillar", "justification"],
				"additionalProperties": False,
			},
		},
		"management_next_steps": {"type": "array", "items": {"type": "string"}},
	},
	"required": [
		"executive_summary",
		"priority_gaps",
		"recommended_indicators",
		"management_next_steps",
	],
	"additionalProperties": False,
}


@dataclass
class CoachAnalysis:
	"""Outcome of one coaching run. ``ai_available=False`` never raises."""

	ok: bool
	company_id: int | None = None
	ai_available: bool = False
	model: str = ""
	generated_at: str = ""
	message: str = ""
	executive_summary: str = ""
	priority_gaps: list = field(default_factory=list)
	recommended_indicators: list = field(default_factory=list)
	management_next_steps: list = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Factual context (tenant-scoped, read-only)
# --------------------------------------------------------------------------- #
def build_context(company, *, period_year=None):
	if company is None:
		return None
	result = assessment_service.assess_company(company, period_year=period_year)
	indicators = (
		Indicator.objects.filter(company=company)
		.select_related("catalog_entry")
		.order_by("catalog_entry__code")[:MAX_INDICATOR_ROWS]
	)
	rows = []
	for indicator in indicators:
		latest = indicator.latest_value()
		rows.append(
			{
				"code": indicator.catalog_entry.code,
				"name": indicator.name,
				"pillar": indicator.pillar,
				"is_active": indicator.is_active,
				"latest_year": latest.period_year if latest else None,
				"latest_status": latest.get_status_display() if latest else "No data",
				"latest_value_present": bool(latest and latest.has_value),
			}
		)
	gaps_flat = []
	for kind, items in result.gaps.items():
		for item in items:
			gaps_flat.append(
				{
					"kind": kind,
					"code": item["code"],
					"name": item["name"],
					"pillar": item["pillar"],
					"detail": item["detail"],
				}
			)
	return {
		"company": {"id": company.pk, "name": company.name},
		"as_of": timezone.localdate().isoformat(),
		"assessment": {
			"period_year": result.period_year,
			"overall_score": float(result.overall_score),
			"readiness": result.readiness,
			"pillar_coverage": {
				"environmental": float(result.environmental_score),
				"social": float(result.social_score),
				"governance": float(result.governance_score),
			},
			"counts": {
				"required": result.total_required,
				"covered": result.covered,
				"eligible": result.eligible,
				"verified": result.verified,
				"with_evidence": result.with_evidence,
			},
			"breakdown": [
				{
					"key": item["key"],
					"label": item["label"],
					"points": item["points"],
					"max_points": item["max_points"],
					"detail": item["detail"],
				}
				for item in result.breakdown
			],
		},
		"gaps": gaps_flat,
		"tracked_indicators": rows,
		"source_signals": [
			{
				"key": card.key,
				"label": card.label,
				"value": card.value,
				"detail": card.detail,
				"source": card.source,
				"period": card.period,
				"available": card.available,
			}
			for card in signals_service.company_signals(company)
		],
	}


# --------------------------------------------------------------------------- #
# Prompt construction
# --------------------------------------------------------------------------- #
_SYSTEM = """You are CarbonPilot's ESG data-completeness coach.
You are given the factual ESG data state of ONE company, collected by CarbonPilot from its own database.
Your job is to PRIORITISE the data gaps and explain, concretely, what to do next so the company can become ready for ESG reporting.
Base every statement on the supplied facts.
Never invent a KPI value, a measurement, a certification, a regulation or a compliance claim that the facts do not support.
Never present a data-completeness score as an ESG performance rating."""

_RULES = """HARD RULES
- Reply with ONLY a JSON object. No prose, no markdown, no code fences.
- At most {max_gaps} priority gaps, most important first. Prefer fewer; never pad the list.
- "pillar" must be exactly one of: {pillars}.
- "priority" must be exactly one of: HIGH, MEDIUM, LOW.
- Each "explanation" must cite the supplied facts (the gap code/name, a count or a signal) and name the concrete indicator or action.
- At most {max_recs} recommended indicators. Recommend an indicator ONLY when a required metric is not tracked or not reported for the period, or a pillar is clearly under-covered.
- "name" of a recommended indicator must be a clear ESG metric name; do not invent numbers for it.
- At most {max_steps} management next steps, each an actionable instruction.
- Never write a number that is not present in the supplied facts.
- If the data is already complete, return an empty arrays for gaps and recommendations and explain why in the summary."""

_SCHEMA_TEXT = """OUTPUT JSON SCHEMA (exact keys, exact spelling)
{{"executive_summary": "2 to 5 sentences on the company's ESG data readiness",
  "priority_gaps": [
    {{"pillar": "ENVIRONMENTAL" | "SOCIAL" | "GOVERNANCE",
      "issue": "short description of the gap",
      "priority": "HIGH" | "MEDIUM" | "LOW",
      "explanation": "fact-based explanation citing the supplied facts"}}],
  "recommended_indicators": [
    {{"name": "name of the ESG indicator to start tracking",
      "pillar": "ENVIRONMENTAL" | "SOCIAL" | "GOVERNANCE",
      "justification": "why it is needed, from the supplied facts"}}],
  "management_next_steps": ["actionable next step"]}}"""

_FINAL = (
	"Now write your answer: a single JSON object using ONLY the exact keys from "
	"the schema. Output starts with { and ends with }."
)


def build_prompt(context):
	return (
		f"{_SYSTEM}\n\n"
		f"{_RULES.format(max_gaps=MAX_GAPS, max_recs=MAX_RECOMMENDATIONS, max_steps=MAX_STEPS, pillars=', '.join(ESGPillar.values))}\n\n"
		"OUTPUT JSON SCHEMA (exact keys, exact spelling)\n"
		f"{_SCHEMA_TEXT}\n\n"
		"CARBONPILOT ESG FACTS (JSON)\n"
		f"{json.dumps(context, ensure_ascii=False, indent=2)}\n\n"
		f"{_FINAL}"
	)


# --------------------------------------------------------------------------- #
# Parsing and validation
# --------------------------------------------------------------------------- #
def _loads(text):
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


def _clean_text_list(values, limit, item_limit=MAX_ITEM):
	if not isinstance(values, list):
		return []
	cleaned = []
	for value in values:
		text = _text(value, item_limit)
		if text:
			cleaned.append(text)
		if len(cleaned) >= limit:
			break
	return cleaned


def validate_priority_gap(item):
	if not isinstance(item, dict):
		return None
	pillar = str(item.get("pillar") or "").strip().upper()
	if pillar not in ESGPillar.values:
		return None
	issue = _text(item.get("issue"), MAX_ITEM)
	if len(issue) < MIN_TEXT:
		return None
	priority = str(item.get("priority") or "").strip().upper()
	if priority not in PRIORITIES:
		return None
	explanation = _text(item.get("explanation"))
	if len(explanation) < MIN_TEXT:
		return None
	return {
		"pillar": pillar,
		"pillar_label": ESGPillar(pillar).label,
		"issue": issue,
		"priority": priority,
		"explanation": explanation,
	}


def validate_recommendation(item):
	if not isinstance(item, dict):
		return None
	name = _text(item.get("name"), 255)
	if len(name) < 3:
		return None
	pillar = str(item.get("pillar") or "").strip().upper()
	if pillar not in ESGPillar.values:
		return None
	justification = _text(item.get("justification"))
	if len(justification) < MIN_TEXT:
		return None
	return {
		"name": name,
		"pillar": pillar,
		"pillar_label": ESGPillar(pillar).label,
		"justification": justification,
	}


def validate_payload(data):
	"""Validate a parsed coach response. Returns ``(cleaned, rejected)`` or ``None``."""
	if not isinstance(data, dict):
		return None
	summary = _text(data.get("executive_summary"), MAX_SUMMARY)
	if len(summary) < MIN_TEXT:
		return None

	rejected = 0
	gaps = []
	seen = set()
	for item in data.get("priority_gaps") or []:
		candidate = validate_priority_gap(item)
		if candidate is None:
			rejected += 1
			continue
		key = (candidate["pillar"], candidate["issue"].lower())
		if key in seen:
			rejected += 1
			continue
		seen.add(key)
		gaps.append(candidate)
		if len(gaps) >= MAX_GAPS:
			break

	recommendations = []
	seen_names = set()
	for item in data.get("recommended_indicators") or []:
		candidate = validate_recommendation(item)
		if candidate is None:
			rejected += 1
			continue
		key = candidate["name"].lower()
		if key in seen_names:
			rejected += 1
			continue
		seen_names.add(key)
		recommendations.append(candidate)
		if len(recommendations) >= MAX_RECOMMENDATIONS:
			break

	steps = _clean_text_list(data.get("management_next_steps"), MAX_STEPS)
	return {
		"executive_summary": summary,
		"priority_gaps": gaps,
		"recommended_indicators": recommendations,
		"management_next_steps": steps,
		"rejected": rejected,
	}


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def _ask(prompt):
	"""One Qwen call with the coach timeout floor. Never raises."""
	return ollama_client.generate(
		prompt,
		timeout=max(MIN_TIMEOUT_SECONDS, ollama_client.timeout_seconds()),
		schema=COACH_SCHEMA,
	)


def run_coach(company, *, period_year=None):
	"""Run the completeness coach for one company. Never raises."""
	generated_at = timezone.now().isoformat()
	model = ollama_client.model_name()
	if company is None:
		return CoachAnalysis(
			ok=False,
			model=model,
			generated_at=generated_at,
			message="No company is associated with this account.",
		)

	context = build_context(company, period_year=period_year)
	if not context["assessment"]["counts"]["required"] and not context[
		"tracked_indicators"
	]:
		return CoachAnalysis(
			ok=True,
			company_id=company.pk,
			ai_available=True,
			model=model,
			generated_at=generated_at,
			message=(
				"No ESG indicator is configured yet — add indicators to your catalog, "
				"then run the coach again."
			),
		)

	result = _ask(build_prompt(context))
	if not result.ok:
		return CoachAnalysis(
			ok=True,
			company_id=company.pk,
			ai_available=False,
			model=model,
			generated_at=generated_at,
			message=UNAVAILABLE_MESSAGE,
		)

	cleaned = validate_payload(_loads(result.text))
	if cleaned is None:
		return CoachAnalysis(
			ok=True,
			company_id=company.pk,
			ai_available=False,
			model=model,
			generated_at=generated_at,
			message=INVALID_RESPONSE_MESSAGE,
		)

	parts = [
		f"AI · Qwen reviewed your ESG data completeness "
		f"({context['assessment']['overall_score']}/100) and prioritised "
		f"{len(cleaned['priority_gaps'])} gap(s)."
	]
	if cleaned["rejected"]:
		parts.append(
			f"{cleaned['rejected']} suggestion(s) failed validation and were discarded."
		)
	return CoachAnalysis(
		ok=True,
		company_id=company.pk,
		ai_available=True,
		model=model,
		generated_at=generated_at,
		message=" ".join(parts),
		executive_summary=cleaned["executive_summary"],
		priority_gaps=cleaned["priority_gaps"],
		recommended_indicators=cleaned["recommended_indicators"],
		management_next_steps=cleaned["management_next_steps"],
	)
