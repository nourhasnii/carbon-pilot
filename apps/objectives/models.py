"""Reduction objectives and targets.

An objective states how much a company — or a single department — intends to
reduce its emissions from a baseline over a reporting period. Its live progress
is always computed from *approved* carbon activities, never entered by hand.
"""

import calendar
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from apps.enterprise.models import Company, Department

ZERO_TONNES = Decimal("0.00")


class PeriodType(models.TextChoices):
	ANNUAL = "ANNUAL", "Annual"
	QUARTERLY = "QUARTERLY", "Quarterly"


class Quarter(models.TextChoices):
	Q1 = "Q1", "Q1 · Jan–Mar"
	Q2 = "Q2", "Q2 · Apr–Jun"
	Q3 = "Q3", "Q3 · Jul–Sep"
	Q4 = "Q4", "Q4 · Oct–Dec"


# First month of each quarter, used to resolve a period's calendar bounds.
_QUARTER_FIRST_MONTH = {
	Quarter.Q1: 1,
	Quarter.Q2: 4,
	Quarter.Q3: 7,
	Quarter.Q4: 10,
}


class ObjectiveStatus(models.TextChoices):
	ACTIVE = "ACTIVE", "Active"
	ACHIEVED = "ACHIEVED", "Achieved"
	MISSED = "MISSED", "Missed"
	ARCHIVED = "ARCHIVED", "Archived"


class ObjectiveQuerySet(models.QuerySet):
	def for_company(self, company):
		return self.filter(company=company)

	def active(self):
		return self.filter(status=ObjectiveStatus.ACTIVE)

	def for_year(self, year):
		return self.filter(year=year)

	def live(self):
		"""Objectives still being tracked (not archived)."""
		return self.exclude(status=ObjectiveStatus.ARCHIVED)


class Objective(models.Model):
	"""A reduction target for a company or one of its departments.

	Emissions are stored in tonnes of CO2e here, matching how targets are set by
	ESG teams. ``current_tco2e`` is derived from approved carbon activities so the
	figure can never drift from the official footprint.
	"""

	class Meta:
		ordering = ("-year", "name")
		constraints = [
			models.CheckConstraint(
				condition=models.Q(baseline_tco2e__gte=0),
				name="objective_baseline_non_negative",
			),
			models.CheckConstraint(
				condition=models.Q(target_tco2e__gte=0),
				name="objective_target_non_negative",
			),
		]
		indexes = [
			models.Index(fields=("company", "status")),
			models.Index(fields=("company", "year")),
			models.Index(fields=("department",)),
		]

	company = models.ForeignKey(
		Company,
		on_delete=models.CASCADE,
		related_name="objectives",
	)
	# Null means a company-wide objective; otherwise it targets one department.
	department = models.ForeignKey(
		Department,
		null=True,
		blank=True,
		on_delete=models.SET_NULL,
		related_name="objectives",
	)
	name = models.CharField(max_length=255)
	year = models.PositiveSmallIntegerField()
	period_type = models.CharField(
		max_length=16, choices=PeriodType.choices, default=PeriodType.ANNUAL
	)
	quarter = models.CharField(max_length=2, choices=Quarter.choices, blank=True, default="")
	baseline_tco2e = models.DecimalField(max_digits=18, decimal_places=2)
	target_tco2e = models.DecimalField(max_digits=18, decimal_places=2)
	status = models.CharField(
		max_length=16, choices=ObjectiveStatus.choices, default=ObjectiveStatus.ACTIVE
	)
	created_by = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		null=True,
		blank=True,
		on_delete=models.SET_NULL,
		related_name="objectives_created",
	)
	created_at = models.DateTimeField(auto_now_add=True)
	updated_at = models.DateTimeField(auto_now=True)

	objects = ObjectiveQuerySet.as_manager()

	def __str__(self):
		return f"{self.name} ({self.scope_label} · {self.period_label})"

	# ------------------------------------------------------------------ #
	# Display helpers
	# ------------------------------------------------------------------ #
	@property
	def scope_label(self):
		return self.department.name if self.department_id else "Company-wide"

	@property
	def period_label(self):
		if self.period_type == PeriodType.QUARTERLY and self.quarter:
			return f"{self.quarter} {self.year}"
		return str(self.year)

	@property
	def is_archived(self):
		return self.status == ObjectiveStatus.ARCHIVED

	def period_bounds(self):
		"""Return the (start, end) calendar dates the objective covers."""
		if self.period_type == PeriodType.QUARTERLY and self.quarter in _QUARTER_FIRST_MONTH:
			first_month = _QUARTER_FIRST_MONTH[self.quarter]
			last_month = first_month + 2
			start = date(self.year, first_month, 1)
			end = date(self.year, last_month, calendar.monthrange(self.year, last_month)[1])
			return start, end
		return date(self.year, 1, 1), date(self.year, 12, 31)

	def is_period_closed(self):
		return self.period_bounds()[1] < timezone.localdate()

	# ------------------------------------------------------------------ #
	# Live progress (approved activities only)
	# ------------------------------------------------------------------ #
	def scoped_activities(self):
		"""Approved, non-archived activities that count towards this objective."""
		from apps.carbon.models import ActivityStatus, CarbonActivity

		start, end = self.period_bounds()
		queryset = CarbonActivity.objects.filter(
			company_id=self.company_id,
			status=ActivityStatus.APPROVED,
			is_archived=False,
			period_start__gte=start,
			period_start__lte=end,
		)
		if self.department_id:
			queryset = queryset.filter(department_id=self.department_id)
		return queryset

	@property
	def current_tco2e(self):
		"""tCO2e already emitted in scope this period, from approved activities."""
		if getattr(self, "_current_tco2e", None) is None:
			from django.db.models import Sum

			from apps.carbon.services.analytics import to_tonnes

			total_kg = self.scoped_activities().aggregate(total=Sum("co2e_kg"))["total"]
			self._current_tco2e = to_tonnes(total_kg)
		return self._current_tco2e

	@property
	def reduction_required_tco2e(self):
		"""How much the objective committed to cut, baseline − target."""
		required = self.baseline_tco2e - self.target_tco2e
		return required if required > 0 else ZERO_TONNES

	@property
	def progress_percent(self):
		"""Share of the committed reduction already achieved (0–100)."""
		required = self.reduction_required_tco2e
		current = self.current_tco2e
		if required <= 0:
			# No reduction committed: met as soon as we are at or below target.
			return 100.0 if current <= self.target_tco2e else 0.0
		achieved = self.baseline_tco2e - current
		percent = float(achieved) / float(required) * 100
		return round(max(0.0, min(percent, 100.0)), 1)

	@property
	def gap_tco2e(self):
		"""Remaining reduction needed to reach the target (never negative)."""
		gap = self.current_tco2e - self.target_tco2e
		return gap.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if gap > 0 else ZERO_TONNES

	@property
	def is_achieved(self):
		return self.current_tco2e <= self.target_tco2e

	def suggested_status(self):
		"""Work out the status from live progress and the reporting period."""
		if self.status == ObjectiveStatus.ARCHIVED:
			return ObjectiveStatus.ARCHIVED
		if self.is_achieved:
			return ObjectiveStatus.ACHIEVED
		if self.is_period_closed():
			return ObjectiveStatus.MISSED
		return ObjectiveStatus.ACTIVE

	def refresh_status(self, save=True):
		"""Persist the status derived from the latest approved emissions."""
		status = self.suggested_status()
		if save and status != self.status:
			self.status = status
			self.save(update_fields=("status", "updated_at"))
		return status

	# ------------------------------------------------------------------ #
	# Validation
	# ------------------------------------------------------------------ #
	def clean(self):
		errors = {}
		if self.department_id and self.company_id and self.department.company_id != self.company_id:
			errors["department"] = "The department must belong to the objective's company."
		if self.period_type == PeriodType.QUARTERLY and not self.quarter:
			errors["quarter"] = "Pick a quarter for a quarterly objective."
		if self.period_type == PeriodType.ANNUAL and self.quarter:
			errors["quarter"] = "An annual objective does not need a quarter."
		if (
			self.baseline_tco2e is not None
			and self.target_tco2e is not None
			and self.target_tco2e > self.baseline_tco2e
		):
			errors["target_tco2e"] = "Target must be lower than baseline for a reduction objective."
		if errors:
			raise ValidationError(errors)


