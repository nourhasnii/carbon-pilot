"""Django admin registration for the ESG intelligence hub.

The catalog entry is the one model an operator configures globally (required
vs optional, active vs retired), so it is editable from the admin alongside the
tenant-scoped indicators and their history.
"""

from django.contrib import admin

from .models import (
	AssessmentSnapshot,
	Indicator,
	IndicatorCatalogEntry,
	IndicatorHistory,
	IndicatorValue,
)


@admin.register(IndicatorCatalogEntry)
class IndicatorCatalogEntryAdmin(admin.ModelAdmin):
	list_display = ("code", "name", "pillar", "unit", "is_required", "is_active", "company")
	list_filter = ("pillar", "is_required", "is_active")
	search_fields = ("code", "name")
	ordering = ("pillar", "sort_order", "code")


class IndicatorValueInline(admin.TabularInline):
	model = IndicatorValue
	extra = 0
	fields = ("period_year", "value", "unit", "status", "evidence_reference")
	ordering = ("-period_year",)


@admin.register(Indicator)
class IndicatorAdmin(admin.ModelAdmin):
	list_display = ("reference", "name", "pillar", "company", "department", "owner", "is_active")
	list_filter = ("catalog_entry__pillar", "is_active")
	search_fields = ("catalog_entry__code", "catalog_entry__name")
	inlines = (IndicatorValueInline,)


@admin.register(IndicatorValue)
class IndicatorValueAdmin(admin.ModelAdmin):
	list_display = ("indicator", "period_year", "value", "unit", "status", "company")
	list_filter = ("status", "period_year")
	search_fields = ("indicator__catalog_entry__code",)


@admin.register(IndicatorHistory)
class IndicatorHistoryAdmin(admin.ModelAdmin):
	list_display = ("indicator", "action", "period_year", "actor", "created_at")
	list_filter = ("action",)
	readonly_fields = ("indicator", "company", "actor", "action", "summary",
	                   "period_year", "from_status", "to_status", "created_at")


@admin.register(AssessmentSnapshot)
class AssessmentSnapshotAdmin(admin.ModelAdmin):
	list_display = ("company", "period_year", "overall_score", "readiness",
	                "delta_score", "created_at")
	list_filter = ("readiness", "period_year")
	readonly_fields = ("company", "period_year", "overall_score", "readiness",
	                   "environmental_score", "social_score", "governance_score",
	                   "summary", "breakdown_json", "gaps_json", "delta_score",
	                   "created_by", "created_at")
