"""AI audit analysis decided by the local Qwen model (Ollama).

Three features, all tenant-scoped. Django only supplies facts from one
company's own rows; Qwen decides.

* **Finding theme classification.** Qwen reads the tenant's findings (title,
  description, severity, status, related policy) and assigns each one a theme
  and a confidence. A ``finding_id`` the company does not own is rejected, so
  the model can never reference another tenant's row or invent a finding.
* **Delay-risk prediction.** Deterministic rules run first — days to due date,
  severity, remediation progress, overdue flag — which is the honest
  ``RULE_BASED`` answer when there is no historical data to learn from. Qwen may
  then add an explanation, and the entry is marked ``HYBRID``. No machine
  learning metric is ever fabricated.
* **Audit closing note.** Qwen writes the closing narrative for one engagement
  from its real counts (findings by severity/status, remediation completion,
  evidence presence, overdue/escalated totals).

Every model reply is parsed tolerantly and validated value by value. When
Ollama is unreachable the caller receives ``ai_available=False`` and a clear
message — the audit register, findings, remediation and evidence keep working.
"""

import json
from dataclasses import dataclass, field

from django.utils import timezone

from apps.recommendations.services import ollama_client

from .models import (
	Finding,
	FindingSeverity,
	FindingStatus,
	FindingTheme,
	OPEN_FINDING_STATUSES,
	OPEN_REMEDIATION_STATUSES,
	RemediationStatus,
)

# Local CPU inference of a 7B model is slow; the spec requires >= 120s.
MIN_TIMEOUT_SECONDS = 120.0

UNAVAILABLE_MESSAGE = (
	"AI assistance is temporarily unavailable, so no audit analysis could be "
	"generated. Nothing was changed — please try again in a moment."
)
INVALID_RESPONSE_MESSAGE = (
	"The AI returned an answer that could not be read, so no audit analysis is "
	"shown. Please try running the analysis again."
)
NO_FINDINGS_MESSAGE = (
	"No findings are on record yet — record findings on an audit, then run the "
	"analysis again."
)

THEMES = tuple(FindingTheme.values)
CONFIDENCES = ("HIGH", "MEDIUM", "LOW")
DELAY_LEVELS = ("LOW", "MEDIUM", "HIGH")

# Signals used by the deterministic delay-risk rules.
DUE_SOON_DAYS = 3
DUE_NEAR_DAYS = 7

MAX_FINDINGS = 40
MAX_REASONS = 4
MAX_ITEM = 400
MAX_TEXT = 4000
MAX_LIST_ITEMS = 6
MAX_DESCRIPTION = 600
MIN_TEXT = 8

SEVERITY_RANK = {
	FindingSeverity.CRITICAL: 0,
	FindingSeverity.HIGH: 1,
	FindingSeverity.MEDIUM: 2,
	FindingSeverity.LOW: 3,
}


# --------------------------------------------------------------------------- #
# Ollama structured-output schemas
# --------------------------------------------------------------------------- #
THEME_SCHEMA = {
	"type": "object",
	"properties": {
		"classifications": {
			"type": "array",
			"items": {
				"type": "object",
				"properties": {
					"finding_id": {"type": "integer"},
					"theme": {"type": "string", "enum": list(THEMES)},
					"confidence": {"type": "string", "enum": list(CONFIDENCES)},
					"reason": {"type": "string"},
				},
				"required": ["finding_id", "theme", "confidence", "reason"],
				"additionalProperties": False,
			},
		},
	},
	"required": ["classifications"],
	"additionalProperties": False,
}

DELAY_SCHEMA = {
	"type": "object",
	"properties": {
		"explanations": {
			"type": "array",
			"items": {
				"type": "object",
				"properties": {
					"finding_id": {"type": "integer"},
					"reasons": {"type": "array", "items": {"type": "string"}},
				},
				"required": ["finding_id", "reasons"],
				"additionalProperties": False,
			},
		},
	},
	"required": ["explanations"],
	"additionalProperties": False,
}

