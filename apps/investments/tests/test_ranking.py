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
from apps.investments.services.ranking import (
	compare_projects,
	filter_by_budget,
	project_indicators,
	rank_projects,
)


class RankingTests(TestCase):
	def setUp(self):
		self.owner = User.objects.create_user(
			email="ranking@test.example",
			password="SecureTestPassword2026!",
			role=User.Role.COMPANY_OWNER,
			status=User.Status.ACTIVE,
		)
		self.company = Company.objects.create(owner=self.owner, name="Entreprise classement")

	def project(self, name):
		return DecarbonizationProject.objects.create(
			company=self.company,
			name=name,
			project_type=ProjectType.SOLAR,
			status=ProjectStatus.PROPOSED,
			start_date=date(2026, 1, 1),
			end_date=date(2026, 12, 31),
		)

	def test_indicators_use_total_net_cost_and_latest_impact(self):
		project = self.project("Projet A")
		Investment.objects.create(
			project=project, planned_budget=Decimal("100"), actual_cost=None,
			funding_source="Budget", date=date(2026, 1, 1),
		)
		Investment.objects.create(
			project=project, planned_budget=Decimal("80"), actual_cost=Decimal("70"),
			funding_source="Subvention", date=date(2026, 2, 1),
		)
		ImpactAssessment.objects.create(
			project=project, estimated_tco2e_per_year=Decimal("1"),
			estimated_kwh_saved_per_year=Decimal("10"), annual_savings=Decimal("20"),
			lifetime_years=5, assumptions="Anciennes hypothèses",
		)
		latest = ImpactAssessment.objects.create(
			project=project, estimated_tco2e_per_year=Decimal("2"),
			estimated_kwh_saved_per_year=Decimal("20"), annual_savings=Decimal("30"),
			lifetime_years=4, assumptions="Nouvelles hypothèses",
		)
		indicators = project_indicators(project)
		self.assertEqual(indicators["net_cost"], Decimal("170.00"))
		self.assertEqual(indicators["tco2e_per_year"], latest.estimated_tco2e_per_year)

	def test_rank_places_none_last_and_budget_filters(self):
		rows = [
			{"name": "Sans donnée", "roi": None, "net_cost": None},
			{"name": "B", "roi": Decimal("0.20"), "net_cost": Decimal("200")},
			{"name": "A", "roi": Decimal("0.50"), "net_cost": Decimal("100")},
		]
		self.assertEqual([row["name"] for row in rank_projects(rows, "roi")], ["A", "B", "Sans donnée"])
		self.assertEqual([row["name"] for row in filter_by_budget(rows, Decimal("150"))], ["A"])

	def test_compare_projects_returns_rows(self):
		project = self.project("Projet comparé")
		self.assertEqual(compare_projects([project])[0]["name"], "Projet comparé")
