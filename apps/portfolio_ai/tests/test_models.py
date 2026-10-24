from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase

from apps.accounts.models import User
from apps.enterprise.models import Company
from apps.investments.models import ProjectStatus
from apps.portfolio_ai.models import (
	PortfolioRun,
	PortfolioRunObjective,
	PortfolioRunStatus,
)


class PortfolioRunModelTests(TestCase):
	def setUp(self):
		self.user = User.objects.create_user(
			email="portfolio-model@test.example",
			password="SecureTestPassword2026!",
			role=User.Role.COMPANY_OWNER,
			status=User.Status.ACTIVE,
		)
		self.company = Company.objects.create(owner=self.user, name="Entreprise portefeuille")
		self.other_company = Company.objects.create(
			owner=User.objects.create_user(
				email="portfolio-other@test.example",
				password="SecureTestPassword2026!",
				role=User.Role.COMPANY_OWNER,
				status=User.Status.ACTIVE,
			),
			name="Autre entreprise portefeuille",
		)

	def make_run(self, company=None):
		return PortfolioRun(
			company=company or self.company,
			created_by=self.user,
			budget=Decimal("100000"),
			objective=PortfolioRunObjective.MAX_TCO2E,
			included_statuses=[ProjectStatus.PROPOSED],
		)

	def test_defaults_and_string_are_french(self):
		run = self.make_run()
		self.assertEqual(run.status, PortfolioRunStatus.PENDING)
		self.assertEqual(run.selected_project_ids, [])
		self.assertEqual(run.metrics, {})
		self.assertIn("Optimisation", str(run))

	def test_budget_must_be_positive(self):
		run = self.make_run()
		run.budget = Decimal("0")
		with self.assertRaises(ValidationError):
			run.full_clean()

	def test_json_fields_are_validated(self):
		run = self.make_run()
		run.included_statuses = ["INVALID"]
		with self.assertRaises(ValidationError):
			run.full_clean()
		run = self.make_run()
		run.metrics = []
		with self.assertRaises(ValidationError):
			run.full_clean()

	def test_for_company_scopes_runs(self):
		PortfolioRun.objects.create(
			company=self.company,
			created_by=self.user,
			budget=Decimal("100000"),
			objective=PortfolioRunObjective.MAX_TCO2E,
			included_statuses=[ProjectStatus.PROPOSED],
		)
		PortfolioRun.objects.create(
			company=self.other_company,
			budget=Decimal("100000"),
			objective=PortfolioRunObjective.MAX_ROI,
			included_statuses=[ProjectStatus.APPROVED],
		)
		self.assertEqual(PortfolioRun.objects.for_company(self.company).count(), 1)
		self.assertEqual(PortfolioRun.objects.for_company(self.other_company).count(), 1)
