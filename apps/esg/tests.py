"""Tests for the ESG intelligence hub.

Covered here:
* indicator CRUD, validation and the append-only history;
* missing-value versus genuine-zero semantics;
* the deterministic completeness scoring rules and readiness thresholds;
* reporting-period consistency and evidence coverage;
* persisted assessment snapshots and historical comparisons;
* tenant isolation and role permissions (404 / 403);
* the Qwen coach: JSON validation and graceful Ollama fallback;
* read-only source-module signals and regression checks on the existing apps.
"""

import json
import re
from decimal import Decimal
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import resolve, reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.enterprise.models import Company, Department
from apps.recommendations.services.ollama_client import OllamaResult

from .forms import (
	REPORTING_YEAR_MAX_OFFSET,
	REPORTING_YEAR_MIN_OFFSET,
	IndicatorForm,
	IndicatorValueForm,
)
from .models import (
	AssessmentSnapshot,
	ESGPillar,
	HistoryAction,
	Indicator,
	IndicatorCatalogEntry,
	IndicatorHistory,
	IndicatorStatus,
	IndicatorValue,
	Readiness,
)
from .services import assessment as assessment_service
from .services import coach_ai
from .services import signals as signals_service
from .services import workflow
from .services.access import (
	can_manage,
	can_verify,
	is_company_admin,
	visible_indicators,
)

GENERATE = "apps.esg.services.coach_ai.ollama_client.generate"
PASSWORD = "SecureTestPassword2026!"


def _next_link(html):
	"""Return the href of the pagination "Next" link (rel="next"), or ""."""
	match = re.search(r'<a[^>]*href="([^"]*)"[^>]*rel="next"', html)
	return match.group(1) if match else ""


def coach_json(**overrides):
	payload = {
		"executive_summary": (
			"Environmental data is largely reported but governance coverage is thin, "
			"so the company is not yet ready for reporting."
		),
		"priority_gaps": [
			{
				"pillar": "GOVERNANCE",
				"issue": "Governance controls are not tracked",
				"priority": "HIGH",
				"explanation": "GOV-004 is required but no indicator reports it for the period.",
			}
		],
		"recommended_indicators": [
			{
				"name": "Governance controls tested",
				"pillar": "GOVERNANCE",
				"justification": "A required governance metric is missing from the catalog.",
			}
		],
		"management_next_steps": [
			"Assign an owner to every governance indicator",
			"Report the outstanding period values",
		],
	}
	payload.update(overrides)
	return json.dumps(payload)


@override_settings(
	PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"]
)
class ESGTestCase(TestCase):
	"""Shared tenant, role and catalog fixtures."""

	def setUp(self):
		# Neutralise the seeded shared catalog so each test controls the
		# required-indicator set exactly.
		IndicatorCatalogEntry.objects.filter(company__isnull=True).update(is_active=False)

		self.owner = User.objects.create_user(
			email="esg-owner@carbonpilot.test",
			password=PASSWORD,
			role=User.Role.COMPANY_OWNER,
			status=User.Status.ACTIVE,
		)
		self.company = Company.objects.create(owner=self.owner, name="ESG Works")
		self.operations = Department.objects.create(company=self.company, name="Operations")
		self.finance = Department.objects.create(company=self.company, name="Finance")

		self.other_owner = User.objects.create_user(
			email="esg-other-owner@carbonpilot.test",
			password=PASSWORD,
			role=User.Role.COMPANY_OWNER,
			status=User.Status.ACTIVE,
		)
		self.other_company = Company.objects.create(owner=self.other_owner, name="Other ESG")

		self.esg_manager = self._member(
			User.Role.ESG_REPORTING_MANAGER, "esg-manager@carbonpilot.test"
		)
		self.ops_manager = self._member(
			User.Role.DEPARTMENT_MANAGER, "esg-ops@carbonpilot.test", department="Operations"
		)
		self.finance_manager = self._member(
			User.Role.DEPARTMENT_MANAGER, "esg-finance@carbonpilot.test", department="Finance"
		)
		self.auditor = self._member(User.Role.AUDITOR, "esg-auditor@carbonpilot.test")
		self.outsider = User.objects.create_user(
			email="esg-outsider@carbonpilot.test",
			password=PASSWORD,
			role=User.Role.DEPARTMENT_MANAGER,
			status=User.Status.ACTIVE,
			company_membership=self.other_company,
			department="Operations",
		)

	def _member(self, role, email, department=""):
		return User.objects.create_user(
			email=email,
			password=PASSWORD,
			role=role,
			status=User.Status.ACTIVE,
			company_membership=self.company,
			department=department,
		)

	# -- fixtures ---------------------------------------------------------- #
	def _catalog(self, code, pillar=ESGPillar.ENVIRONMENTAL, *, required=True, unit="unit", company=None):
		return IndicatorCatalogEntry.objects.create(
			code=code,
			name=f"{code} metric",
			pillar=pillar,
			unit=unit,
			is_required=required,
			is_active=True,
			company=company,
		)

	def _track(self, entry, *, company=None, department=None, owner=None):
		return workflow.create_indicator(
			company=company or self.company,
			catalog_entry=entry,
			actor=self.owner,
			department=department,
			owner=owner,
		)

	def _measure(self, indicator, year, value, *, status=IndicatorStatus.DRAFT, evidence=""):
		return workflow.record_value(
			indicator,
			actor=self.owner,
			period_year=year,
			value=value,
			evidence_reference=evidence,
			status=status,
		)

	def _verified(self, indicator, year, value, *, evidence="evidence ref"):
		row = self._measure(indicator, year, value, status=IndicatorStatus.REPORTED, evidence=evidence)
		return workflow.verify_value(row, actor=self.owner)


