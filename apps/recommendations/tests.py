"""Tests for the AI Recommendations Engine (local Qwen via Ollama).

The Ollama HTTP call is always mocked: the standard suite never needs a running
model server. Coverage follows the specification — tenant scoping, output
validation, numeric grounding, duplicate prevention, fallback, and permissions.
"""

import json
from datetime import date
from decimal import Decimal
from unittest import mock

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.carbon.models import (
	ActivityCategory,
	ActivityStatus,
	AnomalyLog,
	AnomalySeverity,
	AnomalyStatus,
	AnomalyType,
	CarbonActivity,
	EmissionFactor,
	Scope,
)
from apps.enterprise.models import Company, Department
from apps.objectives.models import Objective, ObjectiveStatus, PeriodType

from .models import (
	GenerationMethod,
	RecommendationLog,
	RecommendationPriority,
	RecommendationSource,
	RecommendationStatus,
)
from .services import recommendations as engine
from .services import access
from .services.context import build_context
from .services.ollama_client import OllamaResult, response_schema_for
from .services.prompt import build_prompt

QWEN_PATCH = "apps.recommendations.services.recommendations.ollama_client.generate"

# Sentinel: "use the default (Operations) department" so callers can pass
# ``department=None`` to create a genuinely department-less activity.
_DEFAULT_DEPARTMENT = object()


def qwen_text(items):
	return json.dumps({"recommendations": items})


def structured(**overrides):
	"""A valid structured Qwen item body (the narrative sections filled in)."""
	item = {
		"problem": "A spike was detected in the Operations department.",
		"analysis": "The flagged activity deviates from the recorded baseline.",
		"recommendation": "Verify the flagged activity and correct or reclassify it.",
		"expected_impact": "To be estimated",
		"action_plan": [
			"Open the flagged activity.",
			"Verify the quantity and the emission factor.",
			"Correct the data if an error is found.",
		],
	}
	item.update(overrides)
	return item


class RecommendationTestCase(TestCase):
	password = "SecureTestPassword2026!"

	def setUp(self):
		self.owner = User.objects.create_user(
			email="reco-owner@carbonpilot.test",
			password=self.password,
			role=User.Role.COMPANY_OWNER,
			status=User.Status.ACTIVE,
		)
		self.company = Company.objects.create(owner=self.owner, name="Recommendations Ltd")
		self.operations = Department.objects.create(company=self.company, name="Operations")
		self.finance = Department.objects.create(company=self.company, name="Finance")

		self.other_owner = User.objects.create_user(
			email="reco-other@carbonpilot.test",
			password=self.password,
			role=User.Role.COMPANY_OWNER,
			status=User.Status.ACTIVE,
		)
		self.other_company = Company.objects.create(owner=self.other_owner, name="Other Tenant")

		self.ops_manager = self._member(
			role=User.Role.DEPARTMENT_MANAGER, email="reco-ops@carbonpilot.test", department="Operations"
		)
		self.auditor = self._member(
			role=User.Role.AUDITOR, email="reco-auditor@carbonpilot.test", department=""
		)
		self.factor = EmissionFactor.objects.create(
			name="Reco test factor",
			category=ActivityCategory.ELECTRICITY,
			scope=Scope.SCOPE_2,
			value=Decimal("1000"),
			unit="unit",
			region="Global",
			year=2026,
			source="Test",
		)

	def _member(self, *, role, email, department=""):
		return User.objects.create_user(
			email=email,
			password=self.password,
			role=role,
			status=User.Status.ACTIVE,
			company_membership=self.company,
			department=department,
		)

	def _activity(self, tonnes, *, company=None, department=_DEFAULT_DEPARTMENT,
				  status=ActivityStatus.APPROVED):
		return CarbonActivity.objects.create(
			company=company or self.company,
			department=self.operations if department is _DEFAULT_DEPARTMENT else department,
			name=f"Activity {tonnes}t",
			category=ActivityCategory.ELECTRICITY,
			scope=Scope.SCOPE_2,
			quantity=Decimal(str(tonnes)),
			unit="unit",
			period_start=date(2026, 3, 1),
			emission_factor=self.factor,
			status=status,
			created_by=self.owner,
		)

	def _anomaly(self, *, severity=AnomalySeverity.HIGH, company=None, department=None,
				 activity=None, fingerprint=None, status=None):
		company = company or self.company
		department = department if department is not None else (activity.department if activity else self.operations)
		kwargs = {
			"company": company,
			"department": department,
			"activity": activity,
			"anomaly_type": AnomalyType.SPIKE,
			"score": Decimal("0.90000"),
			"severity": severity,
			"message": "Sharp increase versus the department baseline.",
			"period_start": date(2026, 3, 1),
			"period_end": date(2026, 3, 31),
			"fingerprint": fingerprint or f"fp-{company.pk}-{severity}-{AnomalyLog.objects.count()}",
		}
		if status is not None:
			kwargs["status"] = status
		return AnomalyLog.objects.create(**kwargs)

	def _objective(self, **overrides):
		payload = {
			"company": self.company,
			"name": "Cut emissions 20%",
			"year": 2026,
			"period_type": PeriodType.ANNUAL,
			"quarter": "",
			"baseline_tco2e": Decimal("100.00"),
			"target_tco2e": Decimal("80.00"),
			"status": ObjectiveStatus.ACTIVE,
			"created_by": self.owner,
		}
		payload.update(overrides)
		return Objective.objects.create(**payload)

	def _recommendation(self, **overrides):
		payload = {
			"company": self.company,
			"title": "Existing open recommendation",
			"message": "A previously generated action.",
			"priority": RecommendationPriority.HIGH,
			"source": RecommendationSource.ANOMALY,
			"generation_method": GenerationMethod.QWEN,
			"fingerprint": f"seed-{RecommendationLog.objects.count()}",
		}
		payload.update(overrides)
		return RecommendationLog.objects.create(**payload)

	# -- evidence fixtures ------------------------------------------------- #
	def _evidence(self):
		"""One open HIGH anomaly and one at-risk objective for the company."""
		activity = self._activity(120)
		anomaly = self._anomaly(activity=activity)
		objective = self._objective(name="Halve logistics emissions", department=self.operations)
		self._activity(90, department=self.operations)
		return anomaly, objective


