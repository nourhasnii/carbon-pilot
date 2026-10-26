import json
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

from django.test import Client, TestCase, override_settings
from django.urls import reverse

from apps.accounts.models import User
from apps.accounts.services.email_service import build_account_activation_url, send_company_invitation_email
from apps.carbon.models import (
	ActivityCategory,
	ActivityStatus,
	CarbonActivity,
	EmissionFactor,
	Scope,
	VerificationStatus,
)
from apps.objectives.models import (
	ExpectedOutcome,
	Objective,
	ObjectiveStatus,
	PredictionRun,
	RiskLevel as ObjectiveRiskLevel,
)
from apps.recommendations.models import (
	RecommendationLog,
	RecommendationPriority,
	RecommendationSource,
)
from apps.risk.models import RiskAssessment, RiskLevel, RiskMethod

from .forms import DepartmentForm
from .models import Company, Department
from .services import company_intelligence as intel


class CompanyOwnerTests(TestCase):
	def make_user(
		self,
		email,
		role=User.Role.COMPANY_OWNER,
		status=User.Status.ACTIVE,
		company="Carbon Works",
		department="",
	):
		return User.objects.create_user(
			email=email,
			password="SecureTestPassword2026!",
			role=role,
			status=status,
			company=company,
			department=department,
		)

	def test_owner_can_create_and_update_company_profile(self):
		owner = self.make_user("owner@carbonpilot.test")
		self.client.force_login(owner)

		response = self.client.get(reverse("accounts:dashboard"))
		self.assertEqual(response.status_code, 200)
		self.assertContains(response, "Your carbon footprint")
		self.assertContains(response, "Not set up")
		self.assertNotContains(response, "Company Identity")
		self.assertIsNone(response.context["company_form"])

		response = self.client.get(reverse("accounts:dashboard"), {"section": "company"})
		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.context["company_form"].initial["name"], "Carbon Works")
		self.assertContains(response, "Company Identity")
		self.assertContains(response, "Contact &amp; Location")
		self.assertContains(response, "Sustainability Profile")
		self.assertContains(response, 'class="side-link is-active"')
		self.assertTrue(all(field.field.required for field in response.context["company_form"]))

		response = self.client.post(
			reverse("accounts:dashboard") + "?section=company",
			self.company_data(),
		)
		self.assertRedirects(response, reverse("accounts:dashboard") + "?section=company")
		company = Company.objects.get(owner=owner)
		self.assertEqual(company.name, "Carbon Works Ltd")
		self.assertEqual(company.siret, "12345678901234")
		self.assertTrue(company.reports_esg)
		self.assertEqual(company.target_year, 2035)
		owner.refresh_from_db()
		self.assertEqual(owner.company, "Carbon Works Ltd")
		response = self.client.get(reverse("accounts:dashboard"), {"section": "company"})
		self.assertContains(response, "Contact &amp; Location")
		self.assertContains(response, "Carbon Works Limited")
		self.assertContains(response, "12345678901234")
		self.assertNotContains(response, "Create your company")
		self.assertIsNone(response.context["company_form"])
		self.assertEqual(response.context["company"], company)

		response = self.client.get(reverse("accounts:dashboard"), {"section": "company", "edit": "1"})
		self.assertContains(response, "Company Identity")
		self.assertEqual(response.context["company_form"].instance, company)
		member = self.make_user(
			"member@carbonpilot.test",
			role=User.Role.DEPARTMENT_MANAGER,
			company="Carbon Works Ltd",
			department="Operations",
		)
		member.company_membership = company
		member.save(update_fields=("company_membership",))
		response = self.client.get(reverse("accounts:dashboard"))
		self.assertContains(response, "Carbon Works Ltd")
		self.assertContains(response, member.email)
		self.assertContains(response, "Operations")
		self.assertContains(response, "Emissions by scope")
		self.assertNotContains(response, "Create your company")
		self.assertIsNone(response.context["company_form"])
		self.assertEqual(response.context["active_member_count"], 1)
		self.assertEqual(response.context["department_count"], 1)

		response = self.client.post(
			reverse("accounts:dashboard") + "?section=company&edit=1",
			{
				**self.company_data(),
				"name": "Carbon Works Group",
			},
		)
		self.assertRedirects(response, reverse("accounts:dashboard") + "?section=company")
		company.refresh_from_db()
		self.assertEqual(company.name, "Carbon Works Group")
		self.assertEqual(Company.objects.filter(owner=owner).count(), 1)
		member.refresh_from_db()
		self.assertEqual(member.company, "Carbon Works Group")

	def company_data(self):
		return {
			"name": "Carbon Works Ltd",
			"legal_name": "Carbon Works Limited",
			"siret": "12345678901234",
			"sector": Company.Sector.MANUFACTURING,
			"size": Company.Size.SMALL,
			"country": "France",
			"city": "Lyon",
			"address": "10 Rue Verte",
			"website": "https://carbonworks.example",
			"contact_email": "contact@carbonworks.example",
			"reports_esg": "yes",
			"sustainability_goal": "Reach net zero operations.",
			"target_year": "2035",
		}

	def test_non_owner_cannot_access_company_management(self):
		user = self.make_user("manager@carbonpilot.test", role=User.Role.DEPARTMENT_MANAGER)
		self.client.force_login(user)
		self.assertEqual(self.client.get(reverse("enterprise:company_dashboard")).status_code, 403)
		self.assertEqual(self.client.get(reverse("enterprise:company_profile")).status_code, 403)

	def test_pending_company_owner_cannot_manage_company(self):
		owner = self.make_user("pending-owner@carbonpilot.test", status=User.Status.PENDING)
		self.client.force_login(owner)
		response = self.client.get(reverse("enterprise:company_dashboard"))
		self.assertEqual(response.status_code, 302)
		self.assertIn(reverse("accounts:login"), response.url)
		self.assertEqual(
			self.client.get(reverse("enterprise:company_profile")).status_code,
			302,
		)
		self.assertFalse(Company.objects.filter(owner=owner).exists())

	def test_company_form_rejects_invalid_siret_and_past_target_year(self):
		owner = self.make_user("invalid-company@carbonpilot.test")
		self.client.force_login(owner)
		data = self.company_data()
		data["siret"] = "not-a-siret"
		data["target_year"] = "2020"
		response = self.client.post(reverse("accounts:dashboard") + "?section=company", data)
		self.assertEqual(response.status_code, 200)
		self.assertIn("siret", response.context["company_form"].errors)
		self.assertIn("target_year", response.context["company_form"].errors)
		self.assertFalse(Company.objects.filter(owner=owner).exists())

	def test_owner_can_manage_departments_inside_dashboard(self):
		owner = self.make_user("department-owner@carbonpilot.test")
		company = Company.objects.create(owner=owner, name="Carbon Works")
		self.client.force_login(owner)

		response = self.client.get(reverse("accounts:dashboard"), {"section": "departments"})
		self.assertEqual(response.status_code, 200)
		self.assertTemplateUsed(response, "accounts/admin_dashboard.html")
		self.assertContains(response, "No departments yet")
		self.assertContains(response, "Create department")
		self.assertContains(response, 'class="side-link is-active"')
		self.assertEqual(response.context["company_departments"].count(), 0)

		response = self.client.get(
			reverse("accounts:dashboard"),
			{"section": "departments", "action": "create"},
		)
		self.assertContains(response, "Save department")
		self.assertTrue(response.context["department_form"].fields["name"].required)
		self.assertFalse(response.context["department_form"].fields["code"].required)
		self.assertFalse(response.context["department_form"].fields["description"].required)

		response = self.client.post(
			reverse("accounts:dashboard") + "?section=departments&action=create",
			{"name": "", "code": "OPS", "description": "Operations"},
		)
		self.assertEqual(response.status_code, 200)
		self.assertIn("name", response.context["department_form"].errors)
		self.assertFalse(Department.objects.filter(company=company).exists())

		response = self.client.post(
			reverse("accounts:dashboard") + "?section=departments&action=create",
			{"name": "Operations", "code": "OPS", "description": "Operations team"},
		)
		self.assertRedirects(
			response,
			reverse("accounts:dashboard") + "?section=departments",
		)
		department = Department.objects.get(company=company)
		self.assertEqual(department.code, "OPS")
		response = self.client.get(reverse("accounts:dashboard"), {"section": "departments"})
		self.assertContains(response, "Operations")
		self.assertContains(response, "Operations team")
		self.assertEqual(response.context["company_departments"].count(), 1)

		response = self.client.get(
			reverse("accounts:dashboard"),
			{"section": "departments", "edit": department.pk},
		)
		self.assertEqual(response.context["department_form"].instance, department)
		response = self.client.post(
			reverse("accounts:dashboard") + f"?section=departments&edit={department.pk}",
			{"name": "Operations & Facilities", "code": "OPS-FAC", "description": "Updated team"},
		)
		self.assertRedirects(
			response,
			reverse("accounts:dashboard") + "?section=departments",
		)
		department.refresh_from_db()
		self.assertEqual(department.name, "Operations & Facilities")
		self.assertEqual(department.code, "OPS-FAC")
		self.assertEqual(department.description, "Updated team")

	@override_settings(
		BREVO_API_KEY="test-api-key",
		DEFAULT_FROM_EMAIL="noreply@carbonpilot.test",
		APP_BASE_URL="http://127.0.0.1:8000",
	)
	def test_owner_can_invite_company_users_inside_dashboard(self):
		owner = self.make_user("user-owner@carbonpilot.test")
		company = Company.objects.create(owner=owner, name="Carbon Works")
		department = Department.objects.create(
			company=company,
			name="Operations",
			code="OPS",
		)
		self.client.force_login(owner)

		response = self.client.get(reverse("accounts:dashboard"), {"section": "users"})
		self.assertEqual(response.status_code, 200)
		self.assertTemplateUsed(response, "accounts/admin_dashboard.html")
		self.assertContains(response, "No users yet")
		self.assertContains(response, "Invite user")
		self.assertContains(response, 'class="side-link is-active"')

		response = self.client.get(
			reverse("accounts:dashboard"),
			{"section": "users", "action": "invite"},
		)
		self.assertTrue(response.context["invite_form"].fields["email"].required)
		self.assertTrue(response.context["invite_form"].fields["role"].required)
		self.assertFalse(response.context["invite_form"].fields["department"].required)
		self.assertIn((department.name, department.name), response.context["invite_form"].fields["department"].choices)
		self.assertNotIn(User.Role.COMPANY_OWNER, dict(response.context["invite_form"].fields["role"].choices))
		self.assertNotIn(User.Role.SUPER_ADMIN, dict(response.context["invite_form"].fields["role"].choices))

		response_mock = MagicMock()
		response_mock.__enter__.return_value.status = 201
		response_mock.__enter__.return_value.read.return_value = b'{"messageId":"test"}'
		with patch(
			"apps.accounts.services.email_service.urlopen",
			return_value=response_mock,
		) as send:
			response = self.client.post(
				reverse("accounts:dashboard") + "?section=users&action=invite",
				{
					"email": "new-manager@carbonpilot.test",
					"role": User.Role.DEPARTMENT_MANAGER,
					"department": department.name,
				},
			)
		self.assertRedirects(response, reverse("accounts:dashboard") + "?section=users")
		invited_user = User.objects.get(email="new-manager@carbonpilot.test")
		self.assertEqual(invited_user.status, User.Status.INVITED)
		self.assertEqual(invited_user.company, company.name)
		self.assertEqual(invited_user.role, User.Role.DEPARTMENT_MANAGER)
		self.assertEqual(invited_user.department, department.name)
		self.assertFalse(invited_user.has_usable_password())
		self.assertTrue(send.called)
		request = send.call_args.args[0]
		self.assertEqual(request.full_url, "https://api.brevo.com/v3/smtp/email")
		payload = json.loads(request.data.decode())
		self.assertEqual(payload["to"][0]["email"], invited_user.email)
		self.assertEqual(payload["subject"], "You are invited to CarbonPilot")
		activation_url = next(
			line for line in payload["textContent"].splitlines()
			if line.startswith("http://127.0.0.1:8000/accounts/activate/")
		)
		self.assertIn(activation_url, payload["htmlContent"])
		activation_path = urlparse(activation_url).path

		self.client.logout()
		response = self.client.get(activation_path)
		self.assertEqual(response.status_code, 200)
		self.assertContains(response, "Set your CarbonPilot password")
		response = self.client.post(activation_path, {
			"new_password1": "SecureTestPassword2026!",
			"new_password2": "SecureTestPassword2026!",
		})
		self.assertRedirects(response, reverse("accounts:login"))
		self.assertNotIn("_auth_user_id", self.client.session)
		invited_user.refresh_from_db()
		self.assertEqual(invited_user.status, User.Status.ACTIVE)
		self.assertTrue(invited_user.is_active)
		self.assertTrue(invited_user.check_password("SecureTestPassword2026!"))
		self.assertEqual(self.client.get(activation_path).status_code, 400)

		self.client.force_login(owner)
		response = self.client.get(reverse("accounts:dashboard"), {"section": "users"})
		self.assertContains(response, invited_user.email)
		self.assertContains(response, "Department Manager")
		self.assertContains(response, "Operations")
		self.assertContains(response, "Active")

	@override_settings(
		DEBUG=True,
		BREVO_API_KEY="test-api-key",
		DEFAULT_FROM_EMAIL="noreply@carbonpilot.test",
	)
	def test_invitation_email_prints_activation_link_to_development_log(self):
		owner = self.make_user("console-owner@carbonpilot.test")
		company = Company.objects.create(owner=owner, name="Carbon Works")
		invited_user = User.objects.create_user(
			email="console-invite@carbonpilot.test",
			role=User.Role.SUPPLIER,
			company=company.name,
			status=User.Status.INVITED,
		)
		activation_url = build_account_activation_url(invited_user)
		response_mock = MagicMock()
		response_mock.__enter__.return_value.status = 201
		response_mock.__enter__.return_value.read.return_value = b'{}'
		with (
			patch("apps.accounts.services.email_service.urlopen", return_value=response_mock),
			self.assertLogs("apps.accounts.services.email_service", level="INFO") as logs,
		):
			send_company_invitation_email(invited_user, activation_url)
		self.assertIn(activation_url, " ".join(logs.output))

	@override_settings(
		BREVO_API_KEY="test-api-key",
		DEFAULT_FROM_EMAIL="noreply@carbonpilot.test",
		APP_BASE_URL="http://127.0.0.1:8000",
	)
	def test_owner_can_resend_existing_invitation(self):
		owner = self.make_user("resend-owner@carbonpilot.test")
		company = Company.objects.create(owner=owner, name="Carbon Works")
		invited_user = User.objects.create_user(
			email="resend-invite@carbonpilot.test",
			role=User.Role.SUPPLIER,
			company=company.name,
			status=User.Status.INVITED,
		)
		self.client.force_login(owner)
		response_mock = MagicMock()
		response_mock.__enter__.return_value.status = 201
		response_mock.__enter__.return_value.read.return_value = b'{}'
		with patch(
			"apps.accounts.services.email_service.urlopen",
			return_value=response_mock,
		) as send:
			response = self.client.post(
				reverse("accounts:dashboard") + "?section=users",
				{"action": "resend_invitation", "user_id": invited_user.pk},
			)
		self.assertRedirects(response, reverse("accounts:dashboard") + "?section=users")
		self.assertTrue(send.called)
		payload = json.loads(send.call_args.args[0].data.decode())
		self.assertEqual(payload["to"][0]["email"], invited_user.email)
		self.assertEqual(User.objects.filter(email=invited_user.email).count(), 1)

	def test_owner_user_actions_are_limited_to_company_members(self):
		owner = self.make_user("scope-owner@carbonpilot.test")
		company = Company.objects.create(owner=owner, name="Shared Name")
		owner.company_membership = company
		owner.save(update_fields=("company_membership",))
		other_owner = self.make_user("other-scope-owner@carbonpilot.test")
		other_company = Company.objects.create(owner=other_owner, name="Shared Name")
		member = User.objects.create_user(
			email="scoped-member@carbonpilot.test",
			password="SecureTestPassword2026!",
			role=User.Role.SUPPLIER,
			company=company.name,
			company_membership=company,
			status=User.Status.ACTIVE,
		)
		other_member = User.objects.create_user(
			email="other-member@carbonpilot.test",
			password="SecureTestPassword2026!",
			role=User.Role.SUPPLIER,
			company=other_company.name,
			company_membership=other_company,
			status=User.Status.ACTIVE,
		)
		platform_admin = User.objects.create_user(
			email="protected-admin@carbonpilot.test",
			password="SecureTestPassword2026!",
			role=User.Role.SUPER_ADMIN,
			company=company.name,
			company_membership=company,
			status=User.Status.ACTIVE,
		)
		self.client.force_login(owner)
		users_url = reverse("accounts:dashboard") + "?section=users"

		response = self.client.get(reverse("accounts:dashboard"), {"section": "users"})
		self.assertContains(response, owner.email)
		self.assertTrue(response.context["company_users"].filter(pk=owner.pk).exists())
		self.assertContains(response, member.email)
		self.assertNotContains(response, other_member.email)
		self.assertNotContains(response, platform_admin.email)

		response = self.client.post(users_url, {
			"action": "deactivate_user", "user_id": other_member.pk,
		})
		self.assertEqual(response.status_code, 404)
		response = self.client.post(users_url, {
			"action": "deactivate_user", "user_id": owner.pk,
		})
		self.assertEqual(response.status_code, 403)
		response = self.client.post(users_url, {
			"action": "deactivate_user", "user_id": platform_admin.pk,
		})
		self.assertEqual(response.status_code, 403)
		platform_admin.refresh_from_db()
		self.assertEqual(platform_admin.status, User.Status.ACTIVE)

		response = self.client.post(users_url, {
			"action": "deactivate_user", "user_id": member.pk,
		})
		self.assertRedirects(response, users_url)
		member.refresh_from_db()
		self.assertEqual(member.status, User.Status.INACTIVE)
		self.assertFalse(member.is_active)
		login_response = Client().post(reverse("accounts:login"), {
			"username": member.email,
			"password": "SecureTestPassword2026!",
		})
		self.assertEqual(login_response.status_code, 200)
		self.assertNotIn("_auth_user_id", login_response.wsgi_request.session)
		response = self.client.post(users_url, {
			"action": "reactivate_user", "user_id": member.pk,
		})
		self.assertRedirects(response, users_url)
		member.refresh_from_db()
		self.assertEqual(member.status, User.Status.ACTIVE)
		self.assertTrue(member.is_active)

	def test_owner_company_deactivation_blocks_all_company_access(self):
		owner = self.make_user("deactivate-company-owner@carbonpilot.test")
		company = Company.objects.create(owner=owner, name="Carbon Works")
		member = User.objects.create_user(
			email="company-member@carbonpilot.test",
			password="SecureTestPassword2026!",
			role=User.Role.SUPPLIER,
			company=company.name,
			company_membership=company,
			status=User.Status.ACTIVE,
		)
		member_client = Client()
		member_client.force_login(member)
		self.client.force_login(owner)
		company_url = reverse("accounts:dashboard") + "?section=company"

		response = self.client.post(company_url, {"action": "deactivate_company"})
		self.assertEqual(response.status_code, 403)
		company.refresh_from_db()
		self.assertTrue(company.is_active)

		response = self.client.post(company_url, {
			"action": "deactivate_company", "confirm_deactivation": "yes",
		})
		self.assertRedirects(response, reverse("accounts:login"))
		company.refresh_from_db()
		self.assertFalse(company.is_active)
		blocked_response = member_client.get(reverse("accounts:dashboard"))
		self.assertEqual(blocked_response.status_code, 302)
		self.assertTrue(blocked_response.url.startswith(reverse("accounts:login")))
		login_response = Client().post(reverse("accounts:login"), {
			"username": member.email,
			"password": "SecureTestPassword2026!",
		})
		self.assertEqual(login_response.status_code, 200)
		self.assertNotIn("_auth_user_id", login_response.wsgi_request.session)

	def test_super_admin_can_manage_companies_and_view_company_users(self):
		owner = self.make_user("admin-company-owner@carbonpilot.test")
		company = Company.objects.create(
			owner=owner,
			name="Carbon Works",
			sector=Company.Sector.MANUFACTURING,
		)
		member = User.objects.create_user(
			email="admin-company-member@carbonpilot.test",
			password="SecureTestPassword2026!",
			role=User.Role.SUPPLIER,
			company=company.name,
			company_membership=company,
			status=User.Status.ACTIVE,
		)
		inactive_company_owner = self.make_user("inactive-company-owner@carbonpilot.test")
		inactive_company = Company.objects.create(
			owner=inactive_company_owner,
			name="Inactive Works",
			is_active=False,
		)
		admin = self.make_user(
			"super-admin-companies@carbonpilot.test",
			role=User.Role.SUPER_ADMIN,
		)
		self.client.force_login(admin)

		response = self.client.get(reverse("accounts:dashboard"), {"section": "companies"})
		self.assertContains(response, company.name)
		self.assertContains(response, inactive_company.name)
		self.assertContains(response, "Active")
		self.assertContains(response, "Deactivated")
		response = self.client.get(
			reverse("accounts:dashboard"),
			{"section": "companies", "q": "Carbon", "status": "active"},
		)
		self.assertContains(response, company.name)
		self.assertNotContains(response, inactive_company.name)
		response = self.client.get(
			reverse("accounts:dashboard"),
			{"section": "companies", "company": company.pk},
		)
		self.assertContains(response, member.email)
		self.assertContains(response, owner.email)

		response = self.client.post(
			reverse("accounts:dashboard") + "?section=companies",
			{"company_id": company.pk, "action": "deactivate_company"},
		)
		self.assertEqual(response.status_code, 403)
		response = self.client.post(
			reverse("accounts:dashboard") + "?section=companies",
			{
				"company_id": company.pk,
				"confirm_company_status": "yes",
				"action": "deactivate_company",
			},
		)
		self.assertRedirects(
			response,
			reverse("accounts:dashboard") + f"?section=companies&company={company.pk}",
		)
		company.refresh_from_db()
		self.assertFalse(company.is_active)
		response = self.client.post(
			reverse("accounts:dashboard") + "?section=companies",
			{
				"company_id": company.pk,
				"confirm_company_status": "yes",
				"action": "activate_company",
			},
		)
		self.assertEqual(response.status_code, 302)
		company.refresh_from_db()
		self.assertTrue(company.is_active)


