"""Permissions, tenant scoping, workflows and dashboard aggregation for audits.

The tenant is always resolved from the authenticated user (never from request
parameters), so a record from another company is a 404 rather than a 403. Role
rules follow the rest of CarbonPilot:

* Company Owner / ESG Reporting Manager — full manage (audits, findings,
  remediation plans, evidence).
* Department Manager — manage (and escalate) findings in their own department.
* Auditor and any other member — read-only.
"""

from django.core.exceptions import ValidationError
from django.db.models import Count
from django.utils import timezone

from apps.accounts.models import User
from apps.carbon.services.tenancy import company_for_user  # re-exported for views
from apps.enterprise.models import Department
from apps.governance.services import company_members  # re-exported, one source of truth

from .models import (
	Audit,
	AuditStatus,
	Finding,
	FindingSeverity,
	FindingStatus,
	OPEN_FINDING_STATUSES,
	RemediationPlan,
	RemediationStatus,
)

# Roles that may administrate the whole audit module.
MANAGER_ROLES = (User.Role.COMPANY_OWNER, User.Role.ESG_REPORTING_MANAGER)

# Roles that may open the audit workspace (auditors are read-only).
VIEWER_ROLES = (
	User.Role.COMPANY_OWNER,
	User.Role.DEPARTMENT_MANAGER,
	User.Role.ESG_REPORTING_MANAGER,
	User.Role.AUDITOR,
)

# Allowed status moves. Each workflow only moves forward.
AUDIT_TRANSITIONS = {
	AuditStatus.PLANNED: (AuditStatus.IN_PROGRESS, AuditStatus.CANCELLED),
	AuditStatus.IN_PROGRESS: (AuditStatus.COMPLETED, AuditStatus.CANCELLED),
	AuditStatus.COMPLETED: (),
	AuditStatus.CANCELLED: (),
}

FINDING_TRANSITIONS = {
	FindingStatus.OPEN: (FindingStatus.IN_PROGRESS, FindingStatus.CLOSED),
	FindingStatus.IN_PROGRESS: (FindingStatus.CLOSED,),
	FindingStatus.CLOSED: (),
}

REMEDIATION_TRANSITIONS = {
	RemediationStatus.PLANNED: (
		RemediationStatus.IN_PROGRESS,
		RemediationStatus.CANCELLED,
	),
	RemediationStatus.IN_PROGRESS: (
		RemediationStatus.DONE,
		RemediationStatus.CANCELLED,
	),
	RemediationStatus.DONE: (),
	RemediationStatus.CANCELLED: (),
}


# ---------------------------------------------------------------------- #
# Permissions
# ---------------------------------------------------------------------- #
def is_company_admin(user):
	return user.role == User.Role.SUPER_ADMIN or user.role in MANAGER_ROLES


def can_view_module(user, company):
	if company is None:
		return False
	if user.role == User.Role.SUPER_ADMIN:
		return True
	return user.role in VIEWER_ROLES


def can_manage_audit(user, company, audit=None):
	"""Audits are administrated by the Company Owner and ESG roles only."""
	if company is None:
		return False
	return is_company_admin(user)


def user_department(company, user):
	"""The company :class:`Department` matching the member's department name.

	``User.department`` is a free-text label; it is matched to the company's
	department register by name so a Department Manager is scoped to a real,
	tenant-owned department.
	"""
	name = (getattr(user, "department", "") or "").strip()
	if not name or company is None:
		return None
	return Department.objects.filter(company=company, name__iexact=name).first()


def can_manage_finding(user, company, finding=None):
	"""Server-side guard for creating/updating a finding or its plans.

	Company owners and the ESG reporting manager manage every finding of their
	tenant. A department manager manages only the findings of their own
	department — the ones they own or that are filed against it.
	"""
	if company is None:
		return False
	if is_company_admin(user):
		return True
	if user.role == User.Role.DEPARTMENT_MANAGER:
		department = user_department(company, user)
		if finding is None:
			# A manager may only raise findings against their own department.
			return department is not None
		if finding.owner_id == getattr(user, "pk", None):
			return True
		return bool(department and finding.department_id == department.pk)
	return False


def visible_audits(user, company):
	"""Audits this user may see, scoped to their tenant."""
	if not can_view_module(user, company):
		return Audit.objects.none()
	return Audit.objects.for_company(company)


def visible_findings(user, company):
	"""Findings this user may see (all company roles read the whole register)."""
	if not can_view_module(user, company):
		return Finding.objects.none()
	return Finding.objects.for_company(company)


def department_choices(company):
	if company is None:
		return Department.objects.none()
	return Department.objects.filter(company=company).order_by("name")