class ContextTests(RecommendationTestCase):
	def test_context_contains_only_the_authorized_company(self):
		anomaly, objective = self._evidence()
		foreign_activity = self._activity(500, company=self.other_company, department=None)
		foreign_anomaly = self._anomaly(
			company=self.other_company, department=None, activity=foreign_activity, fingerprint="foreign-fp"
		)
		Objective.objects.create(
			company=self.other_company,
			name="Foreign objective",
			year=2026,
			baseline_tco2e=Decimal("100"),
			target_tco2e=Decimal("10"),
			status=ObjectiveStatus.ACTIVE,
		)

		context = build_context(self.owner, self.company)

		anomaly_ids = {row["id"] for row in context.payload["open_anomalies"]}
		objective_ids = {row["id"] for row in context.payload["at_risk_objectives"]}
		self.assertIn(anomaly.pk, anomaly_ids)
		self.assertIn(objective.pk, objective_ids)
		self.assertNotIn(foreign_anomaly.pk, anomaly_ids)
		self.assertNotIn(foreign_anomaly.pk, context.anomaly_ids)

	def test_only_new_anomalies_are_offered_as_evidence(self):
		reviewed = self._anomaly(status=AnomalyStatus.REVIEWED, fingerprint="reviewed")
		context = build_context(self.owner, self.company)
		self.assertNotIn(reviewed.pk, context.anomaly_ids)

	def test_at_risk_objective_excludes_met_targets(self):
		objective = self._objective(baseline_tco2e=Decimal("100"), target_tco2e=Decimal("80"))
		self._activity(70)  # already at/below target → no gap
		context = build_context(self.owner, self.company)
		self.assertNotIn(objective.pk, context.objective_ids)

	def test_prediction_is_included_only_when_a_run_exists(self):
		from apps.objectives.models import ExpectedOutcome, PredictionRun, RiskLevel

		objective = self._objective()
		self._activity(90)
		context = build_context(self.owner, self.company)
		row = next(r for r in context.payload["at_risk_objectives"] if r["id"] == objective.pk)
		self.assertIsNone(row["prediction"])

		PredictionRun.objects.create(
			objective=objective,
			probability_achievement=Decimal("33.00"),
			risk_level=RiskLevel.HIGH,
			expected_outcome=ExpectedOutcome.AT_RISK,
			current_progress_percent=Decimal("50.00"),
		)
		context = build_context(self.owner, self.company)
		row = next(r for r in context.payload["at_risk_objectives"] if r["id"] == objective.pk)
		self.assertEqual(row["prediction"]["risk_level"], RiskLevel.HIGH)

	def test_department_manager_context_is_limited_to_their_department(self):
		self._evidence()
		finance_activity = self._activity(60, department=self.finance)
		finance_anomaly = self._anomaly(
			department=self.finance, activity=finance_activity, fingerprint="finance-fp"
		)
		context = build_context(self.ops_manager, self.company)
		self.assertNotIn(finance_anomaly.pk, context.anomaly_ids)

	def test_prompt_exposes_ids_and_the_decision_instructions(self):
		anomaly, objective = self._evidence()
		context = build_context(self.owner, self.company)
		prompt = build_prompt(context)
		self.assertIn("independently decide", prompt)
		self.assertIn("empty recommendations array", prompt)
		self.assertIn(str(anomaly.pk), prompt)
		self.assertIn(str(objective.pk), prompt)
		self.assertIn("allowed_ids", prompt)
		context.payload["data_quality"] = {"total_issues": 98765}
		prompt = build_prompt(context)
		self.assertNotIn("data_quality", prompt)
		self.assertNotIn("98765", prompt)

	def test_output_schema_restricts_ids_and_sources_to_context(self):
		anomaly, objective = self._evidence()
		context = build_context(self.owner, self.company)
		schema = response_schema_for(context)
		properties = schema["properties"]["recommendations"]["items"]["properties"]
		self.assertEqual(properties["related_anomaly_id"]["enum"], [None, anomaly.pk])
		self.assertIn(objective.pk, properties["related_objective_id"]["enum"])
		self.assertIn(self.operations.pk, properties["related_department_id"]["enum"])
		self.assertNotIn("DATA_QUALITY", properties["source"]["enum"])


