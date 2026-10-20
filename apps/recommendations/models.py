"""AI recommendations produced by the local Qwen model (via Ollama).

A ``RecommendationLog`` is a decision the local LLM made from CarbonPilot's own
evidence: which corrective or preventive action to propose, why, and at which
priority. CarbonPilot never preselects the actions — Qwen chooses them from the
supplied factual context. When Ollama is unavailable the deterministic
rule-based fallback may still produce recommendations, but those rows are marked
``RULE_BASED`` so an AI recommendation is never confused with a rule.

Every row belongs to exactly one company through ``company`` and can never
reference an anomaly, objective or department from another tenant.
"""

import hashlib
import re

from django.core.exceptions import ValidationError
from django.db import models

from apps.enterprise.models import Company, Department


class RecommendationPriority(models.TextChoices):
	LOW = "LOW", "Low"
	MEDIUM = "MEDIUM", "Medium"
	HIGH = "HIGH", "High"


class RecommendationStatus(models.TextChoices):
	NEW = "NEW", "New"
	IN_PROGRESS = "IN_PROGRESS", "In progress"
	DONE = "DONE", "Done"
	DISMISSED = "DISMISSED", "Dismissed"


class RecommendationSource(models.TextChoices):
	ANOMALY = "ANOMALY", "Anomaly"
	OBJECTIVE = "OBJECTIVE", "Objective"
	MIXED = "MIXED", "Mixed"
	DATA_QUALITY = "DATA_QUALITY", "Data quality"


class GenerationMethod(models.TextChoices):
	QWEN = "QWEN", "Qwen (local LLM)"
	RULE_BASED = "RULE_BASED", "Rule-based fallback"


# Statuses that still count as "open" for duplicate prevention.
OPEN_STATUSES = (RecommendationStatus.NEW, RecommendationStatus.IN_PROGRESS)

# Sources the local model is allowed to decide. DATA_QUALITY recommendations are
# produced only by CarbonPilot's deterministic rules, never by the LLM.
AI_SOURCES = (
	RecommendationSource.ANOMALY,
	RecommendationSource.OBJECTIVE,
	RecommendationSource.MIXED,
)

# Expected impact is only ever set from a deterministic model. None exists yet,
# so every recommendation honestly reports that it is to be estimated rather
# than inventing a saving or a percentage.
IMPACT_TO_BE_ESTIMATED = "To be estimated"


def normalized_title(value):
	"""Collapse a title to a stable, comparable form for fingerprinting."""
	return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


class RecommendationLogQuerySet(models.QuerySet):
	def for_company(self, company):
		return self.filter(company=company)

	def open(self):
		return self.filter(status__in=OPEN_STATUSES)