class CompanyIntelligenceTests(TestCase):
	"""Company Intelligence reuses existing data only, always tenant-scoped."""

	password = "SecureTestPassword2026!"

	def setUp(self):
		self.owner = User.objects.create_user(
			email="intel-owner@carbonpilot.test",
			password=self.password,
			role=User.Role.COMPANY_OWNER,
			status=User.Status.ACTIVE,
		)
		self.company = Company.objects.create(
			owner=self.owner, name="Carbon Works", country="Tunisia"
		)
		self.department = Department.objects.create(company=self.company, name="Operations")
		self.factor = EmissionFactor.objects.create(
			name="Electricity — Tunisia",
			category=ActivityCategory.ELECTRICITY,
			scope=Scope.SCOPE_2,
			value=Decimal("0.42"),
			unit="kWh",
			region="Tunisia",
			year=2026,
			source="Demo source",
			verification_status=VerificationStatus.VERIFIED,
		)

		# A separate tenant used to prove isolation.
		self.other_owner = User.objects.create_user(
			email="other-intel-owner@carbonpilot.test",
			password=self.password,
			role=User.Role.COMPANY_OWNER,
			status=User.Status.ACTIVE,
		)
		self.other_company = Company.objects.create(
			owner=self.other_owner, name="Other Works", country="Tunisia"
		)

	def make_activity(self, *, company=None, status=ActivityStatus.APPROVED, period_year=2026, **overrides):
		company = company or self.company
		payload = {
			"company": company,
			"department": self.department if company == self.company else None,
			"name": "Electricity meter — January",
			"category": ActivityCategory.ELECTRICITY,
			"scope": Scope.SCOPE_2,
			"quantity": Decimal("5000"),
			"unit": "kWh",
			"period_start": date(period_year, 1, 1),
			"period_end": date(period_year, 1, 31),
			"emission_factor": self.factor,
			"data_source": "Meter reading",
			"evidence": "carbon/evidence/company_1/2026/receipt.pdf",
			"status": status,
			"created_by": self.owner,
		}
		payload.update(overrides)
		activity = CarbonActivity(**payload)
		activity.save()
		return activity

	def make_objective(self, *, company=None, status=ObjectiveStatus.ACTIVE, year=2026, tag="o"):
		company = company or self.company
		return Objective.objects.create(
			company=company,
			name=f"Objective {tag}",
			year=year,
			baseline_tco2e=Decimal("100.00"),
			target_tco2e=Decimal("80.00"),
			status=status,
		)

	def make_prediction(self, objective, *, risk=ObjectiveRiskLevel.LOW, probability="90.00"):
		return PredictionRun.objects.create(
			objective=objective,
			probability_achievement=Decimal(probability),
			risk_level=risk,
			expected_outcome=ExpectedOutcome.LIKELY_TO_ACHIEVE,
			current_progress_percent=Decimal("30.00"),
		)

	def make_recommendation(self, *, company=None, priority=RecommendationPriority.HIGH, tag="r"):
		company = company or self.company
		return RecommendationLog.objects.create(
			company=company,
			title=f"Recommendation {tag}",
			message="Reduce stand-by consumption.",
			recommended_action="Switch off idle equipment overnight.",
			priority=priority,
			source=RecommendationSource.ANOMALY,
			fingerprint=f"{company.pk}:{tag}",
		)

	def make_assessment(self, *, company=None, score="42.00", level=RiskLevel.MEDIUM):
		company = company or self.company
		return RiskAssessment.objects.create(
			company=company,
			score=Decimal(score),
			level=level,
			summary="Two drivers contributed to the score.",
			drivers_json=[
				{"key": "anomalies", "label": "Open anomalies", "points": 8.0,
				 "max_points": 30, "detail": "1 open high-severity anomaly."},
			],
			period_year=2026,
			method=RiskMethod.RULE_BASED,
			created_by=self.owner,
		)

	# ------------------------------------------------------------------ #
	# Service: data assembly and tenant isolation
	# ------------------------------------------------------------------ #
	def test_snapshot_uses_only_approved_activities(self):
		self.make_activity(status=ActivityStatus.APPROVED)
		for status in (
			ActivityStatus.DRAFT,
			ActivityStatus.SUBMITTED,
			ActivityStatus.UNDER_REVIEW,
			ActivityStatus.REJECTED,
		):
			self.make_activity(status=status)

		result = intel.build_intelligence(self.company, period_year=2026)
		self.assertEqual(result["emissions"]["approved_activities"], 1)
		self.assertEqual(result["emissions"]["total_tonnes"], 2.1)
		self.assertEqual(result["company"]["name"], "Carbon Works")

	def test_other_tenant_data_never_leaks(self):
		self.make_activity()  # own tenant
		self.make_activity(company=self.other_company)  # other tenant
		self.make_objective(company=self.other_company, tag="b")
		self.make_recommendation(company=self.other_company, tag="b")
		self.make_assessment(company=self.other_company, score="90.00", level=RiskLevel.HIGH)

		result = intel.build_intelligence(self.company, period_year=2026)
		self.assertEqual(result["emissions"]["approved_activities"], 1)
		self.assertEqual(result["emissions"]["total_tonnes"], 2.1)
		self.assertEqual(result["objectives"]["total"], 0)
		self.assertEqual(result["recommendations"]["open"], 0)
		self.assertIsNone(result["risk"])
		self.assertEqual(result["priority_actions"], [])
		self.assertNotIn("Other Works", result["summary"])

	def test_requires_a_company(self):
		with self.assertRaises(ValueError):
			intel.build_intelligence(None, period_year=2026)

	def test_resolve_year_defaults_to_newest_and_respects_request(self):
		self.make_activity(period_year=2025)
		self.make_activity(period_year=2026)

		self.assertEqual(intel.available_years(self.company), [2026, 2025])
		self.assertEqual(intel.resolve_year(self.company, None), 2026)
		self.assertEqual(intel.resolve_year(self.company, "2025"), 2025)
		# An unknown / invalid year falls back to the newest available year.
		self.assertEqual(intel.resolve_year(self.company, "1999"), 2026)
		self.assertEqual(intel.resolve_year(self.company, "not-a-year"), 2026)

	def test_year_selection_changes_reported_emissions(self):
		self.make_activity(period_year=2025)
		self.make_activity(period_year=2026)
		self.make_activity(period_year=2026)

		result_2026 = intel.build_intelligence(self.company, period_year=2026)
		result_2025 = intel.build_intelligence(self.company, period_year=2025)
		self.assertEqual(result_2026["emissions"]["approved_activities"], 2)
		self.assertEqual(result_2025["emissions"]["approved_activities"], 1)

	def test_objectives_at_risk_filters_missed_and_predicted(self):
		missed = self.make_objective(status=ObjectiveStatus.MISSED, tag="m")
		risky = self.make_objective(tag="risk")
		self.make_prediction(risky, risk=ObjectiveRiskLevel.HIGH, probability="20.00")
		safe = self.make_objective(tag="safe")
		self.make_prediction(safe, risk=ObjectiveRiskLevel.LOW, probability="95.00")

		result = intel.build_intelligence(self.company, period_year=2026)
		objectives = result["objectives"]
		self.assertEqual(objectives["total"], 3)
		self.assertEqual(objectives["missed"], 1)
		names = {item["name"] for item in objectives["items"]}
		self.assertEqual(names, {missed.name, risky.name})
		self.assertNotIn(safe.name, names)

	def test_priority_actions_combine_risk_drivers_and_recommendations(self):
		self.make_assessment(score="42.00", level=RiskLevel.MEDIUM)
		self.make_recommendation(priority=RecommendationPriority.HIGH, tag="r1")
		self.make_recommendation(priority=RecommendationPriority.LOW, tag="r2")

		result = intel.build_intelligence(self.company, period_year=2026)
		kinds = [action["kind"] for action in result["priority_actions"]]
		self.assertIn("risk_driver", kinds)
		self.assertIn("recommendation", kinds)

		driver = next(a for a in result["priority_actions"] if a["kind"] == "risk_driver")
		self.assertEqual(driver["title"], "Open anomalies")
		self.assertEqual(driver["severity"], intel.SEVERITY_RISK)

		# High priority recommendation is surfaced before the low one.
		recommendations = [
			a for a in result["priority_actions"] if a["kind"] == "recommendation"
		]
		self.assertEqual(recommendations[0]["priority_label"], "High")
		self.assertEqual(recommendations[0]["severity"], intel.SEVERITY_HIGH)

	# ------------------------------------------------------------------ #
	# View: permissions, isolation, rendering
	# ------------------------------------------------------------------ #
	def test_requires_login(self):
		response = self.client.get(reverse("enterprise:company_intelligence"))
		self.assertEqual(response.status_code, 302)
		self.assertIn(reverse("accounts:login"), response.url)

	def test_non_owner_cannot_access(self):
		manager = User.objects.create_user(
			email="intel-manager@carbonpilot.test",
			password=self.password,
			role=User.Role.DEPARTMENT_MANAGER,
			status=User.Status.ACTIVE,
			company_membership=self.company,
		)
		self.client.force_login(manager)
		response = self.client.get(reverse("enterprise:company_intelligence"))
		self.assertEqual(response.status_code, 403)

	def test_owner_without_company_is_redirected_to_company_section(self):
		owner = User.objects.create_user(
			email="intel-companyless@carbonpilot.test",
			password=self.password,
			role=User.Role.COMPANY_OWNER,
			status=User.Status.ACTIVE,
		)
		self.client.force_login(owner)
		response = self.client.get(reverse("enterprise:company_intelligence"))
		self.assertRedirects(
			response, reverse("accounts:dashboard") + "?section=company"
		)

	def test_owner_sees_intelligence_page_with_all_sections(self):
		self.make_activity()
		self.make_objective(status=ObjectiveStatus.MISSED, tag="m")
		self.make_recommendation(tag="r1")
		self.make_assessment(score="42.00", level=RiskLevel.MEDIUM)
		self.client.force_login(self.owner)

		response = self.client.get(reverse("enterprise:company_intelligence"))
		self.assertEqual(response.status_code, 200)
		self.assertTemplateUsed(response, "enterprise/intelligence.html")
		self.assertEqual(response.context["company"], self.company)
		self.assertEqual(response.context["intelligence"]["period_year"], 2026)
		self.assertContains(response, "Total emissions")
		self.assertContains(response, "Top emitting departments")
		self.assertContains(response, "ESG data completeness")
		self.assertContains(response, "Objectives at risk")
		self.assertContains(response, "Priority actions")
		self.assertContains(response, "Carbon Works")

	def test_year_query_param_selects_reported_year(self):
		self.make_activity(period_year=2025)
		self.make_activity(period_year=2026)
		self.client.force_login(self.owner)

		response = self.client.get(
			reverse("enterprise:company_intelligence"), {"year": "2025"}
		)
		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.context["intelligence"]["period_year"], 2025)

	def test_page_does_not_leak_other_tenant(self):
		self.make_activity()
		self.make_activity(company=self.other_company)
		self.client.force_login(self.owner)

		response = self.client.get(reverse("enterprise:company_intelligence"))
		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.context["intelligence"]["emissions"]["approved_activities"], 1)
		self.assertNotContains(response, "Other Works")