class ValidationTests(RecommendationTestCase):
	def test_valid_item_is_cleaned(self):
		anomaly, _ = self._evidence()
		context = build_context(self.owner, self.company)
		item = structured(
			title="Investigate the spike anomaly",
			message="Review the flagged activity and correct or reclassify the source data.",
			priority="HIGH",
			source="ANOMALY",
			related_department_id=self.operations.pk,
			related_anomaly_id=anomaly.pk,
			related_objective_id=None,
			evidence_ids=[anomaly.pk],
		)
		cleaned, reason = engine.validate_item(item, context)
		self.assertIsNone(reason)
		self.assertEqual(cleaned["priority"], RecommendationPriority.HIGH)
		self.assertEqual(cleaned["evidence"], [f"anomaly:{anomaly.pk}"])
		self.assertTrue(cleaned["problem"])
		self.assertTrue(cleaned["analysis"])
		self.assertTrue(cleaned["recommended_action"])
		self.assertTrue(cleaned["action_plan"])
		self.assertEqual(cleaned["expected_impact"], "To be estimated")
		self.assertTrue(cleaned["evidence_items"])

	def test_incomplete_structured_sections_are_rejected(self):
		anomaly, _ = self._evidence()
		context = build_context(self.owner, self.company)
		item = {
			"title": "Investigate the spike anomaly",
			"message": "Review the flagged activity and correct the source data.",
			"priority": "HIGH",
			"source": "ANOMALY",
			"related_anomaly_id": anomaly.pk,
			"evidence_ids": [anomaly.pk],
			"analysis": "This item is missing its problem, recommendation and action plan.",
		}
		cleaned, reason = engine.validate_item(item, context)
		self.assertIsNone(cleaned)
		self.assertIn("problem", reason)

	def test_cross_company_id_is_rejected(self):
		self._evidence()
		foreign_activity = self._activity(70, company=self.other_company, department=None)
		foreign_anomaly = self._anomaly(
			company=self.other_company, department=None, activity=foreign_activity, fingerprint="foreign-2"
		)
		context = build_context(self.owner, self.company)
		item = {
			"title": "Handle a foreign anomaly",
			"message": "This references an anomaly from another tenant and must be rejected.",
			"priority": "HIGH",
			"source": "ANOMALY",
			"related_anomaly_id": foreign_anomaly.pk,
			"evidence_ids": [foreign_anomaly.pk],
		}
		cleaned, reason = engine.validate_item(item, context)
		self.assertIsNone(cleaned)
		self.assertIn("not supplied", reason)

	def test_unknown_id_is_rejected(self):
		self._evidence()
		context = build_context(self.owner, self.company)
		item = {
			"title": "Handle a ghost anomaly",
			"message": "The referenced anomaly does not exist anywhere in the context.",
			"priority": "HIGH",
			"source": "ANOMALY",
			"related_anomaly_id": 999999,
			"evidence_ids": [999999],
		}
		cleaned, reason = engine.validate_item(item, context)
		self.assertIsNone(cleaned)
		self.assertIn("not supplied", reason)

	def test_unsupported_numeric_claim_is_rejected(self):
		anomaly, _ = self._evidence()
		context = build_context(self.owner, self.company)
		item = structured(
			title="Investigate the spike anomaly",
			message="Cut emissions by 4242 tCO2e immediately across the whole company.",
			priority="HIGH",
			source="ANOMALY",
			related_anomaly_id=anomaly.pk,
			evidence_ids=[anomaly.pk],
		)
		cleaned, reason = engine.validate_item(item, context)
		self.assertIsNone(cleaned)
		self.assertIn("unsupported numeric claim", reason)

	def test_grounded_numbers_are_accepted(self):
		anomaly, _ = self._evidence()
		context = build_context(self.owner, self.company)
		item = structured(
			title="Investigate the spike anomaly",
			message="The linked activity accounts for 120 tCO2e; review and correct it.",
			priority="HIGH",
			source="ANOMALY",
			related_anomaly_id=anomaly.pk,
			evidence_ids=[anomaly.pk],
		)
		cleaned, reason = engine.validate_item(item, context)
		self.assertIsNone(reason, reason)

	def test_invalid_priority_and_source_are_rejected(self):
		anomaly, _ = self._evidence()
		context = build_context(self.owner, self.company)
		base = structured(
			title="Investigate the spike anomaly",
			message="Review the flagged activity and correct the source data.",
			related_anomaly_id=anomaly.pk,
			evidence_ids=[anomaly.pk],
		)
		cleaned, reason = engine.validate_item({**base, "priority": "URGENT", "source": "ANOMALY"}, context)
		self.assertIsNone(cleaned)
		self.assertIn("priority", reason)
		cleaned, reason = engine.validate_item({**base, "priority": "HIGH", "source": "GUESS"}, context)
		self.assertIsNone(cleaned)
		self.assertIn("source", reason)

	def test_model_cannot_claim_data_quality_source(self):
		anomaly, _ = self._evidence()
		context = build_context(self.owner, self.company)
		item = structured(
			title="Complete the activity data",
			message="Validate the pending activities so the figures can be trusted.",
			priority="MEDIUM",
			source="DATA_QUALITY",
			related_anomaly_id=anomaly.pk,
			evidence_ids=[anomaly.pk],
		)
		cleaned, reason = engine.validate_item(item, context)
		self.assertIsNone(cleaned)
		self.assertIn("not AI-decidable", reason)

	def test_source_must_match_referenced_entity(self):
		anomaly, _ = self._evidence()
		context = build_context(self.owner, self.company)
		item = structured(
			title="Investigate the spike anomaly",
			message="Review the flagged activity and correct the source data.",
			priority="HIGH",
			source="OBJECTIVE",
			related_anomaly_id=anomaly.pk,
			evidence_ids=[anomaly.pk],
		)
		cleaned, reason = engine.validate_item(item, context)
		self.assertIsNone(cleaned)
		self.assertIn("OBJECTIVE", reason)

	def test_model_clean_rejects_cross_company_references(self):
		foreign_activity = self._activity(10, company=self.other_company, department=None)
		foreign_anomaly = self._anomaly(
			company=self.other_company, department=None, activity=foreign_activity, fingerprint="foreign-3"
		)
		recommendation = RecommendationLog(
			company=self.company,
			anomaly=foreign_anomaly,
			title="Cross tenant",
			message="Should never validate.",
			priority=RecommendationPriority.HIGH,
			source=RecommendationSource.ANOMALY,
			fingerprint="cross-tenant",
		)
		with self.assertRaises(ValidationError):
			recommendation.full_clean()


