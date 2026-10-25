import json
from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.enterprise.models import Company
from apps.investments.models import ProjectStatus
from apps.portfolio_ai.models import PortfolioRun, PortfolioRunObjective, PortfolioRunStatus
from apps.portfolio_ai.services.llm import LLMError, LLMResult

GENERATE = "apps.portfolio_ai.views.generate_project_description"
SUMMARIZE = "apps.portfolio_ai.views.summarize_portfolio"


class LLMViewsTests(TestCase):
	def setUp(self):
		cache.clear()
		self.password = "SecureTestPassword2026!"
		self.owner = self.user("llm-owner", User.Role.COMPANY_OWNER)
		self.company = Company.objects.create(owner=self.owner, name="Entreprise LLM")
		self.auditor = self.user("llm-auditor", User.Role.AUDITOR, self.company)
		self.supplier = self.user("llm-supplier", User.Role.SUPPLIER, self.company)
		self.other_owner = self.user("llm-other", User.Role.COMPANY_OWNER)
		self.other_company = Company.objects.create(owner=self.other_owner, name="Autre LLM")
		self.run = PortfolioRun.objects.create(
			company=self.company,
			created_by=self.owner,
			budget=Decimal("10000"),
			objective=PortfolioRunObjective.MAX_TCO2E,
			included_statuses=[ProjectStatus.PROPOSED],
			status=PortfolioRunStatus.SUCCESS,
			total_cost=Decimal("8000"),
			total_tco2e=Decimal("120"),
			explanation="Explication de l'optimiseur.",
		)
		self.description_url = reverse("portfolio_ai:generate_description")
		self.summary_url = reverse("portfolio_ai:run_summary", args=[self.run.pk])

	def tearDown(self):
		cache.clear()

	def user(self, name, role, company=None):
		return User.objects.create_user(
			email=f"{name}@test.example",
			password=self.password,
			role=role,
			status=User.Status.ACTIVE,
			company_membership=company,
		)

	def post_title(self, title):
		return self.client.post(
			self.description_url,
			data=json.dumps({"title": title}),
			content_type="application/json",
		)

	# ---- Description : accès ----

	def test_description_anonymous_redirected(self):
		response = self.post_title("Panneaux solaires")
		self.assertEqual(response.status_code, 302)

	def test_description_supplier_and_auditor_refused(self):
		for user in (self.supplier, self.auditor):
			self.client.force_login(user)
			with mock.patch(GENERATE) as generate:
				response = self.post_title("Panneaux solaires")
			self.assertEqual(response.status_code, 403)
			generate.assert_not_called()

	def test_description_get_is_405(self):
		self.client.force_login(self.owner)
		self.assertEqual(self.client.get(self.description_url).status_code, 405)

	# ---- Description : validation ----

	def test_description_invalid_titles_return_400(self):
		self.client.force_login(self.owner)
		for title in ("", "  ", "ab", "x" * 121):
			with mock.patch(GENERATE) as generate:
				response = self.post_title(title)
			self.assertEqual(response.status_code, 400, title)
			self.assertFalse(response.json()["ok"])
			self.assertTrue(response.json()["error"])
			generate.assert_not_called()

	def test_description_invalid_json_returns_400(self):
		self.client.force_login(self.owner)
		response = self.client.post(
			self.description_url, data="pas du json", content_type="application/json",
		)
		self.assertEqual(response.status_code, 400)
		self.assertFalse(response.json()["ok"])

	def test_description_json_not_an_object_returns_400(self):
		self.client.force_login(self.owner)
		response = self.client.post(
			self.description_url, data="[1, 2]", content_type="application/json",
		)
		self.assertEqual(response.status_code, 400)

	# ---- Description : succès et erreurs ----

	def test_description_success(self):
		self.client.force_login(self.owner)
		result = LLMResult(text="Quatre lignes de description.", fallback=False, response_ms=12)
		with mock.patch(GENERATE, return_value=result) as generate:
			response = self.post_title("  Panneaux solaires  ")
		self.assertEqual(response.status_code, 200)
		data = response.json()
		self.assertTrue(data["ok"])
		self.assertEqual(data["text"], "Quatre lignes de description.")
		self.assertEqual(data["error"], "")
		generate.assert_called_once_with("Panneaux solaires")

	def test_description_llm_error_returns_friendly_message(self):
		self.client.force_login(self.owner)
		message = "Le service Gemini est momentanément indisponible."
		with mock.patch(GENERATE, side_effect=LLMError(message)):
			response = self.post_title("Panneaux solaires")
		self.assertEqual(response.status_code, 502)
		data = response.json()
		self.assertFalse(data["ok"])
		self.assertEqual(data["error"], message)
		self.assertNotIn("Traceback", response.content.decode())

	def test_description_rate_limit_after_ten_calls(self):
		self.client.force_login(self.owner)
		result = LLMResult(text="Texte.", fallback=False, response_ms=1)
		with mock.patch(GENERATE, return_value=result) as generate:
			for _ in range(10):
				self.assertEqual(self.post_title("Panneaux solaires").status_code, 200)
			response = self.post_title("Panneaux solaires")
		self.assertEqual(response.status_code, 429)
		self.assertFalse(response.json()["ok"])
		self.assertEqual(generate.call_count, 10)

	def test_description_rate_limit_is_per_user(self):
		other_writer = self.user("llm-esg", User.Role.ESG_REPORTING_MANAGER, self.company)
		result = LLMResult(text="Texte.", fallback=False, response_ms=1)
		with mock.patch(GENERATE, return_value=result):
			self.client.force_login(self.owner)
			for _ in range(11):
				self.post_title("Panneaux solaires")
			self.client.force_login(other_writer)
			response = self.post_title("Panneaux solaires")
		self.assertEqual(response.status_code, 200)

	# ---- Résumé : accès ----

	def test_summary_anonymous_redirected(self):
		self.assertEqual(self.client.post(self.summary_url).status_code, 302)

	def test_summary_supplier_and_auditor_refused(self):
		for user in (self.supplier, self.auditor):
			self.client.force_login(user)
			with mock.patch(SUMMARIZE) as summarize:
				response = self.client.post(self.summary_url)
			self.assertEqual(response.status_code, 403)
			summarize.assert_not_called()

	def test_summary_get_is_405(self):
		self.client.force_login(self.owner)
		self.assertEqual(self.client.get(self.summary_url).status_code, 405)

	def test_summary_other_company_gets_404(self):
		self.client.force_login(self.other_owner)
		with mock.patch(SUMMARIZE) as summarize:
			response = self.client.post(self.summary_url)
		self.assertEqual(response.status_code, 404)
		summarize.assert_not_called()

	def test_summary_unknown_run_gets_404(self):
		self.client.force_login(self.owner)
		url = reverse("portfolio_ai:run_summary", args=[999999])
		self.assertEqual(self.client.post(url).status_code, 404)

	# ---- Résumé : comportement ----

	def test_summary_requires_successful_run(self):
		PortfolioRun.objects.filter(pk=self.run.pk).update(status=PortfolioRunStatus.PENDING)
		self.client.force_login(self.owner)
		with mock.patch(SUMMARIZE) as summarize:
			response = self.client.post(self.summary_url)
		self.assertEqual(response.status_code, 400)
		self.assertFalse(response.json()["ok"])
		summarize.assert_not_called()

	def test_summary_success_saves_metrics(self):
		self.client.force_login(self.owner)
		result = LLMResult(text="Résumé exécutif.", fallback=False, response_ms=345)
		with mock.patch(SUMMARIZE, return_value=result):
			response = self.client.post(self.summary_url)
		self.assertEqual(response.status_code, 200)
		data = response.json()
		self.assertTrue(data["ok"])
		self.assertEqual(data["text"], "Résumé exécutif.")
		self.assertFalse(data["fallback"])
		self.run.refresh_from_db()
		self.assertEqual(self.run.metrics["llm_ms"], 345)
		self.assertIs(self.run.metrics["llm_fallback_used"], False)
		self.assertEqual(self.run.metrics["llm_summary"], "Résumé exécutif.")

	def test_summary_fallback_uses_optimizer_explanation(self):
		self.client.force_login(self.owner)
		result = LLMResult(text="Explication de l'optimiseur.", fallback=True, response_ms=20)
		with mock.patch(SUMMARIZE, return_value=result):
			response = self.client.post(self.summary_url)
		self.assertEqual(response.status_code, 200)
		self.assertTrue(response.json()["fallback"])
		self.run.refresh_from_db()
		self.assertIs(self.run.metrics["llm_fallback_used"], True)
		self.assertEqual(self.run.metrics["llm_summary"], "Explication de l'optimiseur.")

	def test_summary_keeps_existing_metrics(self):
		PortfolioRun.objects.filter(pk=self.run.pk).update(metrics={"exact_ms": 5})
		self.client.force_login(self.owner)
		result = LLMResult(text="Résumé.", fallback=False, response_ms=10)
		with mock.patch(SUMMARIZE, return_value=result):
			self.client.post(self.summary_url)
		self.run.refresh_from_db()
		self.assertEqual(self.run.metrics["exact_ms"], 5)
		self.assertEqual(self.run.metrics["llm_ms"], 10)

	def test_summary_rate_limit_after_ten_calls(self):
		self.client.force_login(self.owner)
		result = LLMResult(text="Résumé.", fallback=False, response_ms=1)
		with mock.patch(SUMMARIZE, return_value=result):
			for _ in range(10):
				self.assertEqual(self.client.post(self.summary_url).status_code, 200)
			response = self.client.post(self.summary_url)
		self.assertEqual(response.status_code, 429)