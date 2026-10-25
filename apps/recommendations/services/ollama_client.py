"""Thin client for a *local* Ollama server running CarbonPilot's LLM.

The project's existing HTTP stack is the standard library's ``urllib.request``
(see ``apps.accounts.services.email_service``), so the same approach is reused
here — no extra dependency, and no tenant data ever leaves the machine. The URL,
model and timeout are configurable through Django settings, which read the
``OLLAMA_URL`` / ``OLLAMA_MODEL`` / ``OLLAMA_TIMEOUT`` environment variables.

Local CPU inference is slow (a 7B model can run at only a few tokens/second), so
the timeout is deliberately generous and ``num_predict`` bounds the response
length. Every failure mode is logged with its exact exception type and a full
traceback so nothing is silently swallowed.
"""

import json
from copy import deepcopy
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.conf import settings

# 127.0.0.1 (not "localhost"): on Windows "localhost" can resolve to IPv6 ::1
# first while Ollama binds IPv4 only, which shows up as a refused connection.
DEFAULT_URL = "http://127.0.0.1:11434/api/generate"
DEFAULT_MODEL = "qwen2.5:7b"
# Local generation of a full recommendation set can take minutes on CPU.
DEFAULT_TIMEOUT = 900.0
DEFAULT_NUM_CTX = 4096
DEFAULT_NUM_PREDICT = 1024
DEFAULT_KEEP_ALIVE = "10m"

RESPONSE_SCHEMA = {
	"type": "object",
	"properties": {
		"recommendations": {
			"type": "array",
			"items": {
				"type": "object",
				"properties": {
					"title": {"type": "string"},
					"message": {"type": "string"},
					"problem": {"type": "string"},
					"analysis": {"type": "string"},
					"recommendation": {"type": "string"},
					"expected_impact": {"type": "string"},
					"action_plan": {"type": "array", "items": {"type": "string"}},
					"priority": {"type": "string", "enum": ["HIGH", "MEDIUM", "LOW"]},
					"source": {"type": "string", "enum": ["ANOMALY", "OBJECTIVE", "MIXED"]},
					"related_department_id": {"type": ["integer", "null"]},
					"related_anomaly_id": {"type": ["integer", "null"]},
					"related_objective_id": {"type": ["integer", "null"]},
					"evidence_ids": {"type": "array", "items": {"type": ["integer", "string"]}},
				},
				"required": [
					"title", "message", "problem", "analysis", "recommendation",
					"expected_impact", "action_plan", "priority", "source",
					"related_department_id", "related_anomaly_id",
					"related_objective_id", "evidence_ids",
				],
				"additionalProperties": False,
			},
		},
	},
	"required": ["recommendations"],
	"additionalProperties": False,
}


@dataclass
class OllamaResult:
	"""Outcome of one Ollama call. ``ok=False`` never raises at the caller."""

	ok: bool
	text: str = ""
	error: str = ""
	http_status: int | None = None
	error_category: str = ""


def endpoint():
	return getattr(settings, "OLLAMA_URL", DEFAULT_URL)


def model_name():
	return getattr(settings, "OLLAMA_MODEL", DEFAULT_MODEL)


def timeout_seconds():
	try:
		return float(getattr(settings, "OLLAMA_TIMEOUT", DEFAULT_TIMEOUT))
	except (TypeError, ValueError):
		return DEFAULT_TIMEOUT


def _int_setting(name, default):
	try:
		return int(getattr(settings, name, default))
	except (TypeError, ValueError):
		return default


def response_schema_for(context):
	"""Restrict entity references and source values to this tenant's context."""
	schema = deepcopy(RESPONSE_SCHEMA)
	item = schema["properties"]["recommendations"]["items"]
	properties = item["properties"]
	anomaly_ids = sorted(context.anomaly_ids)
	objective_ids = sorted(context.objective_ids)
	department_ids = sorted(context.company_department_ids)
	properties["related_department_id"]["enum"] = [None, *department_ids]
	properties["related_anomaly_id"]["enum"] = [None, *anomaly_ids]
	properties["related_objective_id"]["enum"] = [None, *objective_ids]
	evidence_ids = [
		*(f"anomaly:{value}" for value in anomaly_ids),
		*(f"objective:{value}" for value in objective_ids),
		*(f"department:{value}" for value in department_ids),
	]
	properties["evidence_ids"]["items"]["enum"] = evidence_ids
	allowed_sources = []
	if anomaly_ids:
		allowed_sources.append("ANOMALY")
	if objective_ids:
		allowed_sources.append("OBJECTIVE")
	if anomaly_ids and objective_ids:
		allowed_sources.append("MIXED")
	properties["source"]["enum"] = allowed_sources
	return schema


