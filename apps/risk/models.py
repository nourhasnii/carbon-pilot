"""ESG / carbon risk assessments.

A :class:`RiskAssessment` is an immutable record of the deterministic risk
score a company had at a point in time. It is produced by
:mod:`apps.risk.services.risk_assessment` from real CarbonPilot data only —
open anomalies, objective achievement predictions, data quality, missing
evidence and the review backlog. Every row belongs to exactly one company and
is never shared across tenants.
"""

from django.conf import settings
from django.db import models

from apps.enterprise.models import Company


class RiskLevel(models.TextChoices):
	LOW = "LOW", "Low"
	MEDIUM = "MEDIUM", "Medium"
	HIGH = "HIGH", "High"


class RiskMethod(models.TextChoices):
	RULE_BASED = "RULE_BASED", "Rule-based"


class RiskAssessmentQuerySet(models.QuerySet):
	def for_company(self, company):
		return self.filter(company=company)

	def latest_first(self):
		return self.order_by("-created_at", "-id")


class RiskAssessment(models.Model):
	"""One rule-based ESG / carbon risk score for a company.

	The score is a deterministic sum of five documented, capped components
	(see ``services.risk_assessment``). ``drivers_json`` stores the ranked
	explanation of *why* the company scored what it scored, built from the
	actual counts and values of the tenant's own data.
	"""

	class Meta:
		ordering = ("-created_at", "-id")
		constraints = [
			models.CheckConstraint(
				condition=models.Q(score__gte=0) & models.Q(score__lte=100),
				name="risk_assessment_score_0_100",
			),
		]
		indexes = [
			models.Index(fields=("company", "-created_at")),
			models.Index(fields=("company", "level")),
		]

	company = models.ForeignKey(
		Company, on_delete=models.CASCADE, related_name="risk_assessments"
	)
	score = models.DecimalField(
		max_digits=5,
		decimal_places=2,
		help_text="Deterministic risk score from 0 (no risk) to 100 (highest risk).",
	)
	level = models.CharField(max_length=8, choices=RiskLevel.choices)
	summary = models.TextField(
		blank=True, help_text="Plain-language explanation of the score and its drivers."
	)
	drivers_json = models.JSONField(
		default=list,
		blank=True,
		help_text="Ranked risk drivers, most impactful first, with points and detail.",
	)
	period_year = models.PositiveSmallIntegerField(
		null=True, blank=True, help_text="Reporting year assessed; blank means all years."
	)
	method = models.CharField(
		max_length=32, choices=RiskMethod.choices, default=RiskMethod.RULE_BASED
	)
	created_by = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		null=True,
		blank=True,
		on_delete=models.SET_NULL,
		related_name="risk_assessments_created",
	)
	created_at = models.DateTimeField(auto_now_add=True)

	objects = RiskAssessmentQuerySet.as_manager()

	def __str__(self):
		return f"{self.get_level_display()} risk · {self.score}/100 · company {self.company_id}"

	@property
	def is_high(self):
		return self.level == RiskLevel.HIGH

	@property
	def period_label(self):
		return str(self.period_year) if self.period_year else "All years"

	@property
	def drivers(self):
		"""Ranked risk drivers stored with the assessment (most impactful first)."""
		return list(self.drivers_json or [])

	@property
	def breakdown(self):
		"""Full scoring breakdown: every component with its points and weight.

		Components that contributed nothing are still shown, with 0 points, so
		the scoring model is completely transparent in the UI.
		"""
		from .services.risk_assessment import SCORING_WEIGHTS

		points_by_key = {
			driver.get("key"): driver.get("points", 0.0) for driver in self.drivers
		}
		return [
			{**item, "points": points_by_key.get(item["key"], 0.0)}
			for item in SCORING_WEIGHTS
		]