# --------------------------------------------------------------------------- #
# Indicator CRUD, validation and history
# --------------------------------------------------------------------------- #
class IndicatorWorkflowTests(ESGTestCase):
	def test_create_indicator_derives_company_and_writes_history(self):
		entry = self._catalog("ENV-T01")
		indicator = workflow.create_indicator(
			company=self.company,
			catalog_entry=entry,
			actor=self.owner,
			department=self.operations,
		)
		self.assertEqual(indicator.company, self.company)
		self.assertTrue(indicator.is_active)
		history = indicator.history.all()
		self.assertEqual(history.count(), 1)
		self.assertEqual(history.first().action, HistoryAction.CREATED)
		self.assertEqual(history.first().actor, self.owner)

	def test_duplicate_indicator_is_rejected(self):
		entry = self._catalog("ENV-T02")
		self._track(entry)
		with self.assertRaises(ValidationError):
			self._track(entry)

	def test_cannot_track_another_tenants_catalog_entry(self):
		entry = self._catalog("ENV-T03", company=self.other_company)
		with self.assertRaises(ValidationError):
			self._track(entry)

	def test_update_indicator_logs_an_update(self):
		entry = self._catalog("ENV-T04")
		indicator = self._track(entry)
		workflow.update_indicator(
			indicator, actor=self.owner, department=self.finance, notes="Reassigned."
		)
		indicator.refresh_from_db()
		self.assertEqual(indicator.department, self.finance)
		self.assertIn(HistoryAction.UPDATED, [h.action for h in indicator.history.all()])

	def test_deactivate_and_reactivate_are_soft_and_audited(self):
		entry = self._catalog("ENV-T05")
		indicator = self._track(entry)
		workflow.set_indicator_active(indicator, False, actor=self.owner)
		indicator.refresh_from_db()
		self.assertFalse(indicator.is_active)
		self.assertEqual(
			indicator.history.first().action, HistoryAction.ARCHIVED
		)
		workflow.set_indicator_active(indicator, True, actor=self.owner)
		indicator.refresh_from_db()
		self.assertTrue(indicator.is_active)
		self.assertEqual(indicator.history.first().action, HistoryAction.REACTIVATED)
		self.assertEqual(Indicator.objects.filter(pk=indicator.pk).count(), 1)

	def test_record_value_defaults_unit_and_inherits_tenant(self):
		entry = self._catalog("ENV-T06", unit="tCO2e")
		indicator = self._track(entry)
		row = self._measure(indicator, 2025, Decimal("12.5"))
		self.assertEqual(row.company, self.company)
		self.assertEqual(row.unit, "tCO2e")
		self.assertEqual(row.status, IndicatorStatus.DRAFT)
		self.assertEqual(indicator.history.first().action, HistoryAction.VALUE_RECORDED)

	def test_recording_the_same_period_updates_it(self):
		entry = self._catalog("ENV-T07")
		indicator = self._track(entry)
		self._measure(indicator, 2025, Decimal("1"))
		self._measure(indicator, 2025, Decimal("2"))
		self.assertEqual(IndicatorValue.objects.filter(indicator=indicator).count(), 1)
		self.assertEqual(indicator.values.first().value, Decimal("2"))
		self.assertEqual(indicator.history.first().action, HistoryAction.VALUE_UPDATED)

	def test_reporting_a_missing_value_is_rejected(self):
		entry = self._catalog("ENV-T08")
		indicator = self._track(entry)
		with self.assertRaises(ValidationError):
			self._measure(indicator, 2025, None, status=IndicatorStatus.REPORTED)

	def test_negative_measurement_is_rejected(self):
		entry = self._catalog("ENV-T09")
		indicator = self._track(entry)
		with self.assertRaises(ValidationError):
			self._measure(indicator, 2025, Decimal("-1"))

	def test_verified_period_cannot_be_edited(self):
		entry = self._catalog("ENV-T10")
		indicator = self._track(entry)
		self._verified(indicator, 2025, Decimal("9"))
		with self.assertRaises(ValidationError):
			self._measure(indicator, 2025, Decimal("10"))

	def test_verification_lifecycle_and_transitions(self):
		entry = self._catalog("ENV-T11")
		indicator = self._track(entry)
		row = self._measure(indicator, 2025, Decimal("4"))
		with self.assertRaises(ValidationError):
			workflow.verify_value(row, actor=self.owner)  # draft cannot be verified
		workflow.mark_reported(row, actor=self.owner)
		row.refresh_from_db()
		self.assertEqual(row.status, IndicatorStatus.REPORTED)
		self.assertIsNotNone(row.reported_at)
		workflow.verify_value(row, actor=self.auditor)
		row.refresh_from_db()
		self.assertEqual(row.status, IndicatorStatus.VERIFIED)
		self.assertEqual(row.verified_by, self.auditor)
		workflow.return_value_to_draft(row, actor=self.auditor, reason="Recount needed.")
		row.refresh_from_db()
		self.assertEqual(row.status, IndicatorStatus.DRAFT)
		self.assertIsNone(row.verified_by)
		self.assertIn(
			HistoryAction.REJECTED, [h.action for h in indicator.history.all()]
		)

	def test_indicator_history_is_append_only(self):
		entry = self._catalog("ENV-T12")
		indicator = self._track(entry)
		entry_row = indicator.history.first()
		entry_row.summary = "tampered"
		with self.assertRaises(ValidationError):
			entry_row.save()
		self.assertEqual(IndicatorHistory.objects.filter(indicator=indicator).count(), 1)


