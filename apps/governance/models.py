"""Corporate governance & policy control.

An internal ESG/business :class:`Policy` belongs to exactly one company and is
versioned over time. Each :class:`PolicyVersion` is a frozen snapshot of the
policy text; :class:`PolicyApproval` records who signed a version off and
:class:`PolicyOwner` builds the responsibility matrix (owner / reviewer /
contributor, optionally scoped to a department).

Tenant isolation is strict on every model: a policy — and every row that hangs
off it — carries its ``company`` and can never be reached from another tenant.
"""

from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from apps.enterprise.models import Company, Department


class PolicyCategory(models.TextChoices):
	ESG = "ESG", "ESG"
	ENVIRONMENT = "ENVIRONMENT", "Environment"
	SOCIAL = "SOCIAL", "Social"
	GOVERNANCE = "GOVERNANCE", "Governance"
	OPERATIONAL = "OPERATIONAL", "Operational"
	OTHER = "OTHER", "Other"


class PolicyStatus(models.TextChoices):
	DRAFT = "DRAFT", "Draft"
	IN_REVIEW = "IN_REVIEW", "In review"
	APPROVED = "APPROVED", "Approved"
	ARCHIVED = "ARCHIVED", "Archived"


class ResponsibilityRole(models.TextChoices):
	OWNER = "OWNER", "Owner"
	REVIEWER = "REVIEWER", "Reviewer"
	CONTRIBUTOR = "CONTRIBUTOR", "Contributor"


class ApprovalDecision(models.TextChoices):
	PENDING = "PENDING", "Pending"
	APPROVED = "APPROVED", "Approved"
	REJECTED = "REJECTED", "Rejected"


# Default review cadence applied when an approved policy has no explicit date.
DEFAULT_REVIEW_INTERVAL_DAYS = 365


class PolicyQuerySet(models.QuerySet):
	def for_company(self, company):
		return self.filter(company=company)

	def live(self):
		"""Policies still in use (not archived)."""
		return self.exclude(status=PolicyStatus.ARCHIVED)

	def due_for_review(self, on_date=None):
		"""Approved policies whose review date has passed (or falls on ``on_date``)."""
		on_date = on_date or timezone.localdate()
		return self.filter(
			status=PolicyStatus.APPROVED,
			next_review_date__lt=on_date,
		)


