from datetime import date
from decimal import Decimal
import random

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
from apps.portfolio_ai.services.optimizer import (
	InsufficientData,
	ProjectItem,
	build_items,
	brute_force,
	solve_exact,
	solve_greedy,
)


class OptimizerTests(TestCase):
	def setUp(self):
		self.owner = User.objects.create_user(
			email="portfolio-optimizer@test.example",
			password="SecureTestPassword2026!",
			role=User.Role.COMPANY_OWNER,
			status=User.Status.ACTIVE,
		)
		self.company = Company.objects.create(owner=self.owner, name="Entreprise optimisation")
		self.other_owner = User.objects.create_user(
			email="portfolio-optimizer-other@test.example",
			password="SecureTestPassword2026!",
			role=User.Role.COMPANY_OWNER,
			status=User.Status.ACTIVE,
		)
		self.other_company = Company.objects.create(owner=self.other_owner, name="Autre optimisation")

	def item(self, identifier, cost, value, tco2e=None):
		return ProjectItem(
			identifier,
			f"Projet {identifier}",
			Decimal(str(cost)),
			Decimal(str(value)),
			Decimal(str(tco2e if tco2e is not None else value)),
			Decimal("10"),
		)

	def test_exact_matches_brute_force_on_seeded_instances(self):
		rng = random.Random(42)
		for _ in range(200):
			items = [
				self.item(index, rng.randint(100, 50000), rng.randint(1, 1000))
				for index in range(rng.randint(1, 8))
			]
			budget = Decimal(rng.randint(0, 120000))
			exact = solve_exact(items, budget)
			brute = brute_force(items, budget)
			self.assertEqual(exact.total_value, brute.total_value)

	def test_budget_boundaries_and_no_overspending(self):
		items = [self.item(1, 10000, 20), self.item(2, 20000, 30)]
		exact = solve_exact(items, Decimal("10000"))
		self.assertEqual(exact.selected_ids, [1])
		self.assertLessEqual(exact.total_cost, Decimal("10000"))
		self.assertEqual(solve_exact(items, Decimal("0")).selected_ids, [])
		self.assertEqual(solve_exact([self.item(1, 20000, 20)], Decimal("10000")).selected_ids, [])

	def test_greedy_is_never_better_than_exact(self):
		rng = random.Random(42)
		for _ in range(30):
			items = [self.item(i, rng.randint(100, 20000), rng.randint(1, 1000)) for i in range(8)]
			budget = Decimal(rng.randint(0, 50000))
			exact = solve_exact(items, budget)
			greedy = solve_greedy(items, budget)
			self.assertLessEqual(greedy.total_value, exact.total_value)
			self.assertLessEqual(greedy.total_cost, budget)

	def test_build_items_requires_two_eligible_projects_and_excludes_foreign_cancelled(self):
		with self.assertRaises(InsufficientData):
			build_items(self.company, [ProjectStatus.PROPOSED], "MAX_TCO2E")
		for company, name, status in (
			(self.company, "Éligible A", ProjectStatus.PROPOSED),
			(self.company, "Annulé", ProjectStatus.CANCELLED),
			(self.other_company, "Étranger", ProjectStatus.PROPOSED),
			(self.company, "Éligible B", ProjectStatus.APPROVED),
		):
			project = DecarbonizationProject.objects.create(
				company=company,
				name=name,
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
				annual_savings=Decimal("3000"),
				lifetime_years=10,
				assumptions="Hypothèses de test",
			)
		items = build_items(
			self.company,
			[ProjectStatus.PROPOSED, ProjectStatus.APPROVED, ProjectStatus.CANCELLED],
			"MAX_TCO2E",
		)
		self.assertEqual({item.name for item in items}, {"Éligible A", "Éligible B"})
