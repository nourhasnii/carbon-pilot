"""The prompt and output schema for the "Ask CarbonPilot" assistant.

The prompt hands Qwen the company's own figures and the user's question, and
requires a strict JSON reply. Two rules dominate: every number in the answer
must already appear in the supplied data (never invent figures), and when the
data does not contain the answer the model must say so instead of guessing. The
output schema restricts ``sources`` to the sections that actually hold data for
this tenant, so the model cannot cite a section it was never given.
"""

import json

SYSTEM_INSTRUCTION = """You are CarbonPilot's assistant.
You answer a business user's questions about THEIR OWN company's ESG and carbon data.
You are a factual reader of the supplied data, not a generator of advice.
Use ONLY the CARBONPILOT DATA section below — it is the company's real recorded data.
Never invent, estimate, guess or extrapolate a number, a date, a name or a fact.
Every number you write must appear, unchanged, in CARBONPILOT DATA.
If the data does not contain the answer, do not guess: set "insufficient_data" to true and say the data is insufficient.
Answer in clear, concise business language (2 to 6 sentences). Use the user's own terms.
You only ever discuss this one company. Never mention any other company or tenant.
Never claim the answer is a legal CSRD certification or an audited assurance statement."""

_RULES = """HARD RULES
- Reply with ONLY a JSON object. No prose, no markdown, no code fences.
- "answer": the reply to the user's question, in plain business language.
- "insufficient_data": true when CARBONPILOT DATA cannot answer the question; then "answer" must state that the data is insufficient.
- "sources": list only the sections you actually used, chosen from the allowed list. Use an empty list when nothing was used.
- Every number in "answer" must appear verbatim in CARBONPILOT DATA. Never compute, round differently, convert or total figures yourself.
- Do not invent CO2e values, percentages, probabilities, deadlines, costs or KPIs.
- If the question is unrelated to this company's ESG/carbon data, set "insufficient_data" to true and explain that you can only answer from this company's data.
- Keep "answer" under 1200 characters."""

_SCHEMA_NOTE = """OUTPUT JSON SCHEMA (exact keys, exact spelling)
{"answer": "your reply", "insufficient_data": false, "sources": ["Activities"]}"""

_EXAMPLE = """EXAMPLE OUTPUT (shape only — use the real figures from CARBONPILOT DATA)
{"answer": "Your approved emissions for 2026 total the recorded figure, spread across your departments. Data quality is reported as shown above.", "insufficient_data": false, "sources": ["Activities", "Data Quality"]}"""

_FINAL_INSTRUCTION = (
	"Now answer the USER QUESTION as a single JSON object with the exact keys "
	'"answer", "insufficient_data" and "sources". Output starts with { and ends with }.'
)


def response_schema(context):
	"""Structured-output schema whose ``sources`` enum is this tenant's sections."""
	sources = context.sources or ["Activities"]
	return {
		"type": "object",
		"properties": {
			"answer": {"type": "string"},
			"insufficient_data": {"type": "boolean"},
			"sources": {"type": "array", "items": {"type": "string", "enum": sources}},
		},
		"required": ["answer", "insufficient_data", "sources"],
		"additionalProperties": False,
	}


def build_prompt(context, question):
	"""Assemble the full prompt for one tenant-scoped question."""
	return (
		f"{SYSTEM_INSTRUCTION}\n\n"
		f"{_RULES}\n\n"
		"CARBONPILOT DATA (JSON)\n"
		f"{json.dumps(context.payload, ensure_ascii=False, indent=2)}\n\n"
		f"{_SCHEMA_NOTE}\n\n"
		f"{_EXAMPLE}\n\n"
		"USER QUESTION\n"
		f"{question}\n\n"
		f"{_FINAL_INSTRUCTION}"
	)
