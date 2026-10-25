"""STEP B — the prompt sent to the local Qwen model.

The prompt hands Qwen the factual evidence, the existing open recommendations
(to avoid duplication) and an explicit allowlist of numeric facts with their
provenance. Qwen decides *which* actions to recommend and at which priority —
nothing is preselected by CarbonPilot.

Ordering matters for small local models: the output schema and a filled example
are placed **after** the data (recency) and the anti-echo rule is the final
instruction, otherwise a 7B model tends to copy the evidence objects back out.
"""

import json

from .context import numeric_facts

# Maximum recommendations per run. The model is told to prefer fewer.
MAX_RECOMMENDATIONS = 5

SYSTEM_INSTRUCTION = """You are CarbonPilot's sustainability decision-support engine.
Analyze the supplied evidence and independently decide which concrete corrective or preventive actions, if any, should be recommended.
Do not simply rewrite the evidence.
Do not follow a predefined recommendation list.
Do not invent facts.
Choose actions that are relevant to the evidence.
Prioritize based on environmental impact, urgency, severity, and the evidence provided.
Explain why each action is justified.
If evidence is insufficient, return no recommendation for that case.
If no useful new action is warranted, return an empty recommendations array.
Avoid duplicating existing open recommendations.
For each recommendation, structure your reasoning into the sections: problem, analysis, recommendation and action_plan."""

_RULES = """HARD RULES
- Reply with ONLY a JSON object. No prose, no markdown, no code fences.
- Return at most {limit} recommendations. Prefer fewer; never pad to reach the limit.
- Each recommendation is a NEW ACTION to carry out — never an objective or an anomaly copied from the data.
- Propose an action only when the evidence justifies a concrete corrective or preventive action.
- Reference entities strictly by the IDs listed in "allowed_ids". Never invent an ID.
- Every number you write must appear in "evidence" or in "allowed_numeric_facts".
  Never invent CO2e figures, savings, percentages, probabilities, deadlines or KPIs.
- Do not use numeric figures in title, message, problem, analysis, recommendation or action_plan.
  Entity IDs may appear only in their dedicated ID fields. Evidence displays the factual metrics.
- The EVIDENCE section is supplied by CarbonPilot from its own database. Do NOT write an
  evidence field and never restate figures as if you measured them.
- "expected_impact": you have no simulation model, so always return exactly the string
  "To be estimated" — never a percentage, a saving or a cost.
- "source" must match the entities you reference:
  ANOMALY (anomaly only), OBJECTIVE (objective only), MIXED (both an anomaly and an objective).
- Do not repeat anything already listed in "existing_open_recommendations".
- If the evidence is insufficient for a case, emit no recommendation for it.
- If no useful new action is warranted, return exactly: {{"recommendations": []}}"""

_SCHEMA = """OUTPUT JSON SCHEMA (exact keys, exact spelling)
{{"recommendations": [
  {{"title": "short actionable title",
    "message": "one-sentence summary of the action",
    "problem": "what problem was detected, from the evidence only",
    "analysis": "why the system considers this important",
    "recommendation": "what the company should do",
    "expected_impact": "To be estimated",
    "action_plan": ["first concrete step", "second concrete step", "..."],
    "priority": "HIGH" | "MEDIUM" | "LOW",
    "source": "ANOMALY" | "OBJECTIVE" | "MIXED",
    "related_department_id": <integer from allowed_ids.department_ids, or null>,
    "related_anomaly_id": <integer from allowed_ids.anomaly_ids, or null>,
    "related_objective_id": <integer from allowed_ids.objective_ids, or null>,
    "evidence_ids": [<ids copied from allowed_ids that support this action>]}}
]}}"""

_FIELD_NOTES = """EXACT FIELD NAMES (copy them verbatim)
- "title" (string) and "message" (string) — NOT "description", NOT "text", NOT "body".
- "problem", "analysis", "recommendation" (strings) — always include all three.
- "expected_impact" (string) — always exactly "To be estimated".
- "action_plan" (array of strings) — 1 to 12 concrete, imperative steps; always include it.
- "priority" (string) exactly "HIGH", "MEDIUM" or "LOW" in UPPERCASE — never "High".
- "source" (string) exactly "ANOMALY", "OBJECTIVE" or "MIXED" — always include it.
- "related_department_id", "related_anomaly_id", "related_objective_id" (integer or null).
- "evidence_ids" (array of IDs copied from the evidence context) — always include it.
- Do not put numeric figures in narrative text; only identifier fields may contain digits.
- Never output "id", "name", "status", "description", "year", "gap_tco2e",
  "progress_percent", "remaining_days" or any other key."""

_FINAL_INSTRUCTION = (
	'Now write your answer: a single JSON object {{"recommendations": [...]}} '
	"containing only NEW actions you have decided.\n"
	"Do NOT copy, echo, quote, restate or list the objectives or anomalies from CARBONPILOT DATA.\n"
	"Each item must be an action to carry out (imperative), with problem, analysis, recommendation and action_plan filled in.\n"
	"Use ONLY the exact keys from the schema. Output starts with { and ends with }."
)


def _example(context):
  """Build a shape example whose references exist in this tenant's evidence."""
  anomalies = context.payload.get("open_anomalies", [])
  objectives = context.payload.get("at_risk_objectives", [])
  if anomalies:
    row = anomalies[0]
    source = "ANOMALY"
    anomaly_id, objective_id = row["id"], None
    evidence_id = f"anomaly:{anomaly_id}"
  else:
    row = objectives[0]
    source = "OBJECTIVE"
    anomaly_id, objective_id = None, row["id"]
    evidence_id = f"objective:{objective_id}"
  item = {
    "title": "Review the supplied evidence",
    "message": "Verify the underlying records and document the outcome.",
    "problem": "The supplied evidence indicates an issue that needs review.",
    "analysis": "Verification can determine whether corrective action is needed.",
    "recommendation": "Review the linked records and address any confirmed issue.",
    "expected_impact": "To be estimated",
    "action_plan": ["Review the linked records.", "Verify the supporting data."],
    "priority": "MEDIUM",
    "source": source,
    "related_department_id": row.get("department_id"),
    "related_anomaly_id": anomaly_id,
    "related_objective_id": objective_id,
    "evidence_ids": [evidence_id],
  }
  return "EXAMPLE OUTPUT (shape and allowed-ID reference only)\n" + json.dumps(
    {"recommendations": [item]}, ensure_ascii=False, indent=2
  )


def build_prompt(context, *, limit=MAX_RECOMMENDATIONS):
	"""Assemble the full prompt for one tenant-scoped generation run."""
	ai_evidence = {key: value for key, value in context.payload.items() if key != "data_quality"}
	data = {
		"evidence": ai_evidence,
		"existing_open_recommendations": context.existing,
		"allowed_numeric_facts": numeric_facts(context),
		"allowed_ids": {
			"anomaly_ids": sorted(context.anomaly_ids),
			"objective_ids": sorted(context.objective_ids),
			"department_ids": sorted(context.company_department_ids),
		},
	}
	return (
		f"{SYSTEM_INSTRUCTION}\n\n"
		f"{_RULES.format(limit=limit)}\n\n"
		"CARBONPILOT DATA (JSON)\n"
		f"{json.dumps(data, ensure_ascii=False, indent=2)}\n\n"
		f"{_SCHEMA}\n\n"
    f"{_example(context)}\n\n"
		f"{_FIELD_NOTES}\n\n"
		f"{_FINAL_INSTRUCTION}"
	)