class RiskLevel(models.TextChoices):
	LOW = "LOW", "Low"
	MEDIUM = "MEDIUM", "Medium"
	HIGH = "HIGH", "High"


class ExpectedOutcome(models.TextChoices):
	LIKELY_TO_ACHIEVE = "LIKELY TO ACHIEVE", "Likely to achieve"
	AT_RISK = "AT RISK", "At risk"


class PredictionRun(models.Model):
	"""One machine-learning achievement prediction for an objective.

	A run is produced by :mod:`apps.objectives.services.objective_prediction`,
	which trains a scikit-learn ``RandomForestClassifier`` on the company's
	*resolved* historical objectives. The stored row is an immutable record of
	what the model said at a point in time: the probability (0–100), the
	verdict, and the model identity/version. It always belongs to exactly one
	company through ``objective.company`` — predictions are never shared across
	tenants.
	"""

	class Meta:
		ordering = ("-created_at", "-id")
		indexes = [
			models.Index(fields=("objective", "-created_at")),
		]
		constraints = [
			models.CheckConstraint(
				condition=models.Q(probability_achievement__gte=0)
				& models.Q(probability_achievement__lte=100),
				name="prediction_probability_0_100",
			),
		]

	objective = models.ForeignKey(
		Objective,
		on_delete=models.CASCADE,
		related_name="predictions",
	)
	# Model probability that the objective ends ACHIEVED, expressed 0–100.
	probability_achievement = models.DecimalField(
		max_digits=5,
		decimal_places=2,
		help_text="Classifier probability that the objective is achieved, 0–100.",
	)
	risk_level = models.CharField(max_length=8, choices=RiskLevel.choices)
	expected_outcome = models.CharField(max_length=32, choices=ExpectedOutcome.choices)
	current_progress_percent = models.DecimalField(
		max_digits=5,
		decimal_places=2,
		help_text="Objective progress (0–100) at the moment the model ran.",
	)
	model_name = models.CharField(max_length=64, default="RandomForestClassifier")
	model_version = models.CharField(max_length=32, default="1.0.0")
	created_at = models.DateTimeField(auto_now_add=True)

	def __str__(self):
		return (
			f"{self.objective_id} · {self.probability_achievement}% · "
			f"{self.get_risk_level_display()}"
		)

	@property
	def is_high_risk(self):
		return self.risk_level == RiskLevel.HIGH
