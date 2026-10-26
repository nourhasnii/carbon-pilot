import os
import uuid
from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone

from apps.enterprise.models import Company, Department


class ActivityCategory(models.TextChoices):
	ELECTRICITY = "ELECTRICITY", "Electricity"
	NATURAL_GAS = "NATURAL_GAS", "Natural Gas"
	FUEL = "FUEL", "Fuel"
	BUSINESS_TRAVEL = "BUSINESS_TRAVEL", "Business Travel"
	TRANSPORTATION = "TRANSPORTATION", "Transportation"
	PURCHASED_GOODS = "PURCHASED_GOODS", "Purchased Goods"
	WASTE = "WASTE", "Waste"
	WATER = "WATER", "Water"
	OTHER = "OTHER", "Other"


class Scope(models.TextChoices):
	SCOPE_1 = "SCOPE_1", "Scope 1"
	SCOPE_2 = "SCOPE_2", "Scope 2"
	SCOPE_3 = "SCOPE_3", "Scope 3"


class ActivityStatus(models.TextChoices):
	DRAFT = "DRAFT", "Draft"
	SUBMITTED = "SUBMITTED", "Submitted"
	UNDER_REVIEW = "UNDER_REVIEW", "Under review"
	APPROVED = "APPROVED", "Approved"
	REJECTED = "REJECTED", "Rejected"


class VerificationStatus(models.TextChoices):
	VERIFIED = "VERIFIED", "Verified"
	UNVERIFIED = "UNVERIFIED", "Unverified"
	DEMO = "DEMO", "Demo example"


class ActivityAuditAction(models.TextChoices):
	CREATED = "CREATED", "Created"
	SUBMITTED = "SUBMITTED", "Submitted"
	REVIEW_STARTED = "REVIEW_STARTED", "Review started"
	APPROVED = "APPROVED", "Approved"
	REJECTED = "REJECTED", "Rejected"
	CORRECTION_REQUESTED = "CORRECTION_REQUESTED", "Correction requested"
	UPDATED = "UPDATED", "Updated"
	ARCHIVED = "ARCHIVED", "Archived"
	RESTORED = "RESTORED", "Restored"


class DataQualityBand(models.TextChoices):
	HIGH = "HIGH", "High confidence"
	MEDIUM = "MEDIUM", "Medium confidence"
	LOW = "LOW", "Low confidence"


class AnomalyType(models.TextChoices):
	OUTLIER = "OUTLIER", "Outlier"
	SPIKE = "SPIKE", "Spike"
	TREND_INCREASE = "TREND_INCREASE", "Trend increase"


class AnomalySeverity(models.TextChoices):
	LOW = "LOW", "Low"
	MEDIUM = "MEDIUM", "Medium"
	HIGH = "HIGH", "High"


class AnomalyStatus(models.TextChoices):
	NEW = "NEW", "New"
	REVIEWED = "REVIEWED", "Reviewed"
	DISMISSED = "DISMISSED", "Dismissed"


# CO2e results are always stored in kilograms for a single, comparable unit.
CO2E_DECIMAL_PLACES = Decimal("0.0001")


def activity_evidence_upload_to(instance, filename):
	"""Store evidence per company so tenants never share a folder."""
	company_id = instance.company_id or "unassigned"
	year = instance.period_start.year if instance.period_start else timezone.localdate().year
	extension = os.path.splitext(filename)[1].lower()
	return f"carbon/evidence/company_{company_id}/{year}/{uuid.uuid4().hex}{extension}"


class EmissionFactorQuerySet(models.QuerySet):
	def active(self):
		return self.filter(is_active=True)

	def verified(self):
		return self.filter(verification_status=VerificationStatus.VERIFIED)


