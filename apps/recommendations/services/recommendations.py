"""The recommendation engine: Qwen decides, Django validates and persists.

Flow for one generation run:

1. STEP A — :mod:`context` builds the tenant-scoped factual evidence;
2. STEP B — :mod:`prompt` asks the local model (via :mod:`ollama_client`) to
   independently choose the actions and priorities;
3. STEP C — every returned item is validated here (structure, lengths, enum
   values, entity ownership, evidence grounding, numeric grounding) before it is
   persisted. Invalid items are rejected — never silently replaced by a
   template.

When the model is unavailable or returns invalid output, the deterministic
:mod:`fallback` produces ``RULE_BASED`` recommendations instead, clearly flagged
as such. Django — not the model — always owns authorization, validation and
persistence.
"""

import json
import logging
from dataclasses import dataclass, field

from django.db import IntegrityError, transaction
from django.utils import timezone

from ..models import (
	AI_SOURCES,
	GenerationMethod,
	IMPACT_TO_BE_ESTIMATED,
	OPEN_STATUSES,
	RecommendationLog,
	RecommendationPriority,
	RecommendationSource,
	RecommendationStatus,
)
from . import ollama_client
from .access import can_update_status
from .context import build_context, ungrounded_number
from .evidence import evidence_for
from .fallback import build_candidates
from .prompt import build_prompt

logger = logging.getLogger(__name__)

MIN_TITLE = 4
MAX_TITLE = 255
MIN_MESSAGE = 12
MAX_MESSAGE = 2000
MIN_SECTION = 8
MAX_SECTION = 4000
MIN_STEP = 4
MAX_STEP = 300
MAX_STEPS = 12
MAX_RECOMMENDATIONS = 5


@dataclass
class GenerationResult:
	ok: bool
	method: str = ""
	created: list = field(default_factory=list)
	rejected: int = 0
	skipped: int = 0
	ai_unavailable: bool = False
	message: str = ""


# --------------------------------------------------------------------------- #
# Parsing / validation helpers
# --------------------------------------------------------------------------- #
def _as_int(value):
	if isinstance(value, bool) or value is None:
		return None
	if isinstance(value, int):
		return value
	if isinstance(value, str):
		text = value.strip()
		if text.lstrip("-").isdigit():
			return int(text)
	return None


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


def parse_response(text):
	"""Return the ``recommendations`` list from a model reply, or ``None``."""
	data = _loads(text)
	if not isinstance(data, dict):
		return None
	for key, value in data.items():
		if str(key).lower() not in ("recommendations", "recommendation"):
			continue
		if isinstance(value, list):
			return value
		if isinstance(value, dict):
			return [value]
	if "title" in data and "action_plan" in data:
		return [data]
	return None


def response_validation_code(text):
	"""Classify response parsing failures without exposing response content."""
	data = _loads(text)
	if data is None:
		return "invalid_json"
	if not isinstance(data, dict):
		return "response_not_object"
	if parse_response(text) is None:
		return "recommendations_missing_or_not_array"
	return "valid"


