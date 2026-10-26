"""ESG / carbon risk assessment views.

Tenant isolation mirrors the rest of CarbonPilot: the company is always
resolved from the authenticated user (never from the request body), and the
assessment service only ever reads that company's data. A role check guards
who may run an assessment; auditors and other viewers can read it.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from apps.accounts.models import User
from apps.accounts.permissions import role_required
from apps.carbon.services.tenancy import company_for_user
from apps.carbon.views import CARBON_VIEWER_ROLES
from apps.carbon.views import _sidebar as carbon_sidebar

from .models import RiskAssessment
from .services import risk_assessment

# Members who may compute a new assessment (auditors are read-only).
RISK_MANAGE_ROLES = (User.Role.COMPANY_OWNER, User.Role.ESG_REPORTING_MANAGER)

HISTORY_LIMIT = 10


def _module_context(request, company, active):
	return {
		"company": company,
		"sidebar_nav": carbon_sidebar(active, request.user),
	}


def _require_company(request):
	"""Resolve the tenant from the user; never trusts client input."""
	company = company_for_user(request.user)
	if company is None:
		messages.warning(request, "Set up your company before assessing risk.")
	return company


def _can_run(user):
	return user.role == User.Role.SUPER_ADMIN or user.role in RISK_MANAGE_ROLES


@login_required
@role_required(*CARBON_VIEWER_ROLES)
def risk_assessment_view(request):
	company = _require_company(request)
	if company is None:
		return redirect(f"{reverse('accounts:dashboard')}?section=company")

	# Every row read here is filtered by the caller's own company.
	latest = RiskAssessment.objects.filter(company=company).latest_first().first()
	history = list(
		RiskAssessment.objects.filter(company=company).latest_first()[:HISTORY_LIMIT]
	)
	context = _module_context(request, company, "Risk assessment")
	context.update({
		"assessment": latest,
		"history": history,
		"can_run": _can_run(request.user),
		"scoring_weights": risk_assessment.SCORING_WEIGHTS,
	})
	return render(request, "risk/assessment.html", context)


@login_required
@role_required(*CARBON_VIEWER_ROLES)
@require_POST
def risk_assessment_run(request):
	company = _require_company(request)
	if company is None:
		return redirect(f"{reverse('accounts:dashboard')}?section=company")
	if not _can_run(request.user):
		raise PermissionDenied

	# period_year is an optional filter; anything else is ignored, and the
	# company itself is never taken from the request.
	period_year = None
	raw_year = (request.POST.get("period_year") or "").strip()
	if raw_year.isdigit():
		period_year = int(raw_year)

	assessment = risk_assessment.run_assessment(
		company, actor=request.user, period_year=period_year
	)
	messages.success(
		request,
		f"Risk assessment complete · {assessment.get_level_display()} risk "
		f"({assessment.score}/100).",
	)
	return redirect("risk:risk_assessment")
