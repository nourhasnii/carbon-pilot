"""Validation workflow for carbon activities.

Flow:  Employee → submit → Department Manager reviews and approves/rejects →
approved activity joins the official footprint. ESG Reporting Managers and
Company Owners may also approve any activity of their company.

Every transition is validated server-side and written to the append-only audit
trail. Callers must pass an already tenant-scoped activity.
"""

from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.services import notifications as notification_service

from apps.carbon.models import ActivityAuditAction, ActivityStatus
from apps.carbon.services.tenancy import company_for_user, is_company_admin

# Roles allowed to review (start review / reject / request correction).
REVIEWER_ROLES = (
	User.Role.DEPARTMENT_MANAGER,
	User.Role.ESG_REPORTING_MANAGER,
	User.Role.COMPANY_OWNER,
)

# Roles allowed to give final approval into the official footprint. A Department
# Manager may only approve activities of their own department (see can_approve).
APPROVER_ROLES = (
	User.Role.DEPARTMENT_MANAGER,
	User.Role.ESG_REPORTING_MANAGER,
	User.Role.COMPANY_OWNER,
)

OPEN_STATUSES = (ActivityStatus.SUBMITTED, ActivityStatus.UNDER_REVIEW)


class WorkflowError(Exception):
	"""Raised when a workflow transition is not allowed."""


def _belongs_to_user_company(user, activity):
	if user.role == User.Role.SUPER_ADMIN:
		return True
	company = company_for_user(user)
	return company is not None and activity.company_id == company.pk


def _is_department_manager_of(user, activity):
	"""A Department Manager only acts on activities of their own department."""
	return bool(user.department) and (
		activity.department is not None and activity.department.name == user.department
	)


def can_review(user, activity):
	if not _belongs_to_user_company(user, activity):
		return False
	if user.role == User.Role.SUPER_ADMIN:
		return True
	if user.role == User.Role.DEPARTMENT_MANAGER:
		return _is_department_manager_of(user, activity)
	return user.role in REVIEWER_ROLES


def can_approve(user, activity):
	if not _belongs_to_user_company(user, activity):
		return False
	if user.role == User.Role.SUPER_ADMIN:
		return True
	if user.role == User.Role.DEPARTMENT_MANAGER:
		return _is_department_manager_of(user, activity)
	return user.role in APPROVER_ROLES


def can_edit(user, activity):
	"""A draft or returned activity is editable by its creator and company admins."""
	if activity.is_archived:
		return False
	if activity.status not in (ActivityStatus.DRAFT, ActivityStatus.REJECTED):
		return False
	if not _belongs_to_user_company(user, activity):
		return False
	if user.role in (User.Role.SUPER_ADMIN,) or is_company_admin(user):
		return True
	return activity.created_by_id == user.pk


def actionable(user, activity):
	"""The set of actions a user may currently perform on an activity."""
	if activity.is_archived:
		return set()
	actions = set()
	if activity.status in (ActivityStatus.DRAFT, ActivityStatus.REJECTED) and can_edit(user, activity):
		actions.add("submit")
	if activity.status == ActivityStatus.SUBMITTED and can_review(user, activity):
		actions.update({"start_review", "reject", "request_correction"})
	if activity.status == ActivityStatus.UNDER_REVIEW and can_review(user, activity):
		actions.update({"reject", "request_correction"})
	if activity.status in OPEN_STATUSES and can_approve(user, activity):
		actions.add("approve")
	return actions


def _can_manage(user, activity):
	"""Shared ownership rule for archiving/restoring: admins or the reporter."""
	if user.role == User.Role.AUDITOR:
		return False
	if not _belongs_to_user_company(user, activity):
		return False
	if user.role == User.Role.SUPER_ADMIN or is_company_admin(user):
		return True
	return activity.created_by_id == user.pk


def can_delete(user, activity):
	"""Only an untouched draft may be hard-deleted, by its reporter or admins."""
	return activity.is_deletable and _can_manage(user, activity)


def can_archive(user, activity):
	"""Anything already in the workflow is archived instead of deleted."""
	return not activity.is_archived and _can_manage(user, activity)


