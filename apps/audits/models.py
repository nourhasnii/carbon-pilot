"""Internal audit & findings management.

An :class:`Audit` is one internal control engagement of a company (internal,
supplier, compliance…). Its observations are :class:`Finding` rows; each finding
can carry :class:`RemediationPlan` actions and :class:`AuditEvidence` files.

Tenant isolation is strict on every model: each row carries its ``company`` and
can never be reached from another tenant. ``company`` is always derived from the
parent record on save, never from the request.
"""

import os
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from apps.enterprise.models import Company, Department


class AuditType(models.TextChoices):
	INTERNAL = "INTERNAL", "Internal"
	SUPPLIER = "SUPPLIER", "Supplier"
	COMPLIANCE = "COMPLIANCE", "Compliance"
	OTHER = "OTHER", "Other"


class AuditStatus(models.TextChoices):
	PLANNED = "PLANNED", "Planned"
	IN_PROGRESS = "IN_PROGRESS", "In progress"
	COMPLETED = "COMPLETED", "Completed"
	CANCELLED = "CANCELLED", "Cancelled"


class FindingSeverity(models.TextChoices):
	LOW = "LOW", "Low"
	MEDIUM = "MEDIUM", "Medium"
	HIGH = "HIGH", "High"
	CRITICAL = "CRITICAL", "Critical"


class FindingStatus(models.TextChoices):
	OPEN = "OPEN", "Open"
	IN_PROGRESS = "IN_PROGRESS", "In progress"
	CLOSED = "CLOSED", "Closed"


class RemediationStatus(models.TextChoices):
	PLANNED = "PLANNED", "Planned"
	IN_PROGRESS = "IN_PROGRESS", "In progress"
	DONE = "DONE", "Done"
	CANCELLED = "CANCELLED", "Cancelled"


class FindingTheme(models.TextChoices):
	"""Themes an AI analysis may classify a finding under.

	Only used to label findings that the model has classified; nothing is
	persisted on the finding itself.
	"""

	DATA_QUALITY = "DATA_QUALITY", "Data quality"
	EVIDENCE_CONTROL = "EVIDENCE_CONTROL", "Evidence control"
	POLICY_COMPLIANCE = "POLICY_COMPLIANCE", "Policy compliance"
	PROCESS_CONTROL = "PROCESS_CONTROL", "Process control"
	SUPPLIER_RISK = "SUPPLIER_RISK", "Supplier risk"
	OTHER = "OTHER", "Other"


# A finding in one of these states is still actionable (and can escalate).
OPEN_FINDING_STATUSES = (FindingStatus.OPEN, FindingStatus.IN_PROGRESS)

# A remediation plan in one of these states is still open.
OPEN_REMEDIATION_STATUSES = (RemediationStatus.PLANNED, RemediationStatus.IN_PROGRESS)


def audit_evidence_upload_to(instance, filename):
	"""Store each evidence file under its own company/audit folder.

	Tenants never share a folder, so an evidence file always lives next to the
	company that owns it, never next to the uploader.
	"""
	company_id = instance.company_id or "unassigned"
	audit_id = instance.audit_id or "unassigned"
	extension = os.path.splitext(filename)[1].lower()
	return f"audits/company_{company_id}/audit_{audit_id}/{uuid.uuid4().hex}{extension}"


class AuditQuerySet(models.QuerySet):
	def for_company(self, company):
		return self.filter(company=company)

	def open(self):
		"""Engagements that are not finished yet."""
		return self.filter(status__in=(AuditStatus.PLANNED, AuditStatus.IN_PROGRESS))


class FindingQuerySet(models.QuerySet):
	def for_company(self, company):
		return self.filter(company=company)

	def open(self):
		"""Findings that are not closed yet (open or being worked on)."""
		return self.filter(status__in=OPEN_FINDING_STATUSES)

	def overdue(self, on_date=None):
		"""Open findings whose due date has passed — the escalation trigger."""
		on_date = on_date or timezone.localdate()
		return self.open().filter(due_date__lt=on_date)

	def high_severity(self):
		"""Open HIGH or CRITICAL findings."""
		return self.open().filter(
			severity__in=(FindingSeverity.HIGH, FindingSeverity.CRITICAL)
		)


