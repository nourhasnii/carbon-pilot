"""Views for "Ask CarbonPilot".

Tenant isolation follows the platform convention: the company is resolved from
the authenticated user (never from the URL or the form), and only company-wide
roles (Company Owner, ESG Reporting Manager, Auditor) may use the assistant,
because its context spans the whole company. The question history is kept in the
user's session only — nothing is persisted server-side.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from apps.accounts.models import User
from apps.accounts.permissions import role_required
from apps.carbon.services.tenancy import company_for_user
from apps.carbon.views import _sidebar as carbon_sidebar

from .services import assistant

ASSISTANT_LABEL = "Ask CarbonPilot"

# Roles that may use the assistant (they already see the whole company's data).
ASSISTANT_ROLES = (
	User.Role.COMPANY_OWNER,
	User.Role.ESG_REPORTING_MANAGER,
	User.Role.AUDITOR,
)

# What the assistant can read, shown in the UI.
SOURCE_LEGEND = (
	("Activities", "Approved emissions by scope, department and category"),
	("Data Quality", "Activity completeness and evidence coverage"),
	("Objectives", "Reduction objectives and their latest predictions"),
	("Anomalies", "Flagged anomalies and their severity"),
	("Risk Assessment", "Latest rule-based risk score and drivers"),
	("Suppliers", "Supplier ESG status, risk and linked Scope 3 emissions"),
	("Recommendations", "Open corrective recommendations (context only)"),
)


def _sidebar(active, user):
	"""Carbon navigation with the Ask CarbonPilot item inserted after Recommendations."""
	items = carbon_sidebar(active, user)
	entry = {
		"label": ASSISTANT_LABEL,
		"icon": "message-square-text",
		"url_name": "assistant:assistant_page",
		"href": reverse("assistant:assistant_page"),
		"active": active == ASSISTANT_LABEL,
	}
	index = next(
		(i for i, item in enumerate(items) if item.get("label") == "Recommendations"), None
	)
	if index is None:
		items.append(entry)
	else:
		items.insert(index + 1, entry)
	return items


def _require_company(request):
	company = company_for_user(request.user)
	if company is None:
		messages.warning(request, "Set up your company before using Ask CarbonPilot.")
	return company


@login_required
@role_required(*ASSISTANT_ROLES)
def assistant_page(request):
	company = _require_company(request)
	if company is None:
		return redirect(f"{reverse('accounts:dashboard')}?section=company")

	history = list(reversed(assistant.get_history(request)))
	return render(request, "assistant/assistant.html", {
		"company": company,
		"sidebar_nav": _sidebar(ASSISTANT_LABEL, request.user),
		"history": history,
		"latest": history[0] if history else None,
		"source_legend": SOURCE_LEGEND,
	})


@login_required
@role_required(*ASSISTANT_ROLES)
@require_POST
def assistant_ask(request):
	company = _require_company(request)
	if company is None:
		return redirect(f"{reverse('accounts:dashboard')}?section=company")

	question = (request.POST.get("question") or "").strip()
	result = assistant.ask(request.user, company, question)
	if not result.ok:
		messages.warning(request, result.message)
		return redirect("assistant:assistant_page")

	assistant.push_history(request, question, result)
	if result.ai_unavailable:
		messages.warning(
			request,
			"The AI model was unavailable, so a factual summary of your data was returned instead.",
		)
	return redirect("assistant:assistant_page")


@login_required
@role_required(*ASSISTANT_ROLES)
@require_POST
def assistant_clear(request):
	assistant.clear_history(request)
	messages.success(request, "Conversation cleared.")
	return redirect("assistant:assistant_page")
