"""Data quality scoring for carbon activities.

The score rewards the information a professional ESG auditor expects to see
behind every reported figure: a complete reporting period, an accountable
department, a verified emission factor, a stated data source and attached
evidence. It is computed on the fly so it always reflects the current record.
"""

from apps.carbon.models import DataQualityBand, VerificationStatus

# Each check contributes a fixed weight; the total is 100.
_CHECKS = (
	("reporting_period", "Reporting period", 20),
	("department", "Department assigned", 15),
	("emission_factor_verified", "Emission factor verified", 25),
	("data_source", "Source provided", 15),
	("evidence", "Evidence attached", 25),
)

WEIGHTS = {key: weight for key, _label, weight in _CHECKS}
BAND_THRESHOLDS = ((80, DataQualityBand.HIGH), (50, DataQualityBand.MEDIUM))


def _band_for(score):
	for threshold, band in BAND_THRESHOLDS:
		if score >= threshold:
			return band
	return DataQualityBand.LOW


def _evaluate(activity):
	"""Return ``[(key, label, ok, detail, weight), ...]`` for one activity."""
	factor = activity.emission_factor
	factor_details = []
	if factor is None:
		factor_details = ["No emission factor selected"]
	else:
		factor_details = [factor.source or "Source not documented"]
		if factor.verification_status != VerificationStatus.VERIFIED:
			factor_details.append("Not independently verified")

	period_ok = bool(activity.period_start and activity.period_end)

	return [
		(
			"reporting_period",
			"Reporting period",
			period_ok,
			"" if period_ok else "Add a period end date",
			WEIGHTS["reporting_period"],
		),
		(
			"department",
			"Department assigned",
			activity.department_id is not None,
			"" if activity.department_id else "Assign an accountable department",
			WEIGHTS["department"],
		),
		(
			"emission_factor_verified",
			"Emission factor verified",
			factor is not None and factor.verification_status == VerificationStatus.VERIFIED,
			" · ".join(factor_details),
			WEIGHTS["emission_factor_verified"],
		),
		(
			"data_source",
			"Source provided",
			bool((activity.data_source or "").strip()),
			"" if (activity.data_source or "").strip() else "State where the data comes from",
			WEIGHTS["data_source"],
		),
		(
			"evidence",
			"Evidence attached",
			bool(activity.evidence),
			"" if activity.evidence else "Attach an invoice, receipt or meter reading",
			WEIGHTS["evidence"],
		),
	]


def score_activity(activity):
	"""Compute the data-quality score and confidence band for an activity."""
	evaluated = _evaluate(activity)
	score = sum(weight for _key, _label, ok, _detail, weight in evaluated if ok)
	band = _band_for(score)
	return {
		"score": score,
		"band": band,
		"band_label": DataQualityBand(band).label,
		"checks": [
			{"key": key, "label": label, "ok": ok, "detail": detail, "weight": weight}
			for key, label, ok, detail, weight in evaluated
		],
	}


def average_score(activities):
	"""Average data-quality score across an iterable of activities."""
	scores = [score_activity(activity)["score"] for activity in activities]
	if not scores:
		return 0
	return round(sum(scores) / len(scores))
