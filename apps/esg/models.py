"""ESG indicator management and data-completeness assessment.

The module is an **ESG intelligence engine**, not a generic CRUD:

* :class:`IndicatorCatalogEntry` is the configurable catalog of ESG indicators
  (a shared platform default plus optional tenant-scoped entries). Entries
  flagged ``is_required`` form the "required-indicator catalog" the
  completeness engine scores against.
* :class:`Indicator` is a company tracking one catalog entry, with an owner, a
  department and an active/inactive lifecycle.
* :class:`IndicatorValue` is the measurement a company reports for one
  reporting period. ``value`` is nullable on purpose: a *missing* measurement
  is not the same thing as a genuine value of **zero**.
* :class:`IndicatorHistory` is an append-only trail so indicator history is
  preserved even when a value or its verification status changes.
* :class:`AssessmentSnapshot` is an immutable, explainable record of the
  deterministic completeness score at a point in time, so users can track how
  their ESG data readiness evolves.

Tenant isolation is strict: every row carries (or derives) its ``company`` and
can never be reached from another tenant.
"""

from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from apps.enterprise.models import Company, Department


class ESGPillar(models.TextChoices):
	ENVIRONMENTAL = "ENVIRONMENTAL", "Environmental"
	SOCIAL = "SOCIAL", "Social"
	GOVERNANCE = "GOVERNANCE", "Governance"


# Pillar order is the canonical E / S / G reporting order.
PILLAR_ORDER = (ESGPillar.ENVIRONMENTAL, ESGPillar.SOCIAL, ESGPillar.GOVERNANCE)


class IndicatorStatus(models.TextChoices):
	"""Lifecycle of a reported measurement (its verification state)."""

	DRAFT = "DRAFT", "Draft"
	REPORTED = "REPORTED", "Reported"
	VERIFIED = "VERIFIED", "Verified"


# A value in one of these states counts as an *eligible* ESG data point.
ELIGIBLE_STATUSES = (IndicatorStatus.REPORTED, IndicatorStatus.VERIFIED)


class Readiness(models.TextChoices):
	"""How ready a company's ESG data is for reporting (never a performance rating)."""

	NOT_READY = "NOT_READY", "Not ready"
	NEEDS_REVIEW = "NEEDS_REVIEW", "Needs review"
	READY_FOR_REPORTING = "READY_FOR_REPORTING", "Ready for reporting"


class HistoryAction(models.TextChoices):
	CREATED = "CREATED", "Indicator created"
	UPDATED = "UPDATED", "Indicator updated"
	VALUE_RECORDED = "VALUE_RECORDED", "Value recorded"
	VALUE_UPDATED = "VALUE_UPDATED", "Value updated"
	REPORTED = "REPORTED", "Marked as reported"
	VERIFIED = "VERIFIED", "Verified"
	REJECTED = "REJECTED", "Returned to draft"
	ARCHIVED = "ARCHIVED", "Deactivated"
	REACTIVATED = "REACTIVATED", "Reactivated"


def _q2(value):
	return Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class IndicatorCatalogEntryQuerySet(models.QuerySet):
	def for_company(self, company):
		"""Shared (global) entries plus the tenant's own entries."""
		if company is None:
			return self.filter(company__isnull=True)
		return self.filter(models.Q(company__isnull=True) | models.Q(company=company))

	def shared(self):
		return self.filter(company__isnull=True)

	def required(self):
		return self.filter(is_required=True, is_active=True)

	def active(self):
		return self.filter(is_active=True)


class IndicatorCatalogEntry(models.Model):
	"""One entry of the configurable ESG indicator catalog.

	A ``company`` of ``None`` marks a shared platform default (visible to every
	tenant); a tenant may add its own entries without touching the shared ones.
	The catalog is fully configurable — an entry can be flagged required or
	optional, and deactivated — while the seeded defaults give every company a
	meaningful starting point.
	"""

	class Meta:
		ordering = ("pillar", "sort_order", "code")
		constraints = [
			models.UniqueConstraint(
				fields=("code",),
				condition=models.Q(company__isnull=True),
				name="uniq_global_indicator_code",
			),
			models.UniqueConstraint(
				fields=("company", "code"),
				condition=models.Q(company__isnull=False),
				name="uniq_company_indicator_code",
			),
		]
		indexes = [
			models.Index(fields=("pillar", "is_active")),
			models.Index(fields=("company", "is_active")),
		]

	code = models.CharField(max_length=32, help_text="Stable reference, e.g. ENV-001.")
	name = models.CharField(max_length=255)
	pillar = models.CharField(max_length=16, choices=ESGPillar.choices)
	unit = models.CharField(max_length=32, help_text="Reporting unit, e.g. tCO2e, MWh, %.")
	description = models.TextField(blank=True)
	is_required = models.BooleanField(
		default=True, help_text="Part of the required-indicator catalog."
	)
	is_active = models.BooleanField(default=True)
	sort_order = models.PositiveSmallIntegerField(default=100)
	company = models.ForeignKey(
		Company,
		null=True,
		blank=True,
		on_delete=models.CASCADE,
		related_name="esg_catalog_entries",
		help_text="Blank for a shared platform default, set for a tenant entry.",
	)
	created_at = models.DateTimeField(auto_now_add=True)
	updated_at = models.DateTimeField(auto_now=True)

	objects = IndicatorCatalogEntryQuerySet.as_manager()

	def __str__(self):
		return f"{self.code} · {self.name}"

	@property
	def is_shared(self):
		return self.company_id is None

	@property
	def scope_label(self):
		return "Platform default" if self.is_shared else "Company entry"