class EngineTests(RecommendationTestCase):
	def test_parse_response_accepts_markdown_and_surrounding_text(self):
		payload = 'Answer:\n```json\n{"recommendations": [{"title": "Grounded action"}]}\n```\nDone.'
		self.assertEqual(
			engine.parse_response(payload),
			[{"title": "Grounded action"}],
		)
		self.assertEqual(engine.response_validation_code("not-json"), "invalid_json")
		self.assertEqual(
			engine.response_validation_code('{"items": []}'),
			"recommendations_missing_or_not_array",
		)
		self.assertEqual(
			engine.parse_response('{"recommendation": {"title": "Grounded action", "action_plan": []}}'),
			[{"title": "Grounded action", "action_plan": []}],
		)

	def test_valid_qwen_output_is_parsed_and_persisted(self):
		anomaly, _ = self._evidence()
		payload = qwen_text([
			structured(
				title="Investigate the spike anomaly",
				message="Review the flagged activity in Operations and correct or reclassify it.",
				priority="HIGH",
				source="ANOMALY",
				related_department_id=self.operations.pk,
				related_anomaly_id=anomaly.pk,
				related_objective_id=None,
				evidence_ids=[anomaly.pk],
			)
		])
		with mock.patch(QWEN_PATCH, return_value=OllamaResult(ok=True, text=payload)):
			result = engine.run_generation(self.owner, self.company)

		self.assertTrue(result.ok)
		self.assertEqual(result.method, GenerationMethod.QWEN)
		self.assertEqual(len(result.created), 1)
		stored = RecommendationLog.objects.get()
		self.assertEqual(stored.generation_method, GenerationMethod.QWEN)
		self.assertEqual(stored.model_name, "qwen2.5:7b")
		self.assertEqual(stored.priority, RecommendationPriority.HIGH)
		self.assertEqual(stored.anomaly_id, anomaly.pk)
		self.assertEqual(stored.department_id, self.operations.pk)
		self.assertEqual(stored.evidence, [f"anomaly:{anomaly.pk}"])
		self.assertEqual(stored.status, RecommendationStatus.NEW)
		# Structured content persisted; evidence built server-side.
		self.assertTrue(stored.problem)
		self.assertTrue(stored.analysis)
		self.assertTrue(stored.recommended_action)
		self.assertTrue(stored.action_plan)
		self.assertEqual(stored.expected_impact, "To be estimated")
		self.assertTrue(stored.evidence_items)
		self.assertTrue(stored.has_structured_content)

	def test_invalid_json_triggers_the_rule_based_fallback(self):
		self._evidence()
		with mock.patch(QWEN_PATCH, return_value=OllamaResult(ok=True, text="not-json-at-all")):
			result = engine.run_generation(self.owner, self.company)

		self.assertTrue(result.ok)
		self.assertEqual(result.method, GenerationMethod.RULE_BASED)
		self.assertTrue(result.ai_unavailable)
		self.assertTrue(RecommendationLog.objects.exists())
		self.assertTrue(
			RecommendationLog.objects.filter(generation_method=GenerationMethod.RULE_BASED).exists()
		)
		self.assertFalse(
			RecommendationLog.objects.filter(generation_method=GenerationMethod.QWEN).exists()
		)

	def test_ollama_unavailable_triggers_the_rule_based_fallback(self):
		anomaly, objective = self._evidence()
		with mock.patch(QWEN_PATCH, return_value=OllamaResult(ok=False, error="connection refused")):
			result = engine.run_generation(self.owner, self.company)

		self.assertEqual(result.method, GenerationMethod.RULE_BASED)
		self.assertTrue(result.ai_unavailable)
		# One anomaly-based and one objective-based rule candidate.
		self.assertEqual(RecommendationLog.objects.count(), 2)
		self.assertTrue(
			RecommendationLog.objects.filter(
				generation_method=GenerationMethod.RULE_BASED, anomaly_id=anomaly.pk
			).exists()
		)
		self.assertTrue(
			RecommendationLog.objects.filter(
				generation_method=GenerationMethod.RULE_BASED, objective_id=objective.pk
			).exists()
		)

	def test_entirely_invalid_ai_response_triggers_the_fallback(self):
		anomaly, _ = self._evidence()
		payload = qwen_text([
			structured(
				title="Investigate the spike anomaly",
				message="Cut emissions by 4242 tCO2e immediately across every department.",
				priority="HIGH",
				source="ANOMALY",
				related_anomaly_id=anomaly.pk,
				evidence_ids=[anomaly.pk],
			)
		])
		with mock.patch(QWEN_PATCH, return_value=OllamaResult(ok=True, text=payload)):
			result = engine.run_generation(self.owner, self.company)

		# No item survived validation, so the response was unusable: the engine
		# falls back to the deterministic rules rather than displaying nothing.
		self.assertEqual(result.method, GenerationMethod.RULE_BASED)
		self.assertTrue(result.ai_unavailable)
		self.assertFalse(
			RecommendationLog.objects.filter(generation_method=GenerationMethod.QWEN).exists()
		)
		# The fabricated figure was never persisted anywhere.
		for row in RecommendationLog.objects.all():
			text = " ".join(
				[row.title, row.message, row.problem, row.analysis, row.recommended_action, *row.action_plan]
			)
			self.assertNotIn("4242", text)

	def test_empty_recommendations_array_persists_nothing(self):
		self._evidence()
		with mock.patch(QWEN_PATCH, return_value=OllamaResult(ok=True, text=qwen_text([]))):
			result = engine.run_generation(self.owner, self.company)
		self.assertEqual(result.method, GenerationMethod.QWEN)
		self.assertEqual(RecommendationLog.objects.count(), 0)

	def test_no_evidence_needs_no_model_call(self):
		with mock.patch(QWEN_PATCH) as generate:
			result = engine.run_generation(self.owner, self.company)
		generate.assert_not_called()
		self.assertTrue(result.ok)
		self.assertEqual(result.method, "")
		self.assertEqual(RecommendationLog.objects.count(), 0)

	def test_duplicate_open_recommendations_are_prevented(self):
		anomaly, _ = self._evidence()
		payload = qwen_text([
			structured(
				title="Investigate the spike anomaly",
				message="Review the flagged activity in Operations and correct or reclassify it.",
				priority="HIGH",
				source="ANOMALY",
				related_anomaly_id=anomaly.pk,
				evidence_ids=[anomaly.pk],
			)
		])
		with mock.patch(QWEN_PATCH, return_value=OllamaResult(ok=True, text=payload)):
			engine.run_generation(self.owner, self.company)
			second = engine.run_generation(self.owner, self.company)

		self.assertEqual(RecommendationLog.objects.count(), 1)
		self.assertEqual(second.skipped, 1)
		self.assertEqual(len(second.created), 0)

	def test_a_closed_issue_can_be_raised_again(self):
		anomaly, _ = self._evidence()
		payload = qwen_text([
			structured(
				title="Investigate the spike anomaly",
				message="Review the flagged activity in Operations and correct or reclassify it.",
				priority="HIGH",
				source="ANOMALY",
				related_anomaly_id=anomaly.pk,
				evidence_ids=[anomaly.pk],
			)
		])
		with mock.patch(QWEN_PATCH, return_value=OllamaResult(ok=True, text=payload)):
			engine.run_generation(self.owner, self.company)
			RecommendationLog.objects.update(status=RecommendationStatus.DISMISSED)
			second = engine.run_generation(self.owner, self.company)

		self.assertEqual(len(second.created), 1)
		self.assertEqual(RecommendationLog.objects.count(), 2)

	def test_generation_never_mixes_other_companies(self):
		foreign_activity = self._activity(999, company=self.other_company, department=None)
		self._anomaly(
			company=self.other_company, department=None, activity=foreign_activity, fingerprint="foreign-only"
		)
		with mock.patch(QWEN_PATCH) as generate:
			result = engine.run_generation(self.owner, self.company)
		generate.assert_not_called()
		self.assertEqual(result.method, "")
		self.assertEqual(RecommendationLog.objects.count(), 0)