def validate_item(item, context):
	"""Validate one model item. Returns ``(cleaned, reason)``.

	``cleaned`` is ``None`` when the item is rejected; ``reason`` then describes
	why (logged, never shown verbatim to end users).
	"""
	if not isinstance(item, dict):
		return None, "item is not an object"

	title = str(item.get("title") or "").strip()
	message = str(item.get("message") or "").strip()
	if not (MIN_TITLE <= len(title) <= MAX_TITLE):
		return None, "title missing or out of length bounds"
	if not (MIN_MESSAGE <= len(message) <= MAX_MESSAGE):
		return None, "message missing or out of length bounds"

	priority = str(item.get("priority") or "").strip().upper()
	if priority not in RecommendationPriority.values:
		return None, f"invalid priority {priority!r}"

	source = str(item.get("source") or "").strip().upper()
	if source not in RecommendationSource.values:
		return None, f"invalid source {source!r}"
	# DATA_QUALITY is produced only by CarbonPilot's deterministic rules — the
	# model is never allowed to claim it.
	if source not in AI_SOURCES:
		return None, f"source {source!r} is not AI-decidable"

	department_id = _as_int(item.get("related_department_id"))
	if department_id is not None and department_id not in context.company_department_ids:
		return None, f"department {department_id} is not part of this company"

	anomaly_id = _as_int(item.get("related_anomaly_id"))
	if anomaly_id is not None and anomaly_id not in context.anomaly_ids:
		return None, f"anomaly {anomaly_id} was not supplied in the context"

	objective_id = _as_int(item.get("related_objective_id"))
	if objective_id is not None and objective_id not in context.objective_ids:
		return None, f"objective {objective_id} was not supplied in the context"

	# The declared source must be consistent with the referenced entities.
	if source == RecommendationSource.ANOMALY and anomaly_id is None:
		return None, "source ANOMALY but no anomaly referenced"
	if source == RecommendationSource.OBJECTIVE and objective_id is None:
		return None, "source OBJECTIVE but no objective referenced"
	if source == RecommendationSource.MIXED and not (anomaly_id is not None and objective_id is not None):
		return None, "source MIXED requires both an anomaly and an objective"

	evidence_values = item.get("evidence_ids") or []
	if not isinstance(evidence_values, list):
		return None, "evidence_ids is not a list"
	evidence = []
	for value in evidence_values:
		token = context.resolve_evidence(value)
		if token is None:
			return None, f"evidence id {value!r} is not in the context"
		evidence.append(f"{token[0]}:{token[1]}")
	if not evidence:
		return None, "no evidence_ids supplied"
	# The primary reference must itself be grounded in the evidence.
	if anomaly_id is not None and f"anomaly:{anomaly_id}" not in evidence:
		evidence.append(f"anomaly:{anomaly_id}")
	if objective_id is not None and f"objective:{objective_id}" not in evidence:
		evidence.append(f"objective:{objective_id}")

	# ------------------------------------------------------------------ #
	# Structured narrative sections. An item with missing or out-of-bounds
	# sections is incomplete and is rejected — a broken recommendation is
	# never displayed. The failure is logged by the caller.
	# ------------------------------------------------------------------ #
	sections = {
		"problem": str(item.get("problem") or "").strip(),
		"analysis": str(item.get("analysis") or "").strip(),
		"recommendation": str(item.get("recommendation") or "").strip(),
	}
	for name, value in sections.items():
		if not (MIN_SECTION <= len(value) <= MAX_SECTION):
			return None, f"{name} missing or out of length bounds"

	steps = item.get("action_plan")
	if not isinstance(steps, list):
		return None, "action_plan is not a list"
	action_plan = [str(step).strip() for step in steps if str(step).strip()]
	if not (1 <= len(action_plan) <= MAX_STEPS):
		return None, "action_plan must contain between 1 and 12 steps"
	for step in action_plan:
		if not (MIN_STEP <= len(step) <= MAX_STEP):
			return None, "an action step is out of length bounds"

	# Numeric grounding across EVERY narrative field the model wrote. The
	# EVIDENCE items are built server-side from real rows, so they are never
	# subject to this check — the model simply cannot contribute a figure.
	prose = "\n".join(
		[title, message, sections["problem"], sections["analysis"], sections["recommendation"], *action_plan]
	)
	unsupported = ungrounded_number(prose, context.allowed_numbers)
	if unsupported is not None:
		return None, f"unsupported numeric claim {unsupported!r}"

	# Expected impact is never the model's to invent: with no simulation model
	# available it is always the honest placeholder.
	return (
		{
			"title": title,
			"message": message,
			"problem": sections["problem"],
			"analysis": sections["analysis"],
			"recommended_action": sections["recommendation"],
			"expected_impact": IMPACT_TO_BE_ESTIMATED,
			"action_plan": action_plan,
			"priority": priority,
			"source": source,
			"department_id": department_id,
			"objective_id": objective_id,
			"anomaly_id": anomaly_id,
			"evidence": evidence,
			"evidence_items": evidence_for(
				context, source=source, anomaly_id=anomaly_id, objective_id=objective_id
			),
		},
		None,
	)


