from django.contrib import admin

from apps.accounts.models import User

from .models import ESGReport


class SuperAdminOnlyAdmin(admin.ModelAdmin):
	"""ESG reports are tenant-scoped; the Django admin stays a Super Admin tool."""

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


@admin.register(ESGReport)
class ESGReportAdmin(SuperAdminOnlyAdmin):
	list_display = ("title", "company", "period_year", "status", "created_by", "created_at")
	list_filter = ("status", "period_year", "company")
	search_fields = ("title", "company__name", "summary")
	readonly_fields = ("metrics_json", "created_at")
	list_select_related = ("company", "created_by")