# --------------------------------------------------------------------------- #
# Missing versus zero
# --------------------------------------------------------------------------- #
class MissingVersusZeroTests(ESGTestCase):
	def test_missing_measurement_is_not_eligible(self):
		entry = self._catalog("ENV-Z01")
		indicator = self._track(entry)
		row = self._measure(indicator, 2025, None)
		self.assertTrue(row.is_missing)
		self.assertFalse(row.has_value)
		self.assertFalse(row.is_eligible)

	def test_genuine_zero_is_eligible_when_reported(self):
		entry = self._catalog("ENV-Z02")
		indicator = self._track(entry)
		row = self._measure(indicator, 2025, Decimal("0"), status=IndicatorStatus.REPORTED)
		self.assertFalse(row.is_missing)
		self.assertTrue(row.has_value)
		self.assertTrue(row.is_eligible)
		self.assertEqual(row.display_value, "0")

	def test_assessment_distinguishes_zero_from_missing(self):
		zero_entry = self._catalog("ENV-Z03")
		missing_entry = self._catalog("ENV-Z04")
		zero_indicator = self._track(zero_entry)
		missing_indicator = self._track(missing_entry)
		self._measure(zero_indicator, 2025, Decimal("0"), status=IndicatorStatus.REPORTED, evidence="e")
		self._measure(missing_indicator, 2025, None)  # draft, no value

		result = assessment_service.assess_company(self.company, period_year=2025)
		self.assertEqual(result.total_required, 2)
		self.assertEqual(result.covered, 2)
		self.assertEqual(result.eligible, 1)  # only the explicit zero counts
		kinds = {gap["kind"] for kind in result.gaps for gap in result.gaps[kind]}
		self.assertIn("draft_only", kinds)


# --------------------------------------------------------------------------- #
# Completeness scoring rules
# --------------------------------------------------------------------------- #
class CompletenessScoringTests(ESGTestCase):
	def _four_required(self):
		return [
			self._catalog("ENV-S1", ESGPillar.ENVIRONMENTAL),
			self._catalog("ENV-S2", ESGPillar.ENVIRONMENTAL),
			self._catalog("SOC-S1", ESGPillar.SOCIAL),
			self._catalog("GOV-S1", ESGPillar.GOVERNANCE),
		]

	def test_perfect_data_scores_100_and_is_ready(self):
		for entry in self._four_required():
			indicator = self._track(entry)
			self._verified(indicator, 2025, Decimal("1"), evidence="doc")
		result = assessment_service.assess_company(self.company, period_year=2025)
		self.assertEqual(result.overall_score, Decimal("100.00"))
		self.assertEqual(result.readiness, Readiness.READY_FOR_REPORTING)
		self.assertFalse(result.insufficient_data)
		self.assertEqual(result.environmental_score, Decimal("100.00"))

	def test_reported_but_unverified_and_unevidenced_scores_60(self):
		for entry in self._four_required():
			indicator = self._track(entry)
			self._measure(indicator, 2025, Decimal("1"), status=IndicatorStatus.REPORTED)
		result = assessment_service.assess_company(self.company, period_year=2025)
		# coverage 40 + period 20 + verification 0 + evidence 0 = 60
		self.assertEqual(result.overall_score, Decimal("60.00"))
		self.assertEqual(result.readiness, Readiness.NEEDS_REVIEW)

	def test_untracked_required_indicator_reduces_coverage(self):
		entries = self._four_required()
		for entry in entries[:2]:
			indicator = self._track(entry)
			self._verified(indicator, 2025, Decimal("1"))
		result = assessment_service.assess_company(self.company, period_year=2025)
		# coverage 20 + period 20 + verification 25 + evidence 15 = 80
		self.assertEqual(result.overall_score, Decimal("80.00"))
		self.assertEqual(len(result.gaps["missing"]), 2)
		self.assertEqual(result.readiness, Readiness.NEEDS_REVIEW)

	def test_a_blocking_gap_prevents_ready_even_with_a_high_score(self):
		entries = [
			self._catalog("ENV-B1", ESGPillar.ENVIRONMENTAL),
			self._catalog("ENV-B2", ESGPillar.ENVIRONMENTAL),
			self._catalog("SOC-B1", ESGPillar.SOCIAL),
			self._catalog("GOV-B1", ESGPillar.GOVERNANCE),
			self._catalog("GOV-B2", ESGPillar.GOVERNANCE),
		]
		for entry in entries[:4]:
			indicator = self._track(entry)
			self._verified(indicator, 2025, Decimal("1"))
		result = assessment_service.assess_company(self.company, period_year=2025)
		# coverage 32 + period 20 + verification 25 + evidence 15 = 92, but one gap blocks readiness.
		self.assertGreaterEqual(result.overall_score, Decimal("85"))
		self.assertEqual(result.readiness, Readiness.NEEDS_REVIEW)

	def test_partial_verification_allows_ready_when_evidence_is_present(self):
		for entry in self._four_required():
			indicator = self._track(entry)
			self._verified(indicator, 2025, Decimal("1"), evidence="doc")
		# Return one to a reported (unverified) state.
		row = IndicatorValue.objects.filter(status=IndicatorStatus.VERIFIED).first()
		workflow.return_value_to_draft(row, actor=self.owner)
		workflow.mark_reported(row, actor=self.owner)
		result = assessment_service.assess_company(self.company, period_year=2025)
		# 40 + 20 + 18.75 + 15 = 93.75, unverified is not a blocking gap.
		self.assertEqual(result.overall_score, Decimal("93.75"))
		self.assertEqual(result.readiness, Readiness.READY_FOR_REPORTING)

	def test_inactive_indicator_is_excluded_and_reported_as_a_gap(self):
		entries = self._four_required()
		for entry in entries:
			indicator = self._track(entry)
			self._verified(indicator, 2025, Decimal("1"))
		workflow.set_indicator_active(
			Indicator.objects.filter(company=self.company).first(), False, actor=self.owner
		)
		result = assessment_service.assess_company(self.company, period_year=2025)
		self.assertEqual(len(result.gaps["inactive"]), 1)
		self.assertEqual(result.covered, 3)
		self.assertEqual(result.readiness, Readiness.NEEDS_REVIEW)

	def test_no_required_catalog_is_not_ready_and_insufficient(self):
		result = assessment_service.assess_company(self.company, period_year=2025)
		self.assertEqual(result.total_required, 0)
		self.assertTrue(result.insufficient_data)
		self.assertEqual(result.readiness, Readiness.NOT_READY)

	def test_no_eligible_data_is_not_ready(self):
		for entry in self._four_required():
			self._track(entry)
		result = assessment_service.assess_company(self.company, period_year=2025)
		self.assertEqual(result.eligible, 0)
		self.assertTrue(result.insufficient_data)
		self.assertEqual(result.readiness, Readiness.NOT_READY)
		self.assertEqual(len(result.gaps["incomplete"]), 4)

	def test_summary_labels_the_score_as_data_completeness(self):
		for entry in self._four_required():
			self._track(entry)
		result = assessment_service.assess_company(self.company, period_year=2025)
		self.assertIn("data-completeness", result.summary)
		self.assertIn("not an ESG performance rating", result.summary)

	def test_breakdown_components_sum_to_the_score(self):
		for entry in self._four_required():
			indicator = self._track(entry)
			self._measure(indicator, 2025, Decimal("1"), status=IndicatorStatus.REPORTED)
		result = assessment_service.assess_company(self.company, period_year=2025)
		total = sum(Decimal(str(item["points"])) for item in result.breakdown)
		self.assertEqual(total, result.overall_score)
		self.assertEqual(
			sum(item["max_points"] for item in result.breakdown),
			assessment_service.TOTAL_WEIGHT,
		)


