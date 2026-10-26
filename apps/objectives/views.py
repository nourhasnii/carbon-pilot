from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import OuterRef, Subquery
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from apps.accounts.models import User
from apps.accounts.permissions import role_required
from apps.analytics.models import ModelKind, ModelMetrics
from apps.carbon.views import _sidebar as carbon_sidebar
from apps.common.listing import paginate
from apps.enterprise.models import Department

from .forms import ObjectiveFilterForm, ObjectiveForm
from .models import Objective, ObjectiveStatus, PeriodType, PredictionRun, RiskLevel
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


def _apply_objective_filters(queryset, form):
	"""Narrow a company-scoped objective queryset by the shared filter bar.

	Keeps the default behaviour: when no status is chosen the live view hides
	ARCHIVED objectives. Invalid filter input is ignored safely (the form is
	invalid, so the default live view is returned unchanged).
	"""
	if not form.is_valid():
		return queryset.exclude(status=ObjectiveStatus.ARCHIVED)
	data = form.cleaned_data
	if data.get("q"):
		queryset = queryset.filter(name__icontains=data["q"].strip())
	if data.get("status"):
		queryset = queryset.filter(status=data["status"])
	else:
		queryset = queryset.exclude(status=ObjectiveStatus.ARCHIVED)
	if data.get("department"):
		queryset = queryset.filter(department=data["department"])
	if data.get("year"):
		queryset = queryset.filter(year=int(data["year"]))
	if data.get("period_type"):
		queryset = queryset.filter(period_type=data["period_type"])
	if data.get("at_risk") == "1":
		# "At risk" reuses the most recent stored prediction: only the latest
		# PredictionRun per objective counts, and only if it is MEDIUM/HIGH.
		latest_risk = (
			PredictionRun.objects.filter(objective=OuterRef("pk"))
			.order_by("-created_at", "-id")
			.values("risk_level")[:1]
		)
		queryset = queryset.annotate(latest_prediction_risk=Subquery(latest_risk)).filter(
			latest_prediction_risk__in=(RiskLevel.MEDIUM, RiskLevel.HIGH)
		)
	return queryset


@login_required
@role_required(*VIEWER_ROLES)
def objective_list(request):
	company = _require_company(request)
	if company is None:
		return redirect(f"{reverse('accounts:dashboard')}?section=company")

	all_visible = visible_objectives(request.user, company).select_related("department")
	years = sorted({objective.year for objective in all_visible}, reverse=True)

	form = ObjectiveFilterForm(request.GET or None, company=company, years=years)
	queryset = _apply_objective_filters(all_visible, form)
	pagination = paginate(request, queryset)
	for objective in pagination["items"]:
		# Warm the cache so the template reads one figure per objective.
		objective.display_current = objective.current_tco2e

	context = _module_context(request, company, "Objectives")
	context.update({
		"objectives": pagination["items"],
		"pagination": pagination,
		"filter_form": form,
		"years": years or [timezone.localdate().year],
		"status_choices": ObjectiveStatus.choices,
		"period_choices": PeriodType.choices,
		"selected": {
			"year": request.GET.get("year") or "",
			"status": request.GET.get("status") or "",
			"period_type": request.GET.get("period_type") or "",
		},
		"has_filters": any(
			request.GET.get(key)
			for key in ("q", "status", "department", "year", "period_type", "at_risk")
		),
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
		"prediction_metrics": ModelMetrics.latest(
			company, ModelKind.OBJECTIVE_PREDICTION
		),
		"training_summary": objective_prediction.training_summary(company),
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
