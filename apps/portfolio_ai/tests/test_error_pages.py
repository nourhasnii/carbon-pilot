from django.test import RequestFactory, TestCase, override_settings
from django.views.defaults import server_error

from apps.accounts.models import User
from apps.enterprise.models import Company


@override_settings(DEBUG=False, ALLOWED_HOSTS=["testserver"])
class ErrorPageTests(TestCase):
	def setUp(self):
		self.owner = User.objects.create_user(
			email="err-owner@test.example", password="SecureTestPassword2026!",
			role=User.Role.COMPANY_OWNER, status=User.Status.ACTIVE,
		)
		self.company = Company.objects.create(owner=self.owner, name="Entreprise erreurs")
		self.supplier = User.objects.create_user(
			email="err-supplier@test.example", password="SecureTestPassword2026!",
			role=User.Role.SUPPLIER, status=User.Status.ACTIVE, company_membership=self.company,
		)

	def test_custom_404(self):
		self.client.force_login(self.owner)
		response = self.client.get("/page-qui-n-existe-pas/")
		self.assertEqual(response.status_code, 404)
		self.assertContains(response, "Page introuvable", status_code=404)

	def test_custom_403(self):
		self.client.force_login(self.supplier)
		response = self.client.get("/portfolio-ai/runs/")
		self.assertEqual(response.status_code, 403)
		self.assertContains(response, "Accès refusé", status_code=403)

	def test_custom_500(self):
		request = RequestFactory().get("/")
		response = server_error(request)
		self.assertEqual(response.status_code, 500)
		self.assertIn("Erreur du serveur", response.content.decode())