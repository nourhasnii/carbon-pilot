"""Tenant/role scoping and live-progress helpers for objectives.

Mirrors the carbon module's tenancy rules: the tenant is always resolved from the
authenticated user, never from request parameters, and department managers only
ever see objectives that concern their own department.
"""

from django.db.models import Q

from apps.accounts.models import User
from apps.carbon.services.tenancy import company_for_user  # re-exported for views

from ..models import Objective, ObjectiveStatus

# Roles that may read every objective of their company.
READ_ALL_ROLES = (
	User.Role.COMPANY_OWNER,
	User.Role.ESG_REPORTING_MANAGER,
	User.Role.AUDITOR,
)

# Roles that may create and manage company-wide objectives.
ADMIN_ROLES = (
	User.Role.COMPANY_OWNER,
	User.Role.ESG_REPORTING_MANAGER,
)

# Roles allowed to open the objectives workspace at all.
VIEWER_ROLES = (
	User.Role.COMPANY_OWNER,
	User.Role.DEPARTMENT_MANAGER,
	User.Role.ESG_REPORTING_MANAGER,
	User.Role.AUDITOR,
)


def is_company_admin(user):
	return user.role in ADMIN_ROLES


def can_manage(user, company, objective=None):
	"""Server-side guard for creating/editing an objective.

	Company owners (and the ESG reporting manager) manage every objective of
	their tenant. A department manager manages only objectives that target their
	own department — never company-wide ones.
	"""
	if company is None:
		return False
	if user.role in ADMIN_ROLES:
		return True
	if user.role == User.Role.DEPARTMENT_MANAGER:
		if not user.department:
			return False
		if objective is None:
			return True
		return objective.department_id is not None and objective.department.name == user.department
	return False


def visible_objectives(user, company):
	"""Objectives a user is allowed to see, scoped to their tenant and role."""
	queryset = Objective.objects.filter(company=company)
	if user.role in READ_ALL_ROLES:
		return queryset
	if user.role == User.Role.DEPARTMENT_MANAGER:
		if not user.department:
			return queryset.filter(department__isnull=True)
		# Own department plus the company-wide targets that apply to everyone.
		return queryset.filter(
			Q(department__name=user.department) | Q(department__isnull=True)
		)
	return queryset.none()


def dashboard_objectives(user, company, year=None, limit=4):
	"""A short list of live objectives for the carbon dashboard widget."""
	queryset = visible_objectives(user, company).exclude(status=ObjectiveStatus.ARCHIVED)
	if year is not None:
		queryset = queryset.filter(year=year)
	return list(queryset.select_related("department")[:limit])