def policy_choices(company):
	if company is None:
		return None
	from apps.governance.models import Policy

	return Policy.objects.filter(company=company).order_by("title")


# ---------------------------------------------------------------------- #
# Dashboard aggregation
# ---------------------------------------------------------------------- #
def dashboard_counts(company):
	"""Company-wide counters for the audit dashboard widgets."""
	audits = Audit.objects.for_company(company)
	findings = Finding.objects.for_company(company)
	open_findings = findings.open()
	return {
		"audits_total": audits.count(),
		"audits_open": audits.open().count(),
		"audits_completed": audits.filter(status=AuditStatus.COMPLETED).count(),
		"findings_total": findings.count(),
		"open_findings": open_findings.count(),
		"overdue_findings": findings.overdue().count(),
		"high_critical_findings": findings.high_severity().count(),
		"critical_findings": open_findings.filter(
			severity=FindingSeverity.CRITICAL
		).count(),
		"high_findings": open_findings.filter(severity=FindingSeverity.HIGH).count(),
		"closed_findings": findings.filter(status=FindingStatus.CLOSED).count(),
	}


def severity_breakdown(company):
	"""Open findings per severity, in a stable order for the dashboard."""
	rows = (
		Finding.objects.for_company(company)
		.open()
		.values("severity")
		.annotate(total=Count("id"))
	)
	counts = {row["severity"]: row["total"] for row in rows}
	return [
		{"value": value, "label": label, "count": counts.get(value, 0)}
		for value, label in FindingSeverity.choices
	]


def escalated_findings(company, limit=None):
	"""Open, overdue findings — the escalation queue, most overdue first."""
	queryset = (
		Finding.objects.for_company(company)
		.overdue()
		.select_related("audit", "department", "owner")
		.order_by("due_date", "id")
	)
	if limit:
		queryset = queryset[:limit]
	return list(queryset)


# ---------------------------------------------------------------------- #
# Workflows
# ---------------------------------------------------------------------- #
def _apply_transition(instance, target, transitions, allowed_errors):
	current = instance.status
	if target == current:
		raise ValidationError(f"This record is already {instance.get_status_display().lower()}.")
	if target not in transitions.get(current, ()):
		raise ValidationError(
			allowed_errors.get(current, "This status change is not allowed.")
		)
	instance.status = target
	return instance


def transition_audit(audit, target):
	"""Move an engagement through PLANNED -> IN_PROGRESS -> COMPLETED."""
	_apply_transition(
		audit,
		target,
		AUDIT_TRANSITIONS,
		{
			AuditStatus.PLANNED: "A planned audit can only be started or cancelled.",
			AuditStatus.IN_PROGRESS: "An audit in progress can only be completed or cancelled.",
			AuditStatus.COMPLETED: "This audit is already completed.",
			AuditStatus.CANCELLED: "This audit is already cancelled.",
		},
	)
	if audit.status == AuditStatus.COMPLETED and audit.end_date is None:
		audit.end_date = timezone.localdate()
	audit.save(update_fields=("status", "end_date", "updated_at"))
	return audit


def transition_finding(finding, target):
	"""Move a finding through OPEN -> IN_PROGRESS -> CLOSED.

	Closing always records ``closed_at`` (enforced by ``Finding.save``).
	"""
	_apply_transition(
		finding,
		target,
		FINDING_TRANSITIONS,
		{
			FindingStatus.OPEN: "An open finding can only be started or closed.",
			FindingStatus.IN_PROGRESS: "A finding in progress can only be closed.",
			FindingStatus.CLOSED: "This finding is already closed.",
		},
	)
	finding.save(update_fields=("status", "closed_at", "updated_at"))
	return finding


def transition_remediation(plan, target):
	"""Move a plan through PLANNED -> IN_PROGRESS -> DONE."""
	_apply_transition(
		plan,
		target,
		REMEDIATION_TRANSITIONS,
		{
			RemediationStatus.PLANNED: "A planned action can only be started or cancelled.",
			RemediationStatus.IN_PROGRESS: "An action in progress can only be completed or cancelled.",
			RemediationStatus.DONE: "This action is already completed.",
			RemediationStatus.CANCELLED: "This action is already cancelled.",
		},
	)
	plan.save(update_fields=("status", "completed_at", "updated_at"))
	return plan


__all__ = [
	"MANAGER_ROLES",
	"VIEWER_ROLES",
	"can_manage_audit",
	"can_manage_finding",
	"can_view_module",
	"company_for_user",
	"company_members",
	"dashboard_counts",
	"department_choices",
	"escalated_findings",
	"is_company_admin",
	"policy_choices",
	"severity_breakdown",
	"transition_audit",
	"transition_finding",
	"transition_remediation",
	"user_department",
	"visible_audits",
	"visible_findings",
]