# --------------------------------------------------------------------------- #
# Reporting period and evidence coverage
# --------------------------------------------------------------------------- #
class PeriodAndEvidenceTests(ESGTestCase):
	def test_values_from_other_periods_never_count(self):
		entry = self._catalog("ENV-P1")
		indicator = self._track(entry)
		self._verified(indicator, 2024, Decimal("1"))
		result = assessment_service.assess_company(self.company, period_year=2025)
		self.assertEqual(result.eligible, 0)
		self.assertEqual(len(result.gaps["incomplete"]), 1)

	def test_available_period_years_is_distinct_and_descending(self):
		entry = self._catalog("ENV-P2")
		indicator = self._track(entry)
		self._measure(indicator, 2023, Decimal("1"))
		self._measure(indicator, 2025, Decimal("1"))
		self._measure(indicator, 2024, Decimal("1"))
		self.assertEqual(
			assessment_service.available_period_years(self.company), [2025, 2024, 2023]
		)

	def test_target_period_year_prefers_the_request_then_latest(self):
		entry = self._catalog("ENV-P3")
		indicator = self._track(entry)
		self._measure(indicator, 2024, Decimal("1"))
		self.assertEqual(
			assessment_service.target_period_year(self.company), 2024
		)
		self.assertEqual(
			assessment_service.target_period_year(self.company, 2030), 2030
		)

	def test_missing_evidence_reduces_evidence_coverage(self):
		with_evidence = self._catalog("ENV-P4")
		without_evidence = self._catalog("ENV-P5")
		first = self._track(with_evidence)
		second = self._track(without_evidence)
		self._verified(first, 2025, Decimal("1"), evidence="invoice-42")
		self._verified(second, 2025, Decimal("1"), evidence="")
		result = assessment_service.assess_company(self.company, period_year=2025)
		self.assertEqual(result.with_evidence, 1)
		self.assertEqual(len(result.gaps["no_evidence"]), 1)
		evidence_component = next(
			item for item in result.breakdown if item["key"] == "evidence"
		)
		self.assertEqual(evidence_component["points"], 7.5)


# --------------------------------------------------------------------------- #
# Assessment snapshots and historical comparison
# --------------------------------------------------------------------------- #
class AssessmentSnapshotTests(ESGTestCase):
	def test_run_assessment_persists_an_explainable_snapshot(self):
		entry = self._catalog("ENV-A1")
		indicator = self._track(entry)
		self._verified(indicator, 2025, Decimal("1"))
		snapshot = assessment_service.run_assessment(
			self.company, actor=self.owner, period_year=2025
		)
		self.assertEqual(snapshot.period_year, 2025)
		self.assertEqual(len(snapshot.breakdown), 4)
		self.assertIsNone(snapshot.delta_score)
		self.assertEqual(snapshot.created_by, self.owner)
		self.assertTrue(snapshot.summary)
		self.assertEqual(AssessmentSnapshot.objects.for_company(self.company).count(), 1)

	def test_second_snapshot_records_the_delta(self):
		entry = self._catalog("ENV-A2")
		indicator = self._track(entry)
		self._measure(indicator, 2025, Decimal("1"), status=IndicatorStatus.REPORTED)
		assessment_service.run_assessment(self.company, actor=self.owner, period_year=2025)
		workflow.verify_value(indicator.values.first(), actor=self.owner)
		second = assessment_service.run_assessment(
			self.company, actor=self.owner, period_year=2025
		)
		self.assertIsNotNone(second.delta_score)
		self.assertGreater(second.delta_score, Decimal("0"))
		self.assertEqual(second.delta_direction, "up")

	def test_previous_assessment_is_scoped_to_the_same_period(self):
		entry = self._catalog("ENV-A3")
		self._track(entry)
		other_period = assessment_service.run_assessment(
			self.company, actor=self.owner, period_year=2024
		)
		first = assessment_service.run_assessment(
			self.company, actor=self.owner, period_year=2025
		)
		second = assessment_service.run_assessment(
			self.company, actor=self.owner, period_year=2025
		)
		# The 2024 snapshot is never treated as the predecessor of a 2025 one.
		self.assertIsNone(assessment_service.previous_assessment(other_period))
		self.assertIsNone(assessment_service.previous_assessment(first))
		self.assertEqual(assessment_service.previous_assessment(second), first)

	def test_snapshot_isolated_per_tenant(self):
		entry = self._catalog("ENV-A4")
		self._track(entry)
		assessment_service.run_assessment(self.company, actor=self.owner, period_year=2025)
		self.assertEqual(
			AssessmentSnapshot.objects.for_company(self.other_company).count(), 0
		)


