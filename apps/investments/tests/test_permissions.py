from datetime import date
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.enterprise.models import Company

from apps.investments.models import (
	DecarbonizationProject,
	ImpactAssessment,
	Investment,
	ProjectStatus,
	ProjectType,
)


class InvestmentPermissionTests(TestCase):
	password = "SecureTestPassword2026!"

	def setUp(self):
		self.owner_a = self._user("owner-a", User.Role.COMPANY_OWNER)
		self.company_a = Company.objects.create(owner=self.owner_a, name="Entreprise A")
		self.owner_b = self._user("owner-b", User.Role.COMPANY_OWNER)
		self.company_b = Company.objects.create(owner=self.owner_b, name="Entreprise B")
		self.supplier = self._user("supplier", User.Role.SUPPLIER, self.company_a)
		self.auditor = self._user("auditor", User.Role.AUDITOR, self.company_a)
		self.project = self._project(self.company_a, "Projet A")
		self.foreign_project = self._project(self.company_b, "Projet B")
		self.investment = Investment.objects.create(
			project=self.project,
			planned_budget=Decimal("1000"),
			committed_spend=Decimal("100"),
			funding_source="Budget",
			date=date(2026, 1, 1),
		)
		self.impact = ImpactAssessment.objects.create(
			project=self.project,
			estimated_tco2e_per_year=Decimal("2"),
			estimated_kwh_saved_per_year=Decimal("100"),
			annual_savings=Decimal("50"),
			lifetime_years=5,
			assumptions="Hypothèses de test",
		)

	def _user(self, suffix, role, company=None):
		return User.objects.create_user(
			email=f"{suffix}@test.example",
			password=self.password,
			role=role,
			status=User.Status.ACTIVE,
			company_membership=company,
		)

	def _project(self, company, name, status=ProjectStatus.PROPOSED):
		return DecarbonizationProject.objects.create(
			company=company,
			name=name,
			project_type=ProjectType.SOLAR,
			status=status,
			start_date=date(2026, 1, 1),
			end_date=date(2026, 12, 31),
		)

	def test_anonymous_user_is_redirected_to_login(self):
		response = self.client.get(reverse("investments:project_list"))
		self.assertEqual(response.status_code, 302)
		self.assertIn("/login/", response["Location"])

	def test_supplier_is_refused(self):
		self.client.force_login(self.supplier)
		response = self.client.post(reverse("investments:project_create"), {})
		self.assertEqual(response.status_code, 403)

	def test_auditor_cannot_create_modify_or_delete(self):
		self.client.force_login(self.auditor)
		create_response = self.client.post(reverse("investments:project_create"), {})
		edit_response = self.client.post(
			reverse("investments:project_edit", args=[self.project.pk]), {}
		)
		delete_response = self.client.post(
			reverse("investments:project_delete", args=[self.project.pk])
		)
		self.assertEqual(create_response.status_code, 403)
		self.assertEqual(edit_response.status_code, 403)
		self.assertEqual(delete_response.status_code, 403)

	def test_company_b_cannot_access_project_detail_edit_or_delete(self):
		self.client.force_login(self.owner_b)
		for name in ("project_detail", "project_edit", "project_delete_confirm"):
			response = self.client.get(reverse(f"investments:{name}", args=[self.project.pk]))
			self.assertEqual(response.status_code, 404)
		response = self.client.post(reverse("investments:project_delete", args=[self.project.pk]))
		self.assertEqual(response.status_code, 404)

	def test_company_b_cannot_access_project_children(self):
		self.client.force_login(self.owner_b)
		child_routes = (
			("investment_edit", self.investment.pk),
			("investment_delete_confirm", self.investment.pk),
			("impact_edit", self.impact.pk),
			("impact_delete_confirm", self.impact.pk),
		)
		for route_name, child_pk in child_routes:
			response = self.client.get(
				reverse(f"investments:{route_name}", args=[self.project.pk, child_pk])
			)
			self.assertEqual(response.status_code, 404)
		response = self.client.post(
			reverse("investments:investment_delete", args=[self.project.pk, self.investment.pk])
		)
		self.assertEqual(response.status_code, 404)

	def test_delete_action_rejects_get(self):
		self.client.force_login(self.owner_a)
		response = self.client.get(reverse("investments:project_delete", args=[self.project.pk]))
		self.assertEqual(response.status_code, 405)

	def test_in_progress_project_cannot_be_deleted(self):
		project = self._project(self.company_a, "Projet en cours", ProjectStatus.IN_PROGRESS)
		self.client.force_login(self.owner_a)
		response = self.client.post(reverse("investments:project_delete", args=[project.pk]))
		self.assertEqual(response.status_code, 302)
		self.assertTrue(DecarbonizationProject.objects.filter(pk=project.pk).exists())
