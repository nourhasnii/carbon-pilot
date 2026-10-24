from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Q
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST
import os

from apps.accounts.models import User
from apps.accounts.permissions import role_required

from .constants import default_scope_for
from .forms import (
	ActivityCategoryForm,
	ActivityDataForm,
	ActivityEditForm,
	ActivityFactorForm,
	ActivityFilterForm,
	AnomalyFilterForm,
	EmissionFactorForm,
	EmissionFactorOverrideForm,
	ActivitySubmitForm,
)
from .models import (
	ActivityAuditAction,
	ActivityCategory,
	ActivityStatus,
	AnomalyStatus,
	CarbonActivity,
	EmissionFactor,
	Scope,
	VerificationStatus,
)
from .services import (
	analytics,
	anomaly_detection,
	data_quality,
	recommendation,
	tenancy,
	wizard,
	workflow,
)
from .services.recommendation import factors_for_company
from .services.units import COMMON_UNITS
from apps.objectives.services import dashboard_objectives
from apps.recommendations.services import summary as recommendation_summary

# Everyone in a company may look at its carbon data (auditors read-only).
CARBON_VIEWER_ROLES = (
	User.Role.COMPANY_OWNER,
	User.Role.DEPARTMENT_MANAGER,
	User.Role.ESG_REPORTING_MANAGER,
	User.Role.SUPPLIER,
	User.Role.AUDITOR,
)

# Members who may record carbon activity; auditors are read-only.
CARBON_CREATOR_ROLES = (
	User.Role.COMPANY_OWNER,
	User.Role.DEPARTMENT_MANAGER,
	User.Role.ESG_REPORTING_MANAGER,
	User.Role.SUPPLIER,
)

# Members who may open the review queue.
CARBON_REVIEW_ROLES = workflow.REVIEWER_ROLES
FACTOR_MANAGER_ROLES = (User.Role.COMPANY_OWNER, User.Role.ESG_REPORTING_MANAGER)
INVESTMENTS_VIEWER_ROLES = (
	User.Role.COMPANY_OWNER,
	User.Role.ESG_REPORTING_MANAGER,
	User.Role.AUDITOR,
	User.Role.DEPARTMENT_MANAGER,
)

# Members who may run anomaly detection / triage anomaly logs.
ANOMALY_MANAGE_ROLES = (User.Role.COMPANY_OWNER, User.Role.ESG_REPORTING_MANAGER)

company_for_user = tenancy.company_for_user


def _as_int(value):
	try:
		return int(value)
	except (TypeError, ValueError):
		return 0


def _sidebar(active, user):
	items = [
		{"label": "Carbon overview", "icon": "gauge", "url_name": "carbon:dashboard"},
		{"label": "Activities", "icon": "activity", "url_name": "carbon:activity_list"},
		{"label": "New activity", "icon": "plus-circle", "url_name": "carbon:activity_wizard"},
	]
	if user.role == User.Role.SUPER_ADMIN or user.role in CARBON_REVIEW_ROLES:
		items.append({"label": "Review queue", "icon": "clipboard-check", "url_name": "carbon:review_queue"})
	items += [
		{"label": "Footprint analysis", "icon": "bar-chart-3", "url_name": "carbon:footprint"},
		{"label": "Anomalies", "icon": "shield-alert", "url_name": "carbon:anomaly_list"},
		{"label": "Objectives", "icon": "target", "url_name": "objectives:objective_list"},
		{"label": "Recommendations", "icon": "sparkles", "url_name": "recommendations:recommendation_list"},
	]
	if user.role == User.Role.SUPER_ADMIN or user.role in INVESTMENTS_VIEWER_ROLES:
		items.append({"label": "Investments", "icon": "landmark", "url_name": "investments:project_list"})
	items += [
		{"label": "Emission factors", "icon": "beaker", "url_name": "carbon:factor_library"},
	]
	for item in items:
		item["active"] = item["label"] == active
		item["href"] = reverse(item["url_name"])
	return items


def _module_context(request, company, active):
	return {
		"company": company,
		"sidebar_nav": _sidebar(active, request.user),
	}


def _require_company(request):
	"""Resolve the tenant or send the user to set up their company."""
	company = company_for_user(request.user)
	return company


def _can_edit_factor(user, company, factor=None):
	"""Author/edit factor values: platform admins, or a company managing role.

	A tenant edits its own company factors directly. For a shared (global) factor
	it only edits a company-scoped override — the shared record stays untouched.
	"""
	if user.role == User.Role.SUPER_ADMIN:
		return True
	return user.role in FACTOR_MANAGER_ROLES and company is not None