# --------------------------------------------------------------------------- #
# Tenant isolation
# --------------------------------------------------------------------------- #
class TenantIsolationTests(ESGTestCase):
	def test_cross_tenant_indicator_detail_is_404(self):
		entry = self._catalog("ENV-I1")
		indicator = self._track(entry)
		self.client.force_login(self.outsider)
		response = self.client.get(
			reverse("esg:indicator_detail", args=[indicator.pk])
		)
		self.assertEqual(response.status_code, 404)

	def test_cross_tenant_measurement_actions_are_404(self):
		entry = self._catalog("ENV-I2")
		indicator = self._track(entry)
		row = self._measure(indicator, 2025, Decimal("1"), status=IndicatorStatus.REPORTED)
		self.client.force_login(self.outsider)
		self.assertEqual(
			self.client.post(reverse("esg:value_report", args=[row.pk])).status_code, 404
		)
		self.assertEqual(
			self.client.post(reverse("esg:value_verify", args=[row.pk])).status_code, 404
		)

	def test_cross_tenant_assessment_detail_is_404(self):
		entry = self._catalog("ENV-I3")
		self._track(entry)
		snapshot = assessment_service.run_assessment(
			self.company, actor=self.owner, period_year=2025
		)
		self.client.force_login(self.outsider)
		self.assertEqual(
			self.client.get(reverse("esg:assessment_detail", args=[snapshot.pk])).status_code,
			404,
		)

	def test_indicator_list_only_shows_own_company(self):
		mine = self._catalog("ENV-I4")
		theirs = self._catalog("ENV-I5", company=self.other_company)
		self._track(mine)
		workflow.create_indicator(
			company=self.other_company, catalog_entry=theirs, actor=self.other_owner
		)
		self.assertEqual(visible_indicators(self.owner, self.company).count(), 1)
		self.assertEqual(
			visible_indicators(self.other_owner, self.other_company).count(), 1
		)

	def test_creation_ignores_a_client_supplied_company(self):
		entry = self._catalog("ENV-I6")
		self.client.force_login(self.owner)
		self.client.post(
			reverse("esg:indicator_create"),
			{
				"catalog_entry": entry.pk,
				"owner": "",
				"department": "",
				"notes": "",
				"company_id": self.other_company.pk,
			},
		)
		indicator = Indicator.objects.get(catalog_entry=entry)
		self.assertEqual(indicator.company, self.company)

	def test_user_without_a_company_is_redirected(self):
		orphan = User.objects.create_user(
			email="esg-orphan@carbonpilot.test",
			password=PASSWORD,
			role=User.Role.DEPARTMENT_MANAGER,
			status=User.Status.ACTIVE,
		)
		self.client.force_login(orphan)
		response = self.client.get(reverse("esg:dashboard"))
		self.assertEqual(response.status_code, 302)
		self.assertIn("section=company", response.url)


# --------------------------------------------------------------------------- #
# Role permissions
# --------------------------------------------------------------------------- #
class RolePermissionTests(ESGTestCase):
	def test_can_manage_rules(self):
		self.assertTrue(can_manage(self.owner, self.company))
		self.assertTrue(can_manage(self.esg_manager, self.company))
		self.assertTrue(can_manage(self.ops_manager, self.company))
		self.assertFalse(can_manage(self.auditor, self.company))

	def test_department_manager_is_scoped_to_own_department(self):
		entry = self._catalog("ENV-R1")
		ops_indicator = self._track(entry, department=self.operations)
		other_entry = self._catalog("ENV-R2")
		finance_indicator = self._track(other_entry, department=self.finance)
		self.assertTrue(can_manage(self.ops_manager, self.company, ops_indicator))
		self.assertFalse(can_manage(self.ops_manager, self.company, finance_indicator))

	def test_verification_roles(self):
		self.assertTrue(can_verify(self.owner))
		self.assertTrue(can_verify(self.auditor))
		self.assertFalse(can_verify(self.ops_manager))

	def test_admin_roles(self):
		self.assertTrue(is_company_admin(self.owner))
		self.assertTrue(is_company_admin(self.esg_manager))
		self.assertFalse(is_company_admin(self.ops_manager))

	def test_auditor_reads_but_cannot_create(self):
		entry = self._catalog("ENV-R3")
		self.client.force_login(self.auditor)
		self.assertEqual(self.client.get(reverse("esg:dashboard")).status_code, 200)
		self.client.post(
			reverse("esg:indicator_create"),
			{"catalog_entry": entry.pk, "owner": "", "department": "", "notes": ""},
		)
		self.assertEqual(Indicator.objects.count(), 0)

	def test_department_manager_cannot_run_assessment_or_coach(self):
		self.client.force_login(self.ops_manager)
		self.assertEqual(
			self.client.post(reverse("esg:assessment_run"), {"year": 2025}).status_code, 403
		)
		self.assertEqual(self.client.post(reverse("esg:coach_run")).status_code, 403)
		self.assertEqual(AssessmentSnapshot.objects.count(), 0)

	def test_department_manager_cannot_edit_another_departments_indicator(self):
		entry = self._catalog("ENV-R4")
		indicator = self._track(entry, department=self.finance)
		self.client.force_login(self.ops_manager)
		response = self.client.get(reverse("esg:indicator_edit", args=[indicator.pk]))
		self.assertEqual(response.status_code, 403)

	def test_esg_manager_can_run_assessment(self):
		self._catalog("ENV-R5")
		self.client.force_login(self.esg_manager)
		response = self.client.post(reverse("esg:assessment_run"), {"year": 2025})
		self.assertEqual(response.status_code, 302)
		self.assertEqual(AssessmentSnapshot.objects.count(), 1)