class FallbackTests(RecommendationTestCase):
	def test_fallback_rows_are_marked_rule_based(self):
		self._evidence()
		with mock.patch(QWEN_PATCH, return_value=OllamaResult(ok=False, error="down")):
			engine.run_generation(self.owner, self.company)
		self.assertTrue(RecommendationLog.objects.exists())
		self.assertEqual(
			RecommendationLog.objects.filter(generation_method=GenerationMethod.RULE_BASED).count(),
			RecommendationLog.objects.count(),
		)
		for row in RecommendationLog.objects.all():
			self.assertFalse(row.is_qwen)
			self.assertEqual(row.model_name, "")

	def test_fallback_does_not_duplicate_on_the_second_run(self):
		self._evidence()
		with mock.patch(QWEN_PATCH, return_value=OllamaResult(ok=False, error="down")):
			first = engine.run_generation(self.owner, self.company)
			second = engine.run_generation(self.owner, self.company)

		self.assertEqual(len(first.created), 2)  # one anomaly + one objective
		self.assertEqual(len(second.created), 0)
		self.assertEqual(second.skipped, 2)


class AccessTests(RecommendationTestCase):
	def test_visibility_scoping(self):
		mine = self._recommendation(department=self.operations)
		company_wide = self._recommendation(department=None, fingerprint="seed-cw")
		finance = self._recommendation(department=self.finance, fingerprint="seed-fin")

		self.assertEqual(access.visible_recommendations(self.owner, self.company).count(), 3)
		self.assertEqual(access.visible_recommendations(self.auditor, self.company).count(), 3)
		manager_ids = set(
			access.visible_recommendations(self.ops_manager, self.company).values_list("pk", flat=True)
		)
		self.assertEqual(manager_ids, {mine.pk, company_wide.pk})
		self.assertNotIn(finance.pk, manager_ids)

	def test_can_generate_and_update_rules(self):
		self.assertTrue(access.can_generate(self.owner, self.company))
		self.assertTrue(access.can_generate(self.ops_manager, self.company))
		self.assertFalse(access.can_generate(self.auditor, self.company))

		operations = self._recommendation(department=self.operations)
		company_wide = self._recommendation(department=None, fingerprint="seed-cw2")
		self.assertTrue(access.can_update_status(self.owner, self.company, company_wide))
		self.assertTrue(access.can_update_status(self.ops_manager, self.company, operations))
		self.assertFalse(access.can_update_status(self.ops_manager, self.company, company_wide))
		self.assertFalse(access.can_update_status(self.auditor, self.company, operations))