def can_restore(user, activity):
	return activity.is_archived and _can_manage(user, activity)


def _require(condition, message):
	if not condition:
		raise WorkflowError(message)


def _activity_url(activity):
	"""Deep link back to the activity, used by in-app notifications."""
	try:
		return reverse("carbon:activity_detail", args=[activity.pk])
	except Exception:  # pragma: no cover - defensive, URLs are always registered
		return ""


def notify_department_managers_of_activity(activity, actor):
	"""Tell the department's managers an activity is waiting for their review.

	Public because both the review workflow and the "New activity" wizard can put
	an activity into SUBMITTED, and both must notify the same audience.
	"""
	department = activity.department.name if activity.department else ""
	notification_service.notify_department_managers(
		company=activity.company,
		department_name=department,
		title=f"New activity submitted: {activity.display_name}",
		message=(
			f"{activity.co2e_kg:.2f} kg CO2e is waiting for your review in "
			f"{department or 'your department'}."
		),
		url=_activity_url(activity),
		kind=notification_service.Notification.Kind.ACTIVITY_SUBMITTED,
		actor=actor,
		exclude=actor,
	)


def _notify_creator(activity, actor, kind, title, message):
	"""Notify the person who reported the activity about a decision."""
	creator = activity.created_by
	if creator is None or (actor is not None and creator.pk == actor.pk):
		return
	notification_service.notify(
		creator,
		title=title,
		message=message,
		url=_activity_url(activity),
		kind=kind,
		actor=actor,
	)


def submit_activity(activity, actor, comment=""):
	_require(
		activity.status in (ActivityStatus.DRAFT, ActivityStatus.REJECTED),
		"Only draft or returned activities can be submitted.",
	)
	_require(
		activity.co2e_kg is not None,
		"An emission factor is required before submitting for review.",
	)
	previous = activity.status
	activity.status = ActivityStatus.SUBMITTED
	activity.submitted_at = timezone.now()
	activity.save()
	activity.log_event(
		actor=actor,
		action=ActivityAuditAction.SUBMITTED,
		comment=comment,
		from_status=previous,
		to_status=ActivityStatus.SUBMITTED,
	)
	notify_department_managers_of_activity(activity, actor)
	return activity


def start_review(activity, actor, comment=""):
	_require(can_review(actor, activity), "You are not allowed to review this activity.")
	_require(
		activity.status == ActivityStatus.SUBMITTED,
		"Only submitted activities can enter review.",
	)
	previous = activity.status
	activity.status = ActivityStatus.UNDER_REVIEW
	activity.reviewed_by = actor
	activity.reviewed_at = timezone.now()
	activity.save()
	activity.log_event(
		actor=actor,
		action=ActivityAuditAction.REVIEW_STARTED,
		comment=comment,
		from_status=previous,
		to_status=ActivityStatus.UNDER_REVIEW,
	)
	return activity


def approve_activity(activity, actor, comment=""):
	_require(can_approve(actor, activity), "You are not allowed to approve this activity.")
	_require(
		activity.status in OPEN_STATUSES,
		"Only submitted or in-review activities can be approved.",
	)
	previous = activity.status
	activity.status = ActivityStatus.APPROVED
	activity.approved_by = actor
	activity.approved_at = timezone.now()
	activity.reviewed_by = activity.reviewed_by or actor
	activity.reviewed_at = activity.reviewed_at or timezone.now()
	activity.review_comment = (comment or "").strip()
	activity.save()
	activity.log_event(
		actor=actor,
		action=ActivityAuditAction.APPROVED,
		comment=comment,
		from_status=previous,
		to_status=ActivityStatus.APPROVED,
	)
	_notify_creator(
		activity,
		actor,
		notification_service.Notification.Kind.ACTIVITY_APPROVED,
		title=f"Activity approved: {activity.display_name}",
		message="Your activity was approved and now counts in the official carbon KPIs.",
	)
	return activity