class EmissionFactor(models.Model):
	"""A reusable library entry used to convert an activity quantity into CO2e."""

	class Meta:
		ordering = ("category", "region", "name", "-year")
		constraints = [
			models.UniqueConstraint(
				fields=("name", "category", "region", "year"),
				condition=models.Q(company__isnull=True),
				name="uniq_em_factor_global_identity",
			),
			models.UniqueConstraint(
				fields=("company", "name", "category", "region", "year"),
				condition=models.Q(company__isnull=False),
				name="uniq_em_factor_company_identity",
			),
			models.CheckConstraint(
				condition=models.Q(value__gt=0),
				name="emission_factor_value_positive",
			),
		]
		indexes = [
			models.Index(fields=("category", "is_active")),
			models.Index(fields=("region",)),
		]

	name = models.CharField(max_length=255)
	category = models.CharField(max_length=32, choices=ActivityCategory.choices)
	scope = models.CharField(max_length=16, choices=Scope.choices)
	value = models.DecimalField(
		max_digits=18,
		decimal_places=6,
		validators=[MinValueValidator(Decimal("0.000001"))],
		help_text="Kilograms of CO2e emitted per one denominator unit.",
	)
	unit = models.CharField(
		max_length=32,
		help_text="Denominator unit the value applies to, e.g. kWh, L, m3, km.",
	)
	region = models.CharField(max_length=120, default="Global")
	year = models.PositiveSmallIntegerField(
		validators=[MinValueValidator(1900), MaxValueValidator(2100)]
	)
	source = models.CharField(max_length=255)
	methodology = models.TextField(blank=True)
	company = models.ForeignKey(
		Company,
		null=True,
		blank=True,
		on_delete=models.CASCADE,
		related_name="emission_factors",
	)
	created_by = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		null=True,
		blank=True,
		on_delete=models.SET_NULL,
		related_name="emission_factors_created",
	)
	verification_status = models.CharField(
		max_length=16,
		choices=VerificationStatus.choices,
		default=VerificationStatus.UNVERIFIED,
	)
	is_active = models.BooleanField(default=True)
	created_at = models.DateTimeField(auto_now_add=True)
	updated_at = models.DateTimeField(auto_now=True)

	objects = EmissionFactorQuerySet.as_manager()

	def __str__(self):
		return f"{self.name} ({self.value} kg CO2e / {self.unit}, {self.region} {self.year})"

	@property
	def unit_label(self):
		return f"kg CO2e / {self.unit}"

	@property
	def is_verified(self):
		return self.verification_status == VerificationStatus.VERIFIED

	@property
	def status_label(self):
		return "Active" if self.is_active else "Inactive"

	@property
	def is_shared(self):
		"""A shared factor is part of the global library (no owning company)."""
		return self.company_id is None


class EmissionFactorCompanyState(models.Model):
	"""Tenant-scoped override state for a shared (global) emission factor.

	A company may customise a shared factor — its activation and its values —
	for its own reporting without touching the shared record or affecting any
	other tenant. A tenant can only
	further restrict a shared factor; it can never force-enable one the platform
	has globally disabled.
	"""

	class Meta:
		constraints = [
			models.UniqueConstraint(
				fields=("company", "factor"),
				name="uniq_em_factor_company_state",
			),
		]
		indexes = [models.Index(fields=("company", "is_active"))]

	company = models.ForeignKey(
		Company, on_delete=models.CASCADE, related_name="emission_factor_states"
	)
	factor = models.ForeignKey(
		EmissionFactor, on_delete=models.CASCADE, related_name="company_states"
	)
	is_active = models.BooleanField(default=True)
	# Optional company-specific values. Blank/None means "use the shared value".
	# ``value`` is the anchor: when it is set the tenant has a value override.
	value = models.DecimalField(max_digits=18, decimal_places=6, null=True, blank=True)
	unit = models.CharField(max_length=32, blank=True)
	region = models.CharField(max_length=120, blank=True)
	year = models.PositiveSmallIntegerField(null=True, blank=True)
	source = models.CharField(max_length=255, blank=True)
	methodology = models.TextField(blank=True)
	updated_by = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		null=True,
		blank=True,
		on_delete=models.SET_NULL,
		related_name="emission_factor_states_updated",
	)
	updated_at = models.DateTimeField(auto_now=True)

	def __str__(self):
		return f"{self.factor_id} for company {self.company_id}: {self.status_label}"

	@property
	def status_label(self):
		return "Active" if self.is_active else "Inactive"

	@property
	def has_value_override(self):
		"""True when the company has replaced the shared factor's values."""
		return self.value is not None