CLOSING_SCHEMA = {
	"type": "object",
	"properties": {
		"closing_note_title": {"type": "string"},
		"executive_summary": {"type": "string"},
		"key_findings": {"type": "array", "items": {"type": "string"}},
		"remediation_status": {"type": "string"},
		"residual_risks": {"type": "array", "items": {"type": "string"}},
		"conclusion": {"type": "string"},
	},
	"required": [
		"closing_note_title",
		"executive_summary",
		"key_findings",
		"remediation_status",
		"residual_risks",
		"conclusion",
	],
	"additionalProperties": False,
}


@dataclass
class AuditAnalysis:
	"""Outcome of one company-wide AI audit analysis. Never raises."""

	ok: bool
	company_id: int | None = None
	ai_available: bool = False
	model: str = ""
	generated_at: str = ""
	message: str = ""
	classifications: list = field(default_factory=list)
	delay_risk: list = field(default_factory=list)
	rejected: int = 0


@dataclass
class AuditClosingNote:
	"""Outcome of one AI closing note. Never raises."""

	ok: bool
	company_id: int | None = None
	audit_id: int | None = None
	ai_available: bool = False
	model: str = ""
	generated_at: str = ""
	message: str = ""
	note: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Tenant-scoped facts
# --------------------------------------------------------------------------- #
def _scoped_findings(company, findings=None):
	"""This company's findings, most severe and most urgent first."""
	if findings is not None:
		rows = list(findings)
	else:
		rows = list(
			Finding.objects.for_company(company).select_related(
				"audit", "department", "related_policy"
			)
		)
	rows.sort(
		key=lambda finding: (
			finding.status == FindingStatus.CLOSED,
			SEVERITY_RANK.get(finding.severity, 9),
			finding.due_date is None,
			finding.due_date or timezone.localdate(),
			finding.pk,
		)
	)
	return rows[:MAX_FINDINGS]


def _remediation_counts(finding):
	plans = finding.remediation_plans.all()
	return {
		"total": len(plans),
		"open": sum(1 for plan in plans if plan.status in OPEN_REMEDIATION_STATUSES),
		"done": sum(1 for plan in plans if plan.status == RemediationStatus.DONE),
		"cancelled": sum(
			1 for plan in plans if plan.status == RemediationStatus.CANCELLED
		),
		"overdue": sum(1 for plan in plans if plan.is_overdue),
	}


def finding_row(finding, as_of=None):
	"""One finding as plain facts. Nothing is recomputed or invented here."""
	as_of = as_of or timezone.localdate()
	days_to_due = (finding.due_date - as_of).days if finding.due_date else None
	return {
		"finding_id": finding.pk,
		"reference": finding.reference,
		"audit_reference": finding.audit.reference,
		"title": finding.title,
		"description": (finding.description or "")[:MAX_DESCRIPTION],
		"severity": finding.severity,
		"status": finding.status,
		"due_date": finding.due_date.isoformat() if finding.due_date else None,
		"days_to_due": days_to_due,
		"overdue": finding.is_overdue,
		"days_overdue": finding.days_overdue,
		"department": finding.department.name if finding.department_id else None,
		"related_policy": (
			finding.related_policy.title if finding.related_policy_id else None
		),
		"remediation": _remediation_counts(finding),
	}


def build_context(company, *, findings=None, as_of=None):
	"""Factual audit state of one company. ``None`` when there is no tenant."""
	if company is None:
		return None
	as_of = as_of or timezone.localdate()
	rows = _scoped_findings(company, findings)
	severity_counts = {value: 0 for value, _label in FindingSeverity.choices}
	status_counts = {value: 0 for value, _label in FindingStatus.choices}
	for finding in rows:
		severity_counts[finding.severity] += 1
		status_counts[finding.status] += 1
	return {
		"company": {"id": company.pk, "name": company.name},
		"as_of": as_of.isoformat(),
		"finding_totals": {
			"total": len(rows),
			"by_severity": severity_counts,
			"by_status": status_counts,
			"overdue": sum(1 for finding in rows if finding.is_overdue),
		},
		"findings": [finding_row(finding, as_of) for finding in rows],
	}


