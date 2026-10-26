from django.contrib import admin

from .models import AiJob, ModelMetrics


@admin.register(ModelMetrics)
class ModelMetricsAdmin(admin.ModelAdmin):
	"""Read-only audit view of what each ML run saw and how it scored."""

	list_display = (
		"company", "model_kind", "model_name", "model_version",
		"training_size", "anomalies_found", "insufficient_data", "created_at",
	)
	list_filter = ("model_kind", "insufficient_data", "model_name")
	search_fields = ("company__name", "note")
	ordering = ("-created_at",)
	list_select_related = ("company",)
	readonly_fields = (
		"company", "model_kind", "model_name", "model_version", "accuracy",
		"precision", "recall", "f1", "training_size", "class_balance",
		"activities_analyzed", "groups_analyzed", "groups_skipped",
		"anomalies_found", "params", "insufficient_data", "note", "created_at",
	)

	def has_add_permission(self, request):
		# Snapshots are produced by the ML services, never entered by hand.
		return False

	def has_change_permission(self, request, obj=None):
		return False

	def has_delete_permission(self, request, obj=None):
		return False


@admin.register(AiJob)
class AiJobAdmin(admin.ModelAdmin):
	"""Read-only view of background AI jobs — the first place to look when a
	user reports "the AI job failed" (``error_code`` names the cause)."""

	list_display = (
		"id", "company", "job_type", "status", "error_code", "created_by",
		"created_at", "finished_at",
	)
	list_filter = ("job_type", "status", "error_code")
	search_fields = ("company__name", "celery_task_id", "message")
	ordering = ("-created_at",)
	list_select_related = ("company", "created_by")
	readonly_fields = (
		"company", "created_by", "job_type", "status", "celery_task_id",
		"message", "error_code", "result", "created_at", "started_at",
		"finished_at",
	)

	def has_add_permission(self, request):
		# Jobs are created by the enqueue seam, never entered by hand.
		return False

	def has_change_permission(self, request, obj=None):
		return False

	def has_delete_permission(self, request, obj=None):
		return False
