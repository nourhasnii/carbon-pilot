from datetime import date
from decimal import Decimal

from django.test import TestCase

from apps.accounts.models import User
from apps.enterprise.models import Company
from apps.investments.models import (
	DecarbonizationProject,
	ImpactAssessment,
	Investment,
	ProjectStatus,
	ProjectType,
)
from apps.portfolio_ai.services.optimizer import InsufficientData
from apps.portfolio_ai.services.predictor import predict_impact


class PredictorTests(TestCase):
	def setUp(self):
		self.owner = User.objects.create_user(
			email="predictor@test.example",
			password="SecureTestPassword2026!",
			role=User.Role.COMPANY_OWNER,
			status=User.Status.ACTIVE,
		)
		self.company = Company.objects.create(owner=self.owner, name="Entreprise prédiction")
		self.other_owner = User.objects.create_user(
			email="predictor-other@test.example",
			password="SecureTestPassword2026!",
			role=User.Role.COMPANY_OWNER,
			status=User.Status.ACTIVE,
		)
		self.other_company = Company.objects.create(owner=self.other_owner, name="Autre prédiction")

	def add_project(self, company, index, demo=False):
		project = DecarbonizationProject.objects.create(
			company=company,
			name=f"Projet prédiction {company.pk}-{index}",
			project_type=ProjectType.SOLAR if index % 2 else ProjectType.HVAC,
			status=ProjectStatus.COMPLETED,
			start_date=date(2026, 1, 1),
			end_date=date(2026, 12, 31),
			is_demo=demo,
		)
		Investment.objects.create(
			project=project,
			planned_budget=Decimal(10000 + index * 1000),
			funding_source="Test",
			date=date(2026, 1, 1),
		)
		ImpactAssessment.objects.create(
			project=project,
			estimated_tco2e_per_year=Decimal(10 + index),
			estimated_kwh_saved_per_year=Decimal(100 + index),
			annual_savings=Decimal(2000 + index * 10),
			lifetime_years=10,
			assumptions="Hypothèses synthétiques",
			actual_tco2e_per_year=Decimal(12 + index),
		)

	def features(self):
		return {
			"project_type": ProjectType.SOLAR,
			"planned_budget": Decimal("20000"),
			"estimated_tco2e_per_year": Decimal("20"),
			"estimated_kwh_saved_per_year": Decimal("120"),
			"lifetime_years": Decimal("10"),
			"annual_savings": Decimal("2500"),
		}

	def test_fewer_than_ten_projects_raises(self):
		for index in range(9):
			self.add_project(self.company, index)
		with self.assertRaises(InsufficientData):
			predict_impact(self.company, self.features())

	def test_prediction_uses_only_real_projects_of_company(self):
		for index in range(12):
			self.add_project(self.company, index)
		self.add_project(self.company, 99, demo=True)
		for index in range(12):
			self.add_project(self.other_company, index)
		result = predict_impact(self.company, self.features())
		self.assertGreater(result["prediction"], Decimal("0"))
		self.assertEqual(
			result["model"],
			min(result["metrics"], key=lambda name: result["metrics"][name]["rmse"]),
		)
