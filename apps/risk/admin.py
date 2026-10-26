from django.contrib import admin

from apps.accounts.models import User

from .models import RiskAssessment


class SuperAdminOnlyAdmin(admin.ModelAdmin):
	"""Risk assessments are tenant-scoped; the Django admin stays a Super Admin tool."""

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


@admin.register(RiskAssessment)
class RiskAssessmentAdmin(SuperAdminOnlyAdmin):
	list_display = ("company", "score", "level", "period_year", "method", "created_at")
	list_filter = ("level", "method", "period_year")
	search_fields = ("company__name", "summary")
	readonly_fields = ("created_at",)
