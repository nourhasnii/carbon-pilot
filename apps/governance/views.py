"""Corporate governance & policy control views.

Tenant isolation mirrors the rest of CarbonPilot: the company is always
resolved from the authenticated user (never from the request body), and every
policy is fetched with ``company=company`` so another tenant's record is a 404
rather than a 403. Every state change is guarded server-side.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_http_methods, require_POST

from apps.accounts.permissions import role_required
from apps.carbon.views import _sidebar as carbon_sidebar
from apps.common.listing import paginate
from apps.enterprise.models import Department

from .forms import (
	PolicyDecisionForm,
	PolicyFilterForm,
	PolicyForm,
	PolicyOwnerFormSet,
	PolicyVersionForm,
)
from .models import (
	Policy,
	PolicyApproval,
	PolicyCategory,
	PolicyStatus,
)
from .services import (
	VIEWER_ROLES,
	archive_policy,
	can_approve,
	can_manage,
	company_for_user,
	create_initial_version,
	create_new_version,
	is_company_admin,
	record_decision,
	record_review,
	responsibility_matrix,
	submit_for_review,
	visible_policies,
)
from .services import governance_ai

ACTIVE_NAV = "Policies"

# The last AI governance analysis lives in the session (not the database): it is
# a read-only projection of the tenant's own data, produced on demand.
AI_SESSION_KEY = "governance_ai_analysis"


def _module_context(request, company, active=ACTIVE_NAV):
	return {
		"company": company,
		"sidebar_nav": carbon_sidebar(active, request.user),
	}


def _require_company(request):
	"""Resolve the tenant from the user; never trusts client input."""
	company = company_for_user(request.user)
	if company is None:
		messages.warning(request, "Set up your company before managing policies.")
	return company


def _redirect_no_company():
	return redirect(f"{reverse('accounts:dashboard')}?section=company")


def _scoped_policy_or_404(user, company, pk):
	policy = (
		visible_policies(user, company)
		.select_related("current_version", "created_by")
		.filter(pk=pk)
		.first()
	)
	if policy is None:
		# 404 (never 403) so other tenants cannot probe for existing IDs.
		raise Http404("Policy not found.")
	return policy


def _remember_analysis(request, analysis):
	"""Keep the latest analysis for this session so the page can render it."""
	request.session[AI_SESSION_KEY] = {
		"company_id": analysis.company_id,
		"ai_available": analysis.ai_available,
		"model": analysis.model,
		"generated_at": analysis.generated_at,
		"generated_at_display": _generated_at_display(analysis.generated_at),
		"message": analysis.message,
		"missing_policies": analysis.missing_policies,
		"summary": analysis.summary,
	}


def _generated_at_display(value):
	"""Human-readable local timestamp for the stored ISO string."""
	parsed = parse_datetime(value) if value else None
	if parsed is None:
		return value or ""
	return timezone.localtime(parsed).strftime("%d %b %Y, %H:%M")


def _ai_analysis(request, company):
	"""Last analysis for the *current* tenant only, or ``None``."""
	stored = request.session.get(AI_SESSION_KEY)
	if not isinstance(stored, dict):
		return None
	if stored.get("company_id") != company.pk:
		# Never leak another tenant's analysis if the session user switched company.
		return None
	return stored


@login_required
@role_required(*VIEWER_ROLES)
def policy_list(request):
	company = _require_company(request)
	if company is None:
		return _redirect_no_company()

	base = visible_policies(request.user, company)
	queryset = base
	filter_form = PolicyFilterForm(request.GET or None, company=company)
	if filter_form.is_valid():
		data = filter_form.cleaned_data
		status = data.get("status") or ""
		category = data.get("category") or ""
		if status:
			queryset = queryset.filter(status=status)
		else:
			queryset = queryset.exclude(status=PolicyStatus.ARCHIVED)
		if category:
			queryset = queryset.filter(category=category)
		if data.get("due"):
			queryset = queryset.due_for_review()
		department = data.get("department")
		if department is not None:
			queryset = queryset.filter(owners__department=department).distinct()
	else:
		# Unknown/invalid filter values are ignored: fall back to the live list.
		queryset = queryset.exclude(status=PolicyStatus.ARCHIVED)

	pagination = paginate(
		request, queryset.select_related("current_version", "created_by")
	)

	context = _module_context(request, company)
	context.update({
		"filter_form": filter_form,
		"pagination": pagination,
		"policies": pagination["items"],
		"status_choices": PolicyStatus.choices,
		"category_choices": PolicyCategory.choices,
		"departments": Department.objects.filter(company=company).order_by("name"),
		"selected": {
			"status": request.GET.get("status") or "",
			"category": request.GET.get("category") or "",
			"due": request.GET.get("due") or "",
			"department": request.GET.get("department") or "",
		},
		"has_filters": any(
			request.GET.get(key)
			for key in ("status", "category", "due", "department")
		),
		"can_manage": can_manage(request.user, company),
		"can_run_ai": is_company_admin(request.user),
		"ai_analysis": _ai_analysis(request, company),
		"total_count": base.count(),
		"approved_count": base.filter(status=PolicyStatus.APPROVED).count(),
		"in_review_count": base.filter(status=PolicyStatus.IN_REVIEW).count(),
		"due_count": base.due_for_review().count(),
	})
	return render(request, "governance/list.html", context)


@login_required
@role_required(*VIEWER_ROLES)
def policy_detail(request, pk):
	company = _require_company(request)
	if company is None:
		return _redirect_no_company()

	policy = _scoped_policy_or_404(request.user, company, pk)
	context = _module_context(request, company)
	context.update({
		"policy": policy,
		"versions": policy.versions.select_related("created_by"),
		"approvals": policy.approvals.select_related("approver", "policy_version"),
		"matrix": responsibility_matrix(policy),
		"can_manage": can_manage(request.user, company, policy),
		"can_approve": can_approve(request.user, company, policy),
		"my_approval": policy.approvals.filter(
			policy_version=policy.current_version, approver=request.user
		).first(),
		"pending_count": policy.current_pending_approvals.count(),
		"decision_form": PolicyDecisionForm(),
	})
	return render(request, "governance/detail.html", context)


@login_required
@role_required(*VIEWER_ROLES)
@require_http_methods(["GET", "POST"])
def policy_create(request):
	company = _require_company(request)
	if company is None:
		return _redirect_no_company()
	if not can_manage(request.user, company):
		raise PermissionDenied

	# A suggested missing policy can pre-fill the create form (title + category).
	initial = {}
	if request.method != "POST":
		title = (request.GET.get("title") or "").strip()
		category = (request.GET.get("category") or "").strip().upper()
		if title:
			initial["title"] = title[:255]
		if category in PolicyCategory.values:
			initial["category"] = category
	form = PolicyForm(request.POST or None, company=company, initial=initial or None)
	formset = PolicyOwnerFormSet(
		request.POST or None,
		instance=Policy(company=company),
		prefix="owners",
		form_kwargs={"company": company},
	)
	version_form = PolicyVersionForm(request.POST or None, prefix="version")

	if request.method == "POST" and form.is_valid() and formset.is_valid() and version_form.is_valid():
		policy = form.save(commit=False)
		policy.company = company
		policy.created_by = request.user
		policy.status = PolicyStatus.DRAFT
		policy.is_active = True
		policy.save()
		formset.instance = policy
		formset.save()
		create_initial_version(policy, request.user, **version_form.cleaned_data)
		messages.success(request, "Policy created as a draft. Submit it for review when ready.")
		return redirect("governance:policy_detail", pk=policy.pk)

	context = _module_context(request, company)
	context.update({
		"form": form,
		"formset": formset,
		"version_form": version_form,
		"form_title": "New policy",
		"policy": None,
	})
	return render(request, "governance/form.html", context)


@login_required
@role_required(*VIEWER_ROLES)
@require_http_methods(["GET", "POST"])
def policy_edit(request, pk):
	company = _require_company(request)
	if company is None:
		return _redirect_no_company()

	policy = _scoped_policy_or_404(request.user, company, pk)
	if not can_manage(request.user, company, policy):
		raise PermissionDenied
	if not policy.is_editable:
		messages.error(
			request,
			"Only draft policies are freely editable. Create a new version from an approved policy instead.",
		)
		return redirect("governance:policy_detail", pk=policy.pk)

	form = PolicyForm(request.POST or None, instance=policy, company=company)
	formset = PolicyOwnerFormSet(
		request.POST or None,
		instance=policy,
		prefix="owners",
		form_kwargs={"company": company},
	)
	version_form = (
		PolicyVersionForm(
			request.POST or None, instance=policy.current_version, prefix="version"
		)
		if policy.current_version_id
		else None
	)

	forms_valid = form.is_valid() and formset.is_valid() and (
		version_form is None or version_form.is_valid()
	)
	if request.method == "POST" and forms_valid:
		policy = form.save(commit=False)
		policy.company = company
		policy.save()
		formset.save()
		if version_form is not None:
			version = version_form.save(commit=False)
			version.policy = policy
			version.save()
		messages.success(request, "Policy updated.")
		return redirect("governance:policy_detail", pk=policy.pk)

	context = _module_context(request, company)
	context.update({
		"form": form,
		"formset": formset,
		"version_form": version_form,
		"form_title": "Edit policy",
		"policy": policy,
	})
	return render(request, "governance/form.html", context)


@login_required
@role_required(*VIEWER_ROLES)
@require_POST
def policy_submit_review(request, pk):
	company = _require_company(request)
	if company is None:
		return _redirect_no_company()

	policy = _scoped_policy_or_404(request.user, company, pk)
	if not can_manage(request.user, company, policy):
		raise PermissionDenied
	try:
		submit_for_review(policy, request.user)
	except ValidationError as exc:
		messages.error(request, getattr(exc, "messages", [str(exc)])[0])
	else:
		messages.success(request, "Policy submitted for review.")
	return redirect("governance:policy_detail", pk=policy.pk)


@login_required
@role_required(*VIEWER_ROLES)
@require_POST
def policy_decide(request, pk):
	company = _require_company(request)
	if company is None:
		return _redirect_no_company()

	policy = _scoped_policy_or_404(request.user, company, pk)
	if policy.status != PolicyStatus.IN_REVIEW or policy.current_version_id is None:
		messages.error(request, "This policy is not awaiting a decision.")
		return redirect("governance:policy_detail", pk=policy.pk)

	form = PolicyDecisionForm(request.POST)
	if not form.is_valid():
		messages.error(request, "Choose whether to approve or reject the policy.")
		return redirect("governance:policy_detail", pk=policy.pk)

	approval = policy.approvals.filter(
		policy_version=policy.current_version, approver=request.user
	).first()
	if approval is None:
		# A governance role may open a missing approval slot and decide it.
		if not can_approve(request.user, company, policy):
			raise PermissionDenied
		approval = PolicyApproval.objects.create(
			policy=policy,
			policy_version=policy.current_version,
			approver=request.user,
		)

	policy = record_decision(
		approval, request.user, form.cleaned_data["decision"], form.cleaned_data["comment"]
	)
	if policy.status == PolicyStatus.APPROVED:
		messages.success(request, "All approvals recorded — the policy is now approved.")
	elif policy.status == PolicyStatus.DRAFT:
		messages.warning(request, "Policy rejected and returned to draft.")
	else:
		messages.success(request, "Decision recorded. Waiting for the remaining approvals.")
	return redirect("governance:policy_detail", pk=policy.pk)


@login_required
@role_required(*VIEWER_ROLES)
@require_http_methods(["GET", "POST"])
def policy_new_version(request, pk):
	company = _require_company(request)
	if company is None:
		return _redirect_no_company()

	policy = _scoped_policy_or_404(request.user, company, pk)
	if not can_manage(request.user, company, policy):
		raise PermissionDenied
	if policy.status != PolicyStatus.APPROVED:
		messages.error(request, "A new version can only be created from an approved policy.")
		return redirect("governance:policy_detail", pk=policy.pk)

	version_form = PolicyVersionForm(request.POST or None, prefix="version")
	if request.method == "POST" and version_form.is_valid():
		version = create_new_version(policy, request.user, **version_form.cleaned_data)
		messages.success(
			request,
			f"Version {version.label} created as a draft. Review the changes and submit it for approval.",
		)
		return redirect("governance:policy_detail", pk=policy.pk)

	context = _module_context(request, company)
	context.update({
		"policy": policy,
		"version_form": version_form,
		"next_number": (policy.versions.order_by("-version_number").first().version_number + 1)
		if policy.versions.exists()
		else 1,
	})
	return render(request, "governance/version_form.html", context)


@login_required
@role_required(*VIEWER_ROLES)
@require_POST
def policy_archive(request, pk):
	company = _require_company(request)
	if company is None:
		return _redirect_no_company()

	policy = _scoped_policy_or_404(request.user, company, pk)
	if not can_manage(request.user, company, policy):
		raise PermissionDenied
	try:
		archive_policy(policy)
	except ValidationError as exc:
		messages.error(request, getattr(exc, "messages", [str(exc)])[0])
	else:
		messages.success(request, "Policy archived. It is kept for the audit trail.")
	return redirect("governance:policy_detail", pk=policy.pk)


@login_required
@role_required(*VIEWER_ROLES)
@require_POST
def policy_record_review(request, pk):
	company = _require_company(request)
	if company is None:
		return _redirect_no_company()

	policy = _scoped_policy_or_404(request.user, company, pk)
	if not can_manage(request.user, company, policy):
		raise PermissionDenied
	try:
		record_review(policy)
	except ValidationError as exc:
		messages.error(request, getattr(exc, "messages", [str(exc)])[0])
	else:
		messages.success(request, "Review recorded. The next review is scheduled.")
	return redirect("governance:policy_detail", pk=policy.pk)


@login_required
@role_required(*VIEWER_ROLES)
@require_POST
def policy_ai_analysis(request):
	"""Run the Qwen-driven governance analysis for this tenant only.

	The model — never Django — decides which policies are missing and writes the
	governance summary; Django only supplies this company's own facts. When Ollama
	is unreachable the analysis degrades to a clear message instead of failing.
	"""
	company = _require_company(request)
	if company is None:
		return _redirect_no_company()
	if not is_company_admin(request.user):
		raise PermissionDenied

	analysis = governance_ai.run_analysis(company)
	_remember_analysis(request, analysis)

	if not analysis.ai_available:
		messages.warning(request, analysis.message)
	else:
		messages.success(request, analysis.message)
	return redirect("governance:policy_list")