def build_closing_context(company, audit, *, as_of=None):
	"""Factual picture of one engagement, scoped to the caller's company."""
	as_of = as_of or timezone.localdate()
	findings = list(audit.findings.select_related("department", "related_policy"))
	severity_counts = {value: 0 for value, _label in FindingSeverity.choices}
	status_counts = {value: 0 for value, _label in FindingStatus.choices}
	for finding in findings:
		severity_counts[finding.severity] += 1
		status_counts[finding.status] += 1

	plans = [
		plan
		for finding in findings
		for plan in finding.remediation_plans.all()
	]
	audit_evidence = audit.evidence.count()
	finding_evidence = sum(1 for finding in findings if finding.evidence.exists())
	return {
		"company": {"id": company.pk, "name": company.name},
		"as_of": as_of.isoformat(),
		"audit": {
			"audit_id": audit.pk,
			"reference": audit.reference,
			"title": audit.title,
			"audit_type": audit.audit_type,
			"status": audit.status,
			"scope_summary": (audit.scope_summary or "")[:MAX_TEXT],
			"start_date": audit.start_date.isoformat() if audit.start_date else None,
			"end_date": audit.end_date.isoformat() if audit.end_date else None,
			"department": audit.department.name if audit.department_id else None,
			"related_policy": (
				audit.related_policy.title if audit.related_policy_id else None
			),
		},
		"findings": {
			"total": len(findings),
			"by_severity": severity_counts,
			"by_status": status_counts,
			"open": sum(1 for f in findings if f.status in OPEN_FINDING_STATUSES),
			"closed": sum(1 for f in findings if f.status == FindingStatus.CLOSED),
			"overdue": sum(1 for f in findings if f.is_overdue),
			"escalated": sum(1 for f in findings if f.escalated),
			"high_or_critical": sum(1 for f in findings if f.is_high_severity),
		},
		"remediation": {
			"total": len(plans),
			"open": sum(1 for p in plans if p.status in OPEN_REMEDIATION_STATUSES),
			"done": sum(1 for p in plans if p.status == RemediationStatus.DONE),
			"cancelled": sum(
				1 for p in plans if p.status == RemediationStatus.CANCELLED
			),
			"overdue": sum(1 for p in plans if p.is_overdue),
		},
		"evidence": {
			"audit_files": audit_evidence,
			"findings_with_evidence": finding_evidence,
		},
	}


# --------------------------------------------------------------------------- #
# Deterministic delay-risk rules
# --------------------------------------------------------------------------- #
def score_delay_risk(finding, as_of=None):
	"""Rule-based delay risk for one open finding.

	Returns ``None`` for a closed finding (nothing can slip any more), otherwise
	a ``RULE_BASED`` entry. The same function backs the live badge and the
	analysis run, so a finding is never scored two different ways.
	"""
	if finding.status not in OPEN_FINDING_STATUSES:
		return None
	as_of = as_of or timezone.localdate()
	reasons = []
	score = 0

	if finding.is_overdue:
		score += 3
		reasons.append(
			f"Past its due date by {finding.days_overdue} day(s) while still "
			f"{finding.get_status_display().lower()}."
		)
	elif finding.due_date:
		days = (finding.due_date - as_of).days
		if days <= DUE_SOON_DAYS:
			score += 2
			reasons.append(f"Due in {days} day(s).")
		elif days <= DUE_NEAR_DAYS:
			score += 1
			reasons.append(f"Due in {days} day(s).")
	else:
		reasons.append("No due date recorded.")

	if finding.is_high_severity:
		score += 1
		reasons.append(f"Severity is {finding.get_severity_display().lower()}.")

	plans = list(finding.remediation_plans.all())
	if not plans:
		score += 1
		reasons.append("No remediation action has been planned yet.")
	elif not any(
		plan.status in (RemediationStatus.IN_PROGRESS, RemediationStatus.DONE)
		for plan in plans
	):
		score += 1
		reasons.append("Remediation has not started yet.")

	if score >= 3:
		level = "HIGH"
	elif score == 2:
		level = "MEDIUM"
	else:
		level = "LOW"
	return {
		"finding_id": finding.pk,
		"delay_risk": level,
		"reasons": reasons[:MAX_REASONS],
		"method": "RULE_BASED",
		"method_label": "Rule-based",
	}


def delay_risk_for_findings(findings, as_of=None):
	"""Deterministic delay risk for a batch, keyed by finding id."""
	as_of = as_of or timezone.localdate()
	rows = {}
	for finding in findings:
		entry = score_delay_risk(finding, as_of)
		if entry is not None:
			rows[finding.pk] = entry
	return rows