class Policy(models.Model):
	"""An internal ESG/business policy managed by one company.

	The policy's text lives in :class:`PolicyVersion` rows; ``current_version``
	points at the live one so the document never drifts from a specific,
	auditable snapshot. Only ``DRAFT`` policies are freely editable — every
	other status is reached through the review workflow in ``services``.
	"""

	class Meta:
		ordering = ("-updated_at", "title")
		constraints = [
			# A code, when given, is unique inside a company (blank means "no code").
			models.UniqueConstraint(
				fields=("company", "code"),
				condition=~models.Q(code=""),
				name="uniq_policy_code_per_company",
			),
		]
		indexes = [
			models.Index(fields=("company", "status")),
			models.Index(fields=("company", "category")),
			models.Index(fields=("company", "next_review_date")),
		]

	company = models.ForeignKey(
		Company,
		on_delete=models.CASCADE,
		related_name="policies",
	)
	title = models.CharField(max_length=255)
	code = models.CharField(
		max_length=50, blank=True, help_text="Optional internal reference, unique per company."
	)
	category = models.CharField(
		max_length=16, choices=PolicyCategory.choices, default=PolicyCategory.OTHER
	)
	description = models.TextField(blank=True)
	status = models.CharField(
		max_length=16, choices=PolicyStatus.choices, default=PolicyStatus.DRAFT
	)
	current_version = models.ForeignKey(
		"PolicyVersion",
		null=True,
		blank=True,
		on_delete=models.SET_NULL,
		related_name="+",
	)
	next_review_date = models.DateField(
		null=True, blank=True, help_text="Date of the next periodic review."
	)
	is_active = models.BooleanField(default=True)
	created_by = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		null=True,
		blank=True,
		on_delete=models.SET_NULL,
		related_name="policies_created",
	)
	created_at = models.DateTimeField(auto_now_add=True)
	updated_at = models.DateTimeField(auto_now=True)

	objects = PolicyQuerySet.as_manager()

	def __str__(self):
		return f"{self.reference} · {self.title}"

	# ------------------------------------------------------------------ #
	# Display helpers
	# ------------------------------------------------------------------ #
	@property
	def reference(self):
		if self.code:
			return self.code
		return f"POL-{self.pk:04d}" if self.pk else "POL"

	@property
	def status_label(self):
		return self.get_status_display()

	@property
	def is_editable(self):
		"""Only draft policies are freely editable."""
		return self.status == PolicyStatus.DRAFT

	@property
	def is_due_for_review(self):
		"""APPROVED and the review date has passed."""
		if self.status != PolicyStatus.APPROVED or not self.next_review_date:
			return False
		return self.next_review_date < timezone.localdate()

	@property
	def is_archived(self):
		return self.status == PolicyStatus.ARCHIVED

	@property
	def days_until_review(self):
		if not self.next_review_date:
			return None
		return (self.next_review_date - timezone.localdate()).days

	@property
	def current_pending_approvals(self):
		if self.current_version_id is None:
			return PolicyApproval.objects.none()
		return self.current_version.approvals.filter(decision=ApprovalDecision.PENDING)

	@property
	def reviewers(self):
		return self.owners.filter(responsibility_role=ResponsibilityRole.REVIEWER)

	@property
	def responsible_owners(self):
		return self.owners.filter(responsibility_role=ResponsibilityRole.OWNER)

	@property
	def default_review_interval(self):
		return timedelta(days=DEFAULT_REVIEW_INTERVAL_DAYS)

	# ------------------------------------------------------------------ #
	# Validation
	# ------------------------------------------------------------------ #
	def clean(self):
		errors = {}
		if self.current_version_id and self.pk:
			version = self.current_version
			if version is not None and version.policy_id != self.pk:
				errors["current_version"] = "The current version must belong to this policy."
		if errors:
			raise ValidationError(errors)


class PolicyVersion(models.Model):
	"""An immutable-by-convention snapshot of a policy's text.

	Versions are numbered sequentially inside a policy. Exactly one version is
	``is_current`` for a given policy at any time; the flag is enforced by a
	partial unique constraint so the "live" document is unambiguous.
	"""

	class Meta:
		ordering = ("-version_number", "-id")
		constraints = [
			models.UniqueConstraint(
				fields=("policy", "version_number"),
				name="uniq_policy_version_number",
			),
			models.UniqueConstraint(
				fields=("policy",),
				condition=models.Q(is_current=True),
				name="uniq_current_policy_version",
			),
		]
		indexes = [
			models.Index(fields=("policy", "-version_number")),
		]

	policy = models.ForeignKey(
		Policy,
		on_delete=models.CASCADE,
		related_name="versions",
	)
	version_number = models.PositiveIntegerField()
	summary = models.TextField(blank=True, help_text="One-line abstract of this version.")
	content = models.TextField(blank=True, help_text="Full policy text.")
	change_notes = models.TextField(blank=True, help_text="What changed since the last version.")
	effective_from = models.DateField(null=True, blank=True)
	created_by = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		null=True,
		blank=True,
		on_delete=models.SET_NULL,
		related_name="policy_versions_created",
	)
	created_at = models.DateTimeField(auto_now_add=True)
	is_current = models.BooleanField(default=False)

	def __str__(self):
		return f"{self.policy_id} · v{self.version_number}"

	@property
	def label(self):
		return f"v{self.version_number}"

	@property
	def has_pending_approvals(self):
		return self.approvals.filter(decision=ApprovalDecision.PENDING).exists()


