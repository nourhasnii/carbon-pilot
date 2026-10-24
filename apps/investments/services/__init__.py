from apps.accounts.models import User
from apps.carbon.services.tenancy import company_for_user

from ..models import DecarbonizationProject


WRITE_ROLES = (
	User.Role.COMPANY_OWNER,
	User.Role.ESG_REPORTING_MANAGER,
)

VIEWER_ROLES = (
	User.Role.COMPANY_OWNER,
	User.Role.ESG_REPORTING_MANAGER,
	User.Role.AUDITOR,
	User.Role.DEPARTMENT_MANAGER,
)


def visible_projects(user, company):
	return DecarbonizationProject.objects.for_company(company)


def can_manage(user):
	return user.role == User.Role.SUPER_ADMIN or user.role in WRITE_ROLES