# --------------------------------------------------------------------------- #
# Qwen coach
# --------------------------------------------------------------------------- #
class CoachValidationTests(ESGTestCase):
	def test_payload_validation_accepts_a_clean_response(self):
		cleaned = coach_ai.validate_payload(json.loads(coach_json()))
		self.assertIsNotNone(cleaned)
		self.assertEqual(len(cleaned["priority_gaps"]), 1)
		self.assertEqual(cleaned["priority_gaps"][0]["pillar_label"], "Governance")
		self.assertEqual(cleaned["management_next_steps"], [
			"Assign an owner to every governance indicator",
			"Report the outstanding period values",
		])

	def test_payload_validation_rejects_malformed_shapes(self):
		self.assertIsNone(coach_ai.validate_payload(None))
		self.assertIsNone(coach_ai.validate_payload([1, 2, 3]))
		self.assertIsNone(coach_ai.validate_payload({"priority_gaps": []}))  # no summary

	def test_unknown_pillar_or_priority_is_rejected(self):
		cleaned = coach_ai.validate_payload(
			json.loads(
				coach_json(
					priority_gaps=[
						{"pillar": "PLANETARY", "issue": "Unknown pillar here", "priority": "HIGH",
						 "explanation": "This should be discarded entirely."},
						{"pillar": "SOCIAL", "issue": "Safety metric missing", "priority": "URGENT",
						 "explanation": "This priority is not allowed either."},
					]
				)
			)
		)
		self.assertEqual(cleaned["priority_gaps"], [])
		self.assertGreaterEqual(cleaned["rejected"], 2)

	def test_duplicate_gaps_and_recommendations_are_deduplicated(self):
		cleaned = coach_ai.validate_payload(
			json.loads(
				coach_json(
					priority_gaps=[
						{"pillar": "SOCIAL", "issue": "Safety metric missing", "priority": "HIGH",
						 "explanation": "The safety indicator has no reported value."},
						{"pillar": "SOCIAL", "issue": "safety metric missing", "priority": "HIGH",
						 "explanation": "Duplicate of the previous gap."},
					],
					recommended_indicators=[
						{"name": "Injury rate", "pillar": "SOCIAL", "justification": "Not tracked yet."},
						{"name": "injury rate", "pillar": "SOCIAL", "justification": "Duplicate."},
					],
				)
			)
		)
		self.assertEqual(len(cleaned["priority_gaps"]), 1)
		self.assertEqual(len(cleaned["recommended_indicators"]), 1)

	def test_embedded_json_in_prose_is_still_parsed(self):
		text = "Here is the answer:\n" + coach_json() + "\nThanks!"
		cleaned = coach_ai.validate_payload(coach_ai._loads(text))
		self.assertIsNotNone(cleaned)


class CoachRunTests(ESGTestCase):
	def _seed_company(self):
		entry = self._catalog("ENV-C1")
		indicator = self._track(entry)
		self._measure(indicator, 2025, Decimal("1"), status=IndicatorStatus.REPORTED)
		return indicator

	def test_successful_run_returns_validated_suggestions(self):
		self._seed_company()
		with patch(GENERATE, return_value=OllamaResult(ok=True, text=coach_json(), http_status=200)):
			analysis = coach_ai.run_coach(self.company, period_year=2025)
		self.assertTrue(analysis.ai_available)
		self.assertEqual(analysis.company_id, self.company.pk)
		self.assertEqual(len(analysis.priority_gaps), 1)
		self.assertEqual(len(analysis.recommended_indicators), 1)
		self.assertIn("AI · Qwen", analysis.message)

	def test_ollama_failure_degrades_gracefully(self):
		self._seed_company()
		down = OllamaResult(ok=False, error="ConnectionRefusedError", error_category="network_or_request_error")
		with patch(GENERATE, return_value=down):
			analysis = coach_ai.run_coach(self.company, period_year=2025)
		self.assertFalse(analysis.ai_available)
		self.assertEqual(analysis.message, coach_ai.UNAVAILABLE_MESSAGE)
		self.assertEqual(analysis.priority_gaps, [])

	def test_malformed_response_degrades_gracefully(self):
		self._seed_company()
		with patch(GENERATE, return_value=OllamaResult(ok=True, text="definitely not json", http_status=200)):
			analysis = coach_ai.run_coach(self.company, period_year=2025)
		self.assertFalse(analysis.ai_available)
		self.assertEqual(analysis.message, coach_ai.INVALID_RESPONSE_MESSAGE)

	def test_empty_catalog_short_circuits_without_calling_the_model(self):
		with patch(GENERATE) as generate:
			analysis = coach_ai.run_coach(self.company, period_year=2025)
		generate.assert_not_called()
		self.assertTrue(analysis.ai_available)
		self.assertIn("catalog", analysis.message)

	def test_dashboard_still_works_when_the_model_is_down(self):
		self._seed_company()
		self.client.force_login(self.owner)
		down = OllamaResult(ok=False, error="timeout", error_category="timeout")
		with patch(GENERATE, return_value=down):
			self.client.post(reverse("esg:coach_run"), {"year": 2025})
		response = self.client.get(reverse("esg:dashboard"))
		self.assertEqual(response.status_code, 200)
		self.assertContains(response, "Completeness score")

	def test_confirming_a_recommendation_creates_a_draft_indicator(self):
		self._seed_company()
		self.client.force_login(self.owner)
		response = self.client.post(
			reverse("esg:coach_confirm_recommendation"),
			{
				"name": "Governance controls tested",
				"pillar": ESGPillar.GOVERNANCE,
				"unit": "count",
				"justification": "A required governance metric was missing.",
			},
		)
		self.assertEqual(response.status_code, 302)
		indicator = Indicator.objects.filter(
			company=self.company, catalog_entry__name="Governance controls tested"
		).first()
		self.assertIsNotNone(indicator)
		self.assertEqual(indicator.catalog_entry.pillar, ESGPillar.GOVERNANCE)
		self.assertEqual(indicator.catalog_entry.unit, "count")

	def test_confirming_without_a_unit_is_refused(self):
		self._seed_company()
		self.client.force_login(self.owner)
		self.client.post(
			reverse("esg:coach_confirm_recommendation"),
			{"name": "Governance controls tested", "pillar": ESGPillar.GOVERNANCE, "unit": ""},
		)
		self.assertFalse(
			IndicatorCatalogEntry.objects.filter(
				company=self.company, name="Governance controls tested"
			).exists()
		)

	def test_coach_page_shows_the_ai_label_after_a_run(self):
		self._seed_company()
		self.client.force_login(self.owner)
		with patch(GENERATE, return_value=OllamaResult(ok=True, text=coach_json(), http_status=200)):
			self.client.post(reverse("esg:coach_run"), {"year": 2025})
		response = self.client.get(reverse("esg:coach"))
		self.assertEqual(response.status_code, 200)
		self.assertContains(response, "AI · QWEN")


