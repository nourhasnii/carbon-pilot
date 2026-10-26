"""In-app notification helpers.

Notifications are always addressed to a single user and are never shared across
tenants. Email delivery is intentionally out of scope here: the entry points
below are the hook where a future email channel can be plugged in without
touching the callers.
"""

from django.db.models import Q

from apps.accounts.models import Notification, User


def notify(recipient, *, title, message="", url="", kind=Notification.Kind.GENERIC, actor=None):
	"""Create one in-app notification for ``recipient``."""
	if recipient is None:
		return None
	return Notification.objects.create(
		recipient=recipient,
		actor=actor if getattr(actor, "is_authenticated", False) else None,
		kind=kind,
		title=title,
		message=message,
		url=url,
	)


def notify_many(recipients, *, exclude=None, **kwargs):
	"""Notify several users once each, skipping ``exclude`` and duplicates."""
	seen = set()
	created = []
	exclude_pk = getattr(exclude, "pk", None)
	for recipient in recipients:
		if recipient is None or recipient.pk == exclude_pk or recipient.pk in seen:
			continue
		seen.add(recipient.pk)
		created.append(notify(recipient, **kwargs))
	return created


def department_managers(company, department_name):
	"""Active Department Managers of ``department_name`` inside ``company``."""
	if company is None or not department_name:
		return User.objects.none()
	return User.objects.filter(
		Q(company_membership=company) | Q(company__iexact=company.name),
		role=User.Role.DEPARTMENT_MANAGER,
		status=User.Status.ACTIVE,
		department=department_name,
	).distinct()


def notify_department_managers(company, department_name, *, exclude=None, **kwargs):
	"""Notify every manager of a department (used when an activity is submitted)."""
	return notify_many(
		department_managers(company, department_name), exclude=exclude, **kwargs
	)


def unread_count(user):
	if not getattr(user, "is_authenticated", False):
		return 0
	return Notification.objects.filter(recipient=user, is_read=False).count()


def recent(user, limit=8):
	"""Latest notifications for the bell dropdown."""
	if not getattr(user, "is_authenticated", False):
		return []
	return list(
		Notification.objects.filter(recipient=user).select_related("actor")[:limit]
	)


def mark_all_read(user):
	if not getattr(user, "is_authenticated", False):
		return 0
	return Notification.objects.filter(recipient=user, is_read=False).update(is_read=True)
