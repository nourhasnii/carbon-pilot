"""Permissions, tenant scoping and the policy review workflow.

The tenant is always resolved from the authenticated user, never from request
parameters, so a policy from another company is a 404 rather than a 403. Role
rules follow the rest of CarbonPilot:

* Company Owner / ESG Reporting Manager — full manage (create, edit, submit,
  approve, archive, new version).
* Department Manager — read the company's policies and contribute (edit) only
  the ones where they are named as responsible.
* Auditor and any other member — read-only.
"""

from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.accounts.models import User
from apps.carbon.services.tenancy import company_for_user  # re-exported for views
from apps.enterprise.models import Department

from ..models import (
	ApprovalDecision,
	DEFAULT_REVIEW_INTERVAL_DAYS,
	Policy,
	PolicyApproval,
	PolicyStatus,
	PolicyVersion,
	ResponsibilityRole,
)

# Roles that may administrate the whole governance module.
ADMIN_ROLES = (User.Role.COMPANY_OWNER, User.Role.ESG_REPORTING_MANAGER)

# Roles that may open the policies workspace (auditors are read-only).
VIEWER_ROLES = (
	User.Role.COMPANY_OWNER,
	User.Role.DEPARTMENT_MANAGER,
	User.Role.ESG_REPORTING_MANAGER,
	User.Role.AUDITOR,
)


def is_company_admin(user):
	return user.role == User.Role.SUPER_ADMIN or user.role in ADMIN_ROLES


def visible_policies(user, company):
	"""Policies a user is allowed to see, scoped to their tenant and role."""
	if company is None:
		return Policy.objects.none()
	queryset = Policy.objects.filter(company=company)
	if user.role == User.Role.SUPER_ADMIN or user.role in VIEWER_ROLES:
		return queryset
	return queryset.none()


def is_responsible(policy, user):
	"""True when the user is named on the policy's responsibility matrix."""
	return policy.owners.filter(user=user).exists()


def can_manage(user, company, policy=None):
	"""Server-side guard for creating/editing a policy or its owners.

	Company owners and the ESG reporting manager manage every policy of their
	tenant. A department manager may create a policy (becoming its owner) and
	contribute only to policies where they are named responsible.
	"""
	if company is None:
		return False
	if is_company_admin(user):
		return True
	if user.role == User.Role.DEPARTMENT_MANAGER:
		if policy is None:
			return True
		return is_responsible(policy, user)
	return False


def can_approve(user, company, policy):
	"""Server-side guard for recording an approval decision."""
	if company is None:
		return False
	if is_company_admin(user):
		return True
	return policy.owners.filter(
		user=user, responsibility_role=ResponsibilityRole.REVIEWER
	).exists()


def company_members(company):
	"""Active users belonging to a company (its owner plus its members)."""
	if company is None:
		return User.objects.none()
	return (
		User.objects.filter(status=User.Status.ACTIVE)
		.filter(Q(company_membership=company) | Q(company_profile__pk=company.pk))
		.distinct()
		.order_by("first_name", "last_name", "email")
	)


def department_choices(company):
	if company is None:
		return Department.objects.none()
	return Department.objects.filter(company=company).order_by("name")


# ---------------------------------------------------------------------- #
# Responsibility matrix
# ---------------------------------------------------------------------- #
def responsibility_matrix(policy):
	"""Owners/reviewers grouped by department, for the policy detail page.

	Returns an ordered list of ``{"label", "rows"}`` buckets — one per
	department plus a "Company-wide" bucket for rows with no department — so a
	policy's responsibility is readable department by department.
	"""
	rows = list(
		policy.owners.select_related("department", "user").order_by(
			"responsibility_role", "department__name", "id"
		)
	)
	buckets = {}
	for row in rows:
		label = row.department.name if row.department_id else "Company-wide"
		buckets.setdefault(label, []).append(row)
	return [
		{"label": label, "rows": buckets[label]}
		for label in sorted(buckets, key=lambda name: (name != "Company-wide", name))
	]


# ---------------------------------------------------------------------- #
# Workflow
# ---------------------------------------------------------------------- #
def approver_candidates(policy):
	"""Users expected to approve the policy's next version.

	Named reviewers come first; when a policy has none, the company's own
	governance roles (its owner and ESG reporting managers) act as approvers so
	an approval step always exists.
	"""
	reviewers = list(
		company_members(policy.company).filter(
			policy_ownerships__policy=policy,
			policy_ownerships__responsibility_role=ResponsibilityRole.REVIEWER,
		).distinct()
	)
	if reviewers:
		return reviewers
	return list(
		company_members(policy.company).filter(
			Q(pk=policy.company.owner_id) | Q(role=User.Role.ESG_REPORTING_MANAGER)
		).distinct()
	)


def ensure_pending_approvals(policy, version):
	"""Make sure every approver has an open PENDING row for ``version``.

	Existing decisions are kept; a previously rejected approval is reopened so
	a revised version is genuinely reviewed again.
	"""
	created = []
	for approver in approver_candidates(policy):
		approval, was_created = PolicyApproval.objects.get_or_create(
			policy=policy,
			policy_version=version,
			approver=approver,
			defaults={"decision": ApprovalDecision.PENDING},
		)
		if not was_created and approval.decision == ApprovalDecision.REJECTED:
			approval.decision = ApprovalDecision.PENDING
			approval.comment = ""
			approval.decided_at = None
			approval.save(update_fields=("decision", "comment", "decided_at"))
		created.append(approval)
	return created


