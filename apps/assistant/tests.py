"""Tests for the "Ask CarbonPilot" assistant.

Covers the tenant-scoped context (own company only), the grounding guards
(never an invented number), the insufficient-data path, the Ollama-unavailable
fallback, the HTTP views/roles and the session history.
"""

import json
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.carbon.models import (
	ActivityCategory,
	ActivityStatus,
	CarbonActivity,
	EmissionFactor,
	Scope,
	VerificationStatus,
)
from apps.enterprise.models import Company, Department
from apps.objectives.models import Objective, ObjectiveStatus
from apps.recommendations.services.ollama_client import OllamaResult
from apps.suppliers.models import ESGStatus, RiskLevel, Supplier

from .services import assistant
from .services import fallback as fallback_service
from .services.context import build_context

GENERATE = "apps.assistant.services.assistant.ollama_client.generate"


def ai_response(*, answer, sources=None, insufficient=False):
	return OllamaResult(
		ok=True,
		text=json.dumps({
			"answer": answer,
			"insufficient_data": insufficient,
			"sources": sources if sources is not None else [],
		}),
		http_status=200,
	)


class AssistantTestCase(TestCase):
	password = "SecureTestPassword2026!"

	def setUp(self):
		self.owner = User.objects.create_user(
			email="assistant-owner@carbonpilot.test",
			password=self.password,
			first_name="Assistant",
			last_name="Owner",
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
		# A completely separate tenant used to prove isolation.
		self.other_owner = User.objects.create_user(
			email="other-assistant-owner@carbonpilot.test",
			password=self.password,
			role=User.Role.COMPANY_OWNER,
			status=User.Status.ACTIVE,
		)
		self.other_company = Company.objects.create(
			owner=self.other_owner, name="Other Works"
		)

	def make_activity(self, *, company=None, quantity="5000", status=ActivityStatus.APPROVED, **overrides):
		company = company or self.company
		payload = {
			"company": company,
			"department": self.department if company == self.company else None,
			"name": "Electricity meter — January",
			"category": ActivityCategory.ELECTRICITY,
			"scope": Scope.SCOPE_2,
			"quantity": Decimal(quantity),
			"unit": "kWh",
			"period_start": date(2026, 1, 1),
			"period_end": date(2026, 1, 31),
			"emission_factor": self.factor,
			"data_source": "Meter reading",
			"evidence": "carbon/evidence/company_1/2026/receipt.pdf",
			"status": status,
			"created_by": company.owner,
		}
		payload.update(overrides)
		activity = CarbonActivity(**payload)
		activity.save()
		return activity

	def make_objective(self):
		return Objective.objects.create(
			company=self.company,
			name="Cut electricity intensity",
			year=2026,
			baseline_tco2e=Decimal("100.00"),
			target_tco2e=Decimal("80.00"),
			status=ObjectiveStatus.ACTIVE,
		)

	def make_supplier(self, *, name="Acme Supplies", risk=RiskLevel.HIGH):
		return Supplier.objects.create(
			company=self.company,
			name=name,
			country="Tunisia",
			category="Logistics",
			contact_name="Contact",
			contact_email="contact@acme.test",
			esg_status=ESGStatus.MISSING,
			risk_level=risk,
			created_by=self.owner,
		)


class ContextTests(AssistantTestCase):
	def test_context_only_reads_the_callers_company(self):
		self.make_activity(quantity="5000")  # own: 5000 * 0.42 = 2100 kg = 2.1 t
		self.make_activity(company=self.other_company, quantity="100000")  # other: 42 t

		context = build_context(self.company)

		self.assertEqual(context.company_id, self.company.pk)
		self.assertEqual(context.payload["approved_emissions"]["total_tco2e"], 2.1)
		# The other tenant's figure can never enter the grounding allowlist.
		self.assertNotIn(42.0, context.allowed_numbers)
		self.assertIn(2.1, context.allowed_numbers)

	def test_sources_reflect_available_data(self):
		self.make_activity()
		context = build_context(self.company)
		self.assertIn("Activities", context.sources)
		self.assertIn("Data Quality", context.sources)
		self.assertNotIn("Suppliers", context.sources)

		self.make_supplier()
		self.assertIn("Suppliers", build_context(self.company).sources)

	def test_no_data_company_has_no_sources(self):
		context = build_context(self.company)
		self.assertFalse(context.has_data)
		self.assertEqual(context.sources, [])

	def test_suppliers_payload_is_company_scoped(self):
		self.make_supplier(name="Acme Supplies")
		Supplier.objects.create(
			company=self.other_company,
			name="Foreign Supplier",
			country="France",
			category="Other",
			contact_name="X",
			contact_email="x@other.test",
		)
		context = build_context(self.company)
		self.assertEqual(context.payload["suppliers"]["total"], 1)
		self.assertEqual(context.payload["suppliers"]["high_risk"], 1)


class AskTests(AssistantTestCase):
	def test_empty_question_is_rejected_without_calling_the_model(self):
		with patch(GENERATE) as generate:
			result = assistant.ask(self.owner, self.company, "   ")
		generate.assert_not_called()
		self.assertFalse(result.ok)

	def test_grounded_ai_answer_is_returned(self):
		self.make_activity()
		with patch(GENERATE, return_value=ai_response(
			answer="Your approved emissions are recorded for the year under the Activities section.",
			sources=["Activities"],
		)):
			result = assistant.ask(self.owner, self.company, "What are my emissions?")
		self.assertTrue(result.ok)
		self.assertEqual(result.method, assistant.METHOD_AI)
		self.assertFalse(result.ai_unavailable)
		self.assertEqual(result.sources, ["Activities"])

	def test_ai_cannot_invent_a_number(self):
		self.make_activity()
		with patch(GENERATE, return_value=ai_response(
			answer="Your total emissions are 999999 tCO2e.", sources=["Activities"]
		)):
			result = assistant.ask(self.owner, self.company, "Total emissions?")
		# The invented figure is discarded and a grounded summary is returned instead.
		self.assertEqual(result.method, assistant.METHOD_RULE_BASED)
		self.assertTrue(result.ai_unavailable)
		self.assertNotIn("999999", result.answer)

	def test_insufficient_data_flag_is_preserved(self):
		self.make_activity()
		with patch(GENERATE, return_value=ai_response(
			answer="The recorded data does not include supplier details for that question.",
			sources=[],
			insufficient=True,
		)):
			result = assistant.ask(self.owner, self.company, "Who is my greenest supplier?")
		self.assertEqual(result.method, assistant.METHOD_AI)
		self.assertTrue(result.insufficient_data)

	def test_unknown_sources_are_filtered_out(self):
		self.make_activity()
		with patch(GENERATE, return_value=ai_response(
			answer="Here is a summary of your company data.",
			sources=["Activities", "Made Up Section"],
		)):
			result = assistant.ask(self.owner, self.company, "Summary?")
		self.assertEqual(result.sources, ["Activities"])

	def test_ollama_unavailable_falls_back_to_a_factual_summary(self):
		self.make_activity()
		down = OllamaResult(ok=False, error="ConnectionRefusedError", error_category="network_or_request_error")
		with patch(GENERATE, return_value=down):
			result = assistant.ask(self.owner, self.company, "What are my emissions?")
		self.assertTrue(result.ok)
		self.assertEqual(result.method, assistant.METHOD_RULE_BASED)
		self.assertTrue(result.ai_unavailable)
		self.assertIn(fallback_service.UNAVAILABLE_PREFIX.strip(), result.answer)

	def test_no_data_reports_insufficient_without_calling_the_model(self):
		with patch(GENERATE) as generate:
			result = assistant.ask(self.owner, self.company, "What are my emissions?")
		generate.assert_not_called()
		self.assertTrue(result.ok)
		self.assertTrue(result.insufficient_data)
		self.assertEqual(result.method, assistant.METHOD_RULE_BASED)

	def test_history_is_capped(self):
		session = type("S", (dict,), {"modified": False})()
		request = SimpleNamespace(session=session)
		result = assistant.AskResult(ok=True, answer="ok", method=assistant.METHOD_AI)
		for index in range(assistant.HISTORY_LIMIT + 5):
			assistant.push_history(request, f"question {index}", result)
		self.assertEqual(len(session[assistant.HISTORY_KEY]), assistant.HISTORY_LIMIT)
		# Only the most recent LIMIT questions are kept.
		last = assistant.HISTORY_LIMIT + 4
		self.assertEqual(session[assistant.HISTORY_KEY][-1]["question"], f"question {last}")


class AssistantViewTests(AssistantTestCase):
	def _login(self, user):
		self.client.force_login(user)

	def test_login_required(self):
		response = self.client.get(reverse("assistant:assistant_page"))
		self.assertEqual(response.status_code, 302)
		self.assertIn("/login", response["Location"])

	def test_allowed_role_can_open_the_page(self):
		self._login(self.owner)
		response = self.client.get(reverse("assistant:assistant_page"))
		self.assertEqual(response.status_code, 200)
		self.assertContains(response, "Ask a question about Carbon Works")

	def test_disallowed_roles_get_403(self):
		for role in (User.Role.SUPPLIER, User.Role.DEPARTMENT_MANAGER):
			user = User.objects.create_user(
				email=f"blocked-{role.lower()}@carbonpilot.test",
				password=self.password,
				role=role,
				status=User.Status.ACTIVE,
				company_membership=self.company,
			)
			self._login(user)
			response = self.client.get(reverse("assistant:assistant_page"))
			self.assertEqual(response.status_code, 403)

	def test_esg_manager_can_open_the_page(self):
		manager = User.objects.create_user(
			email="assistant-esg@carbonpilot.test",
			password=self.password,
			role=User.Role.ESG_REPORTING_MANAGER,
			status=User.Status.ACTIVE,
			company_membership=self.company,
		)
		self._login(manager)
		self.assertEqual(self.client.get(reverse("assistant:assistant_page")).status_code, 200)

	def test_asking_stores_the_qa_in_the_session(self):
		self.make_activity()
		self._login(self.owner)
		with patch(GENERATE, return_value=ai_response(
			answer="Your emissions are recorded under Activities.", sources=["Activities"]
		)):
			response = self.client.post(
				reverse("assistant:assistant_ask"), {"question": "What are my emissions?"}
			)
		self.assertEqual(response.status_code, 302)
		history = self.client.session[assistant.HISTORY_KEY]
		self.assertEqual(len(history), 1)
		self.assertEqual(history[0]["question"], "What are my emissions?")
		self.assertEqual(history[0]["method"], assistant.METHOD_AI)

	def test_clear_empties_the_history(self):
		self.make_activity()
		self._login(self.owner)
		with patch(GENERATE, return_value=ai_response(answer="Recorded data only.", sources=["Activities"])):
			self.client.post(reverse("assistant:assistant_ask"), {"question": "Emissions?"})
		self.client.post(reverse("assistant:assistant_clear"))
		self.assertNotIn(assistant.HISTORY_KEY, self.client.session)
