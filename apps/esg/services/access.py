"""Role rules and tenant scoping for the ESG module.

Mirrors the rest of CarbonPilot: the company is always resolved from the
authenticated user (never from the request), and every queryset starts from
that company so another tenant's record is a 404 rather than a 403. Auditors
are read-only; verification is a control function restricted to governance
roles and auditors.
"""

from apps.accounts.models import User
from apps.carbon.services.tenancy import company_for_user  # re-exported for views
from django.db.models import Q

from ..models import Indicator, IndicatorCatalogEntry, IndicatorValue

# Members who may open the ESG workspace (auditors are read-only).
VIEWER_ROLES = (
	User.Role.COMPANY_OWNER,
	User.Role.DEPARTMENT_MANAGER,
	User.Role.ESG_REPORTING_MANAGER,
	User.Role.AUDITOR,
)

# Members who may create/edit indicators and report values.
MANAGE_ROLES = (
	User.Role.COMPANY_OWNER,
	User.Role.ESG_REPORTING_MANAGER,
	User.Role.DEPARTMENT_MANAGER,
)

# Members who may verify a reported value (a control function).
VERIFY_ROLES = (
	User.Role.COMPANY_OWNER,
	User.Role.ESG_REPORTING_MANAGER,
	User.Role.AUDITOR,
)

# Members who may run the AI coach and persist assessments (governance roles).
ADMIN_ROLES = (User.Role.COMPANY_OWNER, User.Role.ESG_REPORTING_MANAGER)


def is_company_admin(user):
	return user.role == User.Role.SUPER_ADMIN or user.role in ADMIN_ROLES


def is_viewer(user):
	return user.role == User.Role.SUPER_ADMIN or user.role in VIEWER_ROLES


def can_verify(user):
	return user.role == User.Role.SUPER_ADMIN or user.role in VERIFY_ROLES


def _department_name_matches(user, department_name):
	return bool(user.department) and user.department == department_name


def can_manage(user, company, indicator=None):
	"""Server-side guard for creating/editing an indicator or reporting a value.

	Company owners and the ESG reporting manager manage every indicator of their
	tenant. A department manager may create an indicator and may only manage the
	ones assigned to their own department.
	"""
	if company is None:
		return False
	if user.role == User.Role.SUPER_ADMIN or user.role in ADMIN_ROLES:
		return True
	if user.role == User.Role.DEPARTMENT_MANAGER:
		if indicator is None:
			return True
		if indicator.department_id:
			return indicator.department.company_id == company.pk and _department_name_matches(
				user, indicator.department.name
			)
		return False
	return False


def visible_indicators(user, company):
	"""Indicators a user is allowed to see, scoped to their tenant and role."""
	if company is None:
		return Indicator.objects.none()
	queryset = Indicator.objects.filter(company=company)
	if user.role == User.Role.SUPER_ADMIN or user.role in VIEWER_ROLES:
		return queryset
	return queryset.none()


def visible_values(user, company):
	if company is None:
		return IndicatorValue.objects.none()
	return IndicatorValue.objects.filter(company=company)


def catalog_entries(company, *, active_only=True):
	"""Shared entries plus the tenant's own, ready for a picker."""
	queryset = IndicatorCatalogEntry.objects.for_company(company)
	if active_only:
		queryset = queryset.filter(is_active=True)
	return queryset.order_by("pillar", "sort_order", "code")


def company_members(company):
	"""Active users belonging to a company (its owner plus its members)."""
	if company is None:
		return User.objects.none()
	return (
		User.objects.filter(status=User.Status.ACTIVE)
		.filter(Q(company_membership=company) | Q(company_profile__pk=company.pk))
		.distinct()
		.order_by("first_name", "last_name", "email")
	)


__all__ = [
	"ADMIN_ROLES",
	"MANAGE_ROLES",
	"VERIFY_ROLES",
	"VIEWER_ROLES",
	"can_manage",
	"can_verify",
	"catalog_entries",
	"company_for_user",
	"company_members",
	"is_company_admin",
	"is_viewer",
	"visible_indicators",
	"visible_values",
]
