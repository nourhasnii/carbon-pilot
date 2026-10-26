from django.urls import path

from . import views

app_name = "audits"

urlpatterns = [
	path("", views.audit_list, name="audit_list"),
	path("new/", views.audit_create, name="audit_create"),

	# Findings (declared before the audit detail pattern).
	path("findings/<int:pk>/", views.finding_detail, name="finding_detail"),
	path("findings/<int:pk>/edit/", views.finding_edit, name="finding_edit"),
	path("findings/<int:pk>/status/", views.finding_set_status, name="finding_set_status"),
	path("findings/<int:pk>/evidence/", views.finding_evidence_upload, name="finding_evidence_upload"),
	path("findings/<int:pk>/remediation/new/", views.remediation_create, name="remediation_create"),

	# Remediation plans.
	path("remediation/<int:pk>/edit/", views.remediation_edit, name="remediation_edit"),
	path("remediation/<int:pk>/status/", views.remediation_set_status, name="remediation_set_status"),

	# Evidence.
	path("evidence/<int:pk>/download/", views.evidence_download, name="evidence_download"),
	path("evidence/<int:pk>/delete/", views.evidence_delete, name="evidence_delete"),

	# AI analysis (declared before the audit detail pattern).
	path("ai-analysis/", views.audit_ai_analysis, name="audit_ai_analysis"),

	# Audits.
	path("<int:pk>/", views.audit_detail, name="audit_detail"),
	path("<int:pk>/edit/", views.audit_edit, name="audit_edit"),
	path("<int:pk>/status/", views.audit_set_status, name="audit_set_status"),
	path("<int:pk>/evidence/", views.audit_evidence_upload, name="audit_evidence_upload"),
	path("<int:pk>/closing-note/", views.audit_closing_note, name="audit_closing_note"),
	path("<int:pk>/findings/new/", views.finding_create, name="finding_create"),
]
