"""Deterministic rule-based fallback recommendations.

Used when the local model is unavailable, times out, returns unparsable output,
or returns output that is entirely invalid. These rows are written by
CarbonPilot's own rules from **verified** anomalies and objectives, and are
always marked ``RULE_BASED`` so they are never presented as AI output.

Like the AI path, every candidate carries the full structured contract
(problem / evidence_items / analysis / recommended_action / expected_impact /
action_plan). The narrative is written by these rules — never by the model — and
the evidence is built from the same tenant-scoped, database-grounded context, so
a fallback recommendation can never reference another company's data.
"""

from apps.carbon.models import AnomalySeverity, AnomalyType

from ..models import (
	IMPACT_TO_BE_ESTIMATED,
	RecommendationPriority,
	RecommendationSource,
)
from .context import ungrounded_number
from .evidence import evidence_for


def _label(choices, value, default):
	if value in dict(choices):
		return dict(choices)[value]
	return default


def _anomaly_candidates(context):
	candidates = []
	for row in context.payload.get("open_anomalies", []):
		severity = row.get("severity") or AnomalySeverity.LOW
		type_label = _label(AnomalyType.choices, row.get("anomaly_type"), "emission")
		department = row.get("department_name") or "company-wide scope"
		if severity == AnomalySeverity.HIGH:
			priority = RecommendationPriority.HIGH
		elif severity == AnomalySeverity.MEDIUM:
			priority = RecommendationPriority.MEDIUM
		else:
			priority = RecommendationPriority.LOW

		problem = (
			f"Anomaly detection flagged a {severity.lower()}-severity "
			f"{type_label.lower()} linked to an activity in {department}."
		)
		if row.get("co2e_tco2e") is not None:
			analysis = (
				f"The flagged activity accounts for {row['co2e_tco2e']} tCO2e, which "
				"deviates from the department baseline recorded at detection time. "
				"The reported emissions may therefore be wrong, or the consumption may "
				"have genuinely increased."
			)
		else:
			analysis = (
				"No approved CO2e figure is attached to the flagged activity, so the "
				"flag is based on the recorded activity data alone."
			)
		recommended_action = (
			"Verify the underlying activity, correct or reclassify the source data, "
			"and record the resolution so the emissions figure can be trusted."
		)
		action_plan = [
			"Open the flagged activity and confirm its quantity and unit.",
			"Check that the emission factor applied is the correct one.",
			"Correct the data or its classification if an error is found.",
			"Record the resolution against the anomaly.",
		]

		candidates.append(
			{
				"title": f"Investigate {severity.lower()} {type_label.lower()} anomaly — {department}",
				"message": recommended_action,
				"problem": problem,
				"analysis": analysis,
				"recommended_action": recommended_action,
				"expected_impact": IMPACT_TO_BE_ESTIMATED,
				"action_plan": action_plan,
				"priority": priority,
				"source": RecommendationSource.ANOMALY,
				"department_id": row.get("department_id"),
				"objective_id": None,
				"anomaly_id": row["id"],
				"evidence": [f"anomaly:{row['id']}"],
				"evidence_items": evidence_for(
					context, source=RecommendationSource.ANOMALY, anomaly_id=row["id"]
				),
			}
		)
	return candidates


