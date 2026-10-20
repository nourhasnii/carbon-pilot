import json
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

from django.test import Client, TestCase, override_settings
from django.urls import reverse

from apps.accounts.models import User
from apps.accounts.services.email_service import build_account_activation_url, send_company_invitation_email

from .models import Company, Department


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
