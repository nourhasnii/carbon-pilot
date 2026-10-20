"""Small read helpers for recommendation summaries shown across the app."""

from ..models import OPEN_STATUSES, RecommendationLog
from .access import visible_recommendations


def open_recommendations(user, company):
	"""Open (NEW / IN_PROGRESS) recommendations the user may see."""
	return visible_recommendations(user, company).filter(status__in=OPEN_STATUSES)


def summary(user, company, *, limit=3):
	"""A compact overview for the Carbon Overview dashboard card."""
	queryset = open_recommendations(user, company)
	return {
		"total": queryset.count(),
		"high": queryset.filter(priority="HIGH").count(),
		"recent": list(
			queryset.select_related("department", "objective", "anomaly")[:limit]
		),
	}


def for_objective(objective, *, limit=3):
	"""Open recommendations linked to one objective (company-safe via the FK)."""
	return list(
		RecommendationLog.objects.filter(objective=objective)
		.filter(status__in=OPEN_STATUSES)
		.order_by("-created_at")[:limit]
	)