class ViewTests(RecommendationTestCase):
	def test_list_renders_for_an_authorized_user(self):
		self._recommendation(title="Trim fleet mileage")
		self.client.force_login(self.owner)
		response = self.client.get(reverse("recommendations:recommendation_list"))
		self.assertEqual(response.status_code, 200)
		self.assertContains(response, "Trim fleet mileage")
		self.assertContains(response, "Generate recommendations")

	def test_list_hides_other_company_recommendations(self):
		RecommendationLog.objects.create(
			company=self.other_company,
			title="Foreign recommendation",
			message="Must not be visible.",
			priority=RecommendationPriority.HIGH,
			source=RecommendationSource.ANOMALY,
			fingerprint="foreign-reco",
		)
		self.client.force_login(self.owner)
		response = self.client.get(reverse("recommendations:recommendation_list"))
		self.assertEqual(response.status_code, 200)
		self.assertNotContains(response, "Foreign recommendation")

	def test_generate_requires_post(self):
		self.client.force_login(self.owner)
		response = self.client.get(reverse("recommendations:recommendation_generate"))
		self.assertEqual(response.status_code, 405)

	def test_generate_creates_recommendations_for_the_owner(self):
		anomaly, _ = self._evidence()
		payload = qwen_text([
			structured(
				title="Investigate the spike anomaly",
				message="Review the flagged activity in Operations and correct or reclassify it.",
				priority="HIGH",
				source="ANOMALY",
				related_anomaly_id=anomaly.pk,
				evidence_ids=[anomaly.pk],
			)
		])
		self.client.force_login(self.owner)
		with mock.patch(QWEN_PATCH, return_value=OllamaResult(ok=True, text=payload)):
			response = self.client.post(
				reverse("recommendations:recommendation_generate"), follow=True
			)
		self.assertEqual(response.status_code, 200)
		self.assertEqual(RecommendationLog.objects.filter(company=self.company).count(), 1)

	def test_auditor_cannot_generate(self):
		self._evidence()
		self.client.force_login(self.auditor)
		response = self.client.post(reverse("recommendations:recommendation_generate"))
		self.assertEqual(response.status_code, 403)
		self.assertEqual(RecommendationLog.objects.count(), 0)

	def test_owner_can_change_status(self):
		recommendation = self._recommendation()
		self.client.force_login(self.owner)
		response = self.client.post(
			reverse(
				"recommendations:recommendation_set_status",
				args=[recommendation.pk, RecommendationStatus.DONE],
			)
		)
		self.assertEqual(response.status_code, 302)
		recommendation.refresh_from_db()
		self.assertEqual(recommendation.status, RecommendationStatus.DONE)

	def test_auditor_cannot_change_status(self):
		recommendation = self._recommendation()
		self.client.force_login(self.auditor)
		response = self.client.post(
			reverse(
				"recommendations:recommendation_set_status",
				args=[recommendation.pk, RecommendationStatus.DONE],
			)
		)
		self.assertEqual(response.status_code, 403)
		recommendation.refresh_from_db()
		self.assertEqual(recommendation.status, RecommendationStatus.NEW)

	def test_department_manager_cannot_touch_a_company_wide_recommendation(self):
		recommendation = self._recommendation(department=None)
		self.client.force_login(self.ops_manager)
		response = self.client.post(
			reverse(
				"recommendations:recommendation_set_status",
				args=[recommendation.pk, RecommendationStatus.DISMISSED],
			)
		)
		self.assertEqual(response.status_code, 403)

	def test_cross_company_recommendation_is_not_found(self):
		foreign = RecommendationLog.objects.create(
			company=self.other_company,
			title="Foreign recommendation",
			message="Not yours.",
			priority=RecommendationPriority.HIGH,
			source=RecommendationSource.ANOMALY,
			fingerprint="foreign-status",
		)
		self.client.force_login(self.owner)
		response = self.client.post(
			reverse(
				"recommendations:recommendation_set_status",
				args=[foreign.pk, RecommendationStatus.DONE],
			)
		)
		self.assertEqual(response.status_code, 404)

	def test_anonymous_user_is_redirected_to_login(self):
		response = self.client.get(reverse("recommendations:recommendation_list"))
		self.assertEqual(response.status_code, 302)
		self.assertIn("/login", response.url)

	def test_dashboard_and_objective_detail_show_recommendation_summaries(self):
		objective = self._objective(department=self.operations)
		self._activity(90)
		self._recommendation(objective=objective, title="Objective linked action")
		self.client.force_login(self.owner)

		dashboard = self.client.get(reverse("carbon:dashboard"))
		self.assertEqual(dashboard.status_code, 200)
		self.assertContains(dashboard, "AI recommendations")

		detail = self.client.get(reverse("objectives:objective_detail", args=[objective.pk]))
		self.assertEqual(detail.status_code, 200)
		self.assertContains(detail, "Objective linked action")


