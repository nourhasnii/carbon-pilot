from django.contrib import admin

from apps.accounts.models import User

from .models import (
	ActivityAuditLog,
	AnomalyLog,
	CarbonActivity,
	EmissionFactor,
	EmissionFactorCompanyState,
)


class SuperAdminOnlyAdmin(admin.ModelAdmin):
	"""Carbon data is tenant-scoped; the Django admin stays a Super Admin tool."""

	def has_module_permission(self, request):
		return request.user.is_active and request.user.role == User.Role.SUPER_ADMIN

	def has_view_permission(self, request, obj=None):
		return self.has_module_permission(request)

	def has_add_permission(self, request):
		return self.has_module_permission(request)

	def has_change_permission(self, request, obj=None):
		return self.has_module_permission(request)

	def has_delete_permission(self, request, obj=None):
		return self.has_module_permission(request)


@admin.register(EmissionFactor)
class EmissionFactorAdmin(SuperAdminOnlyAdmin):
	list_display = (
		"name", "category", "scope", "value", "unit", "region", "year",
		"verification_status", "is_active",
	)
	list_filter = ("category", "scope", "verification_status", "is_active", "year")
	search_fields = ("name", "region", "source")
	ordering = ("category", "region", "name", "-year")
	readonly_fields = ("unit_label", "created_at", "updated_at")

	@admin.display(description="Factor unit")
	def unit_label(self, obj):
		return obj.unit_label


@admin.register(EmissionFactorCompanyState)
class EmissionFactorCompanyStateAdmin(SuperAdminOnlyAdmin):
	list_display = ("factor", "company", "is_active", "value", "unit", "updated_by", "updated_at")
	list_filter = ("is_active", "company")
	search_fields = ("factor__name", "company__name")
	ordering = ("-updated_at",)
	list_select_related = ("factor", "company", "updated_by")
	readonly_fields = ("updated_at",)


@admin.register(CarbonActivity)
class CarbonActivityAdmin(SuperAdminOnlyAdmin):
	list_display = (
		"display_name", "company", "category", "scope", "quantity", "unit",
		"co2e_kg", "status", "is_archived", "period_start",
	)
	list_filter = ("scope", "category", "status", "company", "is_archived")
	search_fields = ("name", "data_source", "company__name")
	ordering = ("-period_start", "-created_at")
	list_select_related = ("company", "department", "emission_factor")
	readonly_fields = (
		"co2e_kg", "emission_factor_value", "emission_factor_unit",
		"submitted_at", "reviewed_at", "approved_at",
		"archived_at", "archived_by", "created_at", "updated_at",
	)


@admin.register(ActivityAuditLog)
class ActivityAuditLogAdmin(SuperAdminOnlyAdmin):
	list_display = ("activity", "action", "actor", "from_status", "to_status", "created_at")
	list_filter = ("action", "created_at")
	search_fields = ("activity__name", "actor__email", "comment")
	ordering = ("-created_at",)
	list_select_related = ("activity", "actor")
	readonly_fields = ("activity", "actor", "action", "from_status", "to_status", "comment", "created_at")

	def has_add_permission(self, request):
		# The audit trail is append-only and written by the workflow itself.
		return False

	def has_change_permission(self, request, obj=None):
		return False

	def has_delete_permission(self, request, obj=None):
		return False


@admin.register(AnomalyLog)
class AnomalyLogAdmin(SuperAdminOnlyAdmin):
	list_display = (
		"company", "activity", "anomaly_type", "severity", "score",
		"status", "period_start", "detected_at",
	)
	list_filter = ("severity", "anomaly_type", "status", "company")
	search_fields = ("activity__name", "message", "company__name")
	ordering = ("-detected_at",)
	list_select_related = ("company", "department", "activity", "detected_by")
	readonly_fields = (
		"company", "department", "activity", "anomaly_type", "score", "severity",
		"message", "period_start", "period_end", "detected_at", "detected_by",
		"fingerprint", "created_at", "updated_at",
	)

	def has_add_permission(self, request):
		# Logs are produced by the anomaly-detection service, not by hand.
		return False