def _can_toggle_factor(user, company, factor=None):
	"""Change a factor's activation: platform admins, or the owning company.

	A tenant may restrict a shared factor for itself (tenant-scoped override)
	but can never re-enable one the platform has globally disabled.
	"""
	if user.role == User.Role.SUPER_ADMIN:
		return True
	if user.role not in FACTOR_MANAGER_ROLES or company is None:
		return False
	if factor is not None and factor.is_shared and not factor.is_active:
		return False
	return True


# The "author the library" capability (adding/editing values).
_can_manage_factors = _can_edit_factor


def _factor_queryset(user, company):
	if user.role == User.Role.SUPER_ADMIN:
		return EmissionFactor.objects.all()
	return factors_for_company(company=company, active_only=False)


def _scoped_factor_or_404(user, company, pk):
	factor = _factor_queryset(user, company).filter(pk=pk).first()
	if factor is None:
		raise Http404("Emission factor not found.")
	return factor


def _scoped_activity_or_404(user, company, pk):
	activity = tenancy.visible_activities(user, company).filter(pk=pk).first()
	if activity is None:
		# 404 (never 403) so other tenants cannot probe for existing IDs.
		raise Http404("Activity not found.")
	return activity


def _apply_filters(queryset, form):
	if not form.is_valid():
		return queryset
	data = form.cleaned_data
	if data.get("year"):
		queryset = queryset.filter(period_start__year=data["year"])
	if data.get("scope"):
		queryset = queryset.filter(scope=data["scope"])
	if data.get("category"):
		queryset = queryset.filter(category=data["category"])
	if data.get("department"):
		queryset = queryset.filter(department=data["department"])
	if data.get("status"):
		queryset = queryset.filter(status=data["status"])
	return queryset


def _apply_anomaly_filters(queryset, form):
	"""Narrow an anomaly queryset by the shared filter bar."""
	if not form.is_valid():
		return queryset
	data = form.cleaned_data
	if data.get("severity"):
		queryset = queryset.filter(severity=data["severity"])
	if data.get("status"):
		queryset = queryset.filter(status=data["status"])
	if data.get("category"):
		queryset = queryset.filter(activity__category=data["category"])
	if data.get("department"):
		queryset = queryset.filter(department=data["department"])
	if data.get("year"):
		queryset = queryset.filter(period_start__year=data["year"])
	return queryset


