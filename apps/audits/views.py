"""Internal audit & findings management views.

Tenant isolation mirrors the rest of CarbonPilot: the company is always
resolved from the authenticated user (never from the request body), and every
audit, finding, remediation plan or evidence file is fetched with
``company=company`` so another tenant's record is a 404 rather than a 403.
Every state change is guarded server-side.
"""

import os

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Count, Q
from django.http import FileResponse, Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_http_methods, require_POST

from apps.accounts.models import User
from apps.accounts.permissions import role_required
from apps.carbon.views import _sidebar as carbon_sidebar
from apps.common.listing import paginate

from . import ai
from .forms import (
	AuditEvidenceForm,
	AuditFilterForm,
	AuditForm,
	FindingForm,
	RemediationPlanForm,
)
from .models import (
	OPEN_FINDING_STATUSES,
	Audit,
	AuditEvidence,
	AuditStatus,
	Finding,
	FindingStatus,
	RemediationPlan,
	RemediationStatus,
)
from .services import (
	AUDIT_TRANSITIONS,
	FINDING_TRANSITIONS,
	VIEWER_ROLES,
	can_manage_audit,
	can_manage_finding,
	company_for_user,
	dashboard_counts,
	escalated_findings,
	severity_breakdown,
	transition_audit,
	transition_finding,
	transition_remediation,
	user_department,
	visible_audits,
)

ACTIVE_NAV = "Internal audit"
ESCALATION_LIMIT = 5

# The last AI analysis lives in the session (not the database): it is a
# read-only projection of the tenant's own findings, produced on demand.
AI_SESSION_KEY = "audits_ai_analysis"
CLOSING_SESSION_KEY = "audits_closing_notes"


def _module_context(request, company, active=ACTIVE_NAV):
	return {
		"company": company,
		"sidebar_nav": carbon_sidebar(active, request.user),
	}


def _require_company(request):
	"""Resolve the tenant from the user; never trusts client input."""
	company = company_for_user(request.user)
	if company is None:
		messages.warning(request, "Set up your company before managing audits.")
	return company


def _redirect_no_company():
	return redirect(f"{reverse('accounts:dashboard')}?section=company")


def _generated_at_display(value):
	"""Human-readable local timestamp for the stored ISO string."""
	parsed = parse_datetime(value) if value else None
	if parsed is None:
		return value or ""
	return timezone.localtime(parsed).strftime("%d %b %Y, %H:%M")


def _enrich_rows(rows, findings):
	"""Attach a finding's title/severity to each AI row for rendering.

	The model only ever returns a ``finding_id``; the title shown in the UI is
	looked up from this company's own rows, so nothing is invented.
	"""
	enriched = []
	for row in rows:
		entry = dict(row)
		finding = findings.get(row["finding_id"])
		entry["finding_title"] = (
			finding.title if finding else f"Finding #{row['finding_id']}"
		)
		entry["finding_severity"] = finding.severity if finding else ""
		enriched.append(entry)
	return enriched


def _remember_analysis(request, analysis, company):
	"""Keep the latest AI audit analysis so the pages can render it."""
	findings = {
		finding.pk: finding
		for finding in Finding.objects.for_company(company).only(
			"id", "title", "severity"
		)
	}
	request.session[AI_SESSION_KEY] = {
		"company_id": analysis.company_id,
		"ai_available": analysis.ai_available,
		"model": analysis.model,
		"generated_at": analysis.generated_at,
		"generated_at_display": _generated_at_display(analysis.generated_at),
		"message": analysis.message,
		"classifications": _enrich_rows(analysis.classifications, findings),
		"delay_risk": _enrich_rows(analysis.delay_risk, findings),
		"rejected": analysis.rejected,
	}


def _ai_analysis(request, company):
	"""Last analysis for the *current* tenant only, or ``None``."""
	stored = request.session.get(AI_SESSION_KEY)
	if not isinstance(stored, dict):
		return None
	if stored.get("company_id") != company.pk:
		# Never leak another tenant's analysis if the session user switched company.
		return None
	return stored