class PolicyApproval(models.Model):
	"""One approver's decision on a specific policy version."""

	class Meta:
		ordering = ("created_at", "id")
		constraints = [
			models.UniqueConstraint(
				fields=("policy_version", "approver"),
				name="uniq_policy_approver_per_version",
			),
		]
		indexes = [
			models.Index(fields=("policy", "decision")),
		]

	policy = models.ForeignKey(
		Policy,
		on_delete=models.CASCADE,
		related_name="approvals",
	)
	policy_version = models.ForeignKey(
		PolicyVersion,
		on_delete=models.CASCADE,
		related_name="approvals",
	)
	approver = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		on_delete=models.CASCADE,
		related_name="policy_approvals",
	)
	decision = models.CharField(
		max_length=16, choices=ApprovalDecision.choices, default=ApprovalDecision.PENDING
	)
	comment = models.TextField(blank=True)
	decided_at = models.DateTimeField(null=True, blank=True)
	created_at = models.DateTimeField(auto_now_add=True)

	def __str__(self):
		return f"{self.policy_id} · {self.approver_id} · {self.get_decision_display()}"

	@property
	def is_pending(self):
		return self.decision == ApprovalDecision.PENDING

	@property
	def is_rejected(self):
		return self.decision == ApprovalDecision.REJECTED


class PolicyOwner(models.Model):
	"""A responsibility assignment on a policy (the responsibility matrix).

	A row always belongs to the policy's company. It may target a user, a
	department, or both — never a user or department from another tenant.
	"""

	class Meta:
		ordering = ("responsibility_role", "id")
		constraints = [
			models.CheckConstraint(
				condition=models.Q(user__isnull=False) | models.Q(department__isnull=False),
				name="policy_owner_has_user_or_department",
			),
		]
		indexes = [
			models.Index(fields=("policy", "responsibility_role")),
			models.Index(fields=("company",)),
		]

	policy = models.ForeignKey(
		Policy,
		on_delete=models.CASCADE,
		related_name="owners",
	)
	company = models.ForeignKey(
		Company,
		on_delete=models.CASCADE,
		related_name="policy_owners",
	)
	department = models.ForeignKey(
		Department,
		null=True,
		blank=True,
		on_delete=models.SET_NULL,
		related_name="policy_owners",
	)
	user = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		null=True,
		blank=True,
		on_delete=models.SET_NULL,
		related_name="policy_ownerships",
	)
	responsibility_role = models.CharField(
		max_length=16, choices=ResponsibilityRole.choices, default=ResponsibilityRole.OWNER
	)
	created_at = models.DateTimeField(auto_now_add=True)

	def __str__(self):
		holder = self.user.email if self.user_id else (self.department.name if self.department_id else "—")
		return f"{self.policy_id} · {self.get_responsibility_role_display()} · {holder}"

	@property
	def holder_label(self):
		if self.user_id:
			return self.user.get_full_name() or self.user.email
		if self.department_id:
			return self.department.name
		return "—"

	def save(self, *args, **kwargs):
		# Responsibility rows are always pinned to the policy's tenant so a
		# caller can never create a cross-company assignment.
		if self.policy_id:
			self.company_id = self.policy.company_id
		super().save(*args, **kwargs)

	def clean(self):
		errors = {}
		# ``company`` is derived from the policy on save, so fall back to it here
		# to catch cross-company assignments during form validation too.
		company_id = self.company_id
		if not company_id and self.policy_id:
			company_id = self.policy.company_id
		if self.policy_id and self.company_id and self.company_id != self.policy.company_id:
			errors["company"] = "The responsibility row must belong to the policy's company."
		if self.department_id and company_id and self.department.company_id != company_id:
			errors["department"] = "The department must belong to the policy's company."
		if self.user_id and company_id:
			from apps.carbon.services.tenancy import company_for_user

			user_company = company_for_user(self.user)
			if user_company is not None and user_company.pk != company_id:
				errors["user"] = "The user must belong to the policy's company."
		if errors:
			raise ValidationError(errors)