def _paginate(request, queryset, per_page=20):
	page = max(_as_int(request.GET.get("page")) or 1, 1)
	total = queryset.count()
	pages = max((total + per_page - 1) // per_page, 1)
	page = min(page, pages)
	start = (page - 1) * per_page
	return {
		"items": list(queryset[start:start + per_page]),
		"page": page,
		"pages": pages,
		"total": total,
		"has_previous": page > 1,
		"has_next": page < pages,
		"previous_page": page - 1,
		"next_page": page + 1,
	}


# --------------------------------------------------------------------------- #
# Wizard (Phase 3)
# --------------------------------------------------------------------------- #
@login_required
@role_required(*CARBON_CREATOR_ROLES)
@require_http_methods(["GET", "POST"])
def activity_wizard(request):
	company = _require_company(request)
	if company is None:
		messages.warning(request, "Set up your company before recording carbon activity.")
		return redirect(f"{reverse('accounts:dashboard')}?section=company")

	created_id = _as_int(request.GET.get("created"))
	if created_id:
		activity = CarbonActivity.objects.filter(pk=created_id, company=company).first()
		if activity:
			context = _module_context(request, company, "New activity")
			context.update(_wizard_context(request, {}, None))
			context["created"] = activity
			return render(request, "carbon/activity_wizard.html", context)

	if request.GET.get("reset") == "1":
		wizard.clear_state(request)
		messages.info(request, "Draft cleared. Start a new carbon activity.")
		return redirect("carbon:activity_wizard")

	state = wizard.get_state(request)
	reachable = wizard.highest_reachable_step(state)

	step = _as_int(request.POST.get("step") or request.GET.get("step")) or reachable
	if step > reachable:
		messages.info(request, "Complete the previous steps before continuing.")
		step = reachable
	step = max(step, 1)

	context = _module_context(request, company, "New activity")
	context.update(_wizard_context(request, state, step))

	if step == 1:
		form = ActivityCategoryForm(
			request.POST or None,
			initial={"category": state.get("category"), "name": state.get("name", "")},
		)
		if request.method == "POST" and form.is_valid():
			data = form.cleaned_data
			if data["category"] != state.get("category"):
				wizard.clear_state(request)
			wizard.update_state(request, category=data["category"], name=data["name"].strip())
			return redirect(f"{reverse('carbon:activity_wizard')}?step=2")
		context["form"] = form

	elif step == 2:
		initial = {
			"quantity": state.get("quantity"),
			"unit": state.get("unit") or wizard.suggested_unit(state.get("category")),
			"period_start": state.get("period_start"),
			"period_end": state.get("period_end"),
			"department": state.get("department_id"),
		}
		form = ActivityDataForm(
			request.POST or None, company=company, user=request.user, initial=initial
		)
		if request.method == "POST" and form.is_valid():
			data = form.cleaned_data
			wizard.update_state(
				request,
				quantity=str(data["quantity"]),
				unit=data["unit"].strip(),
				period_start=data["period_start"].isoformat(),
				period_end=data["period_end"].isoformat() if data["period_end"] else "",
				department_id=data["department"].pk if data["department"] else None,
			)
			return redirect(f"{reverse('carbon:activity_wizard')}?step=3")
		context["form"] = form

	elif step == 3:
		scope = default_scope_for(state.get("category"))
		candidates = recommendation.recommendation_candidates(
			category=state.get("category"),
			scope=scope,
			company=company,
			year=wizard.period_year(state),
		)
		states = recommendation.company_factor_states(company)
		for candidate in candidates:
			candidate.effective = recommendation.effective_factor_attrs(
				candidate, company, states=states
			)
		default_factor = state.get("emission_factor_id") or (candidates[0].pk if candidates else "")
		form = ActivityFactorForm(
			request.POST or None,
			candidates=candidates,
			initial={"emission_factor": str(default_factor)},
		)
		if request.method == "POST" and form.is_valid():
			wizard.update_state(request, emission_factor_id=form.cleaned_data["emission_factor"])
			return redirect(f"{reverse('carbon:activity_wizard')}?step=4")
		context.update({
			"form": form,
			"candidates": candidates,
			"recommended": candidates[0] if candidates else None,
			"scope": scope,
			"scope_label": dict(Scope.choices).get(scope),
		})

	else:
		preview = wizard.build_preview(state, company=company)
		form = ActivitySubmitForm(request.POST or None, request.FILES or None)
		if request.method == "POST" and form.is_valid():
			if not preview["available"]:
				messages.error(request, preview["message"])
			else:
				submit = request.POST.get("action") == "submit"
				try:
					activity = wizard.create_activity(
						request.user,
						company,
						state,
						data_source=form.cleaned_data["data_source"],
						evidence=form.cleaned_data.get("evidence"),
						submit=submit,
					)
				except ValidationError as error:
					messages.error(request, error.messages[0])
				else:
					wizard.clear_state(request)
					messages.success(
						request,
						"Carbon activity submitted for review."
						if submit
						else "Carbon activity saved as draft.",
					)
					return redirect(f"{reverse('carbon:activity_wizard')}?created={activity.pk}")
		context.update({"form": form, "preview": preview})

	return render(request, "carbon/activity_wizard.html", context)


def _wizard_context(request, state, step):
	return {
		"state": state,
		"step": step,
		"step_labels": wizard.STEP_LABELS,
		"total_steps": wizard.TOTAL_STEPS,
		"highest_step": wizard.highest_reachable_step(state),
		"unit_options": COMMON_UNITS,
		"category_choices": ActivityCategory.choices,
		"category_labels": dict(ActivityCategory.choices),
		"category_label": dict(ActivityCategory.choices).get(state.get("category"), ""),
	}


# --------------------------------------------------------------------------- #
# Dashboard (Phase 7)
# --------------------------------------------------------------------------- #
@login_required
@role_required(*CARBON_VIEWER_ROLES)
def carbon_dashboard(request):
	company = _require_company(request)
	if company is None:
		return redirect(f"{reverse('accounts:dashboard')}?section=company")

	visible = tenancy.visible_activities(request.user, company).unarchived()
	approved = visible.approved()
	today = timezone.localdate()
	years = analytics.available_years(approved, today.year)
	year = _as_int(request.GET.get("year")) or years[0]
	if year not in years:
		year = years[0]
	scoped = approved.filter(period_start__year=year)

	context = _module_context(request, company, "Carbon overview")
	context.update({
		"year": year,
		"years": years,
		"total_tonnes": analytics.total_tonnes(scoped),
		"scopes": analytics.scope_breakdown(scoped),
		"categories": analytics.category_breakdown(scoped),
		"departments": analytics.department_breakdown(scoped),
		"monthly": analytics.monthly_series(approved, year),
		"comparison": analytics.year_comparison(approved, year),
		"quality": analytics.quality_summary(scoped),
		"open_review": analytics.open_review_count(visible),
		"recent": analytics.recent(visible),
		"can_review": request.user.role == User.Role.SUPER_ADMIN or request.user.role in CARBON_REVIEW_ROLES,
		"objectives": dashboard_objectives(request.user, company, year=year),
		"anomalies": anomaly_detection.anomaly_counts(
			tenancy.visible_anomalies(request.user, company)
		),
		"recommendations": recommendation_summary.summary(request.user, company),
	})
	return render(request, "carbon/dashboard.html", context)


# --------------------------------------------------------------------------- #
# Footprint analysis (Phase 8)
# --------------------------------------------------------------------------- #
@login_required
@role_required(*CARBON_VIEWER_ROLES)
def carbon_footprint(request):
	company = _require_company(request)
	if company is None:
		return redirect(f"{reverse('accounts:dashboard')}?section=company")

	visible = tenancy.visible_activities(request.user, company).unarchived()
	approved = visible.approved()
	today = timezone.localdate()
	years = analytics.available_years(approved, today.year)
	form = ActivityFilterForm(request.GET or None, company=company, years=years, include_status=False)
	filtered = _apply_filters(approved, form)

	context = _module_context(request, company, "Footprint analysis")
	context.update({
		"filter_form": form,
		"years": years,
		"total_tonnes": analytics.total_tonnes(filtered),
		"scopes": analytics.scope_breakdown(filtered),
		"categories": analytics.category_breakdown(filtered),
		"departments": analytics.department_breakdown(filtered),
		"selected_year": _as_int(form.cleaned_data.get("year")) if form.is_valid() else None,
		"monthly": analytics.monthly_series(
			filtered, _as_int(form.cleaned_data.get("year")) if form.is_valid() and form.cleaned_data.get("year") else today.year
		),
		"query_string": request.GET.urlencode(),
		"quality": analytics.quality_summary(filtered),
		"count": filtered.count(),
	})
	return render(request, "carbon/footprint.html", context)


# --------------------------------------------------------------------------- #
# Activity list (drill-down target of the footprint charts)
# --------------------------------------------------------------------------- #
@login_required
@role_required(*CARBON_VIEWER_ROLES)
def activity_list(request):
	company = _require_company(request)
	if company is None:
		return redirect(f"{reverse('accounts:dashboard')}?section=company")

	visible = tenancy.visible_activities(request.user, company)
	show_archived = request.GET.get("archived") == "1"
	working = visible.archived() if show_archived else visible.unarchived()
	years = analytics.available_years(visible.unarchived(), timezone.localdate().year)
	form = ActivityFilterForm(request.GET or None, company=company, years=years)
	filtered = _apply_filters(working, form)

	base_params = request.GET.copy()
	base_params.pop("page", None)

	pagination = _paginate(request, filtered.order_by("-period_start", "-created_at"))
	for activity in pagination["items"]:
		# Per-row authorization so the template only shows usable actions.
		activity.row_can_edit = workflow.can_edit(request.user, activity)
		activity.row_can_delete = workflow.can_delete(request.user, activity)
		activity.row_can_archive = workflow.can_archive(request.user, activity)
		activity.row_can_restore = workflow.can_restore(request.user, activity)

	context = _module_context(request, company, "Activities")
	context.update({
		"filter_form": form,
		"pagination": pagination,
		"base_query": base_params.urlencode(),
		"show_archived": show_archived,
		"can_create": request.user.role in CARBON_CREATOR_ROLES,
	})
	return render(request, "carbon/activity_list.html", context)


# --------------------------------------------------------------------------- #
# Activity details + audit timeline (Phase 9)
# --------------------------------------------------------------------------- #
@login_required
@role_required(*CARBON_VIEWER_ROLES)
def activity_detail(request, pk):
	company = _require_company(request)
	if company is None:
		return redirect(f"{reverse('accounts:dashboard')}?section=company")

	activity = _scoped_activity_or_404(request.user, company, pk)
	actions = workflow.actionable(request.user, activity)
	context = _module_context(request, company, "Activities")
	context.update({
		"activity": activity,
		"quality": data_quality.score_activity(activity),
		"timeline": activity.audit_log.select_related("actor"),
		"available_actions": [
			{"key": key, **workflow.ACTION_DEFINITIONS[key]} for key in
			["submit", "start_review", "approve", "reject", "request_correction"] if key in actions
		],
		"can_edit": workflow.can_edit(request.user, activity),
		"can_delete": workflow.can_delete(request.user, activity),
		"can_archive": workflow.can_archive(request.user, activity),
		"can_restore": workflow.can_restore(request.user, activity),
	})
	return render(request, "carbon/activity_detail.html", context)


@login_required
@role_required(*CARBON_CREATOR_ROLES)
@require_http_methods(["GET", "POST"])
def activity_edit(request, pk):
	company = _require_company(request)
	if company is None:
		return redirect(f"{reverse('accounts:dashboard')}?section=company")

	activity = _scoped_activity_or_404(request.user, company, pk)
	if not workflow.can_edit(request.user, activity):
		messages.error(request, "This activity can no longer be edited.")
		return redirect("carbon:activity_detail", pk=activity.pk)

	form = ActivityEditForm(
		request.POST or None,
		request.FILES or None,
		instance=activity,
		company=company,
		user=request.user,
		initial={
			"name": activity.name,
			"quantity": activity.quantity,
			"unit": activity.unit,
			"period_start": activity.period_start,
			"period_end": activity.period_end,
			"department": activity.department_id,
			"emission_factor": activity.emission_factor_id,
			"data_source": activity.data_source,
		},
	)
	if request.method == "POST" and form.is_valid():
		data = form.cleaned_data
		activity.name = data["name"].strip()
		activity.quantity = data["quantity"]
		activity.unit = data["unit"].strip()
		activity.period_start = data["period_start"]
		activity.period_end = data["period_end"]
		activity.department = data["department"]
		activity.emission_factor = data["emission_factor"]
		activity.data_source = data["data_source"].strip()
		if data.get("evidence"):
			activity.evidence = data["evidence"]
		activity.save()
		activity.log_event(
			actor=request.user,
			action=ActivityAuditAction.UPDATED,
			comment="Activity details updated.",
			from_status=activity.status,
			to_status=activity.status,
		)
		messages.success(request, "Activity updated. CO2e recalculated automatically.")
		return redirect("carbon:activity_detail", pk=activity.pk)

	context = _module_context(request, company, "Activities")
	context.update({"form": form, "activity": activity, "unit_options": COMMON_UNITS})
	return render(request, "carbon/activity_edit.html", context)


# --------------------------------------------------------------------------- #
# Review queue (Phase 5)
# --------------------------------------------------------------------------- #
@login_required
@role_required(*CARBON_REVIEW_ROLES)
def review_queue(request):
	company = _require_company(request)
	if company is None:
		return redirect(f"{reverse('accounts:dashboard')}?section=company")

	visible = tenancy.visible_activities(request.user, company).unarchived()
	open_items = [
		activity for activity in visible.filter(
			status__in=(ActivityStatus.SUBMITTED, ActivityStatus.UNDER_REVIEW)
		).select_related("department", "created_by", "emission_factor")
		if workflow.actionable(request.user, activity)
	]
	context = _module_context(request, company, "Review queue")
	context.update({
		"submitted": [a for a in open_items if a.status == ActivityStatus.SUBMITTED],
		"under_review": [a for a in open_items if a.status == ActivityStatus.UNDER_REVIEW],
		"open_count": len(open_items),
	})
	return render(request, "carbon/review_queue.html", context)


@login_required
@role_required(*CARBON_CREATOR_ROLES)
@require_POST
def activity_action(request, pk):
	company = _require_company(request)
	if company is None:
		return redirect(f"{reverse('accounts:dashboard')}?section=company")

	activity = _scoped_activity_or_404(request.user, company, pk)
	action = request.POST.get("action", "")
	comment = request.POST.get("comment", "")
	try:
		workflow.perform(action, activity, request.user, comment)
	except workflow.WorkflowError as error:
		messages.error(request, str(error))
	else:
		messages.success(request, f"Activity {workflow.ACTION_DEFINITIONS.get(action, {}).get('label', action).lower()} done.")
	next_url = request.POST.get("next")
	if next_url and next_url.startswith("/") and "//" not in next_url:
		return redirect(next_url)
	return redirect("carbon:activity_detail", pk=activity.pk)


# --------------------------------------------------------------------------- #
# Delete / archive / restore (soft-delete keeps the audit trail)
# --------------------------------------------------------------------------- #
@login_required
@role_required(*CARBON_CREATOR_ROLES)
@require_POST
def activity_delete(request, pk):
	company = _require_company(request)
	if company is None:
		return redirect(f"{reverse('accounts:dashboard')}?section=company")

	activity = _scoped_activity_or_404(request.user, company, pk)
	label = activity.display_name
	try:
		workflow.delete_activity(activity, request.user)
	except workflow.WorkflowError as error:
		messages.error(request, str(error))
		return redirect("carbon:activity_detail", pk=pk)
	messages.success(request, f'Draft activity "{label}" deleted.')
	return redirect("carbon:activity_list")


@login_required
@role_required(*CARBON_CREATOR_ROLES)
@require_POST
def activity_archive(request, pk):
	company = _require_company(request)
	if company is None:
		return redirect(f"{reverse('accounts:dashboard')}?section=company")

	activity = _scoped_activity_or_404(request.user, company, pk)
	try:
		workflow.archive_activity(activity, request.user, request.POST.get("comment", ""))
	except workflow.WorkflowError as error:
		messages.error(request, str(error))
	else:
		messages.success(
			request,
			"Activity archived. Its history is preserved and it can be restored.",
		)
	return redirect("carbon:activity_detail", pk=activity.pk)


@login_required
@role_required(*CARBON_CREATOR_ROLES)
@require_POST
def activity_restore(request, pk):
	company = _require_company(request)
	if company is None:
		return redirect(f"{reverse('accounts:dashboard')}?section=company")

	activity = _scoped_activity_or_404(request.user, company, pk)
	try:
		workflow.restore_activity(activity, request.user, request.POST.get("comment", ""))
	except workflow.WorkflowError as error:
		messages.error(request, str(error))
	else:
		messages.success(request, "Activity restored to the live inventory.")
	return redirect("carbon:activity_detail", pk=activity.pk)


# --------------------------------------------------------------------------- #
# Evidence download (Phase 10 — permission-checked, never a public media URL)
# --------------------------------------------------------------------------- #
@login_required
@role_required(*CARBON_VIEWER_ROLES)
def activity_evidence(request, pk):
	company = _require_company(request)
	if company is None:
		raise Http404("Activity not found.")

	# Reuses the tenant/role scoping: another company's evidence is a 404.
	activity = _scoped_activity_or_404(request.user, company, pk)
	if not activity.evidence:
		raise Http404("No evidence is attached to this activity.")
	try:
		handle = activity.evidence.open("rb")
	except (FileNotFoundError, OSError):
		raise Http404("The evidence file is no longer available.")

	filename = os.path.basename(activity.evidence.name)
	return FileResponse(handle, as_attachment=True, filename=filename)


# --------------------------------------------------------------------------- #
# Emission factor library (Phase 2 / 11)
# --------------------------------------------------------------------------- #
@login_required
@role_required(*CARBON_VIEWER_ROLES)
def factor_library(request):
	company = _require_company(request)
	if company is None and request.user.role != User.Role.SUPER_ADMIN:
		return redirect(f"{reverse('accounts:dashboard')}?section=company")

	queryset = _factor_queryset(request.user, company)
	category = request.GET.get("category") or ""
	scope = request.GET.get("scope") or ""
	region = (request.GET.get("region") or "").strip()
	status = request.GET.get("status") or ""
	year = request.GET.get("year") or ""
	search = (request.GET.get("q") or "").strip()
	# Shared factors a tenant has deactivated for itself count as inactive here.
	restricted_ids = recommendation.company_restricted_factor_ids(company)
	if category:
		queryset = queryset.filter(category=category)
	if scope:
		queryset = queryset.filter(scope=scope)
	if region:
		queryset = queryset.filter(region__icontains=region)
	if status == "ACTIVE":
		queryset = queryset.filter(is_active=True).exclude(pk__in=restricted_ids)
	elif status == "INACTIVE":
		queryset = queryset.filter(Q(is_active=False) | Q(pk__in=restricted_ids))
	if year.isdigit():
		queryset = queryset.filter(year=int(year))
	if search:
		queryset = queryset.filter(name__icontains=search) | queryset.filter(source__icontains=search)
	queryset = queryset.order_by("category", "region", "name", "-year")
	page_data = _paginate(request, queryset)
	base_queryset = _factor_queryset(request.user, company)
	states = recommendation.company_factor_states(company)
	for factor in page_data["items"]:
		factor.effective_active = recommendation.effective_is_active(
			factor, company, states=states
		)
		factor.effective = recommendation.effective_factor_attrs(
			factor, company, states=states
		)
		state = states.get(factor.pk)
		factor.has_override = bool(state and state.has_value_override)
		factor.can_edit = _can_edit_factor(request.user, company, factor)
		factor.can_toggle = _can_toggle_factor(request.user, company, factor)
	all_factors = list(base_queryset)
	active_count = sum(
		1
		for factor in all_factors
		if recommendation.effective_is_active(factor, company, states=states)
	)

	context = _module_context(request, company, "Emission factors")
	context.update({
		"factors": page_data["items"],
		"page": page_data,
		"regions": (
			base_queryset
			.values_list("region", flat=True)
			.distinct()
			.order_by("region")
		),
		"category_choices": ActivityCategory.choices,
		"scope_choices": Scope.choices,
		"years": base_queryset.values_list("year", flat=True).distinct().order_by("-year"),
		"selected": {"category": category, "scope": scope, "region": region, "status": status, "year": year, "q": search},
		"total": len(all_factors),
		"active_count": active_count,
		"inactive_count": len(all_factors) - active_count,
		"scope_metrics": [
			{"label": label, "count": base_queryset.filter(scope=scope_key).count()}
			for scope_key, label in Scope.choices
		],
		"verified_count": base_queryset.filter(verification_status=VerificationStatus.VERIFIED).count(),
		"demo_count": base_queryset.filter(verification_status=VerificationStatus.DEMO).count(),
		"can_manage_factors": _can_manage_factors(request.user, company),
	})
	return render(request, "carbon/factor_library.html", context)


@login_required
@role_required(*CARBON_VIEWER_ROLES)
def factor_detail(request, pk):
	company = _require_company(request)
	if company is None and request.user.role != User.Role.SUPER_ADMIN:
		raise Http404("Emission factor not found.")
	factor = _scoped_factor_or_404(request.user, company, pk)
	activities = (
		CarbonActivity.objects.filter(emission_factor=factor)
		if request.user.role == User.Role.SUPER_ADMIN
		else tenancy.visible_activities(request.user, company).filter(emission_factor=factor)
	)
	states = recommendation.company_factor_states(company)
	state = states.get(factor.pk)
	context = _module_context(request, company, "Emission factors")
	context.update({
		"factor": factor,
		"effective_active": recommendation.effective_is_active(factor, company, states=states),
		"effective": recommendation.effective_factor_attrs(factor, company, states=states),
		"has_override": bool(state and state.has_value_override),
		"used_count": activities.count(),
		"activities": activities.select_related("company", "created_by")[:10],
		"can_edit_factor": _can_edit_factor(request.user, company, factor),
		"can_toggle_factor": _can_toggle_factor(request.user, company, factor),
	})
	return render(request, "carbon/factor_detail.html", context)


@login_required
@role_required(*CARBON_VIEWER_ROLES)
@require_http_methods(["GET", "POST"])
def factor_create(request):
	company = _require_company(request)
	if not _can_manage_factors(request.user, company):
		raise PermissionDenied
	form = EmissionFactorForm(request.POST or None)
	if request.method == "POST" and form.is_valid():
		factor = form.save(commit=False)
		factor.company = None if request.user.role == User.Role.SUPER_ADMIN else company
		factor.created_by = request.user
		factor.save()
		messages.success(request, "Emission factor added to the library.")
		return redirect("carbon:factor_detail", pk=factor.pk)
	context = _module_context(request, company, "Emission factors")
	context.update({"form": form, "form_title": "Add emission factor"})
	return render(request, "carbon/factor_form.html", context)


@login_required
@role_required(*CARBON_VIEWER_ROLES)
@require_http_methods(["GET", "POST"])
def factor_edit(request, pk):
	company = _require_company(request)
	if company is None and request.user.role != User.Role.SUPER_ADMIN:
		raise Http404("Emission factor not found.")
	factor = _scoped_factor_or_404(request.user, company, pk)
	if not _can_manage_factors(request.user, company, factor):
		raise PermissionDenied
	# A tenant customises a shared factor for itself only; the shared record and
	# every other tenant are never touched. Platform admins edit the record itself.
	if factor.is_shared and request.user.role != User.Role.SUPER_ADMIN:
		states = recommendation.company_factor_states(company)
		form = EmissionFactorOverrideForm(
			request.POST or None,
			initial=recommendation.effective_factor_attrs(factor, company, states=states),
		)
		if request.method == "POST" and form.is_valid():
			recommendation.set_company_factor_overrides(
				factor=factor, company=company, data=form.cleaned_data, user=request.user
			)
			messages.success(
				request,
				"Your company's values were saved. Other companies keep using the shared "
				"values, and existing activities retain their stored calculation.",
			)
			return redirect("carbon:factor_detail", pk=factor.pk)
		context = _module_context(request, company, "Emission factors")
		context.update({
			"form": form,
			"form_title": f"Edit values for {factor.name}",
			"factor": factor,
			"is_override": True,
			"platform_values": recommendation.effective_factor_attrs(factor, None),
		})
		return render(request, "carbon/factor_form.html", context)
	form = EmissionFactorForm(request.POST or None, instance=factor)
	if request.method == "POST" and form.is_valid():
		factor = form.save(commit=False)
		factor.save()
		messages.success(request, "Emission factor updated. Existing approved activities retain their stored calculation and source details.")
		return redirect("carbon:factor_detail", pk=factor.pk)
	context = _module_context(request, company, "Emission factors")
	context.update({"form": form, "form_title": "Edit emission factor", "factor": factor})
	return render(request, "carbon/factor_form.html", context)


@login_required
@role_required(*CARBON_VIEWER_ROLES)
@require_POST
def factor_toggle_status(request, pk):
	company = _require_company(request)
	if company is None and request.user.role != User.Role.SUPER_ADMIN:
		raise Http404("Emission factor not found.")
	factor = _scoped_factor_or_404(request.user, company, pk)
	if not _can_toggle_factor(request.user, company, factor):
		raise PermissionDenied
	if factor.is_shared and request.user.role != User.Role.SUPER_ADMIN:
		# A tenant only toggles its own override; the shared record is untouched.
		state = recommendation.set_company_factor_active(
			factor=factor,
			company=company,
			is_active=not recommendation.effective_is_active(factor, company),
			user=request.user,
		)
		messages.success(
			request,
			f"Shared emission factor {state.status_label.lower()} for your company. "
			"Other companies are unaffected.",
		)
	else:
		factor.is_active = not factor.is_active
		factor.save(update_fields=("is_active", "updated_at"))
		messages.success(request, f"Emission factor {factor.status_label.lower()}.")
	return redirect("carbon:factor_detail", pk=factor.pk)


# --------------------------------------------------------------------------- #
# Anomaly detection (Machine Learning — scikit-learn IsolationForest)
# --------------------------------------------------------------------------- #
def _scoped_anomaly_or_404(user, company, pk):
	"""Resolve an anomaly inside the user's tenant/role scope, else 404.

	The queryset always starts from the caller's company, so a manipulated id
	can never reach another tenant's row (404, never 403, to avoid probing).
	"""
	anomaly = (
		tenancy.visible_anomalies(user, company)
		.select_related("activity", "department", "detected_by")
		.filter(pk=pk)
		.first()
	)
	if anomaly is None:
		raise Http404("Anomaly not found.")
	return anomaly


@login_required
@role_required(*CARBON_VIEWER_ROLES)
def anomaly_list(request):
	company = _require_company(request)
	if company is None:
		return redirect(f"{reverse('accounts:dashboard')}?section=company")

	visible = tenancy.visible_anomalies(request.user, company)
	years = sorted(
		{
			year
			for year in visible.values_list("period_start__year", flat=True)
			if year is not None
		},
		reverse=True,
	)
	form = AnomalyFilterForm(request.GET or None, company=company, years=years)
	filtered = _apply_anomaly_filters(visible, form)
	counts = anomaly_detection.anomaly_counts(filtered)
	base_params = request.GET.copy()
	base_params.pop("page", None)
	page_data = _paginate(
		request,
		filtered.order_by("-detected_at", "-id").select_related(
			"activity", "department", "detected_by"
		),
	)

	context = _module_context(request, company, "Anomalies")
	context.update({
		"filter_form": form,
		"pagination": page_data,
		"counts": counts,
		"base_query": base_params.urlencode(),
		"can_run": request.user.role == User.Role.SUPER_ADMIN
		or request.user.role in ANOMALY_MANAGE_ROLES,
	})
	return render(request, "carbon/anomaly_list.html", context)


@login_required
@role_required(*CARBON_VIEWER_ROLES)
@require_POST
def anomaly_run(request):
	"""Run IsolationForest on the current company's approved activities."""
	company = _require_company(request)
	if company is None:
		return redirect(f"{reverse('accounts:dashboard')}?section=company")
	if request.user.role != User.Role.SUPER_ADMIN and request.user.role not in ANOMALY_MANAGE_ROLES:
		raise PermissionDenied

	result = anomaly_detection.detect_anomalies(company, actor=request.user)
	if result.insufficient:
		messages.info(request, result.message)
	elif result.detected:
		messages.success(request, f"{result.message} Review them below.")
	else:
		messages.info(request, result.message)
	return redirect("carbon:anomaly_list")


@login_required
@role_required(*CARBON_VIEWER_ROLES)
def anomaly_detail(request, pk):
	company = _require_company(request)
	if company is None:
		return redirect(f"{reverse('accounts:dashboard')}?section=company")

	anomaly = _scoped_anomaly_or_404(request.user, company, pk)
	activity = anomaly.activity
	# Only expose the linked activity when it is itself inside the user's scope.
	can_view_activity = (
		activity is not None
		and tenancy.visible_activities(request.user, company).filter(pk=activity.pk).exists()
	)
	context = _module_context(request, company, "Anomalies")
	context.update({
		"anomaly": anomaly,
		"activity": activity if can_view_activity else None,
		"can_manage": request.user.role == User.Role.SUPER_ADMIN
		or request.user.role in ANOMALY_MANAGE_ROLES,
	})
	return render(request, "carbon/anomaly_detail.html", context)


@login_required
@role_required(*CARBON_VIEWER_ROLES)
@require_POST
def anomaly_status(request, pk):
	"""Triage an anomaly: mark it REVIEWED or DISMISSED."""
	company = _require_company(request)
	if company is None:
		return redirect(f"{reverse('accounts:dashboard')}?section=company")
	if request.user.role != User.Role.SUPER_ADMIN and request.user.role not in ANOMALY_MANAGE_ROLES:
		raise PermissionDenied

	anomaly = _scoped_anomaly_or_404(request.user, company, pk)
	new_status = request.POST.get("status", "")
	if new_status not in AnomalyStatus.values:
		messages.error(request, "Unknown anomaly status.")
		return redirect("carbon:anomaly_detail", pk=anomaly.pk)

	anomaly.status = new_status
	anomaly.save(update_fields=("status", "updated_at"))
	messages.success(request, f"Anomaly marked {anomaly.get_status_display().lower()}.")
	return redirect("carbon:anomaly_detail", pk=anomaly.pk)