def _decorate_findings(findings, analysis):
	"""Attach the live delay risk and the last AI theme to each finding.

	The deterministic rules always run, so a badge is available even before an
	analysis has been requested; the AI theme is added only when Qwen classified
	that finding.
	"""
	themes = {}
	delays = {}
	if analysis:
		themes = {row["finding_id"]: row for row in analysis.get("classifications") or []}
		delays = {row["finding_id"]: row for row in analysis.get("delay_risk") or []}
	rows = list(findings)
	for finding in rows:
		finding.ai_theme = themes.get(finding.pk)
		finding.delay_risk = delays.get(finding.pk) or ai.score_delay_risk(finding)
	return rows


def _remember_note(request, note):
	"""Keep the latest AI closing note for each audit of this session."""
	store = request.session.get(CLOSING_SESSION_KEY)
	if not isinstance(store, dict):
		store = {}
	store[str(note.audit_id)] = {
		"company_id": note.company_id,
		"audit_id": note.audit_id,
		"ai_available": note.ai_available,
		"model": note.model,
		"generated_at_display": _generated_at_display(note.generated_at),
		"message": note.message,
		"note": note.note,
	}
	request.session[CLOSING_SESSION_KEY] = store


def _closing_note(request, company, audit):
	"""The stored closing note for this audit, or ``None`` (tenant-guarded)."""
	store = request.session.get(CLOSING_SESSION_KEY)
	if not isinstance(store, dict):
		return None
	stored = store.get(str(audit.pk))
	if not isinstance(stored, dict):
		return None
	if stored.get("company_id") != company.pk or stored.get("audit_id") != audit.pk:
		return None
	return stored


def _scoped_audit_or_404(company, pk):
	audit = (
		Audit.objects.select_related("department", "related_policy", "lead_auditor")
		.filter(company=company, pk=pk)
		.first()
	)
	if audit is None:
		raise Http404("Audit not found.")
	return audit


def _scoped_finding_or_404(company, pk):
	finding = (
		Finding.objects.select_related("audit", "department", "related_policy", "owner")
		.filter(company=company, pk=pk)
		.first()
	)
	if finding is None:
		raise Http404("Finding not found.")
	return finding


def _scoped_plan_or_404(company, pk):
	plan = (
		RemediationPlan.objects.select_related("finding", "owner")
		.filter(company=company, pk=pk)
		.first()
	)
	if plan is None:
		raise Http404("Remediation action not found.")
	return plan


def _scoped_evidence_or_404(company, pk):
	evidence = (
		AuditEvidence.objects.select_related("audit", "finding")
		.filter(company=company, pk=pk)
		.first()
	)
	if evidence is None:
		raise Http404("Evidence not found.")
	return evidence


def _restrict_department(request, company):
	"""The department a Department Manager is confined to (``None`` otherwise)."""
	if request.user.role != User.Role.DEPARTMENT_MANAGER:
		return None
	return user_department(company, request.user)


def _apply_filters(queryset, form):
	if not form.is_valid():
		return queryset
	data = form.cleaned_data
	if data.get("status"):
		queryset = queryset.filter(status=data["status"])
	if data.get("audit_type"):
		queryset = queryset.filter(audit_type=data["audit_type"])
	department = data.get("department") or ""
	if department.isdigit():
		queryset = queryset.filter(department_id=int(department))
	return queryset


def _next_statuses(choices, transitions, current):
	allowed = transitions.get(current, ())
	return [{"value": value, "label": label} for value, label in choices if value in allowed]


def _form_error_message(form, fallback):
	errors = [
		error
		for field_errors in form.errors.values()
		for error in field_errors
	]
	if form.non_field_errors():
		errors = list(form.non_field_errors()) + errors
	return " ".join(errors) or fallback