# --------------------------------------------------------------------------- #
# Persistence (shared by Qwen and the fallback)
# --------------------------------------------------------------------------- #
def persist_candidates(context, candidates, method, *, model_name=""):
	"""Create non-duplicate recommendations. Returns ``(created, skipped)``."""
	existing = set(
		RecommendationLog.objects.filter(
			company_id=context.company_id, status__in=OPEN_STATUSES
		).values_list("fingerprint", flat=True)
	)
	seen = set()
	created, skipped = [], 0
	for candidate in candidates[:MAX_RECOMMENDATIONS]:
		fingerprint = RecommendationLog.build_fingerprint(
			company_id=context.company_id,
			source=candidate["source"],
			department_id=candidate["department_id"],
			objective_id=candidate["objective_id"],
			anomaly_id=candidate["anomaly_id"],
			title=candidate["title"],
		)
		if fingerprint in existing or fingerprint in seen:
			skipped += 1
			continue
		seen.add(fingerprint)
		try:
			with transaction.atomic():
				recommendation = RecommendationLog.objects.create(
					company_id=context.company_id,
					department_id=candidate["department_id"],
					objective_id=candidate["objective_id"],
					anomaly_id=candidate["anomaly_id"],
					title=candidate["title"],
					message=candidate["message"],
					problem=candidate.get("problem", ""),
					evidence_items=candidate.get("evidence_items", []),
					analysis=candidate.get("analysis", ""),
					recommended_action=candidate.get("recommended_action", ""),
					expected_impact=candidate.get("expected_impact", IMPACT_TO_BE_ESTIMATED),
					action_plan=candidate.get("action_plan", []),
					priority=candidate["priority"],
					source=candidate["source"],
					generation_method=method,
					model_name=model_name,
					evidence=candidate["evidence"],
					fingerprint=fingerprint,
				)
		except IntegrityError:
			# Lost a race against the partial unique index — treat as duplicate.
			skipped += 1
			continue
		created.append(recommendation)
	return created, skipped


def _validation_error_code(reason):
	"""Map an internal validation reason to a safe, stable log category."""
	if reason == "item is not an object":
		return "item_not_object"
	if "missing or out of length bounds" in reason:
		return f"{reason.split(' ', 1)[0]}_missing_or_out_of_bounds"
	if reason.startswith("invalid priority"):
		return "invalid_priority"
	if reason.startswith("invalid source") or "not AI-decidable" in reason:
		return "invalid_source"
	if reason.startswith("department "):
		return "unprovided_department_reference"
	if reason.startswith("anomaly "):
		return "unprovided_anomaly_reference"
	if reason.startswith("objective "):
		return "unprovided_objective_reference"
	if "source " in reason and ("requires" in reason or "but no" in reason):
		return "source_reference_mismatch"
	if reason == "evidence_ids is not a list":
		return "evidence_ids_not_list"
	if reason.startswith("evidence id "):
		return "unprovided_evidence_reference"
	if reason == "no evidence_ids supplied":
		return "missing_evidence"
	if reason == "action_plan is not a list":
		return "action_plan_not_list"
	if reason.startswith("action_plan must"):
		return "invalid_action_plan_length"
	if reason.startswith("an action step"):
		return "invalid_action_step_length"
	if reason.startswith("unsupported numeric claim"):
		return "unsupported_numeric_claim"
	return "invalid_recommendation_item"


def _log_ai_failure(*, http_status=None, error_category, validation_codes=(), source):
	"""Log diagnostics without response text, prompts, endpoint, or credentials."""
	codes = ",".join(sorted(set(validation_codes))) or "none"
	logger.warning(
		"[RECO] provider=Ollama/Qwen http_status=%s error_category=%s "
		"response_validation_error=%s timestamp=%s recommendation_source=%s",
		http_status or "none",
		error_category,
		codes,
		timezone.now().isoformat(),
		source,
	)


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def run_generation(user, company, *, as_of=None, limit=MAX_RECOMMENDATIONS):
	"""Run one AI recommendation generation for the user's company."""
	context = build_context(user, company, as_of=as_of)
	if context is None:
		return GenerationResult(ok=False, message="No company is associated with this account.")
	if context.is_empty():
		# Nothing for the AI to reason over. Data-quality issues are handled by
		# CarbonPilot's deterministic rules only — the model is never asked
		# about them and is never sent an empty evidence set.
		created, skipped = persist_candidates(
			context, build_candidates(context), GenerationMethod.RULE_BASED
		)
		if created or skipped:
			return GenerationResult(
				ok=True,
				method=GenerationMethod.RULE_BASED,
				created=created,
				skipped=skipped,
				message=_rule_message(created, skipped),
			)
		return GenerationResult(
			ok=True,
			message="No open anomalies or at-risk objectives to analyse — no recommendation needed.",
		)

	# STEP B — the local model makes the decisions.
	ai_result = ollama_client.generate(
		build_prompt(context, limit=limit),
		schema=ollama_client.response_schema_for(context),
	)

	if ai_result.ok:
		items = parse_response(ai_result.text)
		if items is not None:
			candidates, rejected, rejected_reasons = [], 0, []
			for item in items:
				cleaned, reason = validate_item(item, context)
				if cleaned is None:
					rejected += 1
					rejected_reasons.append(reason)
					continue
				candidates.append(cleaned)
			# If the response was structurally unusable — items were returned but
			# every one failed validation — fall back to the deterministic rules
			# rather than leaving the user with nothing, and log the failure.
			if items and not candidates:
				_log_ai_failure(
					http_status=ai_result.http_status,
					error_category="response_validation_error",
					validation_codes=[_validation_error_code(reason) for reason in rejected_reasons],
					source="RULE-BASED",
				)
			else:
				if rejected_reasons:
					_log_ai_failure(
						http_status=ai_result.http_status,
						error_category="partial_response_validation_error",
						validation_codes=[_validation_error_code(reason) for reason in rejected_reasons],
						source="AI-QWEN",
					)
				created, skipped = persist_candidates(
					context, candidates, GenerationMethod.QWEN, model_name=ollama_client.model_name()
				)
				return GenerationResult(
					ok=True,
					method=GenerationMethod.QWEN,
					created=created,
					rejected=rejected,
					skipped=skipped,
					message=_qwen_message(created, rejected, skipped),
				)
		else:
			_log_ai_failure(
				http_status=ai_result.http_status,
				error_category="response_validation_error",
				validation_codes=(response_validation_code(ai_result.text),),
				source="RULE-BASED",
			)
	else:
		_log_ai_failure(
			http_status=ai_result.http_status,
			error_category=ai_result.error_category or "provider_error",
			source="RULE-BASED",
		)

	# STEP C (fallback) — deterministic, clearly marked RULE_BASED.
	created, skipped = persist_candidates(
		context, build_candidates(context), GenerationMethod.RULE_BASED
	)
	return GenerationResult(
		ok=True,
		method=GenerationMethod.RULE_BASED,
		created=created,
		skipped=skipped,
		ai_unavailable=True,
		message=_fallback_message(created, skipped),
	)


