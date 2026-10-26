"""Deterministic ESG **data-completeness** engine.

This module answers one question: *how complete and trustworthy is the
company's ESG data set, and is it ready to be reported?* It is deliberately
**not** a performance rating — a company can report perfect data on poor ESG
performance, and vice versa. Django computes every score from the tenant's own
rows; nothing is hardcoded, sampled or invented.

Scoring rules (total = 100)
---------------------------
Four components are summed. Each is capped and its denominator is stated
explicitly so the score is reproducible:

1. **Indicator coverage** (max 40) — required catalog indicators that the
   company actually tracks with an *active* indicator. ``covered / required``.
2. **Reporting-period completeness** (max 20) — of the covered required
   indicators, the share that has an *eligible* measurement (status
   ``REPORTED``/``VERIFIED`` with a non-null value) in the target period.
3. **Verification** (max 25) — of the covered required indicators with an
   eligible value in the target period, the share that is ``VERIFIED``.
4. **Evidence coverage** (max 15) — the same denominator, the share whose
   eligible value carries an evidence reference.

Eligibility and edge cases
--------------------------
* **Missing vs zero**: a value of ``None`` is a missing measurement and can
  never be eligible; ``Decimal("0")`` is a genuine measured zero and is.
* **Inactive indicators**: excluded from coverage (they are not eligible) and
  reported as a gap.
* **Duplicate records**: impossible by construction — an indicator is unique
  per (company, catalog entry) and a value is unique per (indicator, period).
* **Draft-only**: a value in ``DRAFT`` is work in progress; it does not count
  and the indicator is reported as a *draft-only* gap.
* **Different reporting periods**: only the target period's values count.
  Other periods are preserved as history but never contribute to the target
  period's score.

Readiness
---------
* ``NOT_READY`` — no required catalog or no eligible reported data at all.
* ``NEEDS_REVIEW`` — partial data, or verification/evidence still missing.
* ``READY_FOR_REPORTING`` — score >= 85, every required indicator tracked and
  reported for the period (no blocking gap) and at least half the evidence in
  place.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from django.utils import timezone

from ..models import (
	ELIGIBLE_STATUSES,
	PILLAR_ORDER,
	AssessmentSnapshot,
	ESGPillar,
	Indicator,
	IndicatorCatalogEntry,
	IndicatorStatus,
	IndicatorValue,
	Readiness,
)

# --- Component weights (points). The four weights sum to 100. -------------- #
COVERAGE_WEIGHT = 40
PERIOD_WEIGHT = 20
VERIFICATION_WEIGHT = 25
EVIDENCE_WEIGHT = 15

TOTAL_WEIGHT = COVERAGE_WEIGHT + PERIOD_WEIGHT + VERIFICATION_WEIGHT + EVIDENCE_WEIGHT

# Component catalogue shown in the UI scoring breakdown (display order).
SCORING_WEIGHTS = (
	{
		"key": "coverage",
		"label": "Indicator coverage",
		"max_points": COVERAGE_WEIGHT,
		"description": "Required catalog indicators the company actively tracks.",
	},
	{
		"key": "period",
		"label": "Reporting-period completeness",
		"max_points": PERIOD_WEIGHT,
		"description": "Covered indicators with an eligible value in the target period.",
	},
	{
		"key": "verification",
		"label": "Verification",
		"max_points": VERIFICATION_WEIGHT,
		"description": "Reported values that have been verified.",
	},
	{
		"key": "evidence",
		"label": "Evidence coverage",
		"max_points": EVIDENCE_WEIGHT,
		"description": "Reported values backed by an evidence reference.",
	},
)

# Readiness thresholds (documented so the verdict is fully explainable).
READY_MIN_SCORE = Decimal("85")
REVIEW_MIN_SCORE = Decimal("50")
READY_MIN_EVIDENCE_RATE = 0.5

# Gap kinds, in the order they are shown to the user.
GAP_KINDS = (
	("missing", "Not tracked"),
	("inactive", "Inactive"),
	("incomplete", "Not reported for period"),
	("draft_only", "Draft only"),
	("unverified", "Unverified"),
	("no_evidence", "No evidence"),
)


def _q(value):
	return Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _rate(part, whole):
	if not whole:
		return 0.0
	return float(part) / float(whole)


@dataclass
class Gap:
	"""One concrete gap found by the assessment."""

	kind: str
	code: str
	name: str
	pillar: str
	pillar_label: str
	indicator_id: int | None
	detail: str

	def as_dict(self):
		return {
			"kind": self.kind,
			"code": self.code,
			"name": self.name,
			"pillar": self.pillar,
			"pillar_label": self.pillar_label,
			"indicator_id": self.indicator_id,
			"detail": self.detail,
		}


@dataclass
class AssessmentResult:
	"""Outcome of one completeness run, safe to surface in the UI or persist."""

	overall_score: Decimal
	readiness: str
	period_year: int | None
	environmental_score: Decimal
	social_score: Decimal
	governance_score: Decimal
	summary: str
	breakdown: list = field(default_factory=list)
	gaps: dict = field(default_factory=dict)
	total_required: int = 0
	covered: int = 0
	eligible: int = 0
	verified: int = 0
	with_evidence: int = 0
	insufficient_data: bool = False

	def as_dict(self):
		return {
			"overall_score": self.overall_score,
			"readiness": self.readiness,
			"period_year": self.period_year,
			"environmental_score": self.environmental_score,
			"social_score": self.social_score,
			"governance_score": self.governance_score,
			"summary": self.summary,
			"breakdown": self.breakdown,
			"gaps": self.gaps,
			"total_required": self.total_required,
			"covered": self.covered,
			"eligible": self.eligible,
			"verified": self.verified,
			"with_evidence": self.with_evidence,
			"insufficient_data": self.insufficient_data,
		}


def target_period_year(company, requested=None):
	"""The reporting year to assess.

	An explicitly requested year always wins. Otherwise the latest year the
	company has reported anything for is used, falling back to the current
	year so an empty company is assessed against "now".
	"""
	if requested:
		return int(requested)
	latest = (
		IndicatorValue.objects.filter(company=company)
		.exclude(period_year__isnull=True)
		.order_by("-period_year")
		.values_list("period_year", flat=True)
		.first()
	)
	return latest or timezone.localdate().year


def available_period_years(company):
	"""Reporting years the company has any value for, newest first."""
	years = (
		IndicatorValue.objects.filter(company=company)
		.values_list("period_year", flat=True)
		.distinct()
	)
	return sorted({year for year in years if year}, reverse=True)


def _required_entries(company):
	return list(
		IndicatorCatalogEntry.objects.for_company(company)
		.filter(is_required=True, is_active=True)
		.order_by("pillar", "sort_order", "code")
	)


def assess_company(company, *, period_year=None):
	"""Compute the ESG data-completeness of one company (no persistence).

	Only the given ``company``'s rows are read. Returns an
	:class:`AssessmentResult` with the score, readiness, per-pillar coverage,
	the scoring breakdown and the concrete gaps.
	"""
	if company is None:
		raise ValueError("A company is required to run an ESG assessment.")

	year = target_period_year(company, period_year)
	entries = _required_entries(company)
	indicators = {
		indicator.catalog_entry_id: indicator
		for indicator in Indicator.objects.filter(company=company).select_related(
			"catalog_entry"
		)
	}
	# Eligible values for the target period, keyed by indicator id.
	values_by_indicator = {}
	for value in IndicatorValue.objects.filter(
		company=company, period_year=year
	).select_related("indicator__catalog_entry"):
		values_by_indicator.setdefault(value.indicator_id, []).append(value)

	covered = []
	gaps = {kind: [] for kind, _label in GAP_KINDS}
	pillar_required = {pillar: 0 for pillar in PILLAR_ORDER}
	pillar_covered = {pillar: 0 for pillar in PILLAR_ORDER}
	pillar_eligible = {pillar: 0 for pillar in PILLAR_ORDER}
	verified_count = 0
	evidence_count = 0

	for entry in entries:
		pillar_required[entry.pillar] += 1
		indicator = indicators.get(entry.pk)

		if indicator is None:
			gaps["missing"].append(
				_gap(entry, None, "missing", "No indicator tracks this required metric yet.")
			)
			continue
		if not indicator.is_active:
			gaps["inactive"].append(
				_gap(
					entry,
					indicator,
					"inactive",
					"This indicator is inactive, so it does not count towards completeness.",
				)
			)
			continue

		covered.append(indicator)
		pillar_covered[entry.pillar] += 1

		period_values = values_by_indicator.get(indicator.pk, [])
		eligible = [value for value in period_values if value.is_eligible and value.has_value]
		if not eligible:
			# Distinguish "draft-only" from "nobody reported anything".
			if any(value.status == IndicatorStatus.DRAFT for value in period_values):
				gaps["draft_only"].append(
					_gap(
						entry,
						indicator,
						"draft_only",
						f"A {year} value exists but is still a draft.",
					)
				)
			else:
				gaps["incomplete"].append(
					_gap(
						entry,
						indicator,
						"incomplete",
						f"No eligible {year} value has been reported.",
					)
				)
			continue

		pillar_eligible[entry.pillar] += 1
		best = _best_value(eligible)
		if best.is_verified:
			verified_count += 1
		else:
			gaps["unverified"].append(
				_gap(
					entry,
					indicator,
					"unverified",
					f"The {year} value is reported but not verified.",
				)
			)
		if (best.evidence_reference or "").strip():
			evidence_count += 1
		else:
			gaps["no_evidence"].append(
				_gap(
					entry,
					indicator,
					"no_evidence",
					f"The {year} value has no evidence reference.",
				)
			)

	total_required = len(entries)
	covered_count = len(covered)
	eligible_count = sum(pillar_eligible.values())

	# --- Components ------------------------------------------------------ #
	coverage_points = _q(Decimal(str(_rate(covered_count, total_required))) * COVERAGE_WEIGHT)
	period_points = _q(Decimal(str(_rate(eligible_count, covered_count))) * PERIOD_WEIGHT)
	verification_points = _q(
		Decimal(str(_rate(verified_count, eligible_count))) * VERIFICATION_WEIGHT
	)
	evidence_points = _q(Decimal(str(_rate(evidence_count, eligible_count))) * EVIDENCE_WEIGHT)

	breakdown = [
		{
			"key": "coverage",
			"label": "Indicator coverage",
			"points": float(coverage_points),
			"max_points": COVERAGE_WEIGHT,
			"detail": (
				f"{covered_count} of {total_required} required indicator(s) are actively tracked."
			),
		},
		{
			"key": "period",
			"label": "Reporting-period completeness",
			"points": float(period_points),
			"max_points": PERIOD_WEIGHT,
			"detail": (
				f"{eligible_count} of {covered_count} tracked indicator(s) have an eligible "
				f"{year} value."
			),
		},
		{
			"key": "verification",
			"label": "Verification",
			"points": float(verification_points),
			"max_points": VERIFICATION_WEIGHT,
			"detail": f"{verified_count} of {eligible_count} reported value(s) are verified.",
		},
		{
			"key": "evidence",
			"label": "Evidence coverage",
			"points": float(evidence_points),
			"max_points": EVIDENCE_WEIGHT,
			"detail": (
				f"{evidence_count} of {eligible_count} reported value(s) carry an evidence "
				"reference."
			),
		},
	]

	total = coverage_points + period_points + verification_points + evidence_points
	overall_score = _q(min(max(total, Decimal("0")), Decimal("100")))

	pillar_scores = {
		pillar: _q(
			Decimal(str(_rate(pillar_eligible[pillar], pillar_required[pillar]))) * 100
		)
		for pillar in PILLAR_ORDER
	}

	blocking = (
		gaps["missing"] or gaps["inactive"] or gaps["incomplete"] or gaps["draft_only"]
	)
	evidence_rate = _rate(evidence_count, eligible_count)
	if total_required == 0:
		readiness = Readiness.NOT_READY
		insufficient = True
	elif eligible_count == 0:
		readiness = Readiness.NOT_READY
		insufficient = True
	elif (
		overall_score >= READY_MIN_SCORE
		and not blocking
		and evidence_rate >= READY_MIN_EVIDENCE_RATE
	):
		readiness = Readiness.READY_FOR_REPORTING
		insufficient = False
	elif overall_score >= REVIEW_MIN_SCORE:
		readiness = Readiness.NEEDS_REVIEW
		insufficient = False
	else:
		readiness = Readiness.NOT_READY
		insufficient = False

	summary = _build_summary(
		company.name,
		year,
		overall_score,
		readiness,
		total_required,
		covered_count,
		eligible_count,
		verified_count,
		evidence_count,
		gaps,
		insufficient,
	)

	return AssessmentResult(
		overall_score=overall_score,
		readiness=readiness,
		period_year=year,
		environmental_score=pillar_scores[ESGPillar.ENVIRONMENTAL],
		social_score=pillar_scores[ESGPillar.SOCIAL],
		governance_score=pillar_scores[ESGPillar.GOVERNANCE],
		summary=summary,
		breakdown=breakdown,
		gaps={kind: [gap.as_dict() for gap in items] for kind, items in gaps.items()},
		total_required=total_required,
		covered=covered_count,
		eligible=eligible_count,
		verified=verified_count,
		with_evidence=evidence_count,
		insufficient_data=insufficient,
	)


def _best_value(values):
	"""Prefer a verified value, then the newest one."""
	if not values:
		return None
	return sorted(
		values,
		key=lambda value: (
			value.status == IndicatorStatus.VERIFIED,
			value.updated_at or value.created_at,
		),
		reverse=True,
	)[0]


def _gap(entry, indicator, kind, detail):
	return Gap(
		kind=kind,
		code=entry.code,
		name=entry.name,
		pillar=entry.pillar,
		pillar_label=entry.get_pillar_display(),
		indicator_id=indicator.pk if indicator else None,
		detail=detail,
	)


def _build_summary(
	company_name,
	year,
	score,
	readiness,
	total_required,
	covered,
	eligible,
	verified,
	evidence,
	gaps,
	insufficient,
):
	label = Readiness(readiness).label
	base = (
		f"{company_name}: ESG data completeness {score}/100 for {year} "
		f"— {label}. This is a data-completeness score, not an ESG performance rating."
	)
	if insufficient and total_required == 0:
		return base + " No required indicators are configured yet."
	if covered == 0:
		return base + " None of the required indicators are tracked yet."
	base += (
		f" Coverage: {covered}/{total_required} tracked, {eligible} reported for {year}, "
		f"{verified} verified, {evidence} with evidence."
	)
	notable = [
		f"{len(gaps['missing'])} not tracked",
		f"{len(gaps['incomplete'])} not reported for the period",
		f"{len(gaps['unverified'])} unverified",
		f"{len(gaps['no_evidence'])} without evidence",
	]
	return base + " Gaps: " + ", ".join(notable) + "."


def run_assessment(company, *, actor=None, period_year=None):
	"""Run an assessment and persist it as an immutable snapshot.

	``delta_score`` records the change versus the previous snapshot for the same
	reporting period so progress over time is visible.
	"""
	result = assess_company(company, period_year=period_year)
	previous = (
		AssessmentSnapshot.objects.filter(company=company, period_year=result.period_year)
		.latest_first()
		.first()
	)
	delta = None
	if previous is not None:
		delta = _q(result.overall_score - previous.overall_score)
	return AssessmentSnapshot.objects.create(
		company=company,
		period_year=result.period_year,
		overall_score=result.overall_score,
		readiness=result.readiness,
		environmental_score=result.environmental_score,
		social_score=result.social_score,
		governance_score=result.governance_score,
		summary=result.summary,
		breakdown_json=result.breakdown,
		gaps_json=result.gaps,
		delta_score=delta,
		created_by=actor if getattr(actor, "is_authenticated", False) else None,
	)


def latest_assessment(company):
	if company is None:
		return None
	return AssessmentSnapshot.objects.filter(company=company).latest_first().first()


def previous_assessment(snapshot):
	"""The snapshot that precedes ``snapshot`` for the same company and period."""
	if snapshot is None:
		return None
	return (
		AssessmentSnapshot.objects.filter(
			company_id=snapshot.company_id,
			period_year=snapshot.period_year,
		)
		.exclude(pk=snapshot.pk)
		.filter(created_at__lt=snapshot.created_at)
		.latest_first()
		.first()
	)


def gaps_count(gaps):
	"""Total number of gaps across every kind."""
	return sum(len(items) for items in (gaps or {}).values())


__all__ = [
	"AssessmentResult",
	"Gap",
	"GAP_KINDS",
	"SCORING_WEIGHTS",
	"TOTAL_WEIGHT",
	"assess_company",
	"available_period_years",
	"gaps_count",
	"latest_assessment",
	"previous_assessment",
	"run_assessment",
	"target_period_year",
]
