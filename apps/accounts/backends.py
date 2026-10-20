from django.contrib.auth.backends import ModelBackend

from .permissions import has_active_company_access


class CompanyAccessModelBackend(ModelBackend):
	def user_can_authenticate(self, user):
		return super().user_can_authenticate(user) and has_active_company_access(user)