def _qwen_message(created, rejected, skipped):
	if not created and not rejected and not skipped:
		return "The AI model judged that no new recommendation was warranted."
	parts = [f"AI generated {len(created)} new recommendation(s)."]
	if skipped:
		parts.append(f"{skipped} already open (skipped).")
	if rejected:
		parts.append(f"{rejected} item(s) failed validation and were discarded.")
	return " ".join(parts)


def _fallback_message(created, skipped):
	parts = [
		"AI generation was unavailable, so rule-based recommendations were produced instead."
	]
	if created:
		parts.append(f"{len(created)} created.")
	if skipped:
		parts.append(f"{skipped} already open (skipped).")
	if not created and not skipped:
		parts.append("No rule-based recommendation was necessary.")
	return " ".join(parts)


def _rule_message(created, skipped):
	parts = []
	if created:
		parts.append(f"{len(created)} rule-based recommendation(s) created.")
	if skipped:
		parts.append(f"{skipped} already open (skipped).")
	return " ".join(parts) or "No rule-based recommendation was necessary."


# --------------------------------------------------------------------------- #
# Action seam
# --------------------------------------------------------------------------- #
@dataclass
class ActionStartResult:
	ok: bool
	changed: bool = False
	created_task: bool = False
	message: str = ""


def start_action(user, company, recommendation):
	"""Start a recommendation — the seam for a future Action/Task module.

	CarbonPilot has no Action/Task model today, so this function never
	fabricates a task object. It authorises the caller server-side and moves
	the recommendation's own status to ``IN_PROGRESS`` (``created_task`` stays
	``False``, reported honestly to the user). When a real Action model is
	added later, this is the single place that will create the linked task.
	"""
	if company is None or recommendation.company_id != company.pk:
		return ActionStartResult(
			ok=False, message="This recommendation is not part of your company."
		)
	if not can_update_status(user, company, recommendation):
		return ActionStartResult(ok=False, message="You are not allowed to start this recommendation.")
	if recommendation.status in (RecommendationStatus.DONE, RecommendationStatus.DISMISSED):
		return ActionStartResult(ok=False, message="This recommendation is already closed.")
	if recommendation.status == RecommendationStatus.IN_PROGRESS:
		return ActionStartResult(
			ok=True, changed=False, message="This recommendation is already in progress."
		)
	recommendation.status = RecommendationStatus.IN_PROGRESS
	recommendation.save(update_fields=("status", "updated_at"))
	return ActionStartResult(
		ok=True,
		changed=True,
		message=(
			"Recommendation started. CarbonPilot has no task model yet, so the "
			"action plan is tracked on this recommendation."
		),
	)