class DepartmentValidationTests(TestCase):
	"""Validation rules of the department form (section 6)."""

	password = "SecureTestPassword2026!"

	def setUp(self):
		self.owner = User.objects.create_user(
			email="dept-owner@carbonpilot.test",
			password=self.password,
			role=User.Role.COMPANY_OWNER,
			status=User.Status.ACTIVE,
		)
		self.company = Company.objects.create(owner=self.owner, name="Dept Works")
		self.other_owner = User.objects.create_user(
			email="dept-other@carbonpilot.test",
			password=self.password,
			role=User.Role.COMPANY_OWNER,
			status=User.Status.ACTIVE,
		)
		self.other_company = Company.objects.create(
			owner=self.other_owner, name="Other Dept Works"
		)
		self.operations = Department.objects.create(
			company=self.company, name="Operations", code="OPS"
		)

	def _form(self, **overrides):
		payload = {"name": "Finance", "code": "FIN", "description": ""}
		payload.update(overrides)
		return DepartmentForm(data=payload, company=self.company)

	def test_valid_department_is_accepted(self):
		form = self._form()
		self.assertTrue(form.is_valid(), form.errors)

	def test_name_is_required(self):
		form = self._form(name="")
		self.assertFalse(form.is_valid())
		self.assertIn("Give the department a name.", form.errors["name"])

	def test_duplicate_name_is_rejected(self):
		form = self._form(name="Operations")
		self.assertFalse(form.is_valid())
		self.assertIn(
			"Your company already has a department with this name.", form.errors["name"]
		)

	def test_duplicate_code_is_rejected(self):
		form = self._form(code="OPS")
		self.assertFalse(form.is_valid())
		self.assertIn(
			"Another department of your company already uses this code.",
			form.errors["code"],
		)

	def test_code_is_optional(self):
		form = self._form(code="")
		self.assertTrue(form.is_valid(), form.errors)

	def test_same_code_allowed_in_another_company(self):
		Department.objects.create(company=self.other_company, name="Ops", code="OPS")
		form = self._form()
		self.assertTrue(form.is_valid(), form.errors)

	def test_editing_keeps_its_own_name_and_code(self):
		form = DepartmentForm(
			data={"name": "Operations", "code": "OPS", "description": "Updated"},
			company=self.company,
			instance=self.operations,
		)
		self.assertTrue(form.is_valid(), form.errors)