# --------------------------------------------------------------------------- #
# Prompts
# --------------------------------------------------------------------------- #
_FINAL_INSTRUCTION = (
	"Now write your answer: a single JSON object using ONLY the exact keys from "
	"the schema. Output starts with {{ and ends with }}."
)

_THEME_SYSTEM = """You are CarbonPilot's internal audit analyst.
You are given real findings of ONE company, collected by CarbonPilot from its own database.
Classify EACH supplied finding into exactly one theme.
Base every classification only on the supplied finding text and attributes."""
_THEME_RULES = """HARD RULES
- Reply with ONLY a JSON object. No prose, no markdown, no code fences.
- Use only these "finding_id" values: {ids}. Never invent an id.
- "theme" must be exactly one of: {themes}.
- "confidence" must be exactly one of: HIGH, MEDIUM, LOW.
- "reason" must be one short sentence justifying the theme from that finding's own facts.
- Classify every supplied finding exactly once."""
_THEME_SCHEMA_TEXT = """OUTPUT JSON SCHEMA (exact keys, exact spelling)
{{"classifications": [
  {{"finding_id": 1,
    "theme": "EVIDENCE_CONTROL",
    "confidence": "HIGH",
    "reason": "one sentence citing the finding's own facts"}}
]}}"""

_DELAY_SYSTEM = """You are CarbonPilot's internal audit analyst.
You are given OPEN findings of ONE company together with the delay risk already
computed by CarbonPilot's deterministic rules.
Explain, for each finding, WHY it may slip, as short factual reasons."""
_DELAY_RULES = """HARD RULES
- Reply with ONLY a JSON object. No prose, no markdown, no code fences.
- Use only these "finding_id" values: {ids}. Never invent an id.
- "reasons" must be at most {limit} short sentences that stay consistent with the supplied facts.
- Do not change the risk level; only explain it.
- Never write a number that is not present in the supplied facts."""
_DELAY_SCHEMA_TEXT = """OUTPUT JSON SCHEMA (exact keys, exact spelling)
{{"explanations": [
  {{"finding_id": 1, "reasons": ["short factual reason"]}}
]}}"""

_CLOSING_SYSTEM = """You are CarbonPilot's internal audit lead.
Write the closing note of ONE completed (or closing) internal audit engagement.
Use only the supplied factual counts and metadata for that engagement.
Never invent findings, figures, dates, people or regulations."""
_CLOSING_RULES = """HARD RULES
- Reply with ONLY a JSON object. No prose, no markdown, no code fences.
- "closing_note_title": a short professional title for the note.
- "executive_summary": 2 to 5 sentences describing the engagement and its outcome from the facts.
- "key_findings": at most {limit} bullet points drawn from the supplied findings.
- "remediation_status": one short paragraph on remediation progress from the supplied counts.
- "residual_risks": items still open or escalated according to the facts (may be an empty array).
- "conclusion": one short closing paragraph.
- Never write a number that is not present in the supplied facts.
- If the engagement has no findings, say so plainly instead of inventing any."""
_CLOSING_SCHEMA_TEXT = """OUTPUT JSON SCHEMA (exact keys, exact spelling)
{{"closing_note_title": "...",
  "executive_summary": "2 to 5 sentences",
  "key_findings": ["..."],
  "remediation_status": "...",
  "residual_risks": ["..."],
  "conclusion": "..."}}"""


def build_theme_prompt(context):
	"""Prompt for the finding theme classification."""
	ids = [row["finding_id"] for row in context["findings"]]
	return (
		f"{_THEME_SYSTEM}\n\n"
		f"{_THEME_RULES.format(ids=ids, themes=', '.join(THEMES))}\n\n"
		f"{_THEME_SCHEMA_TEXT}\n\n"
		"CARBONPILOT AUDIT FACTS (JSON)\n"
		f"{json.dumps(context, ensure_ascii=False, indent=2)}\n\n"
		f"{_FINAL_INSTRUCTION}"
	)


