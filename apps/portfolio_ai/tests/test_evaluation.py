from datetime import date
from decimal import Decimal

from django.test import TestCase, SimpleTestCase

from apps.accounts.models import User
from apps.enterprise.models import Company
from apps.investments.models import (
	DecarbonizationProject,
	ImpactAssessment,
	Investment,
	ProjectStatus,
	ProjectType,
)
from apps.portfolio_ai.services.evaluation import metrics_for_optimizer, metrics_for_predictor
from apps.portfolio_ai.services.optimizer import ProjectItem


class OptimizerEvaluationTests(SimpleTestCase):
	def test_optimizer_metrics_validate_small_instance(self):
		items = [
			ProjectItem(1, "A", Decimal("100"), Decimal("10"), Decimal("5"), Decimal("2")),
			ProjectItem(2, "B", Decimal("200"), Decimal("20"), Decimal("8"), Decimal("3")),
		]
		metrics = metrics_for_optimizer(items, Decimal("250"), "MAX_TCO2E")
		self.assertGreaterEqual(metrics["greedy_gap_percent"], Decimal("0"))
		self.assertTrue(metrics["validated_against_brute_force"])
		self.assertIsNotNone(metrics["exact_ms"])


class PredictorEvaluationTests(TestCase):
	def setUp(self):
		self.owner = User.objects.create_user(
			email="evaluation@test.example",
			password="SecureTestPassword2026!",
			role=User.Role.COMPANY_OWNER,
			status=User.Status.ACTIVE,
		)
		self.company = Company.objects.create(owner=self.owner, name="Entreprise évaluation")
		for index in range(10):
			project = DecarbonizationProject.objects.create(
				company=self.company,
				name=f"Projet évaluation {index}",
				project_type=ProjectType.SOLAR,
				status=ProjectStatus.COMPLETED,
				start_date=date(2026, 1, 1),
				end_date=date(2026, 12, 31),
			)
			Investment.objects.create(
				project=project,
				planned_budget=Decimal(10000 + index * 100),
				funding_source="Test",
				date=date(2026, 1, 1),
			)
			ImpactAssessment.objects.create(
				project=project,
				estimated_tco2e_per_year=Decimal(10),
				estimated_kwh_saved_per_year=Decimal(100),
				annual_savings=Decimal(2000),
				lifetime_years=10,
				assumptions="Hypothèses",
				actual_tco2e_per_year=Decimal(12 + index),
			)

	def test_predictor_metrics_are_returned(self):
		metrics = metrics_for_predictor(self.company)
		self.assertIn("mae", metrics)
		self.assertIn("rmse", metrics)
		self.assertIn("r2", metrics)
