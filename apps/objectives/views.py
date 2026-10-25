from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from apps.accounts.models import User
from apps.accounts.permissions import role_required
from apps.carbon.views import _sidebar as carbon_sidebar
from apps.enterprise.models import Department

from .forms import ObjectiveForm
from .models import Objective, ObjectiveStatus, PeriodType
from .services import (
	VIEWER_ROLES,
	can_manage,
	company_for_user,
	visible_objectives,
)
from .services import objective_prediction
from apps.recommendations.services import summary as recommendation_summary


def _module_context(request, company, active):
	return {
		"company": company,
		"sidebar_nav": carbon_sidebar(active, request.user),
	}


def _require_company(request):
	"""Resolve the tenant from the user; never trusts client input."""
	company = company_for_user(request.user)
	if company is None:
		messages.warning(request, "Set up your company before managing objectives.")
	return company


def _scoped_objective_or_404(user, company, pk):
	objective = visible_objectives(user, company).select_related("department").filter(pk=pk).first()
	if objective is None:
		# 404 (never 403) so other tenants cannot probe for existing IDs.
		raise Http404("Objective not found.")
	return objective


@login_required
@role_required(*VIEWER_ROLES)
def objective_list(request):
	company = _require_company(request)
	if company is None:
		return redirect(f"{reverse('accounts:dashboard')}?section=company")

	all_visible = visible_objectives(request.user, company).select_related("department")
	queryset = all_visible
	year = request.GET.get("year") or ""
	status = request.GET.get("status") or ""
	period_type = request.GET.get("period_type") or ""
	if year.isdigit():
		queryset = queryset.filter(year=int(year))
	if status:
		queryset = queryset.filter(status=status)
	else:
		queryset = queryset.exclude(status=ObjectiveStatus.ARCHIVED)
	if period_type:
		queryset = queryset.filter(period_type=period_type)

	objectives = list(queryset)
	for objective in objectives:
		# Warm the cache so the template reads one figure per objective.
		objective.display_current = objective.current_tco2e

	years = sorted({objective.year for objective in all_visible}, reverse=True)

	context = _module_context(request, company, "Objectives")
	context.update({
		"objectives": objectives,
		"years": years or [timezone.localdate().year],
		"status_choices": ObjectiveStatus.choices,
		"period_choices": PeriodType.choices,
		"selected": {"year": year, "status": status, "period_type": period_type},
		"can_manage": can_manage(request.user, company),
		"active_count": all_visible.filter(status=ObjectiveStatus.ACTIVE).count(),
		"achieved_count": all_visible.filter(status=ObjectiveStatus.ACHIEVED).count(),
		"live_count": all_visible.exclude(status=ObjectiveStatus.ARCHIVED).count(),
	})
	return render(request, "objectives/list.html", context)


@login_required
@role_required(*VIEWER_ROLES)
def objective_detail(request, pk):
	company = _require_company(request)
	if company is None:
		return redirect(f"{reverse('accounts:dashboard')}?section=company")

	objective = _scoped_objective_or_404(request.user, company, pk)
	activities = objective.scoped_activities().select_related(
		"department", "emission_factor", "created_by"
	)
	context = _module_context(request, company, "Objectives")
	context.update({
		"objective": objective,
		"current_tco2e": objective.current_tco2e,
		"progress_percent": objective.progress_percent,
		"gap_tco2e": objective.gap_tco2e,
		"reduction_required_tco2e": objective.reduction_required_tco2e,
		"period_start": objective.period_bounds()[0],
		"period_end": objective.period_bounds()[1],
		"activities": activities[:10],
		"activity_count": activities.count(),
		"can_manage": can_manage(request.user, company, objective),
		"prediction": objective_prediction.latest_prediction(objective),
		"recommendations": recommendation_summary.for_objective(objective),
	})
	return render(request, "objectives/detail.html", context)


@login_required
@role_required(*VIEWER_ROLES)
@require_http_methods(["GET", "POST"])
def objective_create(request):
	company = _require_company(request)
	if company is None:
		return redirect(f"{reverse('accounts:dashboard')}?section=company")
	if not can_manage(request.user, company):
		raise PermissionDenied

	form = ObjectiveForm(request.POST or None, company=company, user=request.user)
	if request.method == "POST" and form.is_valid():
		objective = form.save(commit=False)
		objective.company = company
		objective.created_by = request.user
		_apply_department_scope(objective, request.user, company)
		objective.status = objective.suggested_status()
		objective.save()
		messages.success(
			request,
			"Objective created. Progress is computed live from approved activities.",
		)
		return redirect("objectives:objective_detail", pk=objective.pk)

	context = _module_context(request, company, "Objectives")
	context.update({"form": form, "form_title": "New objective", "objective": None})
	return render(request, "objectives/form.html", context)


@login_required
@role_required(*VIEWER_ROLES)
@require_http_methods(["GET", "POST"])
def objective_edit(request, pk):
	company = _require_company(request)
	if company is None:
		return redirect(f"{reverse('accounts:dashboard')}?section=company")

	objective = _scoped_objective_or_404(request.user, company, pk)
	if not can_manage(request.user, company, objective):
		raise PermissionDenied

	form = ObjectiveForm(
		request.POST or None, instance=objective, company=company, user=request.user
	)
	if request.method == "POST" and form.is_valid():
		objective = form.save(commit=False)
		_apply_department_scope(objective, request.user, company)
		objective.status = objective.suggested_status()
		objective.save()
		messages.success(request, "Objective updated.")
		return redirect("objectives:objective_detail", pk=objective.pk)

	context = _module_context(request, company, "Objectives")
	context.update({"form": form, "form_title": "Edit objective", "objective": objective})
	return render(request, "objectives/form.html", context)


@login_required
@role_required(*VIEWER_ROLES)
@require_POST
def objective_refresh_status(request, pk):
	company = _require_company(request)
	if company is None:
		return redirect(f"{reverse('accounts:dashboard')}?section=company")

	objective = _scoped_objective_or_404(request.user, company, pk)
	status = objective.refresh_status()
	messages.success(request, f"Status refreshed: {ObjectiveStatus(status).label}.")
	return redirect("objectives:objective_detail", pk=objective.pk)


@login_required
@role_required(*VIEWER_ROLES)
@require_POST
def objective_run_prediction(request, pk):
	"""Train the ML model and store an achievement prediction for this objective."""
	company = _require_company(request)
	if company is None:
		return redirect(f"{reverse('accounts:dashboard')}?section=company")

	objective = _scoped_objective_or_404(request.user, company, pk)
	if not can_manage(request.user, company, objective):
		raise PermissionDenied

	result = objective_prediction.predict_objective(objective)
	if result.ok:
		run = result.run
		messages.success(
			request,
			f"AI prediction: {run.get_expected_outcome_display()} · "
			f"{run.probability_achievement:.1f}% probability · "
			f"{run.get_risk_level_display()} risk.",
		)
	else:
		messages.warning(request, result.message)
	return redirect("objectives:objective_detail", pk=objective.pk)


def _apply_department_scope(objective, user, company):
	"""A department manager can only ever target their own department."""
	if user.role == User.Role.DEPARTMENT_MANAGER:
		objective.department = Department.objects.filter(
			company=company, name=user.department
		).first()
