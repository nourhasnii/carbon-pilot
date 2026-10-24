import json
from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.test import Client, TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.enterprise.models import Company
from apps.investments.models import ProjectStatus
from apps.portfolio_ai.models import PortfolioRun, PortfolioRunObjective, PortfolioRunStatus
from apps.portfolio_ai.services.llm import LLMError, LLMResult

GENERATE = "apps.portfolio_ai.views.generate_project_description"
SUMMARIZE = "apps.portfolio_ai.views.summarize_portfolio"


class LLMViewTests(TestCase):
	def setUp(self):
		cache.clear()
		self.password = "SecureTestPassword2026!"
		self.owner = self.user("llm-owner", User.Role.COMPANY_OWNER)
		self.company = Company.objects.create(owner=self.owner, name="Entreprise LLM")
		self.auditor = self.user("llm-auditor", User.Role.AUDITOR, self.company)
		self.supplier = self.user("llm-supplier", User.Role.SUPPLIER, self.company)
		self.other_owner = self.user("llm-other", User.Role.COMPANY_OWNER)
		self.other_company = Company.objects.create(owner=self.other_owner, name="Autre LLM")
		self.run = self.make_run(self.company, self.owner, PortfolioRunStatus.SUCCESS)
		self.pending_run = self.make_run(self.company, self.owner, PortfolioRunStatus.PENDING)
		self.other_run = self.make_run(self.other_company, self.other_owner, PortfolioRunStatus.SUCCESS)
		self.description_url = reverse("portfolio_ai:generate_description")

	def user(self, name, role, company=None):
		return User.objects.create_user(
			email=f"{name}@test.example",
			password=self.password,
			role=role,
			status=User.Status.ACTIVE,
			company_membership=company,
		)

	def make_run(self, company, user, status):
		return PortfolioRun.objects.create(
			company=company,
			created_by=user,
			budget=Decimal("10000"),
			total_cost=Decimal("8000"),
			total_tco2e=Decimal("120"),
			objective=PortfolioRunObjective.MAX_TCO2E,
			included_statuses=[ProjectStatus.PROPOSED],
			status=status,
			explanation="Explication de l'optimiseur.",
		)

	def summary_url(self, run):
		return reverse("portfolio_ai:run_summary", args=[run.pk])

	def post_json(self, url, data, client=None):
		return (client or self.client).post(url, data=json.dumps(data), content_type="application/json")

	def ok_result(self, text="Texte généré."):
		return LLMResult(text=text, fallback=False, response_ms=42)

	# --- description ---------------------------------------------------
	def test_description_requires_login(self):
		response = self.post_json(self.description_url, {"title": "Panneaux solaires"})
		self.assertEqual(response.status_code, 302)

	def test_description_refused_for_read_only_and_supplier(self):
		for user in (self.auditor, self.supplier):
			self.client.force_login(user)
			with mock.patch(GENERATE) as generate:
				response = self.post_json(self.description_url, {"title": "Panneaux solaires"})
			self.assertEqual(response.status_code, 403)
			generate.assert_not_called()

	def test_description_get_is_405(self):
		self.client.force_login(self.owner)
		self.assertEqual(self.client.get(self.description_url).status_code, 405)

	def test_description_requires_csrf_token(self):
		client = Client(enforce_csrf_checks=True)
		client.force_login(self.owner)
		response = self.post_json(self.description_url, {"title": "Panneaux solaires"}, client=client)
		self.assertEqual(response.status_code, 403)

	def test_description_invalid_json_and_titles(self):
		self.client.force_login(self.owner)
		with mock.patch(GENERATE) as generate:
			bad_json = self.client.post(self.description_url, data="{pas du json", content_type="application/json")
			not_a_dict = self.post_json(self.description_url, ["x"])
			missing = self.post_json(self.description_url, {})
			too_short = self.post_json(self.description_url, {"title": "ab"})
			blank = self.post_json(self.description_url, {"title": "     "})
			too_long = self.post_json(self.description_url, {"title": "x" * 121})
		for response in (bad_json, not_a_dict, missing, too_short, blank, too_long):
			self.assertEqual(response.status_code, 400)
			self.assertFalse(response.json()["ok"])
		generate.assert_not_called()

	def test_description_success(self):
		self.client.force_login(self.owner)
		with mock.patch(GENERATE, return_value=self.ok_result()) as generate:
			response = self.post_json(self.description_url, {"title": "  Panneaux solaires  "})
		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.json(), {"ok": True, "text": "Texte généré.", "error": ""})
		generate.assert_called_once_with("Panneaux solaires")

	def test_description_llm_error_returns_502_without_details(self):
		self.client.force_login(self.owner)
		with mock.patch(GENERATE, side_effect=LLMError("Le service Gemini est momentanément indisponible.")):
			response = self.post_json(self.description_url, {"title": "Panneaux solaires"})
		self.assertEqual(response.status_code, 502)
		self.assertFalse(response.json()["ok"])
		self.assertNotIn("Traceback", response.content.decode())

	def test_description_rate_limit(self):
		self.client.force_login(self.owner)
		with mock.patch(GENERATE, return_value=self.ok_result()):
			for _ in range(10):
				self.assertEqual(
					self.post_json(self.description_url, {"title": "Panneaux solaires"}).status_code, 200
				)
			response = self.post_json(self.description_url, {"title": "Panneaux solaires"})
		self.assertEqual(response.status_code, 429)
		self.assertFalse(response.json()["ok"])

	# --- résumé --------------------------------------------------------
	def test_summary_requires_login_and_write_role(self):
		self.assertEqual(self.client.post(self.summary_url(self.run)).status_code, 302)
		for user in (self.auditor, self.supplier):
			self.client.force_login(user)
			self.assertEqual(self.client.post(self.summary_url(self.run)).status_code, 403)

	def test_summary_get_is_405(self):
		self.client.force_login(self.owner)
		self.assertEqual(self.client.get(self.summary_url(self.run)).status_code, 405)

	def test_summary_other_company_run_is_404(self):
		self.client.force_login(self.owner)
		with mock.patch(SUMMARIZE) as summarize:
			response = self.client.post(self.summary_url(self.other_run))
		self.assertEqual(response.status_code, 404)
		summarize.assert_not_called()

	def test_summary_requires_successful_run(self):
		self.client.force_login(self.owner)
		with mock.patch(SUMMARIZE) as summarize:
			response = self.client.post(self.summary_url(self.pending_run))
		self.assertEqual(response.status_code, 400)
		summarize.assert_not_called()

	def test_summary_success_saves_metrics(self):
		self.client.force_login(self.owner)
		with mock.patch(SUMMARIZE, return_value=self.ok_result("Résumé IA.")):
			response = self.client.post(self.summary_url(self.run))
		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.json()["text"], "Résumé IA.")
		self.assertFalse(response.json()["fallback"])
		self.run.refresh_from_db()
		self.assertEqual(self.run.metrics["llm_summary"], "Résumé IA.")
		self.assertEqual(self.run.metrics["llm_ms"], 42)
		self.assertFalse(self.run.metrics["llm_fallback_used"])

	def test_summary_fallback_is_flagged(self):
		self.client.force_login(self.owner)
		fallback = LLMResult(text="Explication de l'optimiseur.", fallback=True, response_ms=5)
		with mock.patch(SUMMARIZE, return_value=fallback):
			response = self.client.post(self.summary_url(self.run))
		self.assertEqual(response.status_code, 200)
		self.assertTrue(response.json()["fallback"])
		self.run.refresh_from_db()
		self.assertTrue(self.run.metrics["llm_fallback_used"])

	def test_summary_rate_limit(self):
		self.client.force_login(self.owner)
		with mock.patch(SUMMARIZE, return_value=self.ok_result()):
			for _ in range(10):
				self.assertEqual(self.client.post(self.summary_url(self.run)).status_code, 200)
			response = self.client.post(self.summary_url(self.run))
		self.assertEqual(response.status_code, 429)