# --------------------------------------------------------------------------- #
# Source-module signals
# --------------------------------------------------------------------------- #
class SourceSignalTests(ESGTestCase):
	def test_signals_report_unavailable_without_faking_a_zero(self):
		cards = signals_service.company_signals(self.company)
		self.assertEqual(
			[c.key for c in cards],
			["emissions", "objectives", "risk", "policies", "findings"],
		)
		emissions = cards[0]
		self.assertFalse(emissions.available)
		self.assertEqual(emissions.value, "—")
		self.assertTrue(emissions.source)
		self.assertTrue(emissions.detail)

	def test_signals_are_tenant_scoped(self):
		self.assertEqual(signals_service.company_signals(None), [])


# --------------------------------------------------------------------------- #
# Regression on the existing architecture
# --------------------------------------------------------------------------- #
class RegressionTests(ESGTestCase):
	def test_esg_views_render_for_a_company_owner(self):
		self.client.force_login(self.owner)
		for name in (
			"esg:dashboard",
			"esg:indicator_list",
			"esg:indicator_create",
			"esg:coach",
		):
			with self.subTest(view=name):
				response = self.client.get(reverse(name))
				self.assertEqual(response.status_code, 200)

	def test_catalog_route_is_not_shadowed_by_the_legacy_accounts_view(self):
		"""The legacy ``accounts`` app registers ``esg/indicators/`` first.

		The ESG catalog therefore lives at ``esg/catalog/``; this guards
		against a future re-introduction of a colliding path.
		"""
		match = resolve(reverse("esg:indicator_list"))
		self.assertEqual(match.func.__module__, "apps.esg.views")
		self.assertEqual(match.url_name, "indicator_list")

	def test_esg_is_wired_into_the_carbon_sidebar(self):
		from apps.carbon.views import _sidebar

		nav = _sidebar("ESG intelligence", self.owner)
		self.assertTrue(
			any(item.get("url_name") == "esg:dashboard" for item in nav),
			"the modular sidebar must expose the ESG hub",
		)

	def test_landing_sidebar_exposes_esg_for_authorised_roles(self):
		from apps.accounts.views import ROLE_SIDEBAR

		for role in (
			User.Role.COMPANY_OWNER,
			User.Role.DEPARTMENT_MANAGER,
			User.Role.ESG_REPORTING_MANAGER,
			User.Role.AUDITOR,
		):
			with self.subTest(role=role):
				urls = {item.get("url_name") for item in ROLE_SIDEBAR[role]}
				self.assertIn("esg:dashboard", urls)

	def test_existing_module_urls_still_resolve(self):
		for name in (
			"carbon:footprint",
			"objectives:objective_list",
			"risk:risk_assessment",
			"governance:policy_list",
			"audits:audit_list",
			"reports:report_list",
			"suppliers:supplier_list",
		):
			with self.subTest(url=name):
				self.assertTrue(reverse(name))


# --------------------------------------------------------------------------- #
# Indicator catalog: filtering, pagination and empty states
# --------------------------------------------------------------------------- #
class IndicatorListFilterPaginationTests(ESGTestCase):
	"""Shared filtering, search, pagination and empty states on the catalog."""

	def _track_many(self, count, pillar=ESGPillar.ENVIRONMENTAL):
		indicators = []
		for index in range(count):
			entry = self._catalog(f"QAT-ENV-{index:02d}", pillar)
			indicators.append(self._track(entry))
		return indicators

	def test_filter_by_pillar_narrows_the_result_set(self):
		env = self._catalog("QAT-ENV-1", ESGPillar.ENVIRONMENTAL)
		soc = self._catalog("QAT-SOC-1", ESGPillar.SOCIAL)
		self._track(env)
		self._track(soc)
		self.client.force_login(self.owner)
		response = self.client.get(
			reverse("esg:indicator_list"), {"pillar": ESGPillar.ENVIRONMENTAL}
		)
		self.assertEqual(response.status_code, 200)
		pagination = response.context["pagination"]
		self.assertEqual(pagination["total"], 1)
		self.assertEqual(len(response.context["rows"]), 1)
		self.assertEqual(
			response.context["rows"][0]["indicator"].catalog_entry.code, "QAT-ENV-1"
		)

	def test_default_page_size_is_ten(self):
		self._track_many(12)
		self.client.force_login(self.owner)
		response = self.client.get(reverse("esg:indicator_list"))
		pagination = response.context["pagination"]
		self.assertEqual(pagination["per_page"], 10)
		self.assertEqual(len(pagination["items"]), 10)
		self.assertEqual(len(response.context["rows"]), 10)
		self.assertEqual(pagination["pages"], 2)
		self.assertEqual(response.context["total_count"], 12)

	def test_per_page_twenty_shows_more_rows(self):
		self._track_many(15)
		self.client.force_login(self.owner)
		response = self.client.get(reverse("esg:indicator_list"), {"per_page": "20"})
		pagination = response.context["pagination"]
		self.assertEqual(pagination["per_page"], 20)
		self.assertEqual(len(pagination["items"]), 15)

	def test_next_link_preserves_active_filters(self):
		self._track_many(12)
		self.client.force_login(self.owner)
		response = self.client.get(
			reverse("esg:indicator_list"), {"pillar": ESGPillar.ENVIRONMENTAL}
		)
		pagination = response.context["pagination"]
		self.assertTrue(pagination["has_next"])
		self.assertEqual(pagination["next_page"], 2)
		href = _next_link(response.content.decode())
		self.assertIn("pillar=ENVIRONMENTAL", href)
		self.assertIn("page=2", href)

	def test_filter_with_no_match_renders_empty_state(self):
		self._track_many(2)
		self.client.force_login(self.owner)
		response = self.client.get(
			reverse("esg:indicator_list"), {"q": "no-such-indicator"}
		)
		self.assertEqual(response.status_code, 200)
		self.assertContains(response, "cp-empty")
		self.assertContains(response, "No indicator matches these filters.")

	def test_cross_tenant_indicators_never_appear(self):
		mine = self._catalog("QAT-MINE-1")
		theirs = self._catalog("QAT-THEIRS-1", company=self.other_company)
		self._track(mine)
		workflow.create_indicator(
			company=self.other_company, catalog_entry=theirs, actor=self.other_owner
		)
		self.client.force_login(self.owner)
		response = self.client.get(reverse("esg:indicator_list"))
		codes = [row["indicator"].catalog_entry.code for row in response.context["rows"]]
		self.assertIn("QAT-MINE-1", codes)
		self.assertNotIn("QAT-THEIRS-1", codes)
		self.assertNotContains(response, "QAT-THEIRS-1")


