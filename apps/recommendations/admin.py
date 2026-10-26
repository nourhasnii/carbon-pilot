from django.contrib import admin

from .models import RecommendationLog


@admin.register(RecommendationLog)
class RecommendationLogAdmin(admin.ModelAdmin):
	list_display = (
		"title", "company", "priority", "status", "source", "generation_method", "created_at",
	)
	list_filter = ("priority", "status", "source", "generation_method")
	search_fields = ("title", "message", "company__name")
	list_select_related = ("company", "department", "objective", "anomaly")
	ordering = ("-created_at",)
	readonly_fields = (
		"company", "department", "objective", "anomaly", "source", "generation_method",
		"model_name", "evidence", "fingerprint", "created_at", "updated_at",
	)
