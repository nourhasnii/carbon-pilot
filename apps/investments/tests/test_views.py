from datetime import date

from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.enterprise.models import Company, Department

from apps.investments.models import DecarbonizationProject, ProjectStatus, ProjectType


class InvestmentViewTemplateTests(TestCase):
	password = "SecureTestPassword2026!"

	def setUp(self):
		self.owner = User.objects.create_user(
			email="investment-views@test.example",
			password=self.password,
			role=User.Role.COMPANY_OWNER,
			status=User.Status.ACTIVE,
		)
		self.company = Company.objects.create(owner=self.owner, name="Entreprise vues")
		self.supplier = User.objects.create_user(
			email="investment-supplier@test.example",
			password=self.password,
			role=User.Role.SUPPLIER,
			status=User.Status.ACTIVE,
			company_membership=self.company,
		)
		self.other_owner = User.objects.create_user(
			email="investment-other@test.example",
			password=self.password,
			role=User.Role.COMPANY_OWNER,
			status=User.Status.ACTIVE,
		)
		self.other_company = Company.objects.create(owner=self.other_owner, name="Autre entreprise")
		self.department = Department.objects.create(company=self.company, name="Opérations")
		self.project = DecarbonizationProject.objects.create(
			company=self.company,
			department=self.department,
			name="Projet photovoltaïque",
			project_type=ProjectType.SOLAR,
			status=ProjectStatus.PROPOSED,
			description="Réduire les émissions du site.",
			start_date=date(2026, 1, 1),
			end_date=date(2026, 12, 31),
			created_by=self.owner,
		)
		self.other_project = DecarbonizationProject.objects.create(
			company=self.other_company,
			name="Projet externe",
			project_type=ProjectType.SOLAR,
			status=ProjectStatus.PROPOSED,
			start_date=date(2026, 1, 1),
			end_date=date(2026, 12, 31),
		)

	def project_payload(self, **overrides):
		payload = {
			"name": "Nouveau projet",
			"department": self.department.pk,
			"project_type": ProjectType.HVAC,
			"description": "Projet de test",
			"status": ProjectStatus.PROPOSED,
			"start_date": "2026-02-01",
			"end_date": "2026-11-30",
			"is_demo": "",
		}
		payload.update(overrides)
		return payload

	def test_project_list_renders(self):
		self.client.force_login(self.owner)
		response = self.client.get(reverse("investments:project_list"))
		self.assertEqual(response.status_code, 200)
		self.assertContains(response, "Projet photovoltaïque")

	def test_empty_project_list_displays_empty_state(self):
		self.project.delete()
		self.client.force_login(self.owner)
		response = self.client.get(reverse("investments:project_list"))
		self.assertEqual(response.status_code, 200)
		self.assertContains(response, "Aucun projet. Créez votre premier projet.")

	def test_project_detail_renders(self):
		self.client.force_login(self.owner)
		response = self.client.get(
			reverse("investments:project_detail", args=[self.project.pk])
		)
		self.assertEqual(response.status_code, 200)
		self.assertContains(response, "Informations du projet")
		self.assertContains(response, "Aucun investissement enregistré.")

	def test_project_create_page_renders(self):
		self.client.force_login(self.owner)
		response = self.client.get(reverse("investments:project_create"))
		self.assertEqual(response.status_code, 200)
		self.assertContains(response, "Nouveau projet")
		self.assertContains(response, "csrfmiddlewaretoken")

	def test_project_edit_page_renders(self):
		self.client.force_login(self.owner)
		response = self.client.get(
			reverse("investments:project_edit", args=[self.project.pk])
		)
		self.assertEqual(response.status_code, 200)
		self.assertContains(response, "Modifier le projet")
		self.assertContains(response, self.project.name)

	def test_project_delete_confirmation_page_renders(self):
		self.client.force_login(self.owner)
		response = self.client.get(
			reverse("investments:project_delete_confirm", args=[self.project.pk])
		)
		self.assertEqual(response.status_code, 200)
		self.assertContains(response, "Confirmer la suppression")
		self.assertContains(response, "csrfmiddlewaretoken")

	def test_valid_project_post_redirects(self):
		self.client.force_login(self.owner)
		response = self.client.post(
			reverse("investments:project_create"),
			self.project_payload(),
		)
		self.assertEqual(response.status_code, 302)
		self.assertTrue(DecarbonizationProject.objects.filter(name="Nouveau projet").exists())

	def test_invalid_project_post_renders_errors(self):
		self.client.force_login(self.owner)
		response = self.client.post(
			reverse("investments:project_create"),
			self.project_payload(name=" ", end_date="2026-01-01", start_date="2026-12-31"),
		)
		self.assertEqual(response.status_code, 200)
		self.assertContains(response, "Ce champ est obligatoire.")
		self.assertContains(response, "La date de fin doit être postérieure ou égale à la date de début.")

	def test_compare_and_ranking_pages_render(self):
		self.client.force_login(self.owner)
		compare = self.client.get(reverse("investments:project_compare"))
		ranking = self.client.get(reverse("investments:project_ranking"))
		self.assertEqual(compare.status_code, 200)
		self.assertEqual(ranking.status_code, 200)

	def test_compare_requires_at_least_two_projects(self):
		self.client.force_login(self.owner)
		response = self.client.get(
			reverse("investments:project_compare"),
			{"projects": [self.project.pk]},
		)
		self.assertEqual(response.status_code, 200)
		self.assertContains(response, "Sélectionnez au moins deux projets")

	def test_ranking_rejects_invalid_criterion(self):
		self.client.force_login(self.owner)
		response = self.client.get(
			reverse("investments:project_ranking"),
			{"criterion": "unknown"},
		)
		self.assertEqual(response.status_code, 200)
		self.assertContains(response, "critère de classement sélectionné est invalide")

	def test_anonymous_and_supplier_are_refused(self):
		self.assertEqual(
			self.client.get(reverse("investments:project_compare")).status_code,
			302,
		)
		self.client.force_login(self.supplier)
		self.assertEqual(
			self.client.get(reverse("investments:project_ranking")).status_code,
			403,
		)

	def test_foreign_project_is_not_visible_in_comparison(self):
		self.client.force_login(self.owner)
		response = self.client.get(
			reverse("investments:project_compare"),
			{"projects": [self.project.pk, self.other_project.pk]},
		)
		self.assertEqual(response.status_code, 200)
		self.assertNotContains(response, "Projet externe")
		self.assertContains(response, "Sélectionnez au moins deux projets")
