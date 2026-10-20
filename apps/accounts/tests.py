import json
from io import BytesIO
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

from django.contrib.auth.tokens import default_token_generator
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from .models import AdminActivity, User
from .services.email_service import send_account_rejection_email


class AccountWorkflowTests(TestCase):
	password = "SecureTestPassword2026!"

	def setUp(self):
		self.admin = User.objects.create_superuser(
			email="admin@carbonpilot.test", password=self.password
		)

	def make_user(
		self,
		email,
		role=User.Role.DEPARTMENT_MANAGER,
		status=User.Status.ACTIVE,
		company="Carbon Works",
		department="Operations",
	):
		return User.objects.create_user(
			email=email,
			password=self.password,
			first_name="Test",
			last_name="Person",
			role=role,
			status=status,
			company=company,
			department=department,
		)

	def login_admin(self):
		self.client.force_login(self.admin)

	def test_seed_superadmin_is_idempotent(self):
		with override_settings(
			SUPER_ADMIN_EMAIL="seed@carbonpilot.test",
			SUPER_ADMIN_PASSWORD=self.password,
		):
			call_command("seed_superadmin")
			call_command("seed_superadmin")
		seeded = User.objects.get(email="seed@carbonpilot.test")
		self.assertEqual(User.objects.filter(email="seed@carbonpilot.test").count(), 1)
		self.assertEqual(seeded.role, User.Role.SUPER_ADMIN)
		self.assertEqual(seeded.status, User.Status.ACTIVE)
		self.assertTrue(seeded.check_password(self.password))

	def test_public_signup_renders_and_creates_pending_account(self):
		page = self.client.get(reverse("accounts:signup"))
		self.assertContains(page, "Supplier")
		self.assertNotContains(page, "SUPER_ADMIN")
		self.assertNotContains(page, "Confirm password")
		self.assertNotContains(page, 'name="password1"')
		self.assertNotContains(page, 'name="password2"')
		self.assertNotIn("password1", page.context["form"].fields)
		self.assertNotIn("password2", page.context["form"].fields)
		response = self.client.post(reverse("accounts:signup"), {
			"first_name": "Casey",
			"last_name": "New",
			"email": "casey@carbonpilot.test",
			"company": "Carbon Works",
			"department": "",
			"role": User.Role.SUPPLIER,
			"terms": "on",
		})
		self.assertRedirects(response, reverse("accounts:login"))
		user = User.objects.get(email="casey@carbonpilot.test")
		self.assertEqual(user.status, User.Status.PENDING)
		self.assertFalse(user.is_active)
		self.assertFalse(user.has_usable_password())

	def test_company_owner_signup_is_pending_and_super_admin_can_approve(self):
		response = self.client.post(reverse("accounts:signup"), {
			"first_name": "Casey",
			"last_name": "Owner",
			"email": "owner@carbonpilot.test",
			"company": "Carbon Works",
			"department": "",
			"role": User.Role.COMPANY_OWNER,
			"terms": "on",
		})
		self.assertRedirects(response, reverse("accounts:login"))
		user = User.objects.get(email="owner@carbonpilot.test")
		self.assertEqual(user.role, User.Role.COMPANY_OWNER)
		self.assertEqual(user.status, User.Status.PENDING)
		self.assertFalse(user.is_active)

		self.login_admin()
		with override_settings(BREVO_API_KEY="", DEFAULT_FROM_EMAIL=""):
			self.client.post(reverse("accounts:manage_user_action", args=[user.pk]), {
				"action": "accept",
				"role": User.Role.COMPANY_OWNER,
			})
		user.refresh_from_db()
		self.assertEqual(user.role, User.Role.COMPANY_OWNER)
		self.assertEqual(user.status, User.Status.ACTIVE)
		self.assertTrue(user.is_active)

	def test_super_admin_login_redirects_to_shared_dashboard(self):
		response = self.client.post(reverse("accounts:login"), {
			"username": self.admin.email, "password": self.password,
		})
		self.assertRedirects(response, reverse("accounts:dashboard"))

	def test_super_admin_pages_render(self):
		self.login_admin()
		self.assertEqual(self.client.get(reverse("accounts:admin_dashboard")).status_code, 200)
		self.assertEqual(self.client.get(reverse("accounts:user_management")).status_code, 200)
		self.assertEqual(self.client.get(reverse("accounts:create_user")).status_code, 200)

	def test_super_admin_dashboard_shows_database_statistics(self):
		self.make_user("pending-dashboard@carbonpilot.test", status=User.Status.PENDING)
		self.make_user(
			"supplier-dashboard-count@carbonpilot.test",
			role=User.Role.SUPPLIER,
			company="Supplier Group",
		)
		self.login_admin()
		response = self.client.get(reverse("accounts:admin_dashboard"))
		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.context["stats"], {
			"total": 3,
			"pending": 1,
			"active": 2,
			"companies": 2,
		})
		self.assertEqual(len(response.context["distribution"]), 6)
		self.assertContains(response, "Pending Account Approvals")
		self.assertContains(response, "pending-dashboard@carbonpilot.test")

	def test_all_roles_share_dashboard_without_admin_data_leakage(self):
		menu_items = {
			User.Role.COMPANY_OWNER: (
				"My Company",
				"Users",
				"Departments",
				"Carbon Management",
				"ESG",
				"Audits",
				"Suppliers",
				"Profile",
				"Settings",
			),
			User.Role.DEPARTMENT_MANAGER: ("Activities", "Review queue", "Emissions", "Objectives", "Profile"),
			User.Role.SUPPLIER: ("My information", "Emissions"),
			User.Role.AUDITOR: ("Reports", "Audits"),
			User.Role.ESG_REPORTING_MANAGER: ("ESG reports", "Indicators"),
		}
		for role in (
			User.Role.COMPANY_OWNER,
			User.Role.DEPARTMENT_MANAGER,
			User.Role.SUPPLIER,
			User.Role.AUDITOR,
			User.Role.ESG_REPORTING_MANAGER,
		):
			with self.subTest(role=role):
				self.client.force_login(self.make_user(f"{role.lower()}-dashboard@carbonpilot.test", role=role))
				response = self.client.get(reverse("accounts:admin_dashboard"))
				self.assertEqual(response.status_code, 200)
				self.assertTemplateUsed(response, "accounts/admin_dashboard.html")
				if role == User.Role.COMPANY_OWNER:
					self.assertContains(response, "Not set up")
					self.assertNotContains(response, "Company Identity")
				elif role == User.Role.DEPARTMENT_MANAGER:
					self.assertContains(response, "Department workspace")
				else:
					self.assertContains(response, "Your workspace")
				self.assertNotContains(response, "Pending Account Approvals")
				self.assertNotIn("stats", response.context)
				for menu_item in menu_items[role]:
					self.assertContains(response, menu_item)

	def test_role_dashboard_renders(self):
		self.client.force_login(self.make_user("supplier-dashboard@carbonpilot.test", role=User.Role.SUPPLIER))
		response = self.client.get(reverse("accounts:supplier_dashboard"))
		self.assertTemplateUsed(response, "accounts/admin_dashboard.html")
		self.assertContains(response, "My information")
		self.assertContains(response, "Emissions")

	def test_admin_created_user_can_set_password_after_approval(self):
		user = User.objects.create_user(
			email="new-user@carbonpilot.test",
			first_name="New",
			last_name="User",
			role=User.Role.SUPPLIER,
			status=User.Status.ACTIVE,
		)
		uid = urlsafe_base64_encode(force_bytes(user.pk))
		token = default_token_generator.make_token(user)
		url = reverse("accounts:set_password", kwargs={"uidb64": uid, "token": token})
		self.assertTrue(url.startswith("/accounts/activate/"))
		self.assertEqual(self.client.get(url).status_code, 200)
		response = self.client.post(url, {
			"new_password1": self.password,
			"new_password2": self.password,
		})
		self.assertRedirects(response, reverse("accounts:dashboard"))
		self.assertIn("_auth_user_id", self.client.session)
		user.refresh_from_db()
		self.assertTrue(user.check_password(self.password))
		self.assertEqual(self.client.get(url).status_code, 400)

	def test_expired_activation_token_is_rejected(self):
		user = User.objects.create_user(
			email="expired@carbonpilot.test",
			role=User.Role.AUDITOR,
			status=User.Status.ACTIVE,
		)
		uid = urlsafe_base64_encode(force_bytes(user.pk))
		token = default_token_generator.make_token(user)
		url = reverse("accounts:set_password", kwargs={"uidb64": uid, "token": token})
		with override_settings(PASSWORD_RESET_TIMEOUT=-1):
			response = self.client.get(url)
		self.assertEqual(response.status_code, 400)

	def test_pending_user_cannot_login(self):
		self.make_user("pending@carbonpilot.test", status=User.Status.PENDING)
		response = self.client.post(reverse("accounts:login"), {
			"username": "pending@carbonpilot.test", "password": self.password,
		})
		self.assertContains(response, "Your account is awaiting approval from the administrator.")
		self.assertNotIn("_auth_user_id", self.client.session)

	def test_active_user_can_login(self):
		user = self.make_user("active@carbonpilot.test")
		response = self.client.post(reverse("accounts:login"), {
			"username": user.email, "password": self.password,
		})
		self.assertRedirects(response, reverse("accounts:dashboard"))
		self.assertIn("_auth_user_id", self.client.session)

	def test_temporary_password_requires_change_before_dashboard_access(self):
		user = self.make_user("first-login@carbonpilot.test")
		user.must_change_password = True
		user.save(update_fields=("must_change_password", "updated_at"))

		response = self.client.post(reverse("accounts:login"), {
			"username": user.email, "password": self.password,
		})
		self.assertRedirects(response, reverse("accounts:force_password_change"))
		self.assertRedirects(
			self.client.get(reverse("accounts:dashboard")),
			reverse("accounts:force_password_change"),
		)
		self.assertContains(
			self.client.get(reverse("accounts:force_password_change")),
			"Choose a new password",
		)

		weak_response = self.client.post(reverse("accounts:force_password_change"), {
			"new_password1": "short",
			"new_password2": "short",
		})
		self.assertEqual(weak_response.status_code, 200)
		self.assertTrue(weak_response.context["form"].errors["new_password2"])

		response = self.client.post(reverse("accounts:force_password_change"), {
			"new_password1": self.password,
			"new_password2": self.password,
		})
		self.assertRedirects(response, reverse("accounts:dashboard"))
		user.refresh_from_db()
		self.assertFalse(user.must_change_password)
		self.assertTrue(user.check_password(self.password))
		self.assertEqual(self.client.get(reverse("accounts:dashboard")).status_code, 200)

	def test_rejected_user_cannot_login(self):
		self.make_user("rejected@carbonpilot.test", status=User.Status.REJECTED)
		response = self.client.post(reverse("accounts:login"), {
			"username": "rejected@carbonpilot.test", "password": self.password,
		})
		self.assertContains(response, "Your account request has been rejected.")
		self.assertNotIn("_auth_user_id", self.client.session)

	def test_inactive_user_cannot_login(self):
		self.make_user("inactive@carbonpilot.test", status=User.Status.INACTIVE)
		response = self.client.post(reverse("accounts:login"), {
			"username": "inactive@carbonpilot.test", "password": self.password,
		})
		self.assertContains(response, "Your account is currently inactive.")
		self.assertNotIn("_auth_user_id", self.client.session)

	def test_super_admin_can_create_pending_user(self):
		self.login_admin()
		response = self.client.post(reverse("accounts:create_user"), {
			"first_name": "Ari", "last_name": "Supplier",
			"email": "ari@carbonpilot.test", "role": User.Role.SUPPLIER,
			"company": "Supply Co", "department": "",
		})
		self.assertRedirects(response, reverse("accounts:user_management"))
		user = User.objects.get(email="ari@carbonpilot.test")
		self.assertEqual(user.status, User.Status.PENDING)
		self.assertFalse(user.has_usable_password())
		activity = AdminActivity.objects.get(target_user=user)
		self.assertEqual(activity.action, AdminActivity.Action.CREATE_ACCOUNT)
		self.assertEqual(activity.actor, self.admin)

	def test_super_admin_cannot_create_another_super_admin(self):
		self.login_admin()
		response = self.client.post(reverse("accounts:create_user"), {
			"first_name": "Another", "last_name": "Admin",
			"email": "another@carbonpilot.test", "role": User.Role.SUPER_ADMIN,
			"company": "Carbon Works", "department": "",
		})
		self.assertEqual(response.status_code, 200)
		self.assertFalse(User.objects.filter(email="another@carbonpilot.test").exists())

	def test_super_admin_can_approve_users(self):
		user = self.make_user("approve@carbonpilot.test", status=User.Status.PENDING)
		self.login_admin()
		with override_settings(BREVO_API_KEY="", DEFAULT_FROM_EMAIL=""):
			response = self.client.post(reverse("accounts:manage_user_action", args=[user.pk]), {
				"action": "accept", "role": User.Role.SUPPLIER,
			})
		user.refresh_from_db()
		self.assertEqual(response.status_code, 302)
		self.assertEqual(user.status, User.Status.ACTIVE)
		self.assertEqual(user.role, User.Role.SUPPLIER)
		self.assertTrue(user.is_active)
		self.assertTrue(user.must_change_password)
		self.assertTrue(user.has_usable_password())
		self.assertTrue(AdminActivity.objects.filter(
			target_user=user, action=AdminActivity.Action.APPROVE_ACCOUNT
		).exists())

	def test_approval_rejects_missing_or_super_admin_role(self):
		self.login_admin()
		for suffix, role in (("missing", ""), ("privileged", User.Role.SUPER_ADMIN)):
			with self.subTest(role=role):
				user = self.make_user(
					f"{suffix}-role@carbonpilot.test", status=User.Status.PENDING
				)
				self.client.post(reverse("accounts:manage_user_action", args=[user.pk]), {
					"action": "accept", "role": role,
				})
				user.refresh_from_db()
				self.assertEqual(user.status, User.Status.PENDING)
				self.assertFalse(user.must_change_password)

	def test_super_admin_can_reject_users(self):
		user = self.make_user("reject@carbonpilot.test", status=User.Status.PENDING)
		self.login_admin()
		with override_settings(BREVO_API_KEY="", DEFAULT_FROM_EMAIL=""):
			response = self.client.post(reverse("accounts:manage_user_action", args=[user.pk]), {"action": "reject"})
		user.refresh_from_db()
		self.assertEqual(response.status_code, 302)
		self.assertEqual(user.status, User.Status.REJECTED)
		self.assertFalse(user.is_active)
		self.assertTrue(AdminActivity.objects.filter(
			target_user=user, action=AdminActivity.Action.REJECT_ACCOUNT
		).exists())

	def test_company_owner_cannot_approve_or_reject_accounts(self):
		owner = self.make_user("owner-actions@carbonpilot.test", role=User.Role.COMPANY_OWNER)
		pending_user = self.make_user("pending-target@carbonpilot.test", status=User.Status.PENDING)
		self.client.force_login(owner)
		for action in ("accept", "reject"):
			with self.subTest(action=action):
				response = self.client.post(
					reverse("accounts:manage_user_action", args=[pending_user.pk]),
					{"action": action, "role": User.Role.COMPANY_OWNER},
				)
				self.assertEqual(response.status_code, 403)
		pending_user.refresh_from_db()
		self.assertEqual(pending_user.status, User.Status.PENDING)

	def test_department_manager_cannot_access_super_admin_users(self):
		self.client.force_login(self.make_user("department@carbonpilot.test"))
		response = self.client.get(reverse("accounts:user_management"))
		self.assertEqual(response.status_code, 403)

	def test_supplier_cannot_access_auditor_reports(self):
		self.client.force_login(self.make_user("supplier@carbonpilot.test", role=User.Role.SUPPLIER))
		response = self.client.get(reverse("accounts:auditor_reports"))
		self.assertEqual(response.status_code, 403)

	def test_auditor_cannot_modify_esg_reports(self):
		self.client.force_login(self.make_user("auditor@carbonpilot.test", role=User.Role.AUDITOR))
		response = self.client.post(reverse("accounts:esg_reports"), {"report": "modified"})
		self.assertEqual(response.status_code, 403)

	def test_esg_reporting_manager_cannot_access_user_management(self):
		self.client.force_login(self.make_user(
			"esg@carbonpilot.test", role=User.Role.ESG_REPORTING_MANAGER
		))
		response = self.client.get(reverse("accounts:user_management"))
		self.assertEqual(response.status_code, 403)

	def test_all_role_module_routes_are_scoped(self):
		routes_by_role = {
			User.Role.SUPER_ADMIN: [reverse("accounts:user_management")],
			User.Role.COMPANY_OWNER: [reverse("enterprise:company_dashboard")],
			User.Role.SUPPLIER: [
				reverse("accounts:supplier_information"),
				reverse("accounts:supplier_emissions"),
			],
			User.Role.AUDITOR: [
				reverse("accounts:auditor_reports"),
				reverse("accounts:auditor_audit_risk"),
			],
			User.Role.ESG_REPORTING_MANAGER: [
				reverse("accounts:esg_reports"),
				reverse("accounts:esg_indicators"),
			],
		}
		all_routes = {route for routes in routes_by_role.values() for route in routes}
		for role, permitted_routes in routes_by_role.items():
			with self.subTest(role=role):
				user = self.admin if role == User.Role.SUPER_ADMIN else self.make_user(
					f"{role.lower()}@carbonpilot.test", role=role
				)
				self.client.force_login(user)
				for route in all_routes:
					if route == reverse("enterprise:company_dashboard") and role == User.Role.SUPER_ADMIN:
						expected = 302
					elif route == reverse("enterprise:company_dashboard") and role == User.Role.COMPANY_OWNER:
						expected = 302
					else:
						expected = (
							200
							if role == User.Role.SUPER_ADMIN or route in permitted_routes
							else 403
						)
					with self.subTest(route=route):
						self.assertEqual(self.client.get(route).status_code, expected)

	def test_department_manager_module_routes_are_scoped(self):
		"""The DM workspace reuses the carbon module; routes stay tenant-scoped."""
		dm = self.make_user("dm-routes@carbonpilot.test", role=User.Role.DEPARTMENT_MANAGER)
		self.client.force_login(dm)
		# Without a company linked, every carbon route redirects to company setup.
		for route in (
			reverse("carbon:activity_list"),
			reverse("carbon:review_queue"),
			reverse("carbon:footprint"),
		):
			with self.subTest(route=route):
				self.assertEqual(self.client.get(route).status_code, 302)
		# The profile page is always reachable for an authenticated user.
		self.assertEqual(self.client.get(reverse("accounts:profile")).status_code, 200)

	@override_settings(
		BREVO_API_KEY="test-api-key",
		DEFAULT_FROM_EMAIL="noreply@carbonpilot.test",
		APP_BASE_URL="https://carbonpilot.test",
	)
	def test_approval_triggers_brevo_email(self):
		for role in (
			User.Role.COMPANY_OWNER,
			User.Role.DEPARTMENT_MANAGER,
			User.Role.SUPPLIER,
			User.Role.AUDITOR,
			User.Role.ESG_REPORTING_MANAGER,
		):
			with self.subTest(role=role):
				user = self.make_user(
					f"approve-{role.lower()}@carbonpilot.test",
					role=role,
					status=User.Status.PENDING,
				)
				self.login_admin()
				response_mock = MagicMock()
				response_mock.__enter__.return_value.status = 201
				response_mock.__enter__.return_value.read.return_value = b'{"messageId":"mock"}'
				with patch("apps.accounts.services.email_service.urlopen", return_value=response_mock) as send:
					self.client.post(reverse("accounts:manage_user_action", args=[user.pk]), {
						"action": "accept", "role": role,
					})
				self.assertTrue(send.called)
				request = send.call_args.args[0]
				self.assertEqual(request.full_url, "https://api.brevo.com/v3/smtp/email")
				self.assertNotIn("test-api-key", request.data.decode())
				payload = json.loads(request.data.decode())
				self.assertEqual(payload["to"][0]["email"], user.email)
				self.assertEqual(payload["subject"], "Your CarbonPilot account has been approved")
				self.assertIn("https://carbonpilot.test/login/", payload["htmlContent"])
				self.assertIn(user.email, payload["htmlContent"])
				self.assertIn("Temporary password", payload["htmlContent"])
				user.refresh_from_db()
				self.assertTrue(user.must_change_password)
				temporary_password = next(
					line.partition(": ")[2]
					for line in payload["textContent"].splitlines()
					if line.startswith("Temporary password: ")
				)
				self.assertTrue(user.check_password(temporary_password))
				self.assertTrue(AdminActivity.objects.filter(
					target_user=user, action=AdminActivity.Action.APPROVE_ACCOUNT
				).exists())

	@override_settings(
		BREVO_API_KEY="test-api-key",
		DEFAULT_FROM_EMAIL="noreply@carbonpilot.test",
	)
	def test_rejection_triggers_brevo_email(self):
		for role in (
			User.Role.COMPANY_OWNER,
			User.Role.DEPARTMENT_MANAGER,
			User.Role.SUPPLIER,
			User.Role.AUDITOR,
			User.Role.ESG_REPORTING_MANAGER,
		):
			with self.subTest(role=role):
				user = self.make_user(
					f"reject-{role.lower()}@carbonpilot.test",
					role=role,
					status=User.Status.PENDING,
				)
				self.login_admin()
				response_mock = MagicMock()
				response_mock.__enter__.return_value.status = 201
				response_mock.__enter__.return_value.read.return_value = b'{"messageId":"mock"}'
				with patch("apps.accounts.services.email_service.urlopen", return_value=response_mock) as send:
					self.client.post(reverse("accounts:manage_user_action", args=[user.pk]), {"action": "reject"})
				self.assertTrue(send.called)
				request = send.call_args.args[0]
				self.assertEqual(request.full_url, "https://api.brevo.com/v3/smtp/email")
				payload = json.loads(request.data.decode())
				self.assertEqual(payload["to"][0]["email"], user.email)
				self.assertEqual(payload["subject"], "CarbonPilot account request")
				self.assertIn(user.get_role_display(), payload["textContent"])
				self.assertTrue(AdminActivity.objects.filter(
					target_user=user, action=AdminActivity.Action.REJECT_ACCOUNT
				).exists())

	def test_missing_brevo_configuration_is_logged_and_does_not_call_api(self):
		user = self.make_user("missing-config@carbonpilot.test", status=User.Status.PENDING)
		self.login_admin()
		with (
			override_settings(BREVO_API_KEY="", DEFAULT_FROM_EMAIL=""),
			self.assertLogs("apps.accounts.services.email_service", level="ERROR") as logs,
			patch("apps.accounts.services.email_service.urlopen") as send,
		):
			self.client.post(reverse("accounts:manage_user_action", args=[user.pk]), {
				"action": "accept", "role": User.Role.DEPARTMENT_MANAGER,
			})
		self.assertFalse(send.called)
		self.assertIn("API key configured: False", " ".join(logs.output))
		self.assertIn("sender email configured: False", " ".join(logs.output))

	def test_admin_can_retry_approval_email_with_rotated_password(self):
		user = self.make_user("retry-email@carbonpilot.test", role=User.Role.SUPPLIER)
		user.must_change_password = True
		user.save(update_fields=("must_change_password", "updated_at"))
		self.login_admin()
		response_mock = MagicMock()
		response_mock.__enter__.return_value.status = 201
		response_mock.__enter__.return_value.read.return_value = b'{"messageId":"mock"}'
		with (
			override_settings(
				BREVO_API_KEY="test-api-key",
				DEFAULT_FROM_EMAIL="noreply@carbonpilot.test",
			),
			patch("apps.accounts.services.email_service.urlopen", return_value=response_mock) as send,
		):
			response = self.client.post(
				reverse("accounts:manage_user_action", args=[user.pk]),
				{"action": "resend_approval"},
			)
		self.assertEqual(response.status_code, 302)
		payload = json.loads(send.call_args.args[0].data.decode())
		temporary_password = next(
			line.partition(": ")[2]
			for line in payload["textContent"].splitlines()
			if line.startswith("Temporary password: ")
		)
		user.refresh_from_db()
		self.assertTrue(user.check_password(temporary_password))
		self.assertFalse(user.check_password(self.password))
		self.assertTrue(user.must_change_password)
		self.assertTrue(AdminActivity.objects.filter(
			target_user=user, action=AdminActivity.Action.RESEND_APPROVAL
		).exists())

	@override_settings(
		BREVO_API_KEY="test-api-key",
		DEFAULT_FROM_EMAIL="noreply@carbonpilot.test",
	)
	def test_brevo_http_error_is_logged_without_api_key(self):
		user = self.make_user("brevo-error@carbonpilot.test", status=User.Status.PENDING)
		error = HTTPError(
			"https://api.brevo.com/v3/smtp/email",
			400,
			"Bad Request",
			None,
			BytesIO(b'{"message":"sender is not verified; test-api-key"}'),
		)
		with (
			self.assertLogs("apps.accounts.services.email_service", level="ERROR") as logs,
			patch("apps.accounts.services.email_service.urlopen", side_effect=error),
		):
			self.assertFalse(send_account_rejection_email(user))
		log_output = " ".join(logs.output)
		self.assertIn("Status: 400", log_output)
		self.assertIn("sender is not verified", log_output)
		self.assertNotIn("test-api-key", log_output)

	@override_settings(
		BREVO_API_KEY="test-api-key",
		DEFAULT_FROM_EMAIL="noreply@carbonpilot.test",
	)
	def test_unexpected_brevo_error_is_logged_without_api_key(self):
		user = self.make_user("brevo-runtime-error@carbonpilot.test")
		with (
			self.assertLogs("apps.accounts.services.email_service", level="ERROR") as logs,
			patch(
				"apps.accounts.services.email_service.urlopen",
				side_effect=RuntimeError("transport failed: test-api-key"),
			),
		):
			self.assertFalse(send_account_rejection_email(user))
		log_output = " ".join(logs.output)
		self.assertIn("Unexpected request failure (RuntimeError)", log_output)
		self.assertNotIn("test-api-key", log_output)