class Audit(models.Model):
	"""One internal control engagement of a company."""

	class Meta:
		ordering = ("-start_date", "-id")
		indexes = [
			models.Index(fields=("company", "status")),
			models.Index(fields=("company", "audit_type")),
			models.Index(fields=("company", "department")),
		]

	company = models.ForeignKey(
		Company, on_delete=models.CASCADE, related_name="audits"
	)
	title = models.CharField(max_length=255)
	audit_type = models.CharField(
		max_length=16, choices=AuditType.choices, default=AuditType.INTERNAL
	)
	scope_summary = models.TextField(
		blank=True, help_text="What the engagement covers."
	)
	department = models.ForeignKey(
		Department,
		null=True,
		blank=True,
		on_delete=models.SET_NULL,
		related_name="audits",
	)
	related_policy = models.ForeignKey(
		"governance.Policy",
		null=True,
		blank=True,
		on_delete=models.SET_NULL,
		related_name="audits",
	)
	status = models.CharField(
		max_length=16, choices=AuditStatus.choices, default=AuditStatus.PLANNED
	)
	start_date = models.DateField()
	end_date = models.DateField(null=True, blank=True)
	lead_auditor = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		null=True,
		blank=True,
		on_delete=models.SET_NULL,
		related_name="audits_led",
	)
	created_by = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		null=True,
		blank=True,
		on_delete=models.SET_NULL,
		related_name="audits_created",
	)
	created_at = models.DateTimeField(auto_now_add=True)
	updated_at = models.DateTimeField(auto_now=True)

	objects = AuditQuerySet.as_manager()

	def __str__(self):
		return f"{self.reference} · {self.title}"

	# ------------------------------------------------------------------ #
	# Display helpers
	# ------------------------------------------------------------------ #
	@property
	def reference(self):
		return f"AUD-{self.pk:04d}" if self.pk else "AUD"

	@property
	def status_label(self):
		return self.get_status_display()

	@property
	def is_open(self):
		return self.status in (AuditStatus.PLANNED, AuditStatus.IN_PROGRESS)

	@property
	def is_completed(self):
		return self.status == AuditStatus.COMPLETED

	@property
	def finding_count(self):
		return self.findings.count()

	@property
	def open_finding_count(self):
		return self.findings.open().count()

	# ------------------------------------------------------------------ #
	# Validation
	# ------------------------------------------------------------------ #
	def clean(self):
		errors = {}
		company_id = self.company_id
		if self.department_id and company_id and self.department.company_id != company_id:
			errors["department"] = "The department must belong to the audit's company."
		if (
			self.related_policy_id
			and company_id
			and self.related_policy.company_id != company_id
		):
			errors["related_policy"] = "The policy must belong to the audit's company."
		if self.lead_auditor_id and company_id:
			from apps.carbon.services.tenancy import company_for_user

			user_company = company_for_user(self.lead_auditor)
			if user_company is not None and user_company.pk != company_id:
				errors["lead_auditor"] = "The lead auditor must belong to your company."
		if self.start_date and self.end_date and self.end_date < self.start_date:
			errors["end_date"] = "End date must be on or after start date."
		if errors:
			raise ValidationError(errors)


