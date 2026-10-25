from django.contrib import admin

from .models import Objective, PredictionRun


@admin.register(Objective)
class ObjectiveAdmin(admin.ModelAdmin):
	list_display = ("name", "company", "department", "year", "period_type", "quarter", "status")
	list_filter = ("status", "period_type", "year")
	search_fields = ("name", "company__name", "department__name")
	readonly_fields = ("created_at", "updated_at")


@admin.register(PredictionRun)
class PredictionRunAdmin(admin.ModelAdmin):
	list_display = (
		"objective", "probability_achievement", "risk_level",
		"expected_outcome", "model_name", "model_version", "created_at",
	)
	list_filter = ("risk_level", "expected_outcome", "model_name")
	search_fields = ("objective__name", "objective__company__name")
	ordering = ("-created_at",)
	list_select_related = ("objective", "objective__company")
	readonly_fields = (
		"objective", "probability_achievement", "risk_level", "expected_outcome",
		"current_progress_percent", "model_name", "model_version", "created_at",
	)

	def has_add_permission(self, request):
		# Predictions are produced by the ML service, never entered by hand.
		return False
