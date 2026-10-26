from django.contrib import admin
from django.contrib import messages

from .models import AdminActivity, User
from .services.account_workflow import (
	record_admin_activity,
	resend_approval_email,
	transition_account_status,
)


@admin.register(User)
class AccountAdmin(admin.ModelAdmin):
	list_display = ("email", "first_name", "last_name", "role", "company", "department", "status", "created_at")
	list_filter = ("role", "status")
	search_fields = ("email", "first_name", "last_name", "company", "department")
	ordering = ("-created_at",)
	readonly_fields = ("status", "created_at", "updated_at")
	fields = (
		"email", "first_name", "last_name", "role", "company", "department",
		"status", "created_at", "updated_at",
	)
	actions = (
		"approve_users", "resend_approval_credentials", "reject_users",
		"deactivate_users", "reactivate_users",
	)

	def formfield_for_choice_field(self, db_field, request, **kwargs):
		if db_field.name == "role":
			kwargs["choices"] = [
				choice for choice in User.Role.choices if choice[0] != User.Role.SUPER_ADMIN
			]
		return super().formfield_for_choice_field(db_field, request, **kwargs)

	def save_model(self, request, obj, form, change):
		if not change:
			obj.status = User.Status.PENDING
			obj.is_staff = False
			obj.is_superuser = False
			obj.set_unusable_password()
		super().save_model(request, obj, form, change)
		if not change:
			record_admin_activity(request.user, AdminActivity.Action.CREATE_ACCOUNT, obj)

	def get_readonly_fields(self, request, obj=None):
		fields = list(super().get_readonly_fields(request, obj))
		if obj and obj.role == User.Role.SUPER_ADMIN:
			fields.append("role")
		return fields

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

	def _apply_status_action(self, request, queryset, status, label):
		updated = 0
		email_failures = 0
		for user in queryset.exclude(role=User.Role.SUPER_ADMIN):
			changed, email_sent = transition_account_status(user, status)
			if changed:
				action_by_label = {
					"approved": AdminActivity.Action.APPROVE_ACCOUNT,
					"rejected": AdminActivity.Action.REJECT_ACCOUNT,
					"deactivated": AdminActivity.Action.DEACTIVATE_ACCOUNT,
					"reactivated": AdminActivity.Action.REACTIVATE_ACCOUNT,
				}
				record_admin_activity(request.user, action_by_label[label], user)
			updated += bool(changed)
			email_failures += email_sent is False
		if email_failures:
			self.message_user(
				request,
				f"{updated} account(s) updated; {email_failures} notification email(s) failed.",
				level=messages.WARNING,
			)
		else:
			self.message_user(request, f"{updated} account(s) {label}.", level=messages.SUCCESS)

	@admin.action(description="Approve selected pending accounts")
	def approve_users(self, request, queryset):
		self._apply_status_action(request, queryset, User.Status.ACTIVE, "approved")

	@admin.action(description="Resend temporary approval credentials")
	def resend_approval_credentials(self, request, queryset):
		updated = 0
		email_failures = 0
		for user in queryset.exclude(role=User.Role.SUPER_ADMIN):
			changed, email_sent = resend_approval_email(user)
			if changed:
				record_admin_activity(
					request.user, AdminActivity.Action.RESEND_APPROVAL, user
				)
			updated += bool(changed)
			email_failures += email_sent is False
		if email_failures:
			self.message_user(
				request,
				f"{updated} credential email(s) retried; {email_failures} failed.",
				level=messages.WARNING,
			)
		else:
			self.message_user(
				request, f"{updated} credential email(s) resent.", level=messages.SUCCESS
			)

	@admin.action(description="Reject selected pending accounts")
	def reject_users(self, request, queryset):
		self._apply_status_action(request, queryset, User.Status.REJECTED, "rejected")

	@admin.action(description="Deactivate selected active accounts")
	def deactivate_users(self, request, queryset):
		self._apply_status_action(request, queryset, User.Status.INACTIVE, "deactivated")

	@admin.action(description="Reactivate selected inactive accounts")
	def reactivate_users(self, request, queryset):
		self._apply_status_action(request, queryset, User.Status.ACTIVE, "reactivated")
