from django.contrib import admin

from apps.accounts.models import User

from .models import Supplier, SupplierDocument


class SuperAdminOnlyAdmin(admin.ModelAdmin):
	"""Suppliers are tenant-scoped; the Django admin stays a Super Admin tool."""

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


@admin.register(Supplier)
class SupplierAdmin(SuperAdminOnlyAdmin):
	list_display = ("name", "company", "country", "category", "esg_status", "risk_level", "is_active")
	list_filter = ("esg_status", "risk_level", "is_active", "company")
	search_fields = ("name", "legal_name", "country", "category", "contact_email")
	list_select_related = ("company",)


@admin.register(SupplierDocument)
class SupplierDocumentAdmin(SuperAdminOnlyAdmin):
	list_display = ("title", "supplier", "company", "document_type", "uploaded_by", "uploaded_at")
	list_filter = ("document_type", "company")
	search_fields = ("title", "supplier__name", "company__name")
	list_select_related = ("supplier", "company", "uploaded_by")
