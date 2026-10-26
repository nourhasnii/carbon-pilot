"""The "Ask CarbonPilot" assistant: Qwen answers, Django grounds and guards.

Flow for one question:

1. :mod:`context` builds the tenant-scoped factual payload from the company's
   own data;
2. :mod:`prompt` asks the local Qwen model (via the shared
   :mod:`apps.recommendations.services.ollama_client`) to answer in strict JSON;
3. the reply is validated here — non-empty answer, ``sources`` limited to the
   sections actually supplied, and every number grounded in the payload. A reply
   that invents a figure is discarded.

When the model is unavailable or its answer fails validation, :mod:`fallback`
returns a deterministic, clearly-labelled summary of the same real data, so the
assistant never invents a number and never breaks the page.
"""

import json
import logging
from dataclasses import dataclass, field

from django.conf import settings
from django.utils import timezone

from apps.recommendations.services import ollama_client
from apps.recommendations.services.context import ungrounded_number

from . import fallback, prompt
from .context import build_context

logger = logging.getLogger(__name__)

METHOD_AI = "AI"
METHOD_RULE_BASED = "RULE_BASED"

DEFAULT_QA_TIMEOUT = 180.0
MAX_QUESTION = 500
MIN_ANSWER = 2
MAX_ANSWER = 4000

# Session key and cap for the per-user question history.
HISTORY_KEY = "assistant_history"
HISTORY_LIMIT = 10


@dataclass
class AskResult:
	"""Outcome of one question. ``ok=False`` carries a user-facing message."""

	ok: bool
	answer: str = ""
	insufficient_data: bool = False
	sources: list = field(default_factory=list)
	method: str = ""
	ai_unavailable: bool = False
	message: str = ""


def qa_timeout():
	try:
		return float(getattr(settings, "OLLAMA_QA_TIMEOUT", DEFAULT_QA_TIMEOUT))
	except (TypeError, ValueError):
		return DEFAULT_QA_TIMEOUT


# --------------------------------------------------------------------------- #
# Parsing / validation
# --------------------------------------------------------------------------- #
def _extract_json(text):
	"""Return the first JSON object in ``text``, or ``None``."""
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


def _clean_answer(data, context):
	"""Validate a model reply. Returns ``(cleaned, reason)``; ``cleaned`` is None on reject."""
	if not isinstance(data, dict):
		return None, "response_not_object"

	answer = str(data.get("answer") or "").strip()
	if not (MIN_ANSWER <= len(answer) <= MAX_ANSWER):
		return None, "answer_missing_or_out_of_bounds"

	raw_sources = data.get("sources")
	if not isinstance(raw_sources, list):
		return None, "sources_not_list"
	allowed = set(context.sources)
	sources = []
	for value in raw_sources:
		label = str(value).strip()
		if label in allowed and label not in sources:
			sources.append(label)

	unsupported = ungrounded_number(answer, context.allowed_numbers)
	if unsupported is not None:
		return None, f"unsupported_numeric_claim {unsupported!r}"

	return {
		"answer": answer,
		"insufficient_data": bool(data.get("insufficient_data")),
		"sources": sources,
	}, None


def _log_failure(*, http_status=None, error_category, detail=""):
	"""Log diagnostics without prompt, answer text, endpoint or credentials."""
	logger.warning(
		"[ASSISTANT] provider=Ollama/Qwen http_status=%s error_category=%s "
		"detail=%s timestamp=%s",
		http_status if http_status is not None else "none",
		error_category,
		detail or "none",
		timezone.now().isoformat(),
	)


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def ask(user, company, question, *, period_year=None):
	"""Answer ``question`` from the company's own data. Never raises for outages."""
	question = (question or "").strip()
	if not question:
		return AskResult(ok=False, message="Please type a question.")
	if len(question) > MAX_QUESTION:
		question = question[:MAX_QUESTION]
	if company is None:
		return AskResult(ok=False, message="No company is associated with this account.")

	context = build_context(company, period_year=period_year)
	if not context.has_data or not context.sources:
		# Nothing real to answer from — say so instead of asking the model.
		return AskResult(ok=True, method=METHOD_RULE_BASED, **fallback.digest(context))

	ai_result = ollama_client.generate(
		prompt.build_prompt(context, question),
		schema=prompt.response_schema(context),
		timeout=qa_timeout(),
	)

	if ai_result.ok:
		cleaned, reason = _clean_answer(_extract_json(ai_result.text), context)
		if cleaned is not None:
			return AskResult(ok=True, method=METHOD_AI, **cleaned)
		_log_failure(
			http_status=ai_result.http_status,
			error_category="response_validation_error",
			detail=reason,
		)
	else:
		_log_failure(
			http_status=ai_result.http_status,
			error_category=ai_result.error_category or "provider_error",
		)

	return AskResult(
		ok=True,
		method=METHOD_RULE_BASED,
		ai_unavailable=True,
		**fallback.digest(context, ai_unavailable=True),
	)


# --------------------------------------------------------------------------- #
# Session history (last N Q&A for this user)
# --------------------------------------------------------------------------- #
def get_history(request):
	history = request.session.get(HISTORY_KEY) or []
	if not isinstance(history, list):
		return []
	return [entry for entry in history if isinstance(entry, dict)]


def push_history(request, question, result):
	"""Append one Q&A to the session history and return the stored entry."""
	entry = {
		"question": question,
		"answer": result.answer,
		"sources": list(result.sources),
		"insufficient_data": result.insufficient_data,
		"method": result.method,
		"ai_unavailable": result.ai_unavailable,
		"asked_at": timezone.now().strftime("%Y-%m-%d %H:%M"),
	}
	history = get_history(request)
	history.append(entry)
	request.session[HISTORY_KEY] = history[-HISTORY_LIMIT:]
	request.session.modified = True
	return entry


def clear_history(request):
	request.session.pop(HISTORY_KEY, None)
	request.session.modified = True
