"""Deterministic fallback answer for "Ask CarbonPilot".

Used when the local Ollama model is unavailable, times out, returns unparsable
output, or returns an answer that fails grounding. The fallback never invents a
figure: it reuses the executive summary CarbonPilot already builds from the
company's own approved data (the same text the ESG report opens with), plus a
factual supplier line, and is clearly labelled as not being an AI answer.
"""

NO_DATA_ANSWER = (
	"There is not enough recorded data for your company to answer that question. "
	"No approved carbon activities, objectives, anomalies or suppliers have been "
	"recorded yet. Record activity data (or add suppliers) first, then ask again."
)

UNAVAILABLE_PREFIX = (
	"The AI assistant is currently unavailable, so here is a factual summary of "
	"your company's data instead: "
)


def digest(context, *, ai_unavailable=False):
	"""Return a grounded ``{answer, insufficient_data, sources}`` fallback reply."""
	if not context.has_data or not context.sources:
		return {
			"answer": NO_DATA_ANSWER,
			"insufficient_data": True,
			"sources": [],
		}

	parts = [str(context.payload.get("executive_summary") or "").strip()]
	suppliers = context.payload.get("suppliers")
	if suppliers:
		parts.append(
			f"You track {suppliers['total']} supplier(s), "
			f"{suppliers['high_risk']} of them rated high risk."
		)
	answer = " ".join(part for part in parts if part)
	if ai_unavailable:
		answer = f"{UNAVAILABLE_PREFIX}{answer}"
	return {
		"answer": answer,
		"insufficient_data": False,
		"sources": list(context.sources),
	}
