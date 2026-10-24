from datetime import date
from decimal import Decimal
from unittest.mock import patch

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
from apps.portfolio_ai.models import PortfolioRun, PortfolioRunObjective, PortfolioRunStatus
from apps.portfolio_ai.tasks import GENERIC_ERROR, run_portfolio_task


class PortfolioTaskTests(TestCase):
	def setUp(self):
		self.owner = User.objects.create_user(
			email="task-owner@test.example",
			password="SecureTestPassword2026!",
			role=User.Role.COMPANY_OWNER,
			status=User.Status.ACTIVE,
		)
		self.company = Company.objects.create(owner=self.owner, name="Entreprise tâche")

	def project(self, index, status=ProjectStatus.PROPOSED):
		project = DecarbonizationProject.objects.create(
			company=self.company,
			name=f"Projet tâche {index}",
			project_type=ProjectType.SOLAR,
			status=status,
			start_date=date(2026, 1, 1),
			end_date=date(2026, 12, 31),
		)
		Investment.objects.create(
			project=project,
			planned_budget=Decimal("10000"),
			funding_source="Test",
			date=date(2026, 1, 1),
		)
		ImpactAssessment.objects.create(
			project=project,
			estimated_tco2e_per_year=Decimal("10"),
			estimated_kwh_saved_per_year=Decimal("100"),
			annual_savings=Decimal("2500"),
			lifetime_years=10,
			assumptions="Hypothèses de test",
		)
		return project

	def make_run(self, statuses=None):
		return PortfolioRun.objects.create(
			company=self.company,
			created_by=self.owner,
			budget=Decimal("30000"),
			objective=PortfolioRunObjective.MAX_TCO2E,
			included_statuses=statuses or [ProjectStatus.PROPOSED],
		)

	def test_success_serializes_metrics_and_decimals(self):
		self.project(1)
		self.project(2)
		run = self.make_run()
		run_portfolio_task(run.pk)
		run.refresh_from_db()
		self.assertEqual(run.status, PortfolioRunStatus.SUCCESS)
		self.assertTrue(run.selected_project_ids)
		self.assertIn("sensitivity", run.metrics)
		self.assertIn("prediction", run.metrics)
		self.assertIsInstance(run.metrics["sensitivity"]["stability_score"], str)

	def test_insufficient_data_fails_without_trace(self):
		run = self.make_run()
		run_portfolio_task(run.pk)
		run.refresh_from_db()
		self.assertEqual(run.status, PortfolioRunStatus.FAILED)
		self.assertTrue(run.metrics["insufficient_data"])
		self.assertNotIn("Traceback", run.error_message)

	def test_unexpected_error_uses_generic_message(self):
		run = self.make_run()
		with patch("apps.portfolio_ai.tasks.build_items", side_effect=RuntimeError("secret details")):
			run_portfolio_task(run.pk)
		run.refresh_from_db()
		self.assertEqual(run.status, PortfolioRunStatus.FAILED)
		self.assertEqual(run.error_message, GENERIC_ERROR)
		self.assertNotIn("secret details", run.error_message)
