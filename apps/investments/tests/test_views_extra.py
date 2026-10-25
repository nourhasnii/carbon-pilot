from datetime import date
from decimal import Decimal
from unittest import mock

from django.db import IntegrityError
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.enterprise.models import Company, Department
from apps.investments.models import (
	DecarbonizationProject,
	ImpactAssessment,
	Investment,
	ProjectScenario,
	ProjectStatus,
	ProjectType,
)


class InvestmentViewsExtraTests(TestCase):
	password = "SecureTestPassword2026!"

	def make_user(self, name, role, company=None):
		return User.objects.create_user(
			email=f"{name}@test.example",
			password=self.password,
			role=role,
			status=User.Status.ACTIVE,
			company_membership=company,
		)

	def make_project(self, name, company=None, **extra):
		data = dict(
			company=company or self.company,
			name=name,
			project_type=ProjectType.SOLAR,
			status=ProjectStatus.PROPOSED,
			start_date=date(2026, 1, 1),
			end_date=date(2026, 12, 31),
		)
		data.update(extra)
		return DecarbonizationProject.objects.create(**data)

	def setUp(self):
		self.owner = self.make_user("extra-owner", User.Role.COMPANY_OWNER)
		self.company = Company.objects.create(owner=self.owner, name="Entreprise extra")
		self.auditor = self.make_user("extra-auditor", User.Role.AUDITOR, self.company)
		self.other_owner = self.make_user("extra-other", User.Role.COMPANY_OWNER)
		self.other_company = Company.objects.create(owner=self.other_owner, name="Autre extra")
		self.no_company_owner = self.make_user("extra-nocompany", User.Role.COMPANY_OWNER)
		self.department = Department.objects.create(company=self.company, name="Opérations")

		self.project = self.make_project("Projet A", department=self.department)
		self.other_project = self.make_project("Projet externe", company=self.other_company)

		self.investment = Investment.objects.create(
			project=self.project,
			planned_budget=Decimal("10000"),
			date=date(2026, 3, 1),
		)
		self.impact = ImpactAssessment.objects.create(
			project=self.project,
			estimated_tco2e_per_year=Decimal("10"),
			estimated_kwh_saved_per_year=Decimal("1000"),
			annual_savings=Decimal("2000"),
			lifetime_years=10,
			assumptions="Hypothèses de test.",
		)
		self.scenario = ProjectScenario.objects.create(
			project=self.project,
			name="central",
			investment=self.investment,
			impact=self.impact,
			cost_multiplier=Decimal("1"),
			reduction_multiplier=Decimal("1"),
			energy_price=Decimal("0.5"),
		)
		self.other_investment = Investment.objects.create(
			project=self.other_project,
			planned_budget=Decimal("5000"),
			date=date(2026, 3, 1),
		)

	# ---------- Liste : filtres, pagination, sans entreprise ----------

	def test_list_filters(self):
		self.make_project("Isolation nord", project_type=ProjectType.INSULATION,
			status=ProjectStatus.APPROVED)
		self.client.force_login(self.owner)
		url = reverse("investments:project_list")
		response = self.client.get(url, {"q": "Isolation"})
		self.assertContains(response, "Isolation nord")
		self.assertNotContains(response, "Projet A")
		response = self.client.get(url, {"status": ProjectStatus.APPROVED})
		self.assertContains(response, "Isolation nord")
		self.assertNotContains(response, "Projet A")
		response = self.client.get(url, {"project_type": ProjectType.INSULATION})
		self.assertContains(response, "Isolation nord")
		self.assertNotContains(response, "Projet A")

	def test_list_is_paginated_by_ten(self):
		for index in range(12):
			self.make_project(f"Lot {index:02d}")
		self.client.force_login(self.owner)
		url = reverse("investments:project_list")
		self.assertEqual(len(self.client.get(url).context["projects"]), 10)
		self.assertEqual(len(self.client.get(url, {"page": 2}).context["projects"]), 3)

	def test_user_without_company_is_redirected(self):
		self.client.force_login(self.no_company_owner)
		for name in ("project_list", "project_compare", "project_ranking", "project_create"):
			response = self.client.get(reverse(f"investments:{name}"))
			self.assertEqual(response.status_code, 302, name)

	# ---------- Comparaison et classement ----------

	def test_compare_with_two_projects(self):
		second = self.make_project("Projet B")
		self.client.force_login(self.owner)
		response = self.client.get(
			reverse("investments:project_compare"),
			{"projects": [self.project.pk, second.pk]},
		)
		self.assertEqual(response.status_code, 200)
		self.assertEqual(len(response.context["comparison"]), 2)
		self.assertIsNone(response.context["comparison_error"])

	def test_compare_rejects_more_than_six(self):
		extra = [self.make_project(f"Comp {i}") for i in range(6)]
		self.client.force_login(self.owner)
		ids = [self.project.pk] + [p.pk for p in extra]
		response = self.client.get(reverse("investments:project_compare"), {"projects": ids})
		self.assertContains(response, "au maximum six projets")

	def test_ranking_valid_criteria(self):
		self.client.force_login(self.owner)
		for criterion in ("reduction", "cost", "roi", "cost_per_tco2e", "payback"):
			response = self.client.get(
				reverse("investments:project_ranking"), {"criterion": criterion},
			)
			self.assertEqual(response.status_code, 200, criterion)
			self.assertEqual(response.context["selected_criterion"], criterion)

	def test_ranking_budget_filter(self):
		self.client.force_login(self.owner)
		response = self.client.get(
			reverse("investments:project_ranking"), {"budget": "20000"},
		)
		self.assertEqual(response.status_code, 200)
		self.assertIsNone(response.context["ranking_error"])

	def test_ranking_invalid_and_negative_budget(self):
		self.client.force_login(self.owner)
		for value in ("abc", "-5"):
			response = self.client.get(
				reverse("investments:project_ranking"), {"budget": value},
			)
			self.assertContains(response, "Le budget doit être un nombre")

	# ---------- Édition de projet ----------

	def test_project_edit_valid_post(self):
		self.client.force_login(self.owner)
		response = self.client.post(
			reverse("investments:project_edit", args=[self.project.pk]),
			{
				"name": "Projet A renommé",
				"department": self.department.pk,
				"project_type": ProjectType.SOLAR,
				"description": "Mise à jour",
				"status": ProjectStatus.PROPOSED,
				"start_date": "2026-01-01",
				"end_date": "2026-12-31",
			},
		)
		self.assertEqual(response.status_code, 302)
		self.project.refresh_from_db()
		self.assertEqual(self.project.name, "Projet A renommé")

	def test_project_edit_invalid_post_rerenders(self):
		self.client.force_login(self.owner)
		response = self.client.post(
			reverse("investments:project_edit", args=[self.project.pk]), {"name": ""},
		)
		self.assertEqual(response.status_code, 200)

	def test_project_edit_other_company_is_404(self):
		self.client.force_login(self.owner)
		response = self.client.get(
			reverse("investments:project_edit", args=[self.other_project.pk]),
		)
		self.assertEqual(response.status_code, 404)

	def test_auditor_cannot_write(self):
		self.client.force_login(self.auditor)
		urls = [
			reverse("investments:project_create"),
			reverse("investments:project_edit", args=[self.project.pk]),
			reverse("investments:project_delete_confirm", args=[self.project.pk]),
			reverse("investments:investment_create", args=[self.project.pk]),
		]
		for url in urls:
			self.assertEqual(self.client.get(url).status_code, 403, url)
		self.assertEqual(
			self.client.post(reverse("investments:project_delete", args=[self.project.pk])).status_code,
			403,
		)
		self.assertTrue(DecarbonizationProject.objects.filter(pk=self.project.pk).exists())

	# ---------- Suppression de projet ----------

	def test_project_delete_get_is_405(self):
		self.client.force_login(self.owner)
		response = self.client.get(reverse("investments:project_delete", args=[self.project.pk]))
		self.assertEqual(response.status_code, 405)

	def test_project_delete_post_deletes(self):
		self.client.force_login(self.owner)
		response = self.client.post(reverse("investments:project_delete", args=[self.project.pk]))
		self.assertRedirects(response, reverse("investments:project_list"))
		self.assertFalse(DecarbonizationProject.objects.filter(pk=self.project.pk).exists())

	def test_project_in_progress_cannot_be_deleted(self):
		DecarbonizationProject.objects.filter(pk=self.project.pk).update(
			status=ProjectStatus.IN_PROGRESS,
		)
		self.client.force_login(self.owner)
		response = self.client.post(reverse("investments:project_delete", args=[self.project.pk]))
		self.assertRedirects(
			response, reverse("investments:project_detail", args=[self.project.pk]),
		)
		self.assertTrue(DecarbonizationProject.objects.filter(pk=self.project.pk).exists())

	def test_project_delete_other_company_is_404(self):
		self.client.force_login(self.owner)
		response = self.client.post(
			reverse("investments:project_delete", args=[self.other_project.pk]),
		)
		self.assertEqual(response.status_code, 404)
		self.assertTrue(DecarbonizationProject.objects.filter(pk=self.other_project.pk).exists())

	def test_project_delete_integrity_error_is_handled(self):
		self.client.force_login(self.owner)
		with mock.patch.object(DecarbonizationProject, "delete", side_effect=IntegrityError):
			response = self.client.post(
				reverse("investments:project_delete", args=[self.project.pk]),
			)
		self.assertRedirects(response, reverse("investments:project_list"))
		self.assertTrue(DecarbonizationProject.objects.filter(pk=self.project.pk).exists())

	# ---------- Enfants : pages GET ----------

	def test_child_form_pages_render(self):
		self.client.force_login(self.owner)
		pk = self.project.pk
		pages = [
			reverse("investments:investment_create", args=[pk]),
			reverse("investments:investment_edit", args=[pk, self.investment.pk]),
			reverse("investments:impact_create", args=[pk]),
			reverse("investments:impact_edit", args=[pk, self.impact.pk]),
			reverse("investments:scenario_create", args=[pk]),
			reverse("investments:scenario_edit", args=[pk, self.scenario.pk]),
		]
		for url in pages:
			self.assertEqual(self.client.get(url).status_code, 200, url)

	def test_child_delete_confirm_pages_render(self):
		self.client.force_login(self.owner)
		pk = self.project.pk
		pages = [
			reverse("investments:investment_delete_confirm", args=[pk, self.investment.pk]),
			reverse("investments:impact_delete_confirm", args=[pk, self.impact.pk]),
			reverse("investments:scenario_delete_confirm", args=[pk, self.scenario.pk]),
		]
		for url in pages:
			response = self.client.get(url)
			self.assertEqual(response.status_code, 200, url)
			self.assertContains(response, "csrfmiddlewaretoken")

	# ---------- Enfants : création invalide ----------

	def test_child_invalid_post_rerenders(self):
		self.client.force_login(self.owner)
		pk = self.project.pk
		for name in ("investment_create", "impact_create", "scenario_create"):
			response = self.client.post(reverse(f"investments:{name}", args=[pk]), {})
			self.assertEqual(response.status_code, 200, name)
		self.assertEqual(Investment.objects.filter(project=self.project).count(), 1)

	# ---------- Enfants : suppression ----------

	def test_child_delete_get_is_405(self):
		self.client.force_login(self.owner)
		url = reverse("investments:investment_delete", args=[self.project.pk, self.investment.pk])
		self.assertEqual(self.client.get(url).status_code, 405)

	def test_child_delete_post(self):
		self.client.force_login(self.owner)
		pk = self.project.pk
		target = reverse("investments:project_detail", args=[pk])
		# le scénario dépend de l'investissement et de l'impact : on le supprime en premier
		response = self.client.post(
			reverse("investments:scenario_delete", args=[pk, self.scenario.pk]),
		)
		self.assertRedirects(response, target)
		self.assertFalse(ProjectScenario.objects.filter(pk=self.scenario.pk).exists())
		response = self.client.post(
			reverse("investments:investment_delete", args=[pk, self.investment.pk]),
		)
		self.assertRedirects(response, target)
		self.assertFalse(Investment.objects.filter(pk=self.investment.pk).exists())
		response = self.client.post(
			reverse("investments:impact_delete", args=[pk, self.impact.pk]),
		)
		self.assertRedirects(response, target)
		self.assertFalse(ImpactAssessment.objects.filter(pk=self.impact.pk).exists())

	def test_child_of_other_company_is_404(self):
		self.client.force_login(self.owner)
		# investissement d'une autre entreprise, avec l'identifiant de MON projet dans l'URL
		url = reverse(
			"investments:investment_edit", args=[self.project.pk, self.other_investment.pk],
		)
		self.assertEqual(self.client.get(url).status_code, 404)
		url = reverse(
			"investments:investment_delete", args=[self.project.pk, self.other_investment.pk],
		)
		self.assertEqual(self.client.post(url).status_code, 404)
		self.assertTrue(Investment.objects.filter(pk=self.other_investment.pk).exists())
		# projet d'une autre entreprise dans l'URL
		url = reverse("investments:investment_create", args=[self.other_project.pk])
		self.assertEqual(self.client.get(url).status_code, 404)

	def test_child_delete_integrity_error_is_handled(self):
		self.client.force_login(self.owner)
		with mock.patch.object(Investment, "delete", side_effect=IntegrityError):
			response = self.client.post(
				reverse("investments:investment_delete", args=[self.project.pk, self.investment.pk]),
			)
		self.assertRedirects(
			response, reverse("investments:project_detail", args=[self.project.pk]),
		)
		self.assertTrue(Investment.objects.filter(pk=self.investment.pk).exists())