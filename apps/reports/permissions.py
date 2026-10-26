"""Role scoping for ESG reports.

Kept in its own module so both ``apps.reports.views`` and the carbon dashboard
widget can share it without introducing a circular import between the two view
modules. Read access reuses the carbon viewer roles (see ``apps.carbon.views``).
"""

from apps.accounts.models import User

# Members who may build and generate a report (auditors are read-only).
MANAGE_ROLES = (User.Role.COMPANY_OWNER, User.Role.ESG_REPORTING_MANAGER)
