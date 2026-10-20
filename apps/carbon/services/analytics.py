"""Aggregations powering the Carbon Management dashboard and footprint analysis.

Every figure is computed from the database from *approved* activities only —
never hardcoded. Functions take an already tenant- and role-scoped queryset.
"""

from decimal import Decimal, ROUND_HALF_UP
import calendar

from django.db.models import Count, Sum
from django.db.models.functions import TruncMonth

from apps.carbon.models import ActivityCategory, ActivityStatus, Scope
from apps.carbon.services import data_quality

_KG_PER_TONNE = Decimal("1000")
SUPPORTED_SCOPES = (Scope.SCOPE_1, Scope.SCOPE_2, Scope.SCOPE_3)


def to_tonnes(kilograms):
	if not kilograms:
		return Decimal("0.00")
	return (Decimal(kilograms) / _KG_PER_TONNE).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def total_tonnes(queryset):
	return to_tonnes(queryset.aggregate(total=Sum("co2e_kg"))["total"])


def _percent(part, whole):
	if not whole:
		return 0.0
	return round(float(part) / float(whole) * 100, 1)


def scope_breakdown(queryset):
	"""Totals and share per GHG Protocol scope."""
	rows = {
		row["scope"]: row["total"]
		for row in queryset.values("scope").annotate(total=Sum("co2e_kg"))
	}
	grand = sum((value or 0) for value in rows.values())
	result = []
	for scope in SUPPORTED_SCOPES:
		tonnes = to_tonnes(rows.get(scope))
		result.append(
			{
				"key": scope,
				"label": Scope(scope).label,
				"tonnes": tonnes,
				"percent": _percent(tonnes, to_tonnes(grand)),
			}
		)
	return result


def _grouped_breakdown(queryset, field, label_resolver):
	rows = (
		queryset.exclude(**{f"{field}__isnull": True})
		.values(field)
		.annotate(total=Sum("co2e_kg"), count=Count("id"))
		.order_by("-total")
	)
	grand = sum((row["total"] or 0) for row in rows)
	items = []
	for row in rows:
		tonnes = to_tonnes(row["total"])
		items.append(
			{
				"key": row[field],
				"label": label_resolver(row[field]),
				"tonnes": tonnes,
				"percent": _percent(tonnes, to_tonnes(grand)),
				"activities": row["count"],
			}
		)
	return items


def category_breakdown(queryset):
	return _grouped_breakdown(
		queryset, "category", lambda key: dict(ActivityCategory.choices).get(key, key)
	)


def department_breakdown(queryset):
	return _grouped_breakdown(
		queryset, "department__name", lambda key: key or "Unassigned"
	)


def monthly_series(queryset, year):
	"""Twelve-month emissions trend (tCO2e) for a reporting year."""
	rows = (
		queryset.filter(period_start__year=year)
		.annotate(month=TruncMonth("period_start"))
		.values("month")
		.annotate(total=Sum("co2e_kg"))
		.order_by("month")
	)
	buckets = {row["month"].month: to_tonnes(row["total"]) for row in rows if row["month"]}
	monthly = [
		{"month": month, "label": calendar.month_abbr[month], "tonnes": buckets.get(month, Decimal("0.00"))}
		for month in range(1, 13)
	]
	peak = max((entry["tonnes"] for entry in monthly), default=Decimal("0.00"))
	for entry in monthly:
		entry["height"] = round(float(entry["tonnes"]) / float(peak) * 100, 1) if peak else 0.0
	return monthly


def available_years(queryset, fallback):
	years = sorted(
		{value.year for value in queryset.dates("period_start", "year")}, reverse=True
	)
	return years or [fallback]


def year_comparison(queryset, year):
	"""Current vs previous year totals with a signed percentage change."""
	current = total_tonnes(queryset.filter(period_start__year=year))
	previous = total_tonnes(queryset.filter(period_start__year=year - 1))
	if previous:
		delta = (current - previous) / previous * 100
		delta_percent = round(float(delta), 1)
	else:
		delta_percent = None
	return {
		"year": year,
		"previous_year": year - 1,
		"current": current,
		"previous": previous,
		"delta_percent": delta_percent,
		"direction": "up" if previous and current > previous else ("down" if previous and current < previous else "flat"),
	}


def scope_total(queryset, scope):
	return to_tonnes(queryset.filter(scope=scope).aggregate(total=Sum("co2e_kg"))["total"])


def quality_summary(queryset):
	activities = list(queryset.select_related("emission_factor"))
	return {
		"average": data_quality.average_score(activities),
		"scored": len(activities),
	}


def open_review_count(queryset):
	return queryset.filter(
		status__in=(ActivityStatus.SUBMITTED, ActivityStatus.UNDER_REVIEW)
	).count()


def recent(queryset, limit=6):
	return queryset.order_by("-created_at")[:limit]