class CarbonActivityQuerySet(models.QuerySet):
	def for_company(self, company):
		return self.filter(company=company)

	def approved(self):
		"""Only approved activities feed official carbon footprint KPIs."""
		return self.filter(status=ActivityStatus.APPROVED)

	def pending_review(self):
		return self.filter(status=ActivityStatus.SUBMITTED)

	def for_year(self, year):
		return self.filter(period_start__year=year)

	def unarchived(self):
		"""Activities still part of the live inventory (default working set)."""
		return self.filter(is_archived=False)

	def archived(self):
		return self.filter(is_archived=True)


class CarbonActivity(models.Model):
	"""A single metered or estimated emission source reported by a company user."""

	class Meta:
		ordering = ("-period_start", "-created_at")
		constraints = [
			models.CheckConstraint(
				condition=models.Q(quantity__gte=0),
				name="carbon_activity_quantity_non_negative",
			),
			models.CheckConstraint(
				condition=models.Q(period_end__gte=models.F("period_start"))
				| models.Q(period_end__isnull=True),
				name="carbon_activity_period_ordered",
			),
		]
		indexes = [
			models.Index(fields=("company", "status")),
			models.Index(fields=("company", "scope")),
			models.Index(fields=("company", "category")),
			models.Index(fields=("department",)),
			models.Index(fields=("supplier",)),
			models.Index(fields=("period_start",)),
		]

	company = models.ForeignKey(
		Company,
		on_delete=models.CASCADE,
		related_name="carbon_activities",
	)
	department = models.ForeignKey(
		Department,
		null=True,
		blank=True,
		on_delete=models.SET_NULL,
		related_name="carbon_activities",
	)
	# Optional link to a supplier, typically for Scope 3 purchased goods and
	# services. The supplier must belong to the same company as the activity.
	supplier = models.ForeignKey(
		"suppliers.Supplier",
		null=True,
		blank=True,
		on_delete=models.SET_NULL,
		related_name="activities",
	)
	name = models.CharField(max_length=255, blank=True)
	category = models.CharField(max_length=32, choices=ActivityCategory.choices)
	scope = models.CharField(max_length=16, choices=Scope.choices)
	quantity = models.DecimalField(
		max_digits=18,
		decimal_places=4,
		validators=[MinValueValidator(Decimal("0"))],
	)
	unit = models.CharField(max_length=32)
	period_start = models.DateField()
	period_end = models.DateField(null=True, blank=True)

	emission_factor = models.ForeignKey(
		EmissionFactor,
		null=True,
		blank=True,
		on_delete=models.PROTECT,
		related_name="activities",
	)
	# Immutable snapshot of the factor applied, for traceability over time.
	emission_factor_value = models.DecimalField(
		max_digits=18, decimal_places=6, null=True, blank=True, editable=False
	)
	emission_factor_unit = models.CharField(max_length=32, blank=True, editable=False)
	emission_factor_name = models.CharField(max_length=255, blank=True, editable=False)
	emission_factor_source = models.CharField(max_length=255, blank=True, editable=False)
	emission_factor_region = models.CharField(max_length=120, blank=True, editable=False)
	emission_factor_year = models.PositiveSmallIntegerField(null=True, blank=True, editable=False)
	emission_factor_scope = models.CharField(max_length=16, blank=True, editable=False)
	# Calculated server-side only. Never editable by users.
	co2e_kg = models.DecimalField(
		max_digits=18, decimal_places=4, null=True, blank=True, editable=False
	)

	data_source = models.CharField(
		max_length=255,
		blank=True,
		help_text="Where the raw data comes from (invoice, meter, supplier report...).",
	)
	evidence = models.FileField(
		upload_to=activity_evidence_upload_to,
		blank=True,
		help_text="Supporting document: invoice, receipt, meter reading, supplier document.",
	)

	status = models.CharField(
		max_length=16, choices=ActivityStatus.choices, default=ActivityStatus.DRAFT
	)
	review_comment = models.TextField(
		blank=True,
		help_text="Latest reviewer/approver comment shown to the activity owner.",
	)
	created_by = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		null=True,
		blank=True,
		on_delete=models.SET_NULL,
		related_name="carbon_activities_created",
	)
	reviewed_by = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		null=True,
		blank=True,
		on_delete=models.SET_NULL,
		related_name="carbon_activities_reviewed",
	)
	approved_by = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		null=True,
		blank=True,
		on_delete=models.SET_NULL,
		related_name="carbon_activities_approved",
	)

	submitted_at = models.DateTimeField(null=True, blank=True)
	reviewed_at = models.DateTimeField(null=True, blank=True)
	approved_at = models.DateTimeField(null=True, blank=True)

	# Soft-delete: non-draft activities are archived instead of deleted so the
	# audit trail and the historical footprint stay intact.
	is_archived = models.BooleanField(default=False)
	archived_at = models.DateTimeField(null=True, blank=True)
	archived_by = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		null=True,
		blank=True,
		on_delete=models.SET_NULL,
		related_name="carbon_activities_archived",
	)

	created_at = models.DateTimeField(auto_now_add=True)
	updated_at = models.DateTimeField(auto_now=True)

	objects = CarbonActivityQuerySet.as_manager()

	def __str__(self):
		return self.display_name

	@property
	def display_name(self):
		if self.name:
			return self.name
		return f"{self.get_category_display()} · {self.period_label}"

	@property
	def period_label(self):
		if self.period_end and self.period_end != self.period_start:
			return f"{self.period_start:%b %Y} – {self.period_end:%b %Y}"
		return f"{self.period_start:%b %Y}"

	@property
	def reporting_year(self):
		return self.period_start.year

	@property
	def co2e_tonnes(self):
		if self.co2e_kg is None:
			return None
		return (self.co2e_kg / Decimal("1000")).quantize(
			Decimal("0.0001"), rounding=ROUND_HALF_UP
		)

	@property
	def is_editable(self):
		return self.status in (ActivityStatus.DRAFT, ActivityStatus.REJECTED)

	@property
	def is_deletable(self):
		"""Only a draft that never entered the workflow may be hard-deleted."""
		return self.status == ActivityStatus.DRAFT and not self.is_archived

	def clean(self):
		errors = {}
		if self.period_end and self.period_end < self.period_start:
			errors["period_end"] = "End date must be on or after start date."
		if self.department_id and self.company_id and self.department.company_id != self.company_id:
			errors["department"] = "The department must belong to the activity company."
		if self.supplier_id and self.company_id and self.supplier.company_id != self.company_id:
			errors["supplier"] = "The supplier must belong to the activity company."
		if (
			self.emission_factor_id
			and self.emission_factor.company_id
			and self.emission_factor.company_id != self.company_id
		):
			errors["emission_factor"] = "The emission factor must be shared or belong to the activity company."
		if (
			self.emission_factor_id
			and self.status != ActivityStatus.APPROVED
			and self.emission_factor.scope != self.scope
		):
			errors["scope"] = "The selected emission factor does not match the activity scope."
		if (
			self.emission_factor_id
			and self.status != ActivityStatus.APPROVED
			and self.emission_factor.category != self.category
		):
			errors["emission_factor"] = "The selected emission factor does not match the activity category."
		if self.emission_factor_id and not self.emission_factor.is_active:
			previous_factor_id = None
			previous_quantity = None
			previous_unit = None
			previous_was_approved = False
			if self.pk:
				previous = type(self).objects.filter(pk=self.pk).values(
					"emission_factor_id", "quantity", "unit", "status"
				).first()
				if previous:
					previous_factor_id = previous["emission_factor_id"]
					previous_quantity = previous["quantity"]
					previous_unit = previous["unit"]
					previous_was_approved = previous["status"] == ActivityStatus.APPROVED
			if not previous_was_approved and (
				previous_factor_id != self.emission_factor_id
				or previous_quantity != self.quantity
				or previous_unit != self.unit
			):
				errors["emission_factor"] = "Inactive factors cannot be used for new calculations."
		if errors:
			raise ValidationError(errors)

	def snapshot_emission_factor(self):
		"""Copy the applied factor value/unit so later edits never rewrite history.

		A company that customised a shared factor uses its own effective values.
		"""
		factor = self.emission_factor
		if factor is None:
			self.emission_factor_value = None
			self.emission_factor_unit = ""
			self.emission_factor_name = ""
			self.emission_factor_source = ""
			self.emission_factor_region = ""
			self.emission_factor_year = None
			self.emission_factor_scope = ""
			return None
		# Imported lazily: ``recommendation`` imports this module at load time.
		from apps.carbon.services.recommendation import effective_factor_attrs

		attrs = effective_factor_attrs(factor, self.company)
		self.emission_factor_value = attrs["value"]
		self.emission_factor_unit = attrs["unit"]
		self.emission_factor_name = factor.name
		self.emission_factor_source = attrs["source"]
		self.emission_factor_region = attrs["region"]
		self.emission_factor_year = attrs["year"]
		self.emission_factor_scope = factor.scope
		return factor

	def recalculate_co2e(self):
		"""CO2e = quantity × emission factor, computed with safe unit conversion."""
		if (
			self.status == ActivityStatus.APPROVED
			and self.emission_factor_value is not None
			and self.emission_factor_unit
		):
			factor_value = self.emission_factor_value
			factor_unit = self.emission_factor_unit
			factor_scope = self.emission_factor_scope or self.scope
		else:
			factor = self.snapshot_emission_factor()
			if factor is None:
				self.co2e_kg = None
				return None
			# Use the snapshot just taken so company overrides are honoured.
			factor_value = self.emission_factor_value
			factor_unit = self.emission_factor_unit
			factor_scope = factor.scope
		if self.quantity is None:
			self.co2e_kg = None
			return None
		if self.scope != factor_scope:
			raise ValidationError(
				"The emission factor scope must match the activity scope before calculating CO2e."
			)
		self.co2e_kg = calculate_co2e_kg(
			quantity=self.quantity,
			activity_unit=self.unit,
			factor_value=factor_value,
			factor_unit=factor_unit,
		)
		return self.co2e_kg

	def save(self, *args, **kwargs):
		if self.pk:
			previous = type(self).objects.filter(pk=self.pk).first()
			if previous and previous.status == ActivityStatus.APPROVED:
				self.emission_factor_id = previous.emission_factor_id
				for field in (
					"emission_factor_value", "emission_factor_unit", "emission_factor_name",
					"emission_factor_source", "emission_factor_region", "emission_factor_year",
					"emission_factor_scope",
				):
					setattr(self, field, getattr(previous, field))
		self.full_clean(exclude=["emission_factor_value", "emission_factor_unit", "co2e_kg"])
		self.recalculate_co2e()
		super().save(*args, **kwargs)

	def log_event(self, *, actor, action, comment="", from_status="", to_status=""):
		"""Append an immutable entry to the activity audit trail."""
		return ActivityAuditLog.objects.create(
			activity=self,
			actor=actor if getattr(actor, "is_authenticated", False) else None,
			action=action,
			from_status=from_status or "",
			to_status=to_status or "",
			comment=(comment or "").strip(),
		)