class StructuredContentTests(RecommendationTestCase):
	"""Evidence and structured sections for both the AI and rule-based paths."""

	def test_rule_based_recommendation_has_structured_sections(self):
		self._evidence()
		with mock.patch(QWEN_PATCH, return_value=OllamaResult(ok=False, error="down")):
			engine.run_generation(self.owner, self.company)

		row = RecommendationLog.objects.get(anomaly__isnull=False)
		self.assertFalse(row.is_qwen)
		self.assertTrue(row.problem)
		self.assertTrue(row.analysis)
		self.assertTrue(row.recommended_action)
		self.assertTrue(row.action_plan)
		self.assertEqual(row.expected_impact, "To be estimated")
		self.assertTrue(row.evidence_items)
		self.assertTrue(row.has_structured_content)

	def test_recommendation_linked_to_objective(self):
		_, objective = self._evidence()
		with mock.patch(QWEN_PATCH, return_value=OllamaResult(ok=False, error="down")):
			engine.run_generation(self.owner, self.company)

		row = RecommendationLog.objects.get(objective_id=objective.pk)
		self.assertIn(f"objective:{objective.pk}", row.evidence)
		labels = {item["label"] for item in row.evidence_items}
		self.assertIn("Target", labels)
		self.assertIn("Remaining reduction", labels)

	def test_recommendation_linked_to_anomaly(self):
		anomaly, _ = self._evidence()
		with mock.patch(QWEN_PATCH, return_value=OllamaResult(ok=False, error="down")):
			engine.run_generation(self.owner, self.company)

		row = RecommendationLog.objects.get(anomaly_id=anomaly.pk)
		self.assertIn(f"anomaly:{anomaly.pk}", row.evidence)
		labels = {item["label"] for item in row.evidence_items}
		self.assertIn("Severity", labels)
		self.assertIn("Activity CO2e", labels)

	def test_evidence_items_use_real_database_values(self):
		anomaly, _ = self._evidence()
		with mock.patch(QWEN_PATCH, return_value=OllamaResult(ok=False, error="down")):
			engine.run_generation(self.owner, self.company)

		row = RecommendationLog.objects.get(anomaly_id=anomaly.pk)
		by_label = {item["label"]: item["value"] for item in row.evidence_items}
		expected = f"{float(anomaly.activity.co2e_tonnes):.2f} tCO2e"
		self.assertEqual(by_label["Activity CO2e"], expected)
		self.assertEqual(by_label["Severity"], "High")

	def test_missing_metric_reports_data_unavailable(self):
		anomaly = self._anomaly(activity=None, department=self.operations, fingerprint="no-activity")
		with mock.patch(QWEN_PATCH, return_value=OllamaResult(ok=False, error="down")):
			engine.run_generation(self.owner, self.company)

		row = RecommendationLog.objects.get(anomaly_id=anomaly.pk)
		by_label = {item["label"]: item["value"] for item in row.evidence_items}
		self.assertEqual(by_label["Activity CO2e"], "Data unavailable")
		self.assertEqual(by_label["Linked activity"], "Data unavailable")

	def test_expected_impact_is_never_invented(self):
		self._evidence()
		with mock.patch(QWEN_PATCH, return_value=OllamaResult(ok=False, error="down")):
			engine.run_generation(self.owner, self.company)

		for row in RecommendationLog.objects.all():
			self.assertEqual(row.expected_impact, "To be estimated")

	def test_data_quality_recommendation_is_rule_based(self):
		# A submitted activity with no evidence and no anomalies/objectives at all.
		self._activity(30, status=ActivityStatus.SUBMITTED)
		with mock.patch(QWEN_PATCH) as generate:
			result = engine.run_generation(self.owner, self.company)

		generate.assert_not_called()
		self.assertEqual(result.method, GenerationMethod.RULE_BASED)
		row = RecommendationLog.objects.get(source=RecommendationSource.DATA_QUALITY)
		self.assertFalse(row.is_qwen)
		self.assertEqual(row.expected_impact, "To be estimated")
		labels = {item["label"] for item in row.evidence_items}
		self.assertIn("Unvalidated activities", labels)