# --------------------------------------------------------------------------- #
# Audits
# --------------------------------------------------------------------------- #
@login_required
@role_required(*VIEWER_ROLES)
def audit_list(request):
	company = _require_company(request)
	if company is None:
		return _redirect_no_company()

	filter_form = AuditFilterForm(request.GET or None, company=company)
	audits = visible_audits(request.user, company).select_related(
		"department", "related_policy", "lead_auditor"
	)
	audits = _apply_filters(audits, filter_form).annotate(
		finding_total=Count("findings", distinct=True),
		open_finding_total=Count(
			"findings",
			filter=Q(findings__status__in=OPEN_FINDING_STATUSES),
			distinct=True,
		),
	)

	pagination = paginate(request, audits.order_by("-start_date", "-id"))

	context = _module_context(request, company)
	ai_analysis = _ai_analysis(request, company)
	context.update({
		"filter_form": filter_form,
		"pagination": pagination,
		"audits": pagination["items"],
		"counts": dashboard_counts(company),
		"severity": severity_breakdown(company),
		"escalations": _decorate_findings(
			escalated_findings(company, limit=ESCALATION_LIMIT), ai_analysis
		),
		"can_manage": can_manage_audit(request.user, company),
		"can_manage_findings": can_manage_finding(request.user, company),
		"can_run_ai": can_manage_audit(request.user, company),
		"ai_analysis": ai_analysis,
		"has_filters": any(
			request.GET.get(key) for key in ("status", "audit_type", "department")
		),
		"selected": {
			"status": request.GET.get("status") or "",
			"audit_type": request.GET.get("audit_type") or "",
			"department": request.GET.get("department") or "",
		},
	})
	return render(request, "audits/list.html", context)


@login_required
@role_required(*VIEWER_ROLES)
@require_http_methods(["GET", "POST"])
def audit_create(request):
	company = _require_company(request)
	if company is None:
		return _redirect_no_company()
	if not can_manage_audit(request.user, company):
		raise PermissionDenied

	form = AuditForm(request.POST or None, company=company)
	if request.method == "POST" and form.is_valid():
		audit = form.save(commit=False)
		# The company is taken from the session, never from the form.
		audit.company = company
		audit.created_by = request.user
		audit.status = AuditStatus.PLANNED
		audit.save()
		messages.success(request, f"Audit “{audit.title}” created.")
		return redirect("audits:audit_detail", pk=audit.pk)

	context = _module_context(request, company)
	context.update({
		"form": form,
		"form_title": "New audit",
		"audit": None,
	})
	return render(request, "audits/form.html", context)


@login_required
@role_required(*VIEWER_ROLES)
@require_http_methods(["GET", "POST"])
def audit_edit(request, pk):
	company = _require_company(request)
	if company is None:
		return _redirect_no_company()
	if not can_manage_audit(request.user, company):
		raise PermissionDenied

	audit = _scoped_audit_or_404(company, pk)
	form = AuditForm(request.POST or None, instance=audit, company=company)
	if request.method == "POST" and form.is_valid():
		audit = form.save(commit=False)
		audit.company = company
		audit.save()
		messages.success(request, "Audit updated.")
		return redirect("audits:audit_detail", pk=audit.pk)

	context = _module_context(request, company)
	context.update({
		"form": form,
		"form_title": f"Edit {audit.title}",
		"audit": audit,
	})
	return render(request, "audits/form.html", context)


@login_required
@role_required(*VIEWER_ROLES)
def audit_detail(request, pk):
	company = _require_company(request)
	if company is None:
		return _redirect_no_company()

	audit = _scoped_audit_or_404(company, pk)
	ai_analysis = _ai_analysis(request, company)
	findings = _decorate_findings(
		audit.findings.select_related(
			"department", "related_policy", "owner"
		)
		.prefetch_related("remediation_plans")
		.annotate(plan_total=Count("remediation_plans", distinct=True)),
		ai_analysis,
	)

	context = _module_context(request, company)
	context.update({
		"audit": audit,
		"findings": findings,
		"evidence": audit.evidence.select_related("uploaded_by"),
		"evidence_form": AuditEvidenceForm(),
		"next_statuses": _next_statuses(
			AuditStatus.choices, AUDIT_TRANSITIONS, audit.status
		),
		"can_manage": can_manage_audit(request.user, company),
		"can_manage_findings": can_manage_finding(request.user, company),
		"can_run_ai": can_manage_audit(request.user, company),
		"can_generate_note": (
			can_manage_audit(request.user, company)
			and audit.status in (AuditStatus.IN_PROGRESS, AuditStatus.COMPLETED)
		),
		"ai_analysis": ai_analysis,
		"closing_note": _closing_note(request, company, audit),
		"open_findings": audit.findings.open().count(),
		"overdue_findings": audit.findings.overdue().count(),
	})
	return render(request, "audits/detail.html", context)