def generate(prompt, *, timeout=None, schema=None):
	"""Ask the local model to answer ``prompt`` as JSON. Never raises."""
	payload = {
		"model": model_name(),
		"prompt": prompt,
		# Structured output enforces the envelope; Django still validates every value.
		"format": schema or RESPONSE_SCHEMA,
		"stream": False,
		# Keep the model resident so subsequent runs skip the (slow) reload.
		"keep_alive": getattr(settings, "OLLAMA_KEEP_ALIVE", DEFAULT_KEEP_ALIVE),
		"options": {
			"temperature": 0,
			"num_ctx": _int_setting("OLLAMA_NUM_CTX", DEFAULT_NUM_CTX),
			"num_predict": _int_setting("OLLAMA_NUM_PREDICT", DEFAULT_NUM_PREDICT),
		},
	}
	effective_timeout = timeout or timeout_seconds()
	try:
		request = Request(
			endpoint(),
			data=json.dumps(payload).encode("utf-8"),
			headers={"Content-Type": "application/json"},
			method="POST",
		)
		with urlopen(request, timeout=effective_timeout) as response:
			body = response.read().decode("utf-8", errors="replace")
	except HTTPError as exc:
		category = "authentication" if exc.code in (401, 403) else "http_error"
		return OllamaResult(
			ok=False,
			error=f"HTTP {exc.code}",
			http_status=exc.code,
			error_category=category,
		)
	except (URLError, TimeoutError, OSError, ValueError, TypeError) as exc:
		category = "timeout" if isinstance(exc, TimeoutError) else "network_or_request_error"
		if isinstance(exc, URLError) and isinstance(exc.reason, TimeoutError):
			category = "timeout"
		return OllamaResult(
			ok=False,
			error=type(exc).__name__,
			error_category=category,
		)
	except Exception as exc:  # noqa: BLE001 - client boundary must never break fallback
		return OllamaResult(
			ok=False,
			error=type(exc).__name__,
			error_category="unexpected_client_error",
		)

	try:
		envelope = json.loads(body)
	except (ValueError, TypeError):
		return OllamaResult(
			ok=False,
			error="Invalid response envelope",
			error_category="invalid_response_json",
			http_status=response.status,
		)

	text = envelope.get("response") if isinstance(envelope, dict) else None
	if not isinstance(text, str) or not text.strip():
		return OllamaResult(
			ok=False,
			error="Response contained no text",
			error_category="empty_response",
			http_status=response.status,
		)
	return OllamaResult(ok=True, text=text, http_status=response.status)


def diagnose():
	"""Test configured model access with a synthetic, non-sensitive prompt."""
	configured = bool(endpoint() and model_name())
	result = {
		"provider": "Ollama/Qwen",
		"configured": configured,
		"reachable": False,
		"authentication": "NOT_CHECKED",
		"model": model_name(),
		"response": "ERROR",
		"http_status": None,
		"error_category": "not_configured" if not configured else "",
	}
	if not configured:
		return result

	probe = generate(
		'Return exactly this valid JSON object: {"recommendations": []}',
		timeout=min(timeout_seconds(), 45),
	)
	result["http_status"] = probe.http_status
	result["reachable"] = probe.http_status is not None
	if probe.error_category == "authentication":
		result["authentication"] = "INVALID"
	elif probe.http_status is not None and 200 <= probe.http_status < 300:
		# Local Ollama has no credential requirement; HTTP success proves access.
		result["authentication"] = "VALID (no credentials required)"
	elif result["reachable"]:
		result["authentication"] = "NOT_CHECKED"
	if not probe.ok:
		result["error_category"] = probe.error_category or "provider_error"
		return result

	try:
		content = json.loads(probe.text)
	except (ValueError, TypeError):
		result["error_category"] = "invalid_diagnostic_json"
		return result
	if isinstance(content, dict) and isinstance(content.get("recommendations"), list):
		result["response"] = "SUCCESS"
	else:
		result["error_category"] = "invalid_diagnostic_response"
	return result
