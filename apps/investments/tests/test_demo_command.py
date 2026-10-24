from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from apps.accounts.models import User
from apps.enterprise.models import Company
from apps.investments.models import DecarbonizationProject, ProjectStatus, ProjectType


class LoadDemoInvestmentsCommandTests(TestCase):
	def setUp(self):
		self.password = "SecureTestPassword2026!"
		self.owner = User.objects.create_user(
			email="demo-owner@test.example",
			password=self.password,
			role=User.Role.COMPANY_OWNER,
			status=User.Status.ACTIVE,
		)
		self.other_owner = User.objects.create_user(
			email="demo-other-owner@test.example",
			password=self.password,
			role=User.Role.COMPANY_OWNER,
			status=User.Status.ACTIVE,
		)
		self.company = Company.objects.create(owner=self.owner, name="Entreprise DEMO")
		self.other_company = Company.objects.create(
			owner=self.other_owner,
			name="Autre entreprise DEMO",
		)

	def run_command(self, *args):
		output = StringIO()
		call_command(
			"load_demo_investments",
			"--company-id",
			str(self.company.pk),
			*args,
			stdout=output,
		)
		return output.getvalue()

	def test_command_creates_expected_demo_projects(self):
		self.run_command()
		projects = DecarbonizationProject.objects.filter(company=self.company)
		self.assertEqual(projects.count(), 12)
		self.assertEqual(projects.filter(is_demo=True).count(), 12)
		self.assertEqual(projects.filter(name__startswith="[DEMO]").count(), 12)
		self.assertEqual(
			projects.filter(status=ProjectStatus.COMPLETED).count(),
			5,
		)
		self.assertEqual(
			projects.filter(
				status=ProjectStatus.COMPLETED,
				impacts__actual_tco2e_per_year__isnull=False,
			).count(),
			5,
		)
		self.assertTrue(all(project.investments.count() == 1 for project in projects))
		self.assertTrue(all(project.impacts.count() == 1 for project in projects))

	def test_command_is_idempotent(self):
		self.run_command()
		self.run_command()
		self.assertEqual(
			DecarbonizationProject.objects.filter(company=self.company).count(),
			12,
		)
		self.assertEqual(
			sum(project.investments.count() for project in self.company.decarbonization_projects.all()),
			12,
		)
		self.assertEqual(
			sum(project.impacts.count() for project in self.company.decarbonization_projects.all()),
			12,
		)

	def test_missing_company_raises_command_error(self):
		with self.assertRaises(CommandError):
			call_command(
				"load_demo_investments",
				"--company-id",
				"999999",
			)

	def test_other_company_is_not_touched(self):
		foreign_project = DecarbonizationProject.objects.create(
			company=self.other_company,
			name="[DEMO] Projet externe",
			project_type=ProjectType.OTHER,
			status=ProjectStatus.PROPOSED,
			start_date="2026-01-01",
			end_date="2026-12-31",
			is_demo=True,
		)
		self.run_command()
		self.assertTrue(
			DecarbonizationProject.objects.filter(
				pk=foreign_project.pk,
				name="[DEMO] Projet externe",
			).exists()
		)
		self.assertEqual(
			DecarbonizationProject.objects.filter(company=self.other_company).count(),
			1,
		)

	def test_reset_only_deletes_demo_projects_in_target_company(self):
		regular_project = DecarbonizationProject.objects.create(
			company=self.company,
			name="Projet réel à conserver",
			project_type=ProjectType.OTHER,
			status=ProjectStatus.PROPOSED,
			start_date="2026-01-01",
			end_date="2026-12-31",
			is_demo=False,
		)
		foreign_demo = DecarbonizationProject.objects.create(
			company=self.other_company,
			name="[DEMO] Projet externe à conserver",
			project_type=ProjectType.OTHER,
			status=ProjectStatus.PROPOSED,
			start_date="2026-01-01",
			end_date="2026-12-31",
			is_demo=True,
		)
		self.run_command()
		self.run_command("--reset")
		self.assertTrue(DecarbonizationProject.objects.filter(pk=regular_project.pk).exists())
		self.assertTrue(DecarbonizationProject.objects.filter(pk=foreign_demo.pk).exists())
		self.assertEqual(
			DecarbonizationProject.objects.filter(
				company=self.company,
				is_demo=True,
			).count(),
			12,
		)