@login_required
@role_required(*VIEWER_ROLES)
@require_POST
def audit_set_status(request, pk):
	company = _require_company(request)
	if company is None:
		return _redirect_no_company()

	audit = _scoped_audit_or_404(company, pk)
	if not can_manage_audit(request.user, company):
		raise PermissionDenied
	try:
		transition_audit(audit, request.POST.get("status"))
	except ValidationError as exc:
		messages.error(request, getattr(exc, "messages", [str(exc)])[0])
	else:
		messages.success(request, f"Audit marked {audit.get_status_display().lower()}.")
	return redirect("audits:audit_detail", pk=audit.pk)


# --------------------------------------------------------------------------- #
# AI analysis (Qwen via Ollama)
# --------------------------------------------------------------------------- #
@login_required
@role_required(*VIEWER_ROLES)
@require_POST
def audit_ai_analysis(request):
	"""Run the Qwen-driven audit analysis for this tenant only.

	The model — never Django — decides each finding's theme and explains the
	delay risk; Django only supplies this company's own findings and the
	deterministic risk it already computed. When Ollama is unreachable the run
	degrades to a clear message instead of failing, and the audit register keeps
	working.
	"""
	company = _require_company(request)
	if company is None:
		return _redirect_no_company()
	if not can_manage_audit(request.user, company):
		raise PermissionDenied

	analysis = ai.run_analysis(company)
	_remember_analysis(request, analysis, company)

	if not analysis.ai_available:
		messages.warning(request, analysis.message)
	else:
		messages.success(request, analysis.message or "AI audit analysis completed.")

	# Return the user to the page they launched the run from (validated here so
	# the audit can only ever be one of this tenant's own).
	next_audit = (request.POST.get("next_audit") or "").strip()
	if next_audit.isdigit():
		audit = Audit.objects.filter(company=company, pk=int(next_audit)).first()
		if audit is not None:
			return redirect("audits:audit_detail", pk=audit.pk)
	return redirect("audits:audit_list")


@login_required
@role_required(*VIEWER_ROLES)
@require_POST
def audit_closing_note(request, pk):
	"""Draft the closing note of one engagement with Qwen.

	The audit is resolved within the caller's own company (another tenant's
	engagement is a 404) and Qwen writes the note from the engagement's real
	counts only.
	"""
	company = _require_company(request)
	if company is None:
		return _redirect_no_company()
	if not can_manage_audit(request.user, company):
		raise PermissionDenied

	audit = _scoped_audit_or_404(company, pk)
	note = ai.run_closing_note(company, audit)
	_remember_note(request, note)

	if not note.ai_available:
		messages.warning(request, note.message)
	else:
		messages.success(request, note.message or "Closing note drafted.")
	return redirect("audits:audit_detail", pk=audit.pk)


# --------------------------------------------------------------------------- #
# Findings
# --------------------------------------------------------------------------- #
@login_required
@role_required(*VIEWER_ROLES)
@require_http_methods(["GET", "POST"])
def finding_create(request, pk):
	company = _require_company(request)
	if company is None:
		return _redirect_no_company()

	audit = _scoped_audit_or_404(company, pk)
	if not can_manage_finding(request.user, company):
		raise PermissionDenied

	restrict = _restrict_department(request, company)
	initial = {}
	if restrict is None and audit.department_id:
		initial["department"] = audit.department_id
	form = FindingForm(
		request.POST or None,
		company=company,
		restrict_department=restrict,
		initial=initial or None,
	)
	if request.method == "POST" and form.is_valid():
		finding = form.save(commit=False)
		finding.audit = audit
		finding.company = company
		finding.status = FindingStatus.OPEN
		if restrict is not None:
			finding.department = restrict
		finding.save()
		messages.success(request, f"Finding “{finding.title}” recorded.")
		return redirect("audits:finding_detail", pk=finding.pk)

	context = _module_context(request, company)
	context.update({
		"form": form,
		"form_title": f"New finding · {audit.title}",
		"audit": audit,
		"finding": None,
	})
	return render(request, "audits/finding_form.html", context)