class RecommendationLog(models.Model):
	"""One recommendation, either chosen by Qwen or by the rule-based fallback."""

	class Meta:
		ordering = ("-created_at", "-id")
		constraints = [
			# One *open* recommendation per issue+action. Closed rows leave the
			# partial index, so a materially new issue can raise it again.
			models.UniqueConstraint(
				fields=("fingerprint",),
				condition=models.Q(status__in=OPEN_STATUSES),
				name="uniq_open_recommendation_fingerprint",
			),
		]
		indexes = [
			models.Index(fields=("company", "status")),
			models.Index(fields=("company", "priority")),
			models.Index(fields=("company", "created_at")),
		]

	company = models.ForeignKey(
		Company, on_delete=models.CASCADE, related_name="recommendations"
	)
	department = models.ForeignKey(
		Department,
		null=True,
		blank=True,
		on_delete=models.SET_NULL,
		related_name="recommendations",
	)
	objective = models.ForeignKey(
		"objectives.Objective",
		null=True,
		blank=True,
		on_delete=models.SET_NULL,
		related_name="recommendations",
	)
	anomaly = models.ForeignKey(
		"carbon.AnomalyLog",
		null=True,
		blank=True,
		on_delete=models.SET_NULL,
		related_name="recommendations",
	)
	title = models.CharField(max_length=255)
	# Short summary shown on the listing cards.
	message = models.TextField()
	# ------------------------------------------------------------------ #
	# Structured, evidence-based content (rendered in the Details view).
	# ``problem`` / ``analysis`` / ``recommended_action`` come from Qwen for
	# QWEN rows and from the deterministic rules for RULE_BASED rows.
	# ``evidence_items`` is ALWAYS built server-side from real rows (never by
	# the model) so an invented figure can never reach the screen.
	# ------------------------------------------------------------------ #
	problem = models.TextField(blank=True, default="")  # PROBLEM
	evidence_items = models.JSONField(default=list, blank=True)  # EVIDENCE
	analysis = models.TextField(blank=True, default="")  # ANALYSIS
	recommended_action = models.TextField(blank=True, default="")  # RECOMMENDATION
	expected_impact = models.TextField(blank=True, default="")  # EXPECTED IMPACT
	action_plan = models.JSONField(default=list, blank=True)  # ACTION PLAN (steps)
	priority = models.CharField(
		max_length=8, choices=RecommendationPriority.choices
	)
	status = models.CharField(
		max_length=12,
		choices=RecommendationStatus.choices,
		default=RecommendationStatus.NEW,
	)
	source = models.CharField(max_length=12, choices=RecommendationSource.choices)
	# How the recommendation was produced. The model name is the Ollama model
	# for QWEN runs, or empty for the deterministic fallback.
	generation_method = models.CharField(
		max_length=12,
		choices=GenerationMethod.choices,
		default=GenerationMethod.QWEN,
	)
	model_name = models.CharField(max_length=64, blank=True)
	# The entity IDs the recommendation is grounded in, used for de-duplication.
	evidence = models.JSONField(default=list, blank=True)
	# Deterministic identity of the issue+action, used to block duplicates.
	fingerprint = models.CharField(max_length=64)
	created_at = models.DateTimeField(auto_now_add=True)
	updated_at = models.DateTimeField(auto_now=True)

	objects = RecommendationLogQuerySet.as_manager()

	def __str__(self):
		return f"{self.get_priority_display()} · {self.title}"

	# ------------------------------------------------------------------ #
	# Helpers
	# ------------------------------------------------------------------ #
	@staticmethod
	def build_fingerprint(*, company_id, source, department_id, objective_id, anomaly_id, title):
		raw = "|".join([
			str(company_id),
			str(source or ""),
			str(department_id or ""),
			str(objective_id or ""),
			str(anomaly_id or ""),
			normalized_title(title),
		])
		return hashlib.sha1(raw.encode("utf-8")).hexdigest()

	@property
	def is_open(self):
		return self.status in OPEN_STATUSES

	@property
	def is_qwen(self):
		return self.generation_method == GenerationMethod.QWEN

	@property
	def has_structured_content(self):
		"""Whether the Details view has more to show than the short summary."""
		return bool(
			self.problem
			or self.analysis
			or self.recommended_action
			or self.evidence_items
			or self.action_plan
		)

	@property
	def confidence_label(self):
		"""Confidence wording.

		The local LLM is not calibrated, so an AI row reports that it is
		AI-generated instead of a fabricated percentage; a rule-based row has
		no probabilistic confidence at all.
		"""
		if self.is_qwen:
			return "AI-generated (no calibrated probability)"
		return "Not applicable — deterministic rule"

	def clean(self):
		"""Enforce that no related object belongs to another company."""
		errors = {}
		if self.department_id and self.department.company_id != self.company_id:
			errors["department"] = "The department must belong to the recommendation's company."
		if self.objective_id and self.objective.company_id != self.company_id:
			errors["objective"] = "The objective must belong to the recommendation's company."
		if self.anomaly_id and self.anomaly.company_id != self.company_id:
			errors["anomaly"] = "The anomaly must belong to the recommendation's company."
		if errors:
			raise ValidationError(errors)