class DetailViewTests(RecommendationTestCase):
	def _rich_recommendation(self, **overrides):
		return self._recommendation(
			problem="A spike was detected in Operations.",
			analysis="The activity deviates from the recorded baseline.",
			recommended_action="Verify the flagged activity.",
			expected_impact="To be estimated",
			action_plan=["Open the activity.", "Verify the factor."],
			evidence_items=[{"label": "Severity", "value": "High", "source": "anomaly:1"}],
			**overrides,
		)

	def test_detail_page_renders_sections(self):
		recommendation = self._rich_recommendation()
		self.client.force_login(self.owner)
		response = self.client.get(
			reverse("recommendations:recommendation_detail", args=[recommendation.pk])
		)
		self.assertEqual(response.status_code, 200)
		self.assertContains(response, "Why this matters")
		self.assertContains(response, "Evidence")
		self.assertContains(response, "Action plan")
		self.assertContains(response, "To be estimated")
		self.assertContains(response, "Severity")

	def test_cross_company_detail_is_not_found(self):
		foreign = RecommendationLog.objects.create(
			company=self.other_company,
			title="Foreign detail",
			message="Not yours.",
			priority=RecommendationPriority.HIGH,
			source=RecommendationSource.ANOMALY,
			fingerprint="foreign-detail",
		)
		self.client.force_login(self.owner)
		response = self.client.get(
			reverse("recommendations:recommendation_detail", args=[foreign.pk])
		)
		self.assertEqual(response.status_code, 404)

	def test_department_manager_sees_a_company_wide_detail_read_only(self):
		# The listing already shows company-wide recommendations to a manager, so
		# the detail must open too — but without any management controls.
		recommendation = self._rich_recommendation(department=None)
		self.client.force_login(self.ops_manager)
		response = self.client.get(
			reverse("recommendations:recommendation_detail", args=[recommendation.pk])
		)
		self.assertEqual(response.status_code, 200)
		self.assertContains(response, "Why this matters")
		self.assertNotContains(response, "Start Action")
		self.assertNotContains(response, "Mark done")


class StartActionTests(RecommendationTestCase):
	def test_start_action_moves_to_in_progress(self):
		recommendation = self._recommendation(status=RecommendationStatus.NEW)
		self.client.force_login(self.owner)
		response = self.client.post(
			reverse("recommendations:recommendation_start_action", args=[recommendation.pk])
		)
		self.assertEqual(response.status_code, 302)
		recommendation.refresh_from_db()
		self.assertEqual(recommendation.status, RecommendationStatus.IN_PROGRESS)

	def test_start_action_creates_no_task_object(self):
		recommendation = self._recommendation()
		result = engine.start_action(self.owner, self.company, recommendation)
		self.assertTrue(result.ok)
		self.assertTrue(result.changed)
		self.assertFalse(result.created_task)
		recommendation.refresh_from_db()
		self.assertEqual(recommendation.status, RecommendationStatus.IN_PROGRESS)

	def test_start_action_on_a_closed_recommendation_is_refused(self):
		recommendation = self._recommendation(status=RecommendationStatus.DONE)
		result = engine.start_action(self.owner, self.company, recommendation)
		self.assertFalse(result.ok)
		recommendation.refresh_from_db()
		self.assertEqual(recommendation.status, RecommendationStatus.DONE)

	def test_auditor_cannot_start_action(self):
		recommendation = self._recommendation()
		self.client.force_login(self.auditor)
		response = self.client.post(
			reverse("recommendations:recommendation_start_action", args=[recommendation.pk])
		)
		self.assertEqual(response.status_code, 403)

	def test_cross_company_start_is_not_found(self):
		foreign = RecommendationLog.objects.create(
			company=self.other_company,
			title="Foreign action",
			message="Not yours.",
			priority=RecommendationPriority.HIGH,
			source=RecommendationSource.ANOMALY,
			fingerprint="foreign-start",
		)
		self.client.force_login(self.owner)
		response = self.client.post(
			reverse("recommendations:recommendation_start_action", args=[foreign.pk])
		)
		self.assertEqual(response.status_code, 404)
