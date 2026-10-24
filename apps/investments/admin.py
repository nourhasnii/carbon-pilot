from django.contrib import admin

from .models import DecarbonizationProject, ImpactAssessment, Investment, ProjectScenario


@admin.register(DecarbonizationProject)
class DecarbonizationProjectAdmin(admin.ModelAdmin):
	list_display = ("name", "company", "department", "project_type", "status", "start_date", "end_date", "is_demo")
	list_filter = ("project_type", "status", "is_demo")
	search_fields = ("name", "company__name", "department__name")
	readonly_fields = ("created_at", "updated_at")


@admin.register(Investment)
class InvestmentAdmin(admin.ModelAdmin):
	list_display = ("project", "planned_budget", "actual_cost", "committed_spend", "currency", "date")
	list_filter = ("currency", "funding_source")
	search_fields = ("project__name", "project__company__name", "funding_source")
	readonly_fields = ()
	list_select_related = ("project", "project__company")


@admin.register(ImpactAssessment)
class ImpactAssessmentAdmin(admin.ModelAdmin):
	list_display = (
		"project",
		"estimated_tco2e_per_year",
		"estimated_kwh_saved_per_year",
		"annual_savings",
		"lifetime_years",
		"created_at",
	)
	search_fields = ("project__name", "project__company__name")
	readonly_fields = ("created_at",)
	list_select_related = ("project", "project__company")


@admin.register(ProjectScenario)
class ProjectScenarioAdmin(admin.ModelAdmin):
	list_display = (
		"name",
		"project",
		"investment",
		"impact",
		"cost_multiplier",
		"reduction_multiplier",
		"energy_price",
	)
	search_fields = ("name", "project__name", "project__company__name")
	list_select_related = ("project", "investment", "impact")