@login_required
@role_required(*VIEWER_ROLES)
@require_http_methods(["GET", "POST"])
def finding_edit(request, pk):
	company = _require_company(request)
	if company is None:
		return _redirect_no_company()

	finding = _scoped_finding_or_404(company, pk)
	if not can_manage_finding(request.user, company, finding):
		raise PermissionDenied

	restrict = _restrict_department(request, company)
	if restrict is not None and finding.department_id != restrict.pk:
		# The manager owns this finding but it is filed elsewhere: don't move it.
		restrict = None
	form = FindingForm(
		request.POST or None,
		instance=finding,
		company=company,
		restrict_department=restrict,
	)
	if request.method == "POST" and form.is_valid():
		finding = form.save(commit=False)
		finding.company = company
		finding.save()
		messages.success(request, "Finding updated.")
		return redirect("audits:finding_detail", pk=finding.pk)

	context = _module_context(request, company)
	context.update({
		"form": form,
		"form_title": f"Edit finding · {finding.reference}",
		"audit": finding.audit,
		"finding": finding,
	})
	return render(request, "audits/finding_form.html", context)


@login_required
@role_required(*VIEWER_ROLES)
def finding_detail(request, pk):
	company = _require_company(request)
	if company is None:
		return _redirect_no_company()

	finding = _scoped_finding_or_404(company, pk)
	ai_analysis = _ai_analysis(request, company)
	_decorate_findings([finding], ai_analysis)
	context = _module_context(request, company)
	context.update({
		"finding": finding,
		"audit": finding.audit,
		"plans": finding.remediation_plans.select_related("owner"),
		"evidence": finding.evidence.select_related("uploaded_by"),
		"evidence_form": AuditEvidenceForm(),
		"next_statuses": _next_statuses(
			FindingStatus.choices, FINDING_TRANSITIONS, finding.status
		),
		"can_manage": can_manage_finding(request.user, company, finding),
		"can_run_ai": can_manage_audit(request.user, company),
		"ai_analysis": ai_analysis,
	})
	return render(request, "audits/finding_detail.html", context)


@login_required
@role_required(*VIEWER_ROLES)
@require_POST
def finding_set_status(request, pk):
	company = _require_company(request)
	if company is None:
		return _redirect_no_company()

	finding = _scoped_finding_or_404(company, pk)
	if not can_manage_finding(request.user, company, finding):
		raise PermissionDenied
	try:
		transition_finding(finding, request.POST.get("status"))
	except ValidationError as exc:
		messages.error(request, getattr(exc, "messages", [str(exc)])[0])
	else:
		if finding.status == FindingStatus.CLOSED:
			messages.success(request, "Finding closed and recorded on the audit trail.")
		else:
			messages.success(request, f"Finding marked {finding.get_status_display().lower()}.")
	return redirect("audits:finding_detail", pk=finding.pk)


# --------------------------------------------------------------------------- #
# Remediation plans
# --------------------------------------------------------------------------- #
@login_required
@role_required(*VIEWER_ROLES)
@require_http_methods(["GET", "POST"])
def remediation_create(request, pk):
	company = _require_company(request)
	if company is None:
		return _redirect_no_company()

	finding = _scoped_finding_or_404(company, pk)
	if not can_manage_finding(request.user, company, finding):
		raise PermissionDenied

	form = RemediationPlanForm(request.POST or None, company=company)
	if request.method == "POST" and form.is_valid():
		plan = form.save(commit=False)
		plan.finding = finding
		plan.company = company
		plan.status = RemediationStatus.PLANNED
		plan.save()
		messages.success(request, "Remediation action added.")
		return redirect("audits:finding_detail", pk=finding.pk)

	context = _module_context(request, company)
	context.update({
		"form": form,
		"form_title": f"New remediation action · {finding.reference}",
		"finding": finding,
		"plan": None,
	})
	return render(request, "audits/remediation_form.html", context)


@login_required
@role_required(*VIEWER_ROLES)
@require_http_methods(["GET", "POST"])
def remediation_edit(request, pk):
	company = _require_company(request)
	if company is None:
		return _redirect_no_company()

	plan = _scoped_plan_or_404(company, pk)
	if not can_manage_finding(request.user, company, plan.finding):
		raise PermissionDenied

	form = RemediationPlanForm(request.POST or None, instance=plan, company=company)
	if request.method == "POST" and form.is_valid():
		plan = form.save(commit=False)
		plan.company = company
		plan.save()
		messages.success(request, "Remediation action updated.")
		return redirect("audits:finding_detail", pk=plan.finding_id)

	context = _module_context(request, company)
	context.update({
		"form": form,
		"form_title": f"Edit remediation action · {plan.finding.reference}",
		"finding": plan.finding,
		"plan": plan,
	})
	return render(request, "audits/remediation_form.html", context)