def reject_activity(activity, actor, comment=""):
	_require(can_review(actor, activity), "You are not allowed to reject this activity.")
	_require(
		activity.status in OPEN_STATUSES,
		"Only submitted or in-review activities can be rejected.",
	)
	_require((comment or "").strip(), "A rejection reason is required.")
	previous = activity.status
	activity.status = ActivityStatus.REJECTED
	activity.reviewed_by = actor
	activity.reviewed_at = timezone.now()
	activity.review_comment = comment.strip()
	activity.save()
	activity.log_event(
		actor=actor,
		action=ActivityAuditAction.REJECTED,
		comment=comment,
		from_status=previous,
		to_status=ActivityStatus.REJECTED,
	)
	_notify_creator(
		activity,
		actor,
		notification_service.Notification.Kind.ACTIVITY_REJECTED,
		title=f"Activity rejected: {activity.display_name}",
		message=comment.strip() or "Your activity was rejected. Open it to see the details.",
	)
	return activity


def request_correction(activity, actor, comment=""):
	_require(can_review(actor, activity), "You are not allowed to return this activity.")
	_require(
		activity.status in OPEN_STATUSES,
		"Only submitted or in-review activities can be returned for correction.",
	)
	_require((comment or "").strip(), "Explain what needs to be corrected.")
	previous = activity.status
	activity.status = ActivityStatus.DRAFT
	activity.reviewed_by = actor
	activity.reviewed_at = timezone.now()
	activity.review_comment = comment.strip()
	activity.save()
	activity.log_event(
		actor=actor,
		action=ActivityAuditAction.CORRECTION_REQUESTED,
		comment=comment,
		from_status=previous,
		to_status=ActivityStatus.DRAFT,
	)
	return activity


def delete_activity(activity, actor, comment=""):
	"""Hard-delete an untouched draft. Other statuses must be archived instead."""
	_require(
		can_delete(actor, activity),
		"Only draft activities can be deleted. Archive submitted, reviewed or "
		"approved activities instead to keep the audit trail.",
	)
	activity.delete()


def archive_activity(activity, actor, comment=""):
	"""Soft-delete an activity while keeping its record and audit trail."""
	_require(can_archive(actor, activity), "You are not allowed to archive this activity.")
	activity.is_archived = True
	activity.archived_at = timezone.now()
	activity.archived_by = actor if getattr(actor, "is_authenticated", False) else None
	activity.save()
	activity.log_event(
		actor=actor,
		action=ActivityAuditAction.ARCHIVED,
		comment=comment,
		from_status=activity.status,
		to_status=activity.status,
	)
	return activity


def restore_activity(activity, actor, comment=""):
	"""Bring an archived activity back into the live inventory."""
	_require(can_restore(actor, activity), "You are not allowed to restore this activity.")
	activity.is_archived = False
	activity.archived_at = None
	activity.archived_by = None
	activity.save()
	activity.log_event(
		actor=actor,
		action=ActivityAuditAction.RESTORED,
		comment=comment,
		from_status=activity.status,
		to_status=activity.status,
	)
	return activity


# Human-readable label + required-comment flag, used by the review UI.
ACTION_DEFINITIONS = {
	"submit": {"label": "Submit for review", "requires_comment": False, "style": "primary"},
	"start_review": {"label": "Start review", "requires_comment": False, "style": "outline"},
	"approve": {"label": "Approve", "requires_comment": False, "style": "primary"},
	"reject": {"label": "Reject", "requires_comment": True, "style": "danger"},
	"request_correction": {"label": "Request correction", "requires_comment": True, "style": "outline"},
}


def perform(action, activity, actor, comment=""):
	"""Dispatch a named workflow action. Raises ``WorkflowError`` when invalid."""
	handlers = {
		"submit": submit_activity,
		"start_review": start_review,
		"approve": approve_activity,
		"reject": reject_activity,
		"request_correction": request_correction,
	}
	handler = handlers.get(action)
	if handler is None:
		raise WorkflowError("Unknown workflow action.")
	if action not in actionable(actor, activity):
		raise WorkflowError("This action is not available on the current activity status.")
	if action == "submit":
		_require(can_edit(actor, activity), "You are not allowed to submit this activity.")
	return handler(activity, actor, comment)