class ActivityAuditLog(models.Model):
	"""Immutable, append-only trail of everything that happened to an activity."""

	class Meta:
		ordering = ("created_at", "id")
		verbose_name = "Activity audit entry"
		verbose_name_plural = "Activity audit log"
		indexes = [models.Index(fields=("activity", "created_at"))]

	activity = models.ForeignKey(
		CarbonActivity, on_delete=models.CASCADE, related_name="audit_log"
	)
	actor = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		null=True,
		blank=True,
		on_delete=models.SET_NULL,
		related_name="carbon_audit_entries",
	)
	action = models.CharField(max_length=32, choices=ActivityAuditAction.choices)
	from_status = models.CharField(max_length=16, blank=True)
	to_status = models.CharField(max_length=16, blank=True)
	comment = models.TextField(blank=True)
	created_at = models.DateTimeField(auto_now_add=True)

	def __str__(self):
		return f"{self.get_action_display()} · {self.activity_id}"

	def save(self, *args, **kwargs):
		if self.pk:
			raise ValidationError("Audit entries are append-only and cannot be modified.")
		super().save(*args, **kwargs)


class AnomalyLogQuerySet(models.QuerySet):
	def for_company(self, company):
		return self.filter(company=company)

	def of_severity(self, severity):
		return self.filter(severity=severity)

	def new_only(self):
		return self.filter(status=AnomalyStatus.NEW)