@login_required
@role_required(*VIEWER_ROLES)
@require_POST
def remediation_set_status(request, pk):
	company = _require_company(request)
	if company is None:
		return _redirect_no_company()

	plan = _scoped_plan_or_404(company, pk)
	if not can_manage_finding(request.user, company, plan.finding):
		raise PermissionDenied
	try:
		transition_remediation(plan, request.POST.get("status"))
	except ValidationError as exc:
		messages.error(request, getattr(exc, "messages", [str(exc)])[0])
	else:
		messages.success(request, f"Action marked {plan.get_status_display().lower()}.")
	return redirect("audits:finding_detail", pk=plan.finding_id)


# --------------------------------------------------------------------------- #
# Evidence
# --------------------------------------------------------------------------- #
@login_required
@role_required(*VIEWER_ROLES)
@require_POST
def finding_evidence_upload(request, pk):
	company = _require_company(request)
	if company is None:
		return _redirect_no_company()

	finding = _scoped_finding_or_404(company, pk)
	if not can_manage_finding(request.user, company, finding):
		raise PermissionDenied

	# The parent is attached before validation so the model's cross-tenant
	# guards pass; the company is taken from the session, never the form.
	form = AuditEvidenceForm(
		request.POST,
		request.FILES,
		instance=AuditEvidence(
			finding=finding,
			audit=finding.audit,
			company=company,
			uploaded_by=request.user,
		),
	)
	if form.is_valid():
		evidence = form.save()
		messages.success(request, f"Evidence “{evidence.title}” uploaded.")
	else:
		messages.error(request, _form_error_message(form, "Upload a valid file."))
	return redirect("audits:finding_detail", pk=finding.pk)


@login_required
@role_required(*VIEWER_ROLES)
@require_POST
def audit_evidence_upload(request, pk):
	company = _require_company(request)
	if company is None:
		return _redirect_no_company()

	audit = _scoped_audit_or_404(company, pk)
	if not can_manage_audit(request.user, company):
		raise PermissionDenied

	# The parent is attached before validation so the model's cross-tenant
	# guards pass; the company is taken from the session, never the form.
	form = AuditEvidenceForm(
		request.POST,
		request.FILES,
		instance=AuditEvidence(
			audit=audit, company=company, uploaded_by=request.user
		),
	)
	if form.is_valid():
		evidence = form.save()
		messages.success(request, f"Evidence “{evidence.title}” uploaded.")
	else:
		messages.error(request, _form_error_message(form, "Upload a valid file."))
	return redirect("audits:audit_detail", pk=audit.pk)


@login_required
@role_required(*VIEWER_ROLES)
def evidence_download(request, pk):
	company = _require_company(request)
	if company is None:
		raise Http404("Evidence not found.")

	# Cross-tenant access is impossible: the file is fetched within the
	# caller's own company, so another company's evidence is a 404.
	evidence = _scoped_evidence_or_404(company, pk)
	try:
		handle = evidence.file.open("rb")
	except (FileNotFoundError, OSError):
		raise Http404("The evidence file is no longer available.")
	return FileResponse(
		handle, as_attachment=True, filename=os.path.basename(evidence.file.name)
	)


@login_required
@role_required(*VIEWER_ROLES)
@require_POST
def evidence_delete(request, pk):
	company = _require_company(request)
	if company is None:
		return _redirect_no_company()

	evidence = _scoped_evidence_or_404(company, pk)
	if evidence.finding_id:
		target = evidence.finding
		if not can_manage_finding(request.user, company, target):
			raise PermissionDenied
		redirect_to = ("audits:finding_detail", target.pk)
	else:
		target = evidence.audit
		if not can_manage_audit(request.user, company):
			raise PermissionDenied
		redirect_to = ("audits:audit_detail", target.pk)

	title = evidence.title
	evidence.file.delete(save=False)
	evidence.delete()
	messages.success(request, f"Evidence “{title}” deleted.")
	return redirect(redirect_to[0], pk=redirect_to[1])
