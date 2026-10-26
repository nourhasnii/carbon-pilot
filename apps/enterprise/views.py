from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.urls import reverse

from apps.accounts.models import User
from apps.accounts.permissions import role_required
from apps.accounts.views import ROLE_SIDEBAR
from apps.carbon.services.tenancy import company_for_user

from .services import company_intelligence as intelligence_service


def _sidebar(user):
	"""The company workspace navigation, with the current page flagged active."""
	items = []
	for item in ROLE_SIDEBAR.get(user.role, ()):
		entry = dict(item)
		entry["active"] = entry.get("key") == "intelligence"
		items.append(entry)
	return items


@login_required
@role_required(User.Role.COMPANY_OWNER)
def company_dashboard(request):
	return redirect(f"{reverse('accounts:dashboard')}?section=company")


@login_required
@role_required(User.Role.COMPANY_OWNER)
def company_profile(request):
	return redirect(f"{reverse('accounts:dashboard')}?section=company")


@login_required
@role_required(User.Role.COMPANY_OWNER)
def company_intelligence(request):
	"""A 360° ESG / carbon view built only from the caller's own company data."""
	company = company_for_user(request.user)
	if company is None:
		return redirect(f"{reverse('accounts:dashboard')}?section=company")

	intelligence = intelligence_service.build_intelligence(
		company, period_year=request.GET.get("year")
	)
	return render(request, "enterprise/intelligence.html", {
		"company": company,
		"sidebar_nav": _sidebar(request.user),
		"intelligence": intelligence,
	})