def create_initial_version(policy, user, *, summary="", content="", change_notes="", effective_from=None):
	"""Create version 1 for a freshly created policy and make it current."""
	version = PolicyVersion.objects.create(
		policy=policy,
		version_number=1,
		summary=summary,
		content=content,
		change_notes=change_notes,
		effective_from=effective_from,
		created_by=user,
		is_current=True,
	)
	policy.current_version = version
	policy.save(update_fields=("current_version", "updated_at"))
	return version


@transaction.atomic
def submit_for_review(policy, user):
	"""DRAFT -> IN_REVIEW: open a pending approval for every approver."""
	if policy.status != PolicyStatus.DRAFT:
		raise ValidationError("Only a draft policy can be submitted for review.")
	version = policy.current_version or policy.versions.order_by("-version_number").first()
	if version is None:
		raise ValidationError("Add a policy version before submitting it for review.")
	if not version.is_current:
		PolicyVersion.objects.filter(policy=policy).update(is_current=False)
		version.is_current = True
		version.save(update_fields=("is_current",))
		policy.current_version = version
	policy.status = PolicyStatus.IN_REVIEW
	policy.save(update_fields=("status", "current_version", "updated_at"))
	ensure_pending_approvals(policy, version)
	return policy


@transaction.atomic
def record_decision(approval, user, decision, comment=""):
	"""Record an approver's decision and recompute the policy status.

	Any rejection sends the policy back to DRAFT; once every open approval on
	the current version is APPROVED the policy becomes APPROVED and its review
	date is set.
	"""
	if decision not in (ApprovalDecision.APPROVED, ApprovalDecision.REJECTED):
		raise ValidationError("A decision must be APPROVED or REJECTED.")
	if approval.approver_id != getattr(user, "pk", None):
		raise ValidationError("Only the assigned approver can decide this approval.")
	if approval.decision != ApprovalDecision.PENDING:
		raise ValidationError("This approval has already been decided.")
	approval.decision = decision
	approval.comment = comment or ""
	approval.decided_at = timezone.now()
	approval.save(update_fields=("decision", "comment", "decided_at"))
	return _recompute_status(approval.policy)


def _recompute_status(policy):
	version = policy.current_version
	if version is None:
		return policy
	approvals = list(version.approvals.all())
	if any(a.decision == ApprovalDecision.REJECTED for a in approvals):
		if policy.status != PolicyStatus.DRAFT:
			policy.status = PolicyStatus.DRAFT
			policy.save(update_fields=("status", "updated_at"))
		return policy
	if approvals and all(a.decision == ApprovalDecision.APPROVED for a in approvals):
		return approve_policy(policy)
	return policy


def approve_policy(policy):
	"""Mark an approved version current and guarantee a next review date."""
	policy.status = PolicyStatus.APPROVED
	policy.is_active = True
	if policy.next_review_date is None:
		policy.next_review_date = timezone.localdate() + timedelta(
			days=DEFAULT_REVIEW_INTERVAL_DAYS
		)
	policy.save(update_fields=("status", "is_active", "next_review_date", "updated_at"))
	version = policy.current_version
	if version is not None:
		PolicyVersion.objects.filter(policy=policy, is_current=True).exclude(
			pk=version.pk
		).update(is_current=False)
		if not version.is_current:
			version.is_current = True
			version.save(update_fields=("is_current",))
	return policy


@transaction.atomic
def create_new_version(
	policy, user, *, summary="", content="", change_notes="", effective_from=None
):
	"""APPROVED -> DRAFT with a new, reviewable version.

	The policy returns to DRAFT so the new text goes through the same review
	workflow; the previous version is kept for the audit trail.
	"""
	if policy.status != PolicyStatus.APPROVED:
		raise ValidationError("A new version can only be created from an approved policy.")
	last = policy.versions.order_by("-version_number").first()
	number = (last.version_number if last else 0) + 1
	# Clear the previous current flag first: only one version may be current,
	# and the new row would otherwise collide with the old one on insert.
	PolicyVersion.objects.filter(policy=policy, is_current=True).update(is_current=False)
	version = PolicyVersion.objects.create(
		policy=policy,
		version_number=number,
		summary=summary,
		content=content,
		change_notes=change_notes,
		effective_from=effective_from,
		created_by=user,
		is_current=True,
	)
	policy.current_version = version
	policy.status = PolicyStatus.DRAFT
	# The new text must be reviewed before it becomes effective again.
	policy.next_review_date = None
	policy.save(
		update_fields=("current_version", "status", "next_review_date", "updated_at")
	)
	return version


def archive_policy(policy):
	"""APPROVED -> ARCHIVED (soft archive; the record is kept for audit)."""
	if policy.status != PolicyStatus.APPROVED:
		raise ValidationError("Only an approved policy can be archived.")
	policy.status = PolicyStatus.ARCHIVED
	policy.is_active = False
	policy.save(update_fields=("status", "is_active", "updated_at"))
	return policy


def record_review(policy, on_date=None):
	"""Close a periodic review by scheduling the next one a year out."""
	if policy.status != PolicyStatus.APPROVED:
		raise ValidationError("Only an approved policy can be reviewed.")
	policy.next_review_date = (on_date or timezone.localdate()) + timedelta(
		days=DEFAULT_REVIEW_INTERVAL_DAYS
	)
	policy.save(update_fields=("next_review_date", "updated_at"))
	return policy
