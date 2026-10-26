from django.contrib import admin

from .models import Policy, PolicyApproval, PolicyOwner, PolicyVersion


class PolicyVersionInline(admin.TabularInline):
	model = PolicyVersion
	extra = 0
	readonly_fields = ("created_at",)


class PolicyOwnerInline(admin.TabularInline):
	model = PolicyOwner
	extra = 0
	readonly_fields = ("created_at",)


@admin.register(Policy)
class PolicyAdmin(admin.ModelAdmin):
	list_display = (
		"title", "code", "company", "category", "status", "next_review_date", "is_active",
	)
	list_filter = ("status", "category", "is_active")
	search_fields = ("title", "code", "company__name")
	readonly_fields = ("created_at", "updated_at")
	inlines = (PolicyVersionInline, PolicyOwnerInline)


@admin.register(PolicyVersion)
class PolicyVersionAdmin(admin.ModelAdmin):
	list_display = ("policy", "version_number", "is_current", "effective_from", "created_by", "created_at")
	list_filter = ("is_current",)
	search_fields = ("policy__title", "policy__company__name")
	list_select_related = ("policy", "created_by")


@admin.register(PolicyApproval)
class PolicyApprovalAdmin(admin.ModelAdmin):
	list_display = ("policy", "policy_version", "approver", "decision", "decided_at", "created_at")
	list_filter = ("decision",)
	search_fields = ("policy__title", "approver__email")
	list_select_related = ("policy", "policy_version", "approver")


@admin.register(PolicyOwner)
class PolicyOwnerAdmin(admin.ModelAdmin):
	list_display = ("policy", "company", "department", "user", "responsibility_role", "created_at")
	list_filter = ("responsibility_role",)
	search_fields = ("policy__title", "user__email", "department__name")
	list_select_related = ("policy", "company", "department", "user")