class Finding(models.Model):
	"""An observation raised by an audit, tracked until it is closed."""

	class Meta:
		ordering = ("-created_at", "-id")
		indexes = [
			models.Index(fields=("company", "status")),
			models.Index(fields=("company", "severity")),
			models.Index(fields=("audit", "status")),
			models.Index(fields=("company", "due_date")),
		]

	company = models.ForeignKey(
		Company, on_delete=models.CASCADE, related_name="findings"
	)
	audit = models.ForeignKey(
		Audit, on_delete=models.CASCADE, related_name="findings"
	)
	title = models.CharField(max_length=255)
	description = models.TextField(blank=True)
	severity = models.CharField(
		max_length=8, choices=FindingSeverity.choices, default=FindingSeverity.MEDIUM
	)
	status = models.CharField(
		max_length=16, choices=FindingStatus.choices, default=FindingStatus.OPEN
	)
	department = models.ForeignKey(
		Department,
		null=True,
		blank=True,
		on_delete=models.SET_NULL,
		related_name="findings",
	)
	related_policy = models.ForeignKey(
		"governance.Policy",
		null=True,
		blank=True,
		on_delete=models.SET_NULL,
		related_name="findings",
	)
	owner = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		null=True,
		blank=True,
		on_delete=models.SET_NULL,
		related_name="findings_owned",
	)
	due_date = models.DateField(null=True, blank=True)
	closed_at = models.DateTimeField(null=True, blank=True)
	created_at = models.DateTimeField(auto_now_add=True)
	updated_at = models.DateTimeField(auto_now=True)

	objects = FindingQuerySet.as_manager()

	def __str__(self):
		return f"{self.reference} · {self.title}"

	# ------------------------------------------------------------------ #
	# Display helpers
	# ------------------------------------------------------------------ #
	@property
	def reference(self):
		return f"FND-{self.pk:04d}" if self.pk else "FND"

	@property
	def status_label(self):
		return self.get_status_display()

	@property
	def is_closed(self):
		return self.status == FindingStatus.CLOSED

	@property
	def is_overdue(self):
		"""Open finding whose due date has passed."""
		if self.status not in OPEN_FINDING_STATUSES or not self.due_date:
			return False
		return self.due_date < timezone.localdate()

	@property
	def escalated(self):
		"""Escalation flag: an overdue, still-open finding."""
		return self.is_overdue

	@property
	def days_overdue(self):
		if not self.is_overdue:
			return 0
		return (timezone.localdate() - self.due_date).days

	@property
	def is_high_severity(self):
		return self.severity in (FindingSeverity.HIGH, FindingSeverity.CRITICAL)

	@property
	def open_remediation_count(self):
		return self.remediation_plans.filter(
			status__in=OPEN_REMEDIATION_STATUSES
		).count()

	# ------------------------------------------------------------------ #
	# Validation & persistence
	# ------------------------------------------------------------------ #
	def save(self, *args, **kwargs):
		# The owning company is always derived from the audit — never from the
		# request — so a finding can never be filed against another tenant.
		if self.audit_id:
			self.company_id = self.audit.company_id
		# Closing a finding always records when it happened; reopening clears it.
		if self.status == FindingStatus.CLOSED:
			if self.closed_at is None:
				self.closed_at = timezone.now()
		else:
			self.closed_at = None
		super().save(*args, **kwargs)

	def clean(self):
		errors = {}
		company_id = self.company_id
		if self.audit_id and company_id and self.audit.company_id != company_id:
			errors["audit"] = "The audit must belong to the finding's company."
		if self.department_id and company_id and self.department.company_id != company_id:
			errors["department"] = "The department must belong to the finding's company."
		if (
			self.related_policy_id
			and company_id
			and self.related_policy.company_id != company_id
		):
			errors["related_policy"] = "The policy must belong to the finding's company."
		if self.owner_id and company_id:
			from apps.carbon.services.tenancy import company_for_user

			user_company = company_for_user(self.owner)
			if user_company is not None and user_company.pk != company_id:
				errors["owner"] = "The owner must belong to your company."
		if errors:
			raise ValidationError(errors)


