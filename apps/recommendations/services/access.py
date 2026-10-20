"""Role and tenant scoping for AI recommendations.

Mirrors the carbon/objectives tenancy rules: the tenant is always resolved from
the authenticated user (never from request parameters) and a Department Manager
only ever touches recommendations that concern their own department.
"""

from django.db.models import Q

from apps.accounts.models import User
from apps.carbon.services.tenancy import company_for_user  # re-exported for views

from ..models import RecommendationLog

# Roles that may open the recommendations workspace (read-only for auditors).
VIEWER_ROLES = (
	User.Role.COMPANY_OWNER,
	User.Role.DEPARTMENT_MANAGER,
	User.Role.ESG_REPORTING_MANAGER,
	User.Role.AUDITOR,
)

# Roles that may generate recommendations and change their status.
MANAGER_ROLES = (
	User.Role.COMPANY_OWNER,
	User.Role.ESG_REPORTING_MANAGER,
	User.Role.DEPARTMENT_MANAGER,
)

# Roles that see the whole company's recommendations.
READ_ALL_ROLES = (
	User.Role.COMPANY_OWNER,
	User.Role.ESG_REPORTING_MANAGER,
	User.Role.AUDITOR,
)


def visible_recommendations(user, company):
	"""Recommendations a user is allowed to see, scoped to tenant and role."""
	if company is None:
		return RecommendationLog.objects.none()
	queryset = RecommendationLog.objects.for_company(company)
	if user.role == User.Role.SUPER_ADMIN or user.role in READ_ALL_ROLES:
		return queryset
	if user.role == User.Role.DEPARTMENT_MANAGER:
		if not user.department:
			return queryset.filter(department__isnull=True)
		# Own department plus the company-wide recommendations.
		return queryset.filter(Q(department__name=user.department) | Q(department__isnull=True))
	return queryset.none()


def can_generate(user, company):
	"""Whether the user may trigger a generation run for their company."""
	if user.role == User.Role.SUPER_ADMIN:
		return True
	if company is None:
		return False
	if user.role in (User.Role.COMPANY_OWNER, User.Role.ESG_REPORTING_MANAGER):
		return True
	if user.role == User.Role.DEPARTMENT_MANAGER:
		return bool(user.department)
	return False


def can_update_status(user, company, recommendation):
	"""Whether the user may move a recommendation between statuses."""
	if company is None or recommendation.company_id != company.pk:
		return False
	if user.role == User.Role.SUPER_ADMIN:
		return True
	if user.role in (User.Role.COMPANY_OWNER, User.Role.ESG_REPORTING_MANAGER):
		return True
	if user.role == User.Role.DEPARTMENT_MANAGER:
		if not user.department or not recommendation.department_id:
			return False
		return recommendation.department.name == user.department
	return False
