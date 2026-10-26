from django.contrib import admin

from .models import Audit, AuditEvidence, Finding, RemediationPlan


class FindingInline(admin.TabularInline):
	model = Finding
	extra = 0
	fields = ("title", "severity", "status", "department", "owner", "due_date")
	readonly_fields = ("closed_at",)


class AuditEvidenceInline(admin.TabularInline):
	model = AuditEvidence
	extra = 0
	fields = ("title", "file", "uploaded_by")
	readonly_fields = ("uploaded_at",)


@admin.register(Audit)
class AuditAdmin(admin.ModelAdmin):
	list_display = (
		"title", "company", "audit_type", "status", "department",
		"start_date", "end_date", "lead_auditor",
	)
	list_filter = ("status", "audit_type")
	search_fields = ("title", "company__name", "scope_summary")
	readonly_fields = ("created_at", "updated_at")
	list_select_related = ("company", "department", "related_policy", "lead_auditor")
	inlines = (FindingInline, AuditEvidenceInline)


@admin.register(Finding)
class FindingAdmin(admin.ModelAdmin):
	list_display = (
		"title", "company", "audit", "severity", "status",
		"department", "owner", "due_date", "closed_at",
	)
	list_filter = ("severity", "status")
	search_fields = ("title", "description", "audit__title", "company__name")
	readonly_fields = ("created_at", "updated_at", "closed_at")
	list_select_related = ("company", "audit", "department", "owner")


@admin.register(RemediationPlan)
class RemediationPlanAdmin(admin.ModelAdmin):
	list_display = (
		"title", "company", "finding", "status", "owner", "due_date", "completed_at",
	)
	list_filter = ("status",)
	search_fields = ("title", "description", "finding__title", "company__name")
	readonly_fields = ("created_at", "updated_at", "completed_at")
	list_select_related = ("company", "finding", "owner")


@admin.register(AuditEvidence)
class AuditEvidenceAdmin(admin.ModelAdmin):
	list_display = ("title", "company", "audit", "finding", "uploaded_by", "uploaded_at")
	search_fields = ("title", "company__name")
	readonly_fields = ("uploaded_at",)
	list_select_related = ("company", "audit", "finding", "uploaded_by")