class IndicatorFormValidationTests(ESGTestCase):
	"""Server-side validation of the "track a catalog indicator" form."""

	def _form(self, **overrides):
		payload = {
			"catalog_entry": "",
			"owner": "",
			"department": "",
			"notes": "",
		}
		payload.update(overrides)
		return IndicatorForm(
			data=payload,
			company=self.company,
			instance=Indicator(company=self.company),
		)

	def test_valid_indicator_is_accepted(self):
		entry = self._catalog("FORM-OK")
		form = self._form(catalog_entry=str(entry.pk))
		self.assertTrue(form.is_valid(), form.errors)

	def test_catalog_entry_is_required(self):
		form = self._form()
		self.assertFalse(form.is_valid())
		self.assertIn(
			"Select the catalog indicator this row tracks.",
			form.errors["catalog_entry"],
		)

	def test_duplicate_catalog_entry_is_rejected(self):
		entry = self._catalog("FORM-DUP")
		self._track(entry)
		form = self._form(catalog_entry=str(entry.pk))
		self.assertFalse(form.is_valid())
		self.assertIn(
			"Your company already tracks this indicator — open it instead of creating a duplicate.",
			form.errors["catalog_entry"],
		)

	def test_foreign_department_is_rejected(self):
		entry = self._catalog("FORM-DEPT")
		foreign = Department.objects.create(company=self.other_company, name="Foreign")
		form = self._form(catalog_entry=str(entry.pk), department=str(foreign.pk))
		self.assertFalse(form.is_valid())
		self.assertIn("department", form.errors)

	def test_foreign_owner_is_rejected(self):
		entry = self._catalog("FORM-OWNER")
		form = self._form(catalog_entry=str(entry.pk), owner=str(self.outsider.pk))
		self.assertFalse(form.is_valid())
		self.assertIn("owner", form.errors)

	def test_own_department_and_owner_are_accepted(self):
		entry = self._catalog("FORM-OWN")
		form = self._form(
			catalog_entry=str(entry.pk),
			department=str(self.operations.pk),
			owner=str(self.ops_manager.pk),
		)
		self.assertTrue(form.is_valid(), form.errors)


class IndicatorValueFormValidationTests(ESGTestCase):
	"""Server-side validation of a reporting-period measurement."""

	def _indicator(self, code="VAL-OK"):
		return self._track(self._catalog(code))

	def _form(self, indicator, **overrides):
		payload = {
			"period_year": str(timezone.now().year),
			"value": "120",
			"unit": "tCO2e",
			"evidence_reference": "",
			"note": "",
			"status": IndicatorStatus.DRAFT,
		}
		payload.update(overrides)
		return IndicatorValueForm(
			data=payload,
			indicator=indicator,
			instance=IndicatorValue(company=self.company, indicator=indicator),
		)

	def _min_year(self):
		return timezone.now().year + REPORTING_YEAR_MIN_OFFSET

	def _max_year(self):
		return timezone.now().year + REPORTING_YEAR_MAX_OFFSET

	def test_valid_measurement_is_accepted(self):
		form = self._form(self._indicator())
		self.assertTrue(form.is_valid(), form.errors)

	def test_reporting_year_is_required(self):
		form = self._form(self._indicator(), period_year="")
		self.assertFalse(form.is_valid())
		self.assertIn("Enter the reporting year.", form.errors["period_year"])

	def test_reporting_year_below_range_is_rejected(self):
		form = self._form(self._indicator(), period_year=str(self._min_year() - 1))
		self.assertFalse(form.is_valid())
		self.assertIn(
			f"Reporting year must be between {self._min_year()} and {self._max_year()}.",
			form.errors["period_year"],
		)

	def test_reporting_year_above_range_is_rejected(self):
		form = self._form(self._indicator(), period_year=str(self._max_year() + 1))
		self.assertFalse(form.is_valid())
		self.assertIn("period_year", form.errors)

	def test_blank_value_records_a_missing_measurement(self):
		form = self._form(
			self._indicator(), value="", status=IndicatorStatus.DRAFT
		)
		self.assertTrue(form.is_valid(), form.errors)
		self.assertIsNone(form.cleaned_data["value"])

	def test_zero_is_a_valid_measurement(self):
		form = self._form(self._indicator(), value="0")
		self.assertTrue(form.is_valid(), form.errors)
		self.assertEqual(form.cleaned_data["value"], Decimal("0"))

	def test_reported_status_requires_a_value(self):
		form = self._form(
			self._indicator(), value="", status=IndicatorStatus.REPORTED
		)
		self.assertFalse(form.is_valid())
		self.assertIn(
			"A reported measurement needs a value. Save it as a draft if the data is missing.",
			form.errors["value"],
		)

	def test_non_numeric_value_is_rejected(self):
		form = self._form(self._indicator(), value="lots")
		self.assertFalse(form.is_valid())
		self.assertIn(
			"Enter a valid number, or leave it blank.", form.errors["value"]
		)
