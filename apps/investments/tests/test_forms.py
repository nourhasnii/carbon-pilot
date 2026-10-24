from datetime import date
from decimal import Decimal

from django.test import TestCase

from apps.accounts.models import User
from apps.enterprise.models import Company, Department

from apps.investments.forms import (
	DecarbonizationProjectForm,
	ImpactAssessmentForm,
	InvestmentForm,
	ProjectScenarioForm,
)
from apps.investments.models import (
	DecarbonizationProject,
	ImpactAssessment,
	Investment,
	ProjectStatus,
	ProjectType,
)


class InvestmentFormTests(TestCase):
	def setUp(self):
		self.owner = User.objects.create_user(
			email="investment-owner@test.example",
			password="SecureTestPassword2026!",
			role=User.Role.COMPANY_OWNER,
			status=User.Status.ACTIVE,
		)
		self.company = Company.objects.create(owner=self.owner, name="Investissements Test")
		self.department = Department.objects.create(company=self.company, name="Opérations")
		self.other_owner = User.objects.create_user(
			email="investment-other@test.example",
			password="SecureTestPassword2026!",
			role=User.Role.COMPANY_OWNER,
			status=User.Status.ACTIVE,
		)
		self.other_company = Company.objects.create(owner=self.other_owner, name="Autre Société")
		self.other_department = Department.objects.create(company=self.other_company, name="Opérations")
		self.project = DecarbonizationProject.objects.create(
			company=self.company,
			department=self.department,
			name="Projet solaire",
			project_type=ProjectType.SOLAR,
			status=ProjectStatus.APPROVED,
			start_date=date(2026, 1, 1),
			end_date=date(2026, 12, 31),
		)
		self.investment = Investment.objects.create(
			project=self.project,
			planned_budget=Decimal("10000"),
			committed_spend=Decimal("3000"),
			funding_source="Fonds propres",
			date=date(2026, 1, 15),
		)
		self.impact = ImpactAssessment.objects.create(
			project=self.project,
			estimated_tco2e_per_year=Decimal("12"),
			estimated_kwh_saved_per_year=Decimal("1000"),
			annual_savings=Decimal("500"),
			lifetime_years=10,
			assumptions="Production estimée sur la base des données historiques.",
		)

	def project_data(self, **overrides):
		data = {
			"name": "  Nouveau projet  ",
			"department": self.department.pk,
			"project_type": ProjectType.HVAC,
			"description": "Description",
			"status": ProjectStatus.PROPOSED,
			"start_date": "2026-01-01",
			"end_date": "2026-12-31",
			"is_demo": False,
		}
		data.update(overrides)
		return data

	def test_project_form_is_valid_and_save_assigns_company(self):
		form = DecarbonizationProjectForm(data=self.project_data(), company=self.company, user=self.owner)
		self.assertTrue(form.is_valid(), form.errors)
		project = form.save()
		self.assertEqual(project.company, self.company)
		self.assertEqual(project.name, "Nouveau projet")

	def test_project_form_rejects_duplicate_name_in_company(self):
		form = DecarbonizationProjectForm(
			data=self.project_data(name=" Projet solaire "),
			company=self.company,
			user=self.owner,
		)
		self.assertFalse(form.is_valid())
		self.assertIn("existe déjà", form.errors["name"][0])

	def test_project_form_rejects_other_company_department(self):
		form = DecarbonizationProjectForm(
			data=self.project_data(department=self.other_department.pk),
			company=self.company,
			user=self.owner,
		)
		self.assertFalse(form.is_valid())
		self.assertIn("votre entreprise", form.errors["department"][0])

	def test_project_form_rejects_inverted_dates(self):
		form = DecarbonizationProjectForm(
			data=self.project_data(start_date="2026-12-31", end_date="2026-01-01"),
			company=self.company,
			user=self.owner,
		)
		self.assertFalse(form.is_valid())
		self.assertIn("date de fin", form.errors["end_date"][0])

	def investment_data(self, **overrides):
		data = {
			"planned_budget": "10000",
			"actual_cost": "9000",
			"committed_spend": "5000",
			"currency": "tnd",
			"funding_source": "  Fonds propres ",
			"date": "2026-01-15",
		}
		data.update(overrides)
		return data

	def test_investment_form_is_valid_and_save_assigns_project(self):
		form = InvestmentForm(data=self.investment_data(), project=self.project)
		self.assertTrue(form.is_valid(), form.errors)
		investment = form.save()
		self.assertEqual(investment.project, self.project)
		self.assertEqual(investment.currency, "TND")
		self.assertEqual(investment.funding_source, "Fonds propres")

	def test_investment_form_rejects_zero_planned_budget(self):
		form = InvestmentForm(data=self.investment_data(planned_budget="0"), project=self.project)
		self.assertFalse(form.is_valid())
		self.assertIn("strictement supérieure", form.errors["planned_budget"][0])

	def test_investment_form_rejects_committed_spend_above_actual_cost(self):
		form = InvestmentForm(
			data=self.investment_data(actual_cost="100", committed_spend="101"),
			project=self.project,
		)
		self.assertFalse(form.is_valid())
		self.assertIn("dépasser", form.errors["committed_spend"][0])

	def test_impact_form_is_valid(self):
		form = ImpactAssessmentForm(data={
			"estimated_tco2e_per_year": "12",
			"estimated_kwh_saved_per_year": "1000",
			"annual_savings": "500",
			"lifetime_years": "10",
			"assumptions": "  Hypothèses documentées. ",
			"actual_tco2e_per_year": "",
		}, project=self.project)
		self.assertTrue(form.is_valid(), form.errors)
		self.assertEqual(form.cleaned_data["assumptions"], "Hypothèses documentées.")

	def test_impact_form_rejects_lifetime_above_fifty(self):
		form = ImpactAssessmentForm(data={
			"estimated_tco2e_per_year": "12",
			"estimated_kwh_saved_per_year": "1000",
			"annual_savings": "500",
			"lifetime_years": "51",
			"assumptions": "Hypothèses",
		}, project=self.project)
		self.assertFalse(form.is_valid())
		self.assertIn("entre 1 et 50", form.errors["lifetime_years"][0])

	def scenario_data(self, **overrides):
		data = {
			"name": "  Prix énergie élevé ",
			"investment": self.investment.pk,
			"impact": self.impact.pk,
			"cost_multiplier": "1.2",
			"reduction_multiplier": "0.8",
			"energy_price": "0.35",
		}
		data.update(overrides)
		return data

	def test_scenario_form_limits_related_querysets_to_project(self):
		form = ProjectScenarioForm(project=self.project)
		self.assertEqual(list(form.fields["investment"].queryset), [self.investment])
		self.assertEqual(list(form.fields["impact"].queryset), [self.impact])

	def test_scenario_form_is_valid_and_save_assigns_project(self):
		form = ProjectScenarioForm(data=self.scenario_data(), project=self.project)
		self.assertTrue(form.is_valid(), form.errors)
		scenario = form.save()
		self.assertEqual(scenario.project, self.project)
		self.assertEqual(scenario.name, "Prix énergie élevé")

	def test_scenario_form_rejects_multiplier_outside_bounds(self):
		form = ProjectScenarioForm(
			data=self.scenario_data(cost_multiplier="3.1"),
			project=self.project,
		)
		self.assertFalse(form.is_valid())
		self.assertIn("compris entre", form.errors["cost_multiplier"][0])
