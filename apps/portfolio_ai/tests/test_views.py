from datetime import date
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.enterprise.models import Company
from apps.investments.models import ProjectStatus, ProjectType
from apps.portfolio_ai.models import PortfolioRun, PortfolioRunObjective


class PortfolioViewTests(TestCase):
	def setUp(self):
		self.password = "SecureTestPassword2026!"
		self.owner = self.user("portfolio-owner", User.Role.COMPANY_OWNER)
		self.company = Company.objects.create(owner=self.owner, name="Entreprise vues AI")
		self.auditor = self.user("portfolio-auditor", User.Role.AUDITOR, self.company)
		self.supplier = self.user("portfolio-supplier", User.Role.SUPPLIER, self.company)
		self.other_owner = self.user("portfolio-other", User.Role.COMPANY_OWNER)
		self.other_company = Company.objects.create(owner=self.other_owner, name="Autre vues AI")
		self.run = PortfolioRun.objects.create(
			company=self.company,
			created_by=self.owner,
			budget=Decimal("10000"),
			objective=PortfolioRunObjective.MAX_TCO2E,
			included_statuses=[ProjectStatus.PROPOSED],
		)

	def user(self, name, role, company=None):
		return User.objects.create_user(
			email=f"{name}@test.example",
			password=self.password,
			role=role,
			status=User.Status.ACTIVE,
			company_membership=company,
		)

	def test_form_and_history_render(self):
		self.client.force_login(self.owner)
		self.assertEqual(self.client.get(reverse("portfolio_ai:run_create")).status_code, 200)
		self.assertEqual(self.client.get(reverse("portfolio_ai:run_history")).status_code, 200)

	def test_valid_post_creates_company_scoped_run(self):
		self.client.force_login(self.owner)
		response = self.client.post(reverse("portfolio_ai:run_create"), {
			"budget": "10000",
			"objective": PortfolioRunObjective.MAX_TCO2E,
			"included_statuses": [ProjectStatus.PROPOSED],
		})
		self.assertEqual(response.status_code, 302)
		self.assertTrue(PortfolioRun.objects.filter(company=self.company).exists())

	def test_invalid_post_renders_errors(self):
		self.client.force_login(self.owner)
		response = self.client.post(reverse("portfolio_ai:run_create"), {
			"budget": "0",
			"objective": PortfolioRunObjective.MAX_TCO2E,
		})
		self.assertEqual(response.status_code, 200)
		self.assertContains(response, "strictement supérieur")

	def test_permissions_and_status_endpoint(self):
		self.assertEqual(
			self.client.get(reverse("portfolio_ai:run_create")).status_code,
			302,
		)
		self.client.force_login(self.supplier)
		self.assertEqual(self.client.get(reverse("portfolio_ai:run_history")).status_code, 403)
		self.client.force_login(self.auditor)
		self.assertEqual(self.client.get(reverse("portfolio_ai:run_history")).status_code, 200)
		self.assertEqual(self.client.get(reverse("portfolio_ai:run_create")).status_code, 403)
		self.client.force_login(self.owner)
		response = self.client.get(reverse("portfolio_ai:run_status", args=[self.run.pk]))
		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.json()["status"], "PENDING")
		self.assertEqual(
			self.client.post(reverse("portfolio_ai:run_status", args=[self.run.pk])).status_code,
			405,
		)

	def test_other_company_run_is_hidden(self):
		foreign = PortfolioRun.objects.create(
			company=self.other_company,
			budget=Decimal("10000"),
			objective=PortfolioRunObjective.MAX_TCO2E,
			included_statuses=[ProjectStatus.PROPOSED],
		)
		self.client.force_login(self.owner)
		self.assertEqual(self.client.get(reverse("portfolio_ai:run_detail", args=[foreign.pk])).status_code, 404)
		self.assertEqual(self.client.get(reverse("portfolio_ai:run_status", args=[foreign.pk])).status_code, 404)