def _objective_candidates(context):
	candidates = []
	for row in context.payload.get("at_risk_objectives", []):
		prediction = row.get("prediction")
		progress = row.get("progress_percent")
		if prediction and prediction.get("risk_level") == "HIGH":
			priority = RecommendationPriority.HIGH
		elif prediction and prediction.get("risk_level") == "MEDIUM":
			priority = RecommendationPriority.MEDIUM
		elif progress is not None and progress < 40:
			priority = RecommendationPriority.HIGH
		elif progress is not None and progress < 70:
			priority = RecommendationPriority.MEDIUM
		else:
			priority = RecommendationPriority.LOW

		problem = (
			f"Objective \u201c{row['name']}\u201d is at risk: a reduction of "
			f"{row.get('gap_tco2e')} tCO2e is still required to reach its target of "
			f"{row.get('target_tco2e')} tCO2e before {row.get('deadline')}."
		)
		parts = [
			f"Progress stands at {progress}% with the deadline on {row.get('deadline')}."
		]
		if prediction:
			parts.append(
				f"The latest prediction is {prediction.get('expected_outcome')} at "
				f"{prediction.get('probability_achievement')}% probability "
				f"({prediction.get('risk_level')} risk)."
			)
		analysis = " ".join(parts)
		recommended_action = (
			"Identify and act on the largest remaining emission sources within the "
			"objective's scope so the committed reduction can still be met."
		)
		action_plan = [
			"Review the current emission sources within this objective's scope.",
			"Identify the highest-impact sources that are still addressable before the deadline.",
			"Assign the corrective work to the responsible department.",
			"Implement the corrective action and track its effect on emissions.",
		]

		candidates.append(
			{
				"title": f"Accelerate progress on objective \u201c{row['name']}\u201d",
				"message": recommended_action,
				"problem": problem,
				"analysis": analysis,
				"recommended_action": recommended_action,
				"expected_impact": IMPACT_TO_BE_ESTIMATED,
				"action_plan": action_plan,
				"priority": priority,
				"source": RecommendationSource.OBJECTIVE,
				"department_id": row.get("department_id"),
				"objective_id": row["id"],
				"anomaly_id": None,
				"evidence": [f"objective:{row['id']}"],
				"evidence_items": evidence_for(
					context,
					source=RecommendationSource.OBJECTIVE,
					objective_id=row["id"],
				),
			}
		)
	return candidates


def _data_quality_candidates(context):
	"""A deterministic data-quality recommendation, only when issues exist.

	DATA_QUALITY is never produced by the model — it is computed here from real
	query counts, so the recommendation is honest even when the AI is offline.
	"""
	signals = context.payload.get("data_quality") or {}
	total = signals.get("total_issues") or 0
	if total <= 0:
		return []

	if total >= 5:
		priority = RecommendationPriority.HIGH
	elif total >= 2:
		priority = RecommendationPriority.MEDIUM
	else:
		priority = RecommendationPriority.LOW

	notes = []
	if signals.get("unvalidated_activities"):
		notes.append(
			f"{signals['unvalidated_activities']} activity(ies) are awaiting validation."
		)
	if signals.get("activities_missing_emission_factor"):
		notes.append(
			f"{signals['activities_missing_emission_factor']} activity(ies) have no emission factor."
		)
	if signals.get("stale_draft_activities"):
		notes.append(f"{signals['stale_draft_activities']} draft(s) have been idle for a long time.")
	if signals.get("submitted_without_evidence"):
		notes.append(
			f"{signals['submitted_without_evidence']} submitted activity(ies) have no evidence attached."
		)

	problem = (
		"Some activity records are incomplete or unvalidated, which weakens the "
		"reliability of the emissions figures."
	)
	analysis = " ".join(notes) if notes else (
		"CarbonPilot found incomplete activity data in your company."
	)
	recommended_action = (
		"Complete the missing activity data and validate the pending activities so "
		"the emissions figures can be relied upon."
	)
	action_plan = [
		"List the activities flagged as incomplete or unvalidated.",
		"Attach the missing evidence and assign the correct emission factor.",
		"Validate or reject the pending activities.",
		"Re-check the carbon overview once the data is complete.",
	]

	return [
		{
			"title": "Complete incomplete activity data",
			"message": recommended_action,
			"problem": problem,
			"analysis": analysis,
			"recommended_action": recommended_action,
			"expected_impact": IMPACT_TO_BE_ESTIMATED,
			"action_plan": action_plan,
			"priority": priority,
			"source": RecommendationSource.DATA_QUALITY,
			"department_id": None,
			"objective_id": None,
			"anomaly_id": None,
			"evidence": [],
			"evidence_items": evidence_for(
				context, source=RecommendationSource.DATA_QUALITY
			),
		}
	]


def _narrative_text(candidate):
	return "\n".join(
		[
			candidate["title"],
			candidate["message"],
			candidate.get("problem", ""),
			candidate.get("analysis", ""),
			candidate.get("recommended_action", ""),
			*candidate.get("action_plan", []),
		]
	)


def build_candidates(context):
	"""Deterministic candidates grounded in verified evidence only."""
	candidates = (
		_anomaly_candidates(context)
		+ _objective_candidates(context)
		+ _data_quality_candidates(context)
	)
	valid = []
	for candidate in candidates:
		if ungrounded_number(_narrative_text(candidate), context.allowed_numbers) is not None:
			# Defensive: never emit a rule-based text with an ungrounded figure.
			continue
		valid.append(candidate)
	return valid
