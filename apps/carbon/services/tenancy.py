"""Tenant resolution and activity visibility rules.

Every carbon view must resolve the tenant from the authenticated user — never
from request parameters — and then narrow the queryset further according to the
user's role. This module is the single source of truth for those rules.
"""

from apps.accounts.models import User
from apps.enterprise.models import Company

from apps.carbon.models import AnomalyLog, CarbonActivity

# Roles that may see every activity of their company (read scope).
READ_ALL_ROLES = (
	User.Role.COMPANY_OWNER,
	User.Role.ESG_REPORTING_MANAGER,
	User.Role.AUDITOR,
)

# Roles that administrate the whole company's carbon inventory.
ADMIN_ROLES = (
	User.Role.COMPANY_OWNER,
	User.Role.ESG_REPORTING_MANAGER,
)


def company_for_user(user):
	"""Return the tenant a user belongs to, or ``None``. Never trusts client input."""
	if not getattr(user, "is_authenticated", False):
		return None
	if user.role == User.Role.COMPANY_OWNER:
		return Company.objects.filter(owner=user).first()
	if user.company_membership_id:
		return Company.objects.filter(pk=user.company_membership_id).first()
	if user.company:
		return Company.objects.filter(name__iexact=user.company).first()
	return None


def is_company_wide(user):
	"""True when the user may read every activity of the company."""
	return user.role in READ_ALL_ROLES


def is_company_admin(user):
	"""True when the user may administrate the whole company inventory."""
	return user.role in ADMIN_ROLES


def visible_activities(user, company):
	"""Activities a user is allowed to see, scoped to their tenant and role."""
	queryset = CarbonActivity.objects.filter(company=company)
	if user.role == User.Role.SUPER_ADMIN:
		return queryset
	if is_company_wide(user):
		return queryset
	if user.role == User.Role.DEPARTMENT_MANAGER:
		if not user.department:
			return queryset.none()
		return queryset.filter(department__name=user.department)
	# Suppliers and any other member only ever see what they reported.
	return queryset.filter(created_by=user)


def visible_anomalies(user, company):
	"""Anomaly logs a user is allowed to see, scoped to their tenant and role.

	Mirrors :func:`visible_activities`: a Department Manager only sees anomalies
	in their own department, everyone else only their own, while company-wide
	roles (and platform admins) see the whole tenant. Rows from another company
	can never be reached because the queryset starts from ``company``.
	"""
	queryset = AnomalyLog.objects.filter(company=company)
	if user.role == User.Role.SUPER_ADMIN:
		return queryset
	if is_company_wide(user):
		return queryset
	if user.role == User.Role.DEPARTMENT_MANAGER:
		if not user.department:
			return queryset.none()
		return queryset.filter(department__name=user.department)
	return queryset.filter(activity__created_by=user)