class AnomalyLog(models.Model):
	"""A machine-learning detected unusual emission pattern.

	Entries are produced by ``services.anomaly_detection`` (a scikit-learn
	IsolationForest) from *approved*, non-archived activities only. Every row
	belongs to exactly one company and is never shared across tenants.

	``fingerprint`` is a deterministic identity (company + activity + period)
	used to prevent duplicate logs when detection is run repeatedly.
	"""

	class Meta:
		ordering = ("-detected_at", "-id")
		constraints = [
			models.UniqueConstraint(
				fields=("company", "activity", "period_start"),
				condition=models.Q(activity__isnull=False),
				name="uniq_anomaly_company_activity_period",
			),
		]
		indexes = [
			models.Index(fields=("company", "severity")),
			models.Index(fields=("company", "status")),
			models.Index(fields=("company", "detected_at")),
		]

	company = models.ForeignKey(
		Company, on_delete=models.CASCADE, related_name="anomaly_logs"
	)
	department = models.ForeignKey(
		Department,
		null=True,
		blank=True,
		on_delete=models.SET_NULL,
		related_name="anomaly_logs",
	)
	activity = models.ForeignKey(
		CarbonActivity,
		null=True,
		blank=True,
		on_delete=models.SET_NULL,
		related_name="anomaly_logs",
	)
	anomaly_type = models.CharField(max_length=32, choices=AnomalyType.choices)
	score = models.DecimalField(
		max_digits=8,
		decimal_places=5,
		help_text="Normalised anomaly score in [0, 1]; higher is more unusual.",
	)
	severity = models.CharField(max_length=8, choices=AnomalySeverity.choices)
	message = models.TextField(
		blank=True, help_text="Plain-language explanation of what the model observed."
	)
	period_start = models.DateField(null=True, blank=True)
	period_end = models.DateField(null=True, blank=True)
	status = models.CharField(
		max_length=12, choices=AnomalyStatus.choices, default=AnomalyStatus.NEW
	)
	detected_at = models.DateTimeField(default=timezone.now)
	detected_by = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		null=True,
		blank=True,
		on_delete=models.SET_NULL,
		related_name="anomaly_logs_detected",
	)
	# Deterministic identity used to avoid duplicating the same finding.
	fingerprint = models.CharField(max_length=255, unique=True)
	created_at = models.DateTimeField(auto_now_add=True)
	updated_at = models.DateTimeField(auto_now=True)

	objects = AnomalyLogQuerySet.as_manager()

	def __str__(self):
		return (
			f"{self.get_severity_display()} {self.get_anomaly_type_display()} "
			f"· company {self.company_id}"
		)

	@property
	def is_high(self):
		return self.severity == AnomalySeverity.HIGH

	@property
	def is_new(self):
		return self.status == AnomalyStatus.NEW


def calculate_co2e_kg(*, quantity, activity_unit, factor_value, factor_unit):
	"""Return kilograms of CO2e for a quantity multiplied by an emission factor.

	The activity quantity is converted into the factor's denominator unit when the
	two units describe the same physical dimension (for example MWh → kWh). Unit
	conversion helpers live in ``apps.carbon.services.units`` to keep this import
	dependency-free during model loading.
	"""
	from apps.carbon.services.units import UnitConversionError, convert

	quantity = _to_decimal(quantity)
	factor_value = _to_decimal(factor_value)
	if quantity is None or factor_value is None:
		return None
	if activity_unit and factor_unit:
		try:
			quantity = convert(quantity, activity_unit, factor_unit)
		except UnitConversionError as error:
			raise ValidationError({"unit": str(error)}) from error
	co2e = quantity * factor_value
	return co2e.quantize(CO2E_DECIMAL_PLACES, rounding=ROUND_HALF_UP)


def _to_decimal(value):
	if value is None:
		return None
	if isinstance(value, Decimal):
		return value
	return Decimal(str(value))