def build_delay_prompt(context, rule_risk):
	"""Prompt for the optional Qwen explanation of the deterministic risk."""
	open_ids = [row["finding_id"] for row in rule_risk]
	open_rows = [
		row for row in context["findings"] if row["finding_id"] in set(open_ids)
	]
	data = {
		"as_of": context["as_of"],
		"company": context["company"],
		"open_findings": open_rows,
		"rule_based_delay_risk": rule_risk,
	}
	return (
		f"{_DELAY_SYSTEM}\n\n"
		f"{_DELAY_RULES.format(ids=open_ids, limit=MAX_REASONS)}\n\n"
		f"{_DELAY_SCHEMA_TEXT}\n\n"
		"CARBONPILOT OPEN FINDINGS AND RULE-BASED RISK (JSON)\n"
		f"{json.dumps(data, ensure_ascii=False, indent=2)}\n\n"
		f"{_FINAL_INSTRUCTION}"
	)


def build_closing_prompt(context):
	"""Prompt for the audit closing note."""
	return (
		f"{_CLOSING_SYSTEM}\n\n"
		f"{_CLOSING_RULES.format(limit=MAX_LIST_ITEMS)}\n\n"
		f"{_CLOSING_SCHEMA_TEXT}\n\n"
		"CARBONPILOT ENGAGEMENT FACTS (JSON)\n"
		f"{json.dumps(context, ensure_ascii=False, indent=2)}\n\n"
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


def _list_from(data, *keys):
	"""Return the first list found under any of ``keys`` (case-insensitive)."""
	if not isinstance(data, dict):
		return None
	wanted = {key.lower() for key in keys}
	for key, value in data.items():
		if str(key).lower() not in wanted:
			continue
		if isinstance(value, list):
			return value
		if isinstance(value, dict):
			return [value]
	return None


def parse_classifications(text):
	"""Return the ``classifications`` list from a model reply, or ``None``."""
	return _list_from(_loads(text), "classifications", "classification")


def parse_delay_explanations(text):
	"""Return the ``explanations`` list from a model reply, or ``None``."""
	return _list_from(_loads(text), "explanations", "delay_risk", "delay_risks")


def parse_closing_note(text):
	"""Return the closing-note object from a model reply, or ``None``."""
	data = _loads(text)
	if not isinstance(data, dict):
		return None
	if "executive_summary" in data:
		return data
	for key, value in data.items():
		if str(key).lower() in ("closing_note", "note") and isinstance(value, dict):
			return value
	return None


def _coerce_id(value):
	if isinstance(value, bool):
		return None
	if isinstance(value, int):
		return value
	if isinstance(value, str) and value.strip().isdigit():
		return int(value.strip())
	return None


def validate_classification(item, allowed_ids):
	"""Validate one classification. Returns a cleaned dict, or ``None``."""
	if not isinstance(item, dict):
		return None
	finding_id = _coerce_id(item.get("finding_id"))
	if finding_id is None or finding_id not in allowed_ids:
		# A finding id the company does not own is rejected outright.
		return None
	theme = str(item.get("theme") or "").strip().upper()
	if theme not in THEMES:
		return None
	confidence = str(item.get("confidence") or "").strip().upper()
	if confidence not in CONFIDENCES:
		return None
	reason = _text(item.get("reason"))
	if len(reason) < MIN_TEXT:
		return None
	return {
		"finding_id": finding_id,
		"theme": theme,
		"theme_label": FindingTheme(theme).label,
		"confidence": confidence,
		"reason": reason,
	}


def validate_classifications(items, allowed_ids):
	"""Validate the whole list. Returns ``(cleaned, rejected_count)``."""
	allowed = set(allowed_ids)
	cleaned, rejected, seen = [], 0, set()
	if not isinstance(items, list):
		return cleaned, rejected
	for item in items:
		candidate = validate_classification(item, allowed)
		if candidate is None:
			rejected += 1
			continue
		if candidate["finding_id"] in seen:
			rejected += 1
			continue
		seen.add(candidate["finding_id"])
		cleaned.append(candidate)
	return cleaned, rejected


def validate_delay_explanations(items, allowed_ids):
	"""Validate Qwen's delay explanations. Returns ``(cleaned, rejected)``."""
	allowed = set(allowed_ids)
	cleaned, rejected, seen = [], 0, set()
	if not isinstance(items, list):
		return cleaned, rejected
	for item in items:
		if not isinstance(item, dict):
			rejected += 1
			continue
		finding_id = _coerce_id(item.get("finding_id"))
		if finding_id is None or finding_id not in allowed or finding_id in seen:
			rejected += 1
			continue
		raw = item.get("reasons")
		if not isinstance(raw, list):
			rejected += 1
			continue
		reasons = []
		for value in raw:
			text = _text(value, MAX_ITEM)
			if text and text not in reasons:
				reasons.append(text)
			if len(reasons) >= MAX_REASONS:
				break
		if not reasons:
			rejected += 1
			continue
		seen.add(finding_id)
		cleaned.append({"finding_id": finding_id, "reasons": reasons})
	return cleaned, rejected


def validate_closing_note(data):
	"""Validate the closing note. Returns a cleaned dict, or ``None``."""
	if not isinstance(data, dict):
		return None
	executive = _text(data.get("executive_summary"), MAX_TEXT)
	if len(executive) < MIN_TEXT:
		return None
	title = _text(data.get("closing_note_title"), 255)
	conclusion = _text(data.get("conclusion"), MAX_TEXT)
	remediation = _text(data.get("remediation_status"), MAX_TEXT)

	def _items(key):
		values = data.get(key)
		if not isinstance(values, list):
			return []
		items = []
		for value in values:
			text = _text(value, MAX_ITEM)
			if text:
				items.append(text)
			if len(items) >= MAX_LIST_ITEMS:
				break
		return items

	return {
		"closing_note_title": title or "Audit closing note",
		"executive_summary": executive,
		"key_findings": _items("key_findings"),
		"remediation_status": remediation,
		"residual_risks": _items("residual_risks"),
		"conclusion": conclusion,
	}


# --------------------------------------------------------------------------- #
# Public entry points
# --------------------------------------------------------------------------- #
def _ask(prompt, schema):
	"""One Qwen call with the audit timeout floor. Never raises."""
	try:
		return ollama_client.generate(
			prompt,
			timeout=max(MIN_TIMEOUT_SECONDS, ollama_client.timeout_seconds()),
			schema=schema,
		)
	except Exception as exc:  # noqa: BLE001 - AI must never break the audit pages
		return ollama_client.OllamaResult(
			ok=False,
			error=type(exc).__name__,
			error_category="unexpected_client_error",
		)


def _merge_delay(rule_rows, explanations):
	"""Mark entries explained by Qwen as ``HYBRID`` and append its reasons."""
	by_id = {row["finding_id"]: row for row in explanations}
	merged = []
	for row in rule_rows:
		extra = by_id.get(row["finding_id"])
		if extra is None:
			merged.append(row)
			continue
		reasons = list(row["reasons"])
		for reason in extra["reasons"]:
			if reason not in reasons:
				reasons.append(reason)
		entry = dict(row)
		entry["reasons"] = reasons[: MAX_REASONS + 2]
		entry["method"] = "HYBRID"
		entry["method_label"] = "Rule-based + Qwen"
		merged.append(entry)
	return merged


def run_analysis(company, *, findings=None):
	"""Run theme classification then delay risk for one company. Never raises."""
	generated_at = timezone.now().isoformat()
	model = ollama_client.model_name()
	if company is None:
		return AuditAnalysis(
			ok=False,
			model=model,
			generated_at=generated_at,
			message="No company is associated with this account.",
		)

	rows = _scoped_findings(company, findings)
	if not rows:
		return AuditAnalysis(
			ok=True,
			company_id=company.pk,
			ai_available=True,
			model=model,
			generated_at=generated_at,
			message=NO_FINDINGS_MESSAGE,
		)

	context = build_context(company, findings=rows)
	allowed_ids = [row.pk for row in rows]

	# Feature 1 — Qwen decides the theme of every supplied finding.
	theme_result = _ask(build_theme_prompt(context), THEME_SCHEMA)
	if not theme_result.ok:
		return AuditAnalysis(
			ok=True,
			company_id=company.pk,
			ai_available=False,
			model=model,
			generated_at=generated_at,
			message=UNAVAILABLE_MESSAGE,
		)
	raw_themes = parse_classifications(theme_result.text)
	if raw_themes is None:
		return AuditAnalysis(
			ok=True,
			company_id=company.pk,
			ai_available=False,
			model=model,
			generated_at=generated_at,
			message=INVALID_RESPONSE_MESSAGE,
		)
	classifications, rejected = validate_classifications(raw_themes, allowed_ids)

	# Feature 2 — deterministic rules first, Qwen explanation when available.
	rule_map = delay_risk_for_findings(rows)
	delay_rows = [rule_map[finding.pk] for finding in rows if finding.pk in rule_map]
	delay_note = ""
	if delay_rows:
		delay_result = _ask(build_delay_prompt(context, delay_rows), DELAY_SCHEMA)
		if delay_result.ok:
			explanations, rejected_delay = validate_delay_explanations(
				parse_delay_explanations(delay_result.text) or [], allowed_ids
			)
			rejected += rejected_delay
			if explanations:
				delay_rows = _merge_delay(delay_rows, explanations)
		else:
			delay_note = (
				" The delay-risk explanation could not be retrieved from the model, "
				"so rule-based risk is shown."
			)

	parts = [
		f"AI · Qwen classified {len(classifications)} of {len(rows)} finding(s) by theme."
		if classifications
		else "AI · Qwen returned no theme classification for the supplied findings."
	]
	if delay_rows:
		parts.append(f"{len(delay_rows)} open finding(s) were scored for delay risk.")
	if rejected:
		parts.append(f"{rejected} AI item(s) failed validation and were discarded.")
	message = " ".join(parts) + delay_note

	return AuditAnalysis(
		ok=True,
		company_id=company.pk,
		ai_available=True,
		model=model,
		generated_at=generated_at,
		message=message,
		classifications=classifications,
		delay_risk=delay_rows,
		rejected=rejected,
	)


def run_closing_note(company, audit):
	"""Draft the closing note of one engagement. Never raises."""
	generated_at = timezone.now().isoformat()
	model = ollama_client.model_name()
	if company is None or audit is None:
		return AuditClosingNote(
			ok=False,
			model=model,
			generated_at=generated_at,
			message="No audit is available for a closing note.",
		)
	if audit.company_id != company.pk:
		# Defensive: the caller must have scoped the audit to this tenant already.
		return AuditClosingNote(
			ok=False,
			company_id=company.pk,
			model=model,
			generated_at=generated_at,
			message="This audit does not belong to your company.",
		)

	context = build_closing_context(company, audit)
	result = _ask(build_closing_prompt(context), CLOSING_SCHEMA)
	if not result.ok:
		return AuditClosingNote(
			ok=True,
			company_id=company.pk,
			audit_id=audit.pk,
			ai_available=False,
			model=model,
			generated_at=generated_at,
			message=UNAVAILABLE_MESSAGE,
		)
	note = validate_closing_note(parse_closing_note(result.text))
	if not note:
		return AuditClosingNote(
			ok=True,
			company_id=company.pk,
			audit_id=audit.pk,
			ai_available=False,
			model=model,
			generated_at=generated_at,
			message=INVALID_RESPONSE_MESSAGE,
		)
	return AuditClosingNote(
		ok=True,
		company_id=company.pk,
		audit_id=audit.pk,
		ai_available=True,
		model=model,
		generated_at=generated_at,
		message=(
			f"AI · Qwen drafted the closing note for {audit.reference} from the "
			"engagement's own facts."
		),
		note=note,
	)


__all__ = [
	"AuditAnalysis",
	"AuditClosingNote",
	"CONFIDENCES",
	"DELAY_LEVELS",
	"INVALID_RESPONSE_MESSAGE",
	"MIN_TIMEOUT_SECONDS",
	"NO_FINDINGS_MESSAGE",
	"THEMES",
	"UNAVAILABLE_MESSAGE",
	"build_closing_context",
	"build_closing_prompt",
	"build_context",
	"build_delay_prompt",
	"build_theme_prompt",
	"delay_risk_for_findings",
	"finding_row",
	"parse_classifications",
	"parse_closing_note",
	"parse_delay_explanations",
	"run_analysis",
	"run_closing_note",
	"score_delay_risk",
	"validate_classification",
	"validate_classifications",
	"validate_closing_note",
	"validate_delay_explanations",
]
