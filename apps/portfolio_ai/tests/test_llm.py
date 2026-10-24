import os
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase, override_settings

from apps.portfolio_ai.services import llm
from apps.portfolio_ai.services.llm import LLMError


def _env(key="fake-key-123", model="fake-model"):
	return mock.patch.dict(os.environ, {"GOOGLE_API_KEY": key, "GEMINI_MODEL": model})


def _client_returning(text):
	client = mock.MagicMock()
	client.interactions.create.return_value = SimpleNamespace(output_text=text)
	return client


class FakeHttpError(Exception):
	def __init__(self, status_code):
		super().__init__("détail technique secret")
		self.status_code = status_code


@override_settings(GOOGLE_API_KEY="", GEMINI_MODEL="")
class GenerateTests(SimpleTestCase):
	def test_success(self):
		with _env(), mock.patch("google.genai.Client", return_value=_client_returning("  Texte généré.  ")):
			result = llm.generate_project_description("Panneaux solaires")
		self.assertEqual(result.text, "Texte généré.")
		self.assertFalse(result.fallback)
		self.assertGreaterEqual(result.response_ms, 0)

	def test_missing_key(self):
		with _env(key=""):
			with self.assertRaises(LLMError):
				llm.generate_project_description("Panneaux solaires")

	def test_missing_model(self):
		with _env(model=""):
			with self.assertRaises(LLMError):
				llm.generate_project_description("Panneaux solaires")

	def test_empty_response(self):
		with _env(), mock.patch("google.genai.Client", return_value=_client_returning("   ")):
			with self.assertRaises(LLMError):
				llm.generate_project_description("Panneaux solaires")

	def test_quota_429(self):
		client = mock.MagicMock()
		client.interactions.create.side_effect = FakeHttpError(429)
		with _env(), mock.patch("google.genai.Client", return_value=client):
			with self.assertRaises(LLMError) as ctx:
				llm.generate_project_description("Panneaux solaires")
		self.assertIn("quota", str(ctx.exception).lower())

	def test_timeout_and_network_error(self):
		client = mock.MagicMock()
		client.interactions.create.side_effect = TimeoutError("timeout")
		with _env(), mock.patch("google.genai.Client", return_value=client):
			with self.assertRaises(LLMError):
				llm.generate_project_description("Panneaux solaires")

	def test_error_message_hides_technical_details_and_key(self):
		client = mock.MagicMock()
		client.interactions.create.side_effect = FakeHttpError(500)
		with _env(key="SUPER-SECRET-KEY"), mock.patch("google.genai.Client", return_value=client):
			with self.assertRaises(LLMError) as ctx:
				llm.generate_project_description("Panneaux solaires")
		self.assertNotIn("SUPER-SECRET-KEY", str(ctx.exception))
		self.assertNotIn("secret", str(ctx.exception).lower())


@override_settings(GOOGLE_API_KEY="", GEMINI_MODEL="")
class SummaryTests(SimpleTestCase):
	def _run(self):
		return SimpleNamespace(
			budget=Decimal("100000.00"),
			total_cost=Decimal("80000.00"),
			total_tco2e=Decimal("250.00"),
			selected_project_ids=[1, 2, 3],
			explanation="Explication de l'optimiseur.",
		)

	def test_summary_success(self):
		with _env(), mock.patch("google.genai.Client", return_value=_client_returning("Résumé IA.")):
			result = llm.summarize_portfolio(self._run())
		self.assertEqual(result.text, "Résumé IA.")
		self.assertFalse(result.fallback)

	def test_summary_prompt_contains_only_computed_figures(self):
		client = _client_returning("Résumé IA.")
		with _env(), mock.patch("google.genai.Client", return_value=client):
			llm.summarize_portfolio(self._run())
		prompt = client.interactions.create.call_args.kwargs["input"]
		for figure in ("100000.00", "80000.00", "20000.00", "250.00"):
			self.assertIn(figure, prompt)
		self.assertIn("n'invente aucun chiffre", prompt)

	def test_summary_fallback_on_error(self):
		client = mock.MagicMock()
		client.interactions.create.side_effect = FakeHttpError(500)
		with _env(), mock.patch("google.genai.Client", return_value=client):
			result = llm.summarize_portfolio(self._run())
		self.assertTrue(result.fallback)
		self.assertEqual(result.text, "Explication de l'optimiseur.")

	def test_summary_fallback_without_key(self):
		with _env(key=""):
			result = llm.summarize_portfolio(self._run())
		self.assertTrue(result.fallback)