class IndicatorQuerySet(models.QuerySet):
	def for_company(self, company):
		return self.filter(company=company)

	def active(self):
		return self.filter(is_active=True)

	def with_relations(self):
		return self.select_related("catalog_entry", "owner", "department")


class Indicator(models.Model):
	"""A company tracking one catalog indicator.

	An indicator is unique per (company, catalog entry) so duplicate records can
	never be created for the same metric. Only an active indicator counts
	towards completeness: deactivating one is a soft archive that keeps its
	history for the audit trail.
	"""

	class Meta:
		ordering = ("catalog_entry__pillar", "catalog_entry__sort_order", "id")
		constraints = [
			models.UniqueConstraint(
				fields=("company", "catalog_entry"),
				name="uniq_indicator_per_company_catalog",
			),
		]
		indexes = [
			models.Index(fields=("company", "is_active")),
			models.Index(fields=("company", "department")),
		]

	company = models.ForeignKey(
		Company, on_delete=models.CASCADE, related_name="esg_indicators"
	)
	catalog_entry = models.ForeignKey(
		IndicatorCatalogEntry,
		on_delete=models.PROTECT,
		related_name="indicators",
	)
	owner = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		null=True,
		blank=True,
		on_delete=models.SET_NULL,
		related_name="esg_indicators_owned",
	)
	department = models.ForeignKey(
		Department,
		null=True,
		blank=True,
		on_delete=models.SET_NULL,
		related_name="esg_indicators",
	)
	notes = models.TextField(blank=True)
	is_active = models.BooleanField(default=True)
	created_by = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		null=True,
		blank=True,
		on_delete=models.SET_NULL,
		related_name="esg_indicators_created",
	)
	created_at = models.DateTimeField(auto_now_add=True)
	updated_at = models.DateTimeField(auto_now=True)

	objects = IndicatorQuerySet.as_manager()

	def __str__(self):
		return f"{self.company_id} · {self.catalog_entry.code}"

	# ------------------------------------------------------------------ #
	# Display helpers
	# ------------------------------------------------------------------ #
	@property
	def reference(self):
		return self.catalog_entry.code

	@property
	def name(self):
		return self.catalog_entry.name

	@property
	def pillar(self):
		return self.catalog_entry.pillar

	@property
	def pillar_label(self):
		return self.catalog_entry.get_pillar_display()

	@property
	def unit(self):
		return self.catalog_entry.unit

	@property
	def is_required(self):
		return self.catalog_entry.is_required

	@property
	def is_archived(self):
		return not self.is_active

	def latest_value(self):
		"""Most recent reported measurement, verified first, then newest year."""
		return self.values.order_by("-period_year", "-id").first()

	def values_for_year(self, period_year):
		return self.values.filter(period_year=period_year)

	def status_label(self):
		"""Human label for the indicator's overall reporting state."""
		latest = self.latest_value()
		if latest is None:
			return "No data"
		return latest.get_status_display()

	# ------------------------------------------------------------------ #
	# Validation
	# ------------------------------------------------------------------ #
	def clean(self):
		errors = {}
		if self.department_id and self.company_id and self.department.company_id != self.company_id:
			errors["department"] = "The department must belong to the indicator's company."
		if (
			self.catalog_entry_id
			and self.company_id
			and self.catalog_entry.company_id not in (None, self.company_id)
		):
			errors["catalog_entry"] = "The catalog entry must be shared or belong to your company."
		if self.owner_id and self.company_id:
			from apps.carbon.services.tenancy import company_for_user

			user_company = company_for_user(self.owner)
			if user_company is not None and user_company.pk != self.company_id:
				errors["owner"] = "The owner must belong to your company."
		if errors:
			raise ValidationError(errors)

	def log_event(self, *, actor, action, summary="", period_year=None, from_status="", to_status=""):
		return IndicatorHistory.objects.create(
			indicator=self,
			company_id=self.company_id,
			actor=actor if getattr(actor, "is_authenticated", False) else None,
			action=action,
			summary=(summary or "").strip(),
			period_year=period_year,
			from_status=from_status,
			to_status=to_status,
		)


class IndicatorValueQuerySet(models.QuerySet):
	def for_company(self, company):
		return self.filter(company=company)

	def eligible(self):
		"""Reported or verified measurements — the points that count as ESG data."""
		return self.filter(status__in=ELIGIBLE_STATUSES)

	def for_year(self, period_year):
		return self.filter(period_year=period_year)


class IndicatorValue(models.Model):
	"""One measurement reported for one indicator and reporting period.

	``value`` is nullable: ``None`` means the company has **not** reported a
	measurement (a missing data point), while ``Decimal("0")`` is a genuine
	measured zero. The two must never be conflated. Only ``REPORTED`` and
	``VERIFIED`` values are eligible ESG data; a ``DRAFT`` value is work in
	progress and does not count towards completeness.
	"""

	class Meta:
		ordering = ("-period_year", "-id")
		constraints = [
			models.UniqueConstraint(
				fields=("indicator", "period_year"),
				name="uniq_indicator_value_per_period",
			),
			models.CheckConstraint(
				condition=models.Q(value__isnull=True) | models.Q(value__gte=0),
				name="indicator_value_non_negative",
			),
		]
		indexes = [
			models.Index(fields=("company", "period_year")),
			models.Index(fields=("indicator", "period_year")),
			models.Index(fields=("company", "status")),
		]

	indicator = models.ForeignKey(
		Indicator, on_delete=models.CASCADE, related_name="values"
	)
	company = models.ForeignKey(
		Company, on_delete=models.CASCADE, related_name="esg_indicator_values"
	)
	period_year = models.PositiveSmallIntegerField(help_text="Reporting year covered.")
	value = models.DecimalField(
		max_digits=20,
		decimal_places=4,
		null=True,
		blank=True,
		help_text="Blank means the measurement is missing — not zero.",
	)
	unit = models.CharField(max_length=32, blank=True)
	status = models.CharField(
		max_length=16, choices=IndicatorStatus.choices, default=IndicatorStatus.DRAFT
	)
	evidence_reference = models.CharField(
		max_length=500,
		blank=True,
		help_text="Reference to the supporting document or source system.",
	)
	note = models.TextField(blank=True)
	reported_by = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		null=True,
		blank=True,
		on_delete=models.SET_NULL,
		related_name="esg_values_reported",
	)
	reported_at = models.DateTimeField(null=True, blank=True)
	verified_by = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		null=True,
		blank=True,
		on_delete=models.SET_NULL,
		related_name="esg_values_verified",
	)
	verified_at = models.DateTimeField(null=True, blank=True)
	created_at = models.DateTimeField(auto_now_add=True)
	updated_at = models.DateTimeField(auto_now=True)

	objects = IndicatorValueQuerySet.as_manager()

	def __str__(self):
		amount = "—" if self.value is None else self.value
		return f"{self.indicator_id} · {self.period_year} · {amount}"

	# ------------------------------------------------------------------ #
	# Display helpers
	# ------------------------------------------------------------------ #
	@property
	def has_value(self):
		"""False when the measurement is missing (never conflated with zero)."""
		return self.value is not None

	@property
	def is_eligible(self):
		return self.status in ELIGIBLE_STATUSES

	@property
	def is_verified(self):
		return self.status == IndicatorStatus.VERIFIED

	@property
	def unit_label(self):
		return self.unit or self.indicator.unit

	@property
	def display_value(self):
		if self.value is None:
			return "—"
		return f"{self.value:,.4f}".rstrip("0").rstrip(".")

	@property
	def is_missing(self):
		return self.value is None

	# ------------------------------------------------------------------ #
	# Validation & persistence
	# ------------------------------------------------------------------ #
	def save(self, *args, **kwargs):
		# The tenant is always derived from the indicator — never from input.
		if self.indicator_id:
			self.company_id = self.indicator.company_id
			if not self.unit:
				self.unit = self.indicator.unit
		super().save(*args, **kwargs)

	def clean(self):
		errors = {}
		if self.indicator_id and self.company_id and self.indicator.company_id != self.company_id:
			errors["indicator"] = "The indicator must belong to the value's company."
		if errors:
			raise ValidationError(errors)


class IndicatorHistory(models.Model):
	"""Append-only trail of everything that happened to an indicator."""

	class Meta:
		ordering = ("-created_at", "-id")
		verbose_name = "Indicator history entry"
		verbose_name_plural = "Indicator history"
		indexes = [
			models.Index(fields=("indicator", "-created_at")),
			models.Index(fields=("company", "-created_at")),
		]

	indicator = models.ForeignKey(
		Indicator, on_delete=models.CASCADE, related_name="history"
	)
	company = models.ForeignKey(
		Company, on_delete=models.CASCADE, related_name="esg_indicator_history"
	)
	actor = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		null=True,
		blank=True,
		on_delete=models.SET_NULL,
		related_name="esg_history_entries",
	)
	action = models.CharField(max_length=24, choices=HistoryAction.choices)
	summary = models.TextField(blank=True)
	period_year = models.PositiveSmallIntegerField(null=True, blank=True)
	from_status = models.CharField(max_length=16, blank=True)
	to_status = models.CharField(max_length=16, blank=True)
	created_at = models.DateTimeField(auto_now_add=True)

	def __str__(self):
		return f"{self.get_action_display()} · indicator {self.indicator_id}"

	def save(self, *args, **kwargs):
		if self.pk:
			raise ValidationError("Indicator history entries are append-only.")
		super().save(*args, **kwargs)


class AssessmentSnapshotQuerySet(models.QuerySet):
	def for_company(self, company):
		return self.filter(company=company)

	def latest_first(self):
		return self.order_by("-created_at", "-id")


class AssessmentSnapshot(models.Model):
	"""An immutable, explainable record of one ESG completeness assessment.

	The stored ``score`` is a **data-completeness** score (how complete and
	verified the ESG data set is), never a performance rating. ``breakdown_json``
	keeps every scoring component and its explanation so the number can always
	be reproduced and justified; ``gaps_json`` records the concrete gaps found.
	"""

	class Meta:
		ordering = ("-created_at", "-id")
		constraints = [
			models.CheckConstraint(
				condition=models.Q(overall_score__gte=0) & models.Q(overall_score__lte=100),
				name="assessment_score_0_100",
			),
		]
		indexes = [
			models.Index(fields=("company", "-created_at")),
			models.Index(fields=("company", "readiness")),
		]

	company = models.ForeignKey(
		Company, on_delete=models.CASCADE, related_name="esg_assessments"
	)
	period_year = models.PositiveSmallIntegerField(
		null=True, blank=True, help_text="Reporting year assessed; blank means the latest year."
	)
	overall_score = models.DecimalField(
		max_digits=5,
		decimal_places=2,
		help_text="Data-completeness score, 0 (nothing reported) to 100 (fully reported).",
	)
	readiness = models.CharField(max_length=24, choices=Readiness.choices)
	environmental_score = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0"))
	social_score = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0"))
	governance_score = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0"))
	summary = models.TextField(
		blank=True, help_text="Plain-language explanation of the score and its drivers."
	)
	breakdown_json = models.JSONField(
		default=list, blank=True, help_text="Every scoring component with its points and detail."
	)
	gaps_json = models.JSONField(
		default=dict, blank=True, help_text="Gaps grouped by kind (missing, draft-only, unverified…)."
	)
	delta_score = models.DecimalField(
		max_digits=6,
		decimal_places=2,
		null=True,
		blank=True,
		help_text="Change versus the previous assessment of the same period.",
	)
	created_by = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		null=True,
		blank=True,
		on_delete=models.SET_NULL,
		related_name="esg_assessments_created",
	)
	created_at = models.DateTimeField(auto_now_add=True)

	objects = AssessmentSnapshotQuerySet.as_manager()

	def __str__(self):
		return f"{self.get_readiness_display()} · {self.overall_score}/100 · company {self.company_id}"

	# ------------------------------------------------------------------ #
	# Display helpers
	# ------------------------------------------------------------------ #
	@property
	def period_label(self):
		return str(self.period_year) if self.period_year else "Latest period"

	@property
	def breakdown(self):
		return list(self.breakdown_json or [])

	@property
	def gaps(self):
		return dict(self.gaps_json or {})

	@property
	def is_ready(self):
		return self.readiness == Readiness.READY_FOR_REPORTING

	@property
	def delta_direction(self):
		if self.delta_score is None:
			return "flat"
		if self.delta_score > 0:
			return "up"
		if self.delta_score < 0:
			return "down"
		return "flat"

	@property
	def pillar_rows(self):
		"""Environmental / Social / Governance scores, in reporting order."""
		return [
			{"key": ESGPillar.ENVIRONMENTAL, "label": ESGPillar.ENVIRONMENTAL.label, "score": self.environmental_score},
			{"key": ESGPillar.SOCIAL, "label": ESGPillar.SOCIAL.label, "score": self.social_score},
			{"key": ESGPillar.GOVERNANCE, "label": ESGPillar.GOVERNANCE.label, "score": self.governance_score},
		]