class RemediationPlan(models.Model):
	"""A corrective action that closes out a finding."""

	class Meta:
		ordering = ("-created_at", "-id")
		indexes = [
			models.Index(fields=("company", "status")),
			models.Index(fields=("finding", "status")),
		]

	company = models.ForeignKey(
		Company, on_delete=models.CASCADE, related_name="remediation_plans"
	)
	finding = models.ForeignKey(
		Finding, on_delete=models.CASCADE, related_name="remediation_plans"
	)
	title = models.CharField(max_length=255)
	description = models.TextField(blank=True)
	status = models.CharField(
		max_length=16,
		choices=RemediationStatus.choices,
		default=RemediationStatus.PLANNED,
	)
	owner = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		null=True,
		blank=True,
		on_delete=models.SET_NULL,
		related_name="remediation_plans_owned",
	)
	due_date = models.DateField(null=True, blank=True)
	completed_at = models.DateTimeField(null=True, blank=True)
	created_at = models.DateTimeField(auto_now_add=True)
	updated_at = models.DateTimeField(auto_now=True)

	def __str__(self):
		return f"{self.title} ({self.get_status_display()})"

	@property
	def is_done(self):
		return self.status == RemediationStatus.DONE

	@property
	def is_cancelled(self):
		return self.status == RemediationStatus.CANCELLED

	@property
	def is_open(self):
		return self.status in OPEN_REMEDIATION_STATUSES

	@property
	def is_overdue(self):
		if not self.is_open or not self.due_date:
			return False
		return self.due_date < timezone.localdate()

	def save(self, *args, **kwargs):
		# The company is derived from the finding on save.
		if self.finding_id:
			self.company_id = self.finding.company_id
		# Completing a plan records when it happened; anything else clears it.
		if self.status == RemediationStatus.DONE:
			if self.completed_at is None:
				self.completed_at = timezone.now()
		else:
			self.completed_at = None
		super().save(*args, **kwargs)

	def clean(self):
		errors = {}
		company_id = self.company_id
		if self.finding_id and company_id and self.finding.company_id != company_id:
			errors["finding"] = "The finding must belong to the plan's company."
		if self.owner_id and company_id:
			from apps.carbon.services.tenancy import company_for_user

			user_company = company_for_user(self.owner)
			if user_company is not None and user_company.pk != company_id:
				errors["owner"] = "The owner must belong to your company."
		if errors:
			raise ValidationError(errors)


class AuditEvidence(models.Model):
	"""A supporting file attached to an audit and/or one of its findings."""

	class Meta:
		ordering = ("-uploaded_at", "-id")
		verbose_name_plural = "Audit evidence"
		constraints = [
			models.CheckConstraint(
				condition=models.Q(audit__isnull=False) | models.Q(finding__isnull=False),
				name="audit_evidence_has_audit_or_finding",
			),
		]
		indexes = [
			models.Index(fields=("company", "audit")),
			models.Index(fields=("company", "finding")),
		]

	company = models.ForeignKey(
		Company, on_delete=models.CASCADE, related_name="audit_evidence"
	)
	audit = models.ForeignKey(
		Audit,
		null=True,
		blank=True,
		on_delete=models.CASCADE,
		related_name="evidence",
	)
	finding = models.ForeignKey(
		Finding,
		null=True,
		blank=True,
		on_delete=models.CASCADE,
		related_name="evidence",
	)
	title = models.CharField(max_length=255)
	file = models.FileField(upload_to=audit_evidence_upload_to)
	uploaded_by = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		null=True,
		blank=True,
		on_delete=models.SET_NULL,
		related_name="audit_evidence_uploaded",
	)
	uploaded_at = models.DateTimeField(auto_now_add=True)

	def __str__(self):
		return self.title

	@property
	def filename(self):
		return os.path.basename(self.file.name) if self.file else ""

	def save(self, *args, **kwargs):
		# The owning company is always derived from the parent evidence target,
		# so a file can never be filed against another tenant.
		if self.finding_id:
			self.company_id = self.finding.company_id
			if not self.audit_id:
				self.audit_id = self.finding.audit_id
		elif self.audit_id:
			self.company_id = self.audit.company_id
		super().save(*args, **kwargs)

	def clean(self):
		if not self.audit_id and not self.finding_id:
			raise ValidationError(
				"Evidence must be attached to an audit or to a finding."
			)
		if self.finding_id and self.audit_id:
			if self.finding.audit_id != self.audit_id:
				raise ValidationError(
					{"audit": "The audit must be the finding's own audit."}
				)
		if self.audit_id and self.company_id and self.audit.company_id != self.company_id:
			raise ValidationError(
				{"audit": "The audit must belong to the evidence's company."}
			)
		if self.finding_id and self.company_id and self.finding.company_id != self.company_id:
			raise ValidationError(
				{"finding": "The finding must belong to the evidence's company."}
			)
