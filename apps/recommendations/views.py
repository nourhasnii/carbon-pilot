"""Views for the AI recommendations workspace.

Tenant isolation follows the platform convention: the company is resolved from
the authenticated user (never from the URL), unreachable objects return 404 (not
403) so tenants cannot probe for IDs, and every status change is re-authorised
server-side.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from apps.accounts.permissions import role_required
from apps.analytics.models import AiJob, JobStatus, JobType
from apps.carbon.views import _sidebar as carbon_sidebar
from apps.common.listing import paginate
from apps.enterprise.models import Department

from .forms import RecommendationFilterForm
from .models import (
	GenerationMethod,
	OPEN_STATUSES,
	RecommendationPriority,
	RecommendationSource,
	RecommendationStatus,
)
from .services import access, jobs, recommendations


def _module_context(request, company, active):
	return {
		"company": company,
		"sidebar_nav": carbon_sidebar(active, request.user),
	}


def _require_company(request):
	company = access.company_for_user(request.user)
	if company is None:
		messages.warning(request, "Set up your company before using AI recommendations.")
	return company


def _scoped_or_404(user, company, pk):
	recommendation = (
		access.visible_recommendations(user, company)
		.select_related("department", "objective", "anomaly")
		.filter(pk=pk)
		.first()
	)
	if recommendation is None:
		# 404 (never 403) so other tenants cannot probe for existing IDs.
		raise Http404("Recommendation not found.")
	return recommendation


def _safe_next(request):
	"""Only follow a relative ``next`` on the same host."""
	target = request.POST.get("next") or ""
	if target and url_has_allowed_host_and_scheme(
		target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
	):
		return target
	return reverse("recommendations:recommendation_list")


def _apply_recommendation_filters(queryset, form):
	"""Narrow a company-scoped recommendation queryset by the shared filter bar.

	Keeps the existing default: when no status is chosen only OPEN
	recommendations (NEW / IN_PROGRESS) are shown. Invalid filter input is
	ignored safely (the form is invalid, so the default open view is returned).
	"""
	if not form.is_valid():
		return queryset.filter(status__in=OPEN_STATUSES)
	data = form.cleaned_data
	if data.get("priority"):
		queryset = queryset.filter(priority=data["priority"])
	if data.get("status"):
		queryset = queryset.filter(status=data["status"])
	else:
		queryset = queryset.filter(status__in=OPEN_STATUSES)
	if data.get("source"):
		queryset = queryset.filter(source=data["source"])
	if data.get("department"):
		queryset = queryset.filter(department=data["department"])
	return queryset


@login_required
@role_required(*access.VIEWER_ROLES)
def recommendation_list(request):
	company = _require_company(request)
	if company is None:
		return redirect(f"{reverse('accounts:dashboard')}?section=company")

	visible = access.visible_recommendations(request.user, company).select_related(
		"department", "objective", "anomaly"
	)
	form = RecommendationFilterForm(request.GET or None, company=company)
	queryset = _apply_recommendation_filters(visible, form)
	pagination = paginate(request, queryset)
	for item in pagination["items"]:
		item.user_can_manage = access.can_update_status(request.user, company, item)

	context = _module_context(request, company, "Recommendations")
	context.update({
		"recommendations": pagination["items"],
		"pagination": pagination,
		"filter_form": form,
		"priority_choices": RecommendationPriority.choices,
		"status_choices": RecommendationStatus.choices,
		"source_choices": RecommendationSource.choices,
		"departments": Department.objects.filter(company=company).order_by("name"),
		"selected": {
			"priority": request.GET.get("priority") or "",
			"status": request.GET.get("status") or "",
			"source": request.GET.get("source") or "",
			"department": request.GET.get("department") or "",
		},
		"has_filters": any(
			request.GET.get(key) for key in ("priority", "status", "source", "department")
		),
		"can_generate": access.can_generate(request.user, company),
		# A Pending/Running job (if any) drives the async status banner.
		"ai_job": AiJob.objects.latest_active(
			company, JobType.RECOMMENDATION_GENERATION
		),
		"open_count": visible.filter(status__in=OPEN_STATUSES).count(),
		"high_count": visible.filter(
			status__in=OPEN_STATUSES, priority=RecommendationPriority.HIGH
		).count(),
		"qwen_count": visible.filter(generation_method=GenerationMethod.QWEN).count(),
		"rule_count": visible.filter(generation_method=GenerationMethod.RULE_BASED).count(),
	})
	return render(request, "recommendations/list.html", context)


@login_required
@role_required(*access.VIEWER_ROLES)
def recommendation_detail(request, pk):
	company = _require_company(request)
	if company is None:
		return redirect(f"{reverse('accounts:dashboard')}?section=company")

	recommendation = _scoped_or_404(request.user, company, pk)
	context = _module_context(request, company, "Recommendations")
	context.update({
		"recommendation": recommendation,
		"can_manage": access.can_update_status(request.user, company, recommendation),
		"evidence_items": recommendation.evidence_items or [],
		"action_plan": recommendation.action_plan or [],
		"status_choices": RecommendationStatus.choices,
	})
	return render(request, "recommendations/detail.html", context)


@login_required
@role_required(*access.MANAGER_ROLES)
@require_POST
def recommendation_start_action(request, pk):
	company = _require_company(request)
	if company is None:
		return redirect(f"{reverse('accounts:dashboard')}?section=company")

	recommendation = _scoped_or_404(request.user, company, pk)
	result = recommendations.start_action(request.user, company, recommendation)
	if not result.ok:
		messages.warning(request, result.message)
	else:
		messages.success(request, result.message)
	return redirect(_safe_next(request))


@login_required
@role_required(*access.MANAGER_ROLES)
@require_POST
def recommendation_generate(request):
	"""Enqueue the long Qwen generation job and return immediately.

	The Qwen inference can take minutes, so it runs in a Celery worker instead
	of holding the request open. All authorisation still happens here, before
	anything is queued; the outcome is reported by the status endpoint the page
	polls.
	"""
	company = _require_company(request)
	if company is None:
		return redirect(f"{reverse('accounts:dashboard')}?section=company")
	if not access.can_generate(request.user, company):
		raise PermissionDenied

	result = jobs.enqueue_recommendation_generation(request.user, company)
	if not result.ok:
		messages.warning(request, result.message)
	elif result.job is not None and result.job.is_terminal:
		# The task ran inline (eager mode / tests): report the outcome now
		# instead of handing the user a job that is already finished.
		if result.job.status == JobStatus.FAILED:
			messages.warning(request, result.job.message)
		else:
			messages.success(request, result.job.message)
	elif result.reused:
		messages.info(request, result.message)
	else:
		messages.success(request, result.message)
	return redirect("recommendations:recommendation_list")


@login_required
@role_required(*access.VIEWER_ROLES)
def ai_job_status(request):
	"""Tenant-scoped JSON status of the company's recommendation job.

	The recommendations page polls this to move the banner through
	Pending → Running → Done/Failed. Company scoping is identical to every other
	object in the platform, and an id belonging to another tenant returns 404 so
	tenants cannot probe for existing ids.
	"""
	company = _require_company(request)
	if company is None:
		raise Http404("No company.")

	queryset = AiJob.objects.for_company(company).filter(
		job_type=JobType.RECOMMENDATION_GENERATION
	)
	job_id = request.GET.get("job") or ""
	if job_id:
		if not job_id.isdigit():
			raise Http404("Unknown AI job.")
		job = queryset.filter(pk=int(job_id)).first()
		if job is None:
			raise Http404("AI job not found.")
	else:
		job = queryset.first()

	if job is None:
		return JsonResponse({"job": None})

	# A dead worker writes nothing, so the poll is also where a stuck job is
	# turned into a clear FAILED state (timed out / no worker picked it up).
	job.refresh_stale()
	return JsonResponse({"job": job.as_status_payload()})


@login_required
@role_required(*access.MANAGER_ROLES)
@require_POST
def recommendation_set_status(request, pk, status):
	company = _require_company(request)
	if company is None:
		return redirect(f"{reverse('accounts:dashboard')}?section=company")

	target = (status or "").upper()
	if target not in RecommendationStatus.values:
		raise Http404("Unknown status.")

	recommendation = _scoped_or_404(request.user, company, pk)
	if not access.can_update_status(request.user, company, recommendation):
		raise PermissionDenied

	recommendation.status = target
	recommendation.save(update_fields=("status", "updated_at"))
	messages.success(
		request,
		f"Recommendation marked {RecommendationStatus(target).label}.",
	)
	return redirect(_safe_next(request))