class DepartmentDashboardTests(TestCase):
	"""Detail page, soft deactivate/reactivate and tenant isolation."""

	password = "SecureTestPassword2026!"

	def setUp(self):
		self.owner = User.objects.create_user(
			email="dept-dash-owner@carbonpilot.test",
			password=self.password,
			role=User.Role.COMPANY_OWNER,
			status=User.Status.ACTIVE,
		)
		self.company = Company.objects.create(owner=self.owner, name="Dash Dept Works")
		self.operations = Department.objects.create(
			company=self.company, name="Operations", code="OPS"
		)
		self.other_owner = User.objects.create_user(
			email="dept-dash-other@carbonpilot.test",
			password=self.password,
			role=User.Role.COMPANY_OWNER,
			status=User.Status.ACTIVE,
		)
		self.other_company = Company.objects.create(
			owner=self.other_owner, name="Other Dash Dept Works"
		)
		self.foreign = Department.objects.create(
			company=self.other_company, name="Finance", code="FIN"
		)
		self.client.force_login(self.owner)

	def _url(self, query):
		return f"{reverse('accounts:dashboard')}?{query}"

	def test_detail_page_renders_department(self):
		response = self.client.get(self._url(f"section=departments&view={self.operations.pk}"))
		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.context["department_detail"], self.operations)
		self.assertContains(response, "Operations")

	def test_cross_tenant_detail_is_404(self):
		response = self.client.get(self._url(f"section=departments&view={self.foreign.pk}"))
		self.assertEqual(response.status_code, 404)

	def test_owner_can_deactivate_and_reactivate(self):
		response = self.client.post(
			self._url("section=departments"),
			{
				"action": "deactivate_department",
				"department_id": self.operations.pk,
				"confirm_department_status": "yes",
			},
		)
		self.assertRedirects(response, self._url("section=departments"))
		self.operations.refresh_from_db()
		self.assertFalse(self.operations.is_active)

		response = self.client.post(
			self._url("section=departments"),
			{
				"action": "activate_department",
				"department_id": self.operations.pk,
				"confirm_department_status": "yes",
			},
		)
		self.assertRedirects(response, self._url("section=departments"))
		self.operations.refresh_from_db()
		self.assertTrue(self.operations.is_active)

	def test_deactivate_requires_confirmation(self):
		response = self.client.post(
			self._url("section=departments"),
			{"action": "deactivate_department", "department_id": self.operations.pk},
		)
		self.assertEqual(response.status_code, 403)
		self.operations.refresh_from_db()
		self.assertTrue(self.operations.is_active)

	def test_deactivate_is_blocked_while_members_are_assigned(self):
		User.objects.create_user(
			email="dept-member@carbonpilot.test",
			password=self.password,
			role=User.Role.DEPARTMENT_MANAGER,
			status=User.Status.ACTIVE,
			company_membership=self.company,
			department="Operations",
		)
		response = self.client.post(
			self._url("section=departments"),
			{
				"action": "deactivate_department",
				"department_id": self.operations.pk,
				"confirm_department_status": "yes",
			},
			follow=True,
		)
		self.assertContains(
			response,
			"Reassign them to another department before deactivating it.",
		)
		self.operations.refresh_from_db()
		self.assertTrue(self.operations.is_active)

	def test_cannot_deactivate_a_department_of_another_tenant(self):
		response = self.client.post(
			self._url("section=departments"),
			{
				"action": "deactivate_department",
				"department_id": self.foreign.pk,
				"confirm_department_status": "yes",
			},
		)
		self.assertEqual(response.status_code, 404)
		self.foreign.refresh_from_db()
		self.assertTrue(self.foreign.is_active)
