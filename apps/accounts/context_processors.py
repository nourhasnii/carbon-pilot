"""Template context shared by every authenticated shell (top-bar bell)."""

from .services import notifications as notification_service


def notifications(request):
	user = getattr(request, "user", None)
	if user is None or not getattr(user, "is_authenticated", False):
		return {}
	return {
		"nav_notifications": notification_service.recent(user),
		"nav_unread_count": notification_service.unread_count(user),
	}
