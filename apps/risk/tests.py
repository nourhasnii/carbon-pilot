"""Tests for the ESG / carbon risk assessment module.

Covers the deterministic score, the LOW/MEDIUM/HIGH levels, each component,
tenant isolation (no cross-tenant leakage), the insufficient-data case, the
assessment history and the HTTP views/permissions.
"""

from datetime import date
from decimal import Decimal

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
	VerificationStatus,
)
from apps.enterprise.models import Company, Department
from apps.objectives.models import (
	ExpectedOutcome,
	Objective,
	ObjectiveStatus,
	PredictionRun,
	RiskLevel as ObjectiveRiskLevel,
)

from .models import RiskAssessment, RiskLevel, RiskMethod
from .services import risk_assessment as svc


class RiskTestCase(TestCase):
	password = "SecureTestPassword2026!"

	def setUp(self):
		self.owner = User.objects.create_user(
			email="risk-owner@carbonpilot.test",
			password=self.password,
			first_name="Risk",
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
			email="other-risk-owner@carbonpilot.test",
			password=self.password,
			role=User.Role.COMPANY_OWNER,
			status=User.Status.ACTIVE,
		)
		self.other_company = Company.objects.create(
			owner=self.other_owner, name="Other Works"
		)

	def make_activity(self, *, company=None, status=ActivityStatus.APPROVED, **overrides):
		company = company or self.company
		payload = {
			"company": company,
			"department": self.department if company == self.company else None,
			"name": "Electricity meter — January",
			"category": ActivityCategory.ELECTRICITY,
			"scope": Scope.SCOPE_2,
			"quantity": Decimal("5000"),
			"unit": "kWh",
			"period_start": date(2026, 1, 1),
			"period_end": date(2026, 1, 31),
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

	def make_anomaly(self, *, company=None, severity=AnomalySeverity.HIGH,
					 status=AnomalyStatus.NEW, year=2026, tag="a"):
		company = company or self.company
		return AnomalyLog.objects.create(
			company=company,
			anomaly_type=AnomalyType.SPIKE,
			score=Decimal("0.90000"),
			severity=severity,
			status=status,
			period_start=date(year, 1, 1),
			fingerprint=f"{company.pk}:{tag}:{severity}:{status}:{year}",
		)

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

	def make_prediction(self, objective, *, probability, level):
		return PredictionRun.objects.create(
			objective=objective,
			probability_achievement=Decimal(str(probability)),
			risk_level=level,
			expected_outcome=(
				ExpectedOutcome.LIKELY_TO_ACHIEVE
				if level == ObjectiveRiskLevel.LOW
				else ExpectedOutcome.AT_RISK
			),
			current_progress_percent=Decimal("30.00"),
		)


class LevelThresholdTests(TestCase):
	def test_weights_total_one_hundred(self):
		self.assertEqual(svc.TOTAL_WEIGHT, 100)

	def test_level_boundaries(self):
		self.assertEqual(svc.level_for(Decimal("0")), RiskLevel.LOW)
		self.assertEqual(svc.level_for(Decimal("34.99")), RiskLevel.LOW)
		self.assertEqual(svc.level_for(Decimal("35")), RiskLevel.MEDIUM)
		self.assertEqual(svc.level_for(Decimal("64.99")), RiskLevel.MEDIUM)
		self.assertEqual(svc.level_for(Decimal("65")), RiskLevel.HIGH)
		self.assertEqual(svc.level_for(Decimal("100")), RiskLevel.HIGH)


class ComponentTests(RiskTestCase):
	def test_anomaly_component_counts_open_high_and_medium_only(self):
		self.make_anomaly(severity=AnomalySeverity.HIGH, tag="1")
		self.make_anomaly(severity=AnomalySeverity.MEDIUM, tag="2")
		# Reviewed / dismissed anomalies are not "open" and must not count.
		self.make_anomaly(severity=AnomalySeverity.HIGH, status=AnomalyStatus.REVIEWED, tag="3")
		self.make_anomaly(severity=AnomalySeverity.MEDIUM, status=AnomalyStatus.DISMISSED, tag="4")
		driver = svc._anomaly_component(self.company)
		self.assertEqual(driver.points, Decimal("12"))  # 8 + 4
		self.assertIn("1 open high-severity", driver.detail)

	def test_anomaly_component_is_capped(self):
		for index in range(5):
			self.make_anomaly(severity=AnomalySeverity.HIGH, tag=f"h{index}")
		driver = svc._anomaly_component(self.company)
		self.assertEqual(driver.points, Decimal(str(svc.ANOMALY_WEIGHT)))

	def test_evidence_component_scores_missing_evidence(self):
		self.make_activity()
		self.make_activity(evidence="")
		driver = svc._evidence_component(self.company)
		self.assertEqual(driver.points, Decimal("5.00"))  # 1 of 2 missing → 5/10
		self.assertIn("1 of 2", driver.detail)

	def test_evidence_component_zero_without_activities(self):
		driver = svc._evidence_component(self.company)
		self.assertEqual(driver.points, Decimal("0"))

	def test_backlog_component_counts_pending_review(self):
		self.make_activity(status=ActivityStatus.SUBMITTED)
		self.make_activity(status=ActivityStatus.UNDER_REVIEW)
		# Approved activities are not a backlog.
		self.make_activity(status=ActivityStatus.APPROVED)
		driver = svc._backlog_component(self.company)
		self.assertEqual(driver.points, Decimal("6"))  # 2 × 3

	def test_backlog_component_is_capped(self):
		for _index in range(6):
			self.make_activity(status=ActivityStatus.SUBMITTED)
		driver = svc._backlog_component(self.company)
		self.assertEqual(driver.points, Decimal(str(svc.BACKLOG_WEIGHT)))

	def test_data_quality_component_rewards_good_data(self):
		# Complete, verified crecord → no data-quality risk.
		self.make_activity()
		driver = svc._data_quality_component(self.company)
		self.assertEqual(driver.points, Decimal("0.00"))

	def test_data_quality_component_penalises_poor_data(self):
		# No department, no source, no evidence and an unverified factor.
		unverified = EmissionFactor.objects.create(
			name="Unverified factor",
			category=ActivityCategory.ELECTRICITY,
			scope=Scope.SCOPE_2,
			value=Decimal("0.5"),
			unit="kWh",
			region="Tunisia",
			year=2026,
			source="",
			verification_status=VerificationStatus.UNVERIFIED,
		)
		self.make_activity(
			department=None,
			data_source="",
			evidence="",
			emission_factor=unverified,
		)
		driver = svc._data_quality_component(self.company)
		self.assertGreater(driver.points, Decimal("0"))
		self.assertLessEqual(driver.points, Decimal(str(svc.DATA_QUALITY_WEIGHT)))

	def test_objective_component_counts_missed_and_at_risk(self):
		missed = self.make_objective(status=ObjectiveStatus.MISSED, tag="m1")
		at_risk = self.make_objective(tag="a1")
		self.make_prediction(at_risk, probability=Decimal("10"), level=ObjectiveRiskLevel.HIGH)
		self.make_objective(tag="fine")
		driver = svc._objective_component(self.company)
		self.assertEqual(driver.points, Decimal("20"))  # 12 (missed) + 8 (high risk)

	def test_objective_component_is_capped(self):
		for index in range(4):
			self.make_objective(status=ObjectiveStatus.MISSED, tag=f"m{index}")
		driver = svc._objective_component(self.company)
		self.assertEqual(driver.points, Decimal(str(svc.OBJECTIVE_WEIGHT)))


class ScoreTests(RiskTestCase):
	def test_clean_company_scores_zero_and_low(self):
		self.make_activity()
		result = svc.assess_company(self.company)
		self.assertEqual(result.score, Decimal("0.00"))
		self.assertEqual(result.level, RiskLevel.LOW)
		self.assertEqual(result.method, RiskMethod.RULE_BASED)
		self.assertFalse(result.insufficient_data)
		self.assertEqual(result.drivers, [])

	def test_score_calculation_sums_components(self):
		self.make_activity()  # keeps data quality & evidence at zero
		for index in range(4):
			self.make_anomaly(severity=AnomalySeverity.HIGH, tag=f"h{index}")  # 30
		for _index in range(5):
			self.make_activity(status=ActivityStatus.SUBMITTED)  # 15
		for index in range(3):
			self.make_objective(status=ObjectiveStatus.MISSED, tag=f"m{index}")  # 30
		result = svc.assess_company(self.company)
		# 30 + 30 + 0 + 0 + 15 = 75
		self.assertEqual(result.score, Decimal("75.00"))
		self.assertEqual(result.level, RiskLevel.HIGH)

	def test_medium_level_score(self):
		self.make_activity()
		self.make_anomaly(severity=AnomalySeverity.HIGH, tag="h1")
		self.make_anomaly(severity=AnomalySeverity.HIGH, tag="h2")
		self.make_anomaly(severity=AnomalySeverity.HIGH, tag="h3")  # 24
		for _index in range(5):
			self.make_activity(status=ActivityStatus.SUBMITTED)  # 15
		result = svc.assess_company(self.company)
		self.assertEqual(result.score, Decimal("39.00"))
		self.assertEqual(result.level, RiskLevel.MEDIUM)

	def test_drivers_are_ranked_and_explain_why(self):
		self.make_activity()
		self.make_anomaly(severity=AnomalySeverity.HIGH, tag="h1")
		self.make_objective(status=ObjectiveStatus.MISSED, tag="m1")
		result = svc.assess_company(self.company)
		keys = [driver.key for driver in result.drivers]
		self.assertEqual(keys, ["objectives", "anomalies"])  # 12 before 8
		for driver in result.drivers:
			self.assertTrue(driver.detail)

	def test_period_year_filters_the_data(self):
		self.make_activity(period_start=date(2025, 1, 1), period_end=date(2025, 1, 31))
		self.make_anomaly(severity=AnomalySeverity.HIGH, year=2026, tag="2026")
		self.make_anomaly(severity=AnomalySeverity.HIGH, year=2025, tag="2025")
		result = svc.assess_company(self.company, period_year=2026)
		self.assertEqual(result.score, Decimal("8.00"))
		self.assertEqual(result.period_year, 2026)


class InsufficientDataTests(RiskTestCase):
	def test_no_approved_activities_flags_insufficient_data(self):
		result = svc.assess_company(self.company)
		self.assertTrue(result.insufficient_data)
		self.assertEqual(result.score, Decimal("0.00"))
		self.assertEqual(result.level, RiskLevel.LOW)
		self.assertIn("No approved activity data", result.summary)

	def test_no_approved_activities_but_open_risk_still_scores(self):
		self.make_anomaly(severity=AnomalySeverity.HIGH, tag="h1")
		result = svc.assess_company(self.company)
		self.assertTrue(result.insufficient_data)
		self.assertEqual(result.score, Decimal("8.00"))


class TenantIsolationTests(RiskTestCase):
	def test_only_current_company_data_is_used(self):
		self.make_activity()  # company A is clean
		# Load company B with risk that must never leak into A's score.
		self.make_anomaly(company=self.other_company, severity=AnomalySeverity.HIGH, tag="b1")
		self.make_objective(company=self.other_company, status=ObjectiveStatus.MISSED, tag="b2")
		for index in range(3):
			AnomalyLog.objects.create(
				company=self.other_company,
				anomaly_type=AnomalyType.SPIKE,
				score=Decimal("0.90000"),
				severity=AnomalySeverity.HIGH,
				period_start=date(2026, 1, 1),
				fingerprint=f"other:{index}",
			)
		result = svc.assess_company(self.company)
		self.assertEqual(result.score, Decimal("0.00"))
		self.assertEqual(result.level, RiskLevel.LOW)

	def test_latest_assessment_is_company_scoped(self):
		svc.run_assessment(self.other_company, actor=self.other_owner)
		self.assertIsNone(svc.latest_assessment(self.company))


class AssessmentHistoryTests(RiskTestCase):
	def test_run_assessment_persists_and_returns_row(self):
		self.make_activity()
		self.make_anomaly(severity=AnomalySeverity.HIGH, tag="h1")
		assessment = svc.run_assessment(self.company, actor=self.owner)
		self.assertEqual(assessment.company, self.company)
		self.assertEqual(assessment.score, Decimal("8.00"))
		self.assertEqual(assessment.level, RiskLevel.LOW)
		self.assertEqual(assessment.created_by, self.owner)
		self.assertEqual(assessment.method, RiskMethod.RULE_BASED)
		self.assertEqual(assessment.drivers_json, [d.as_dict() for d in svc.assess_company(self.company).drivers])

	def test_breakdown_always_has_five_components(self):
		self.make_activity()
		assessment = svc.run_assessment(self.company, actor=self.owner)
		breakdown = assessment.breakdown
		self.assertEqual(len(breakdown), 5)
		self.assertEqual(sum(item["points"] for item in breakdown), float(assessment.score))

	def test_latest_assessment_returns_newest(self):
		first = svc.run_assessment(self.company, actor=self.owner)
		second = svc.run_assessment(self.company, actor=self.owner)
		self.assertEqual(svc.latest_assessment(self.company), second)
		self.assertNotEqual(first.pk, second.pk)


class RiskViewTests(RiskTestCase):
	def test_view_requires_login(self):
		response = self.client.get(reverse("risk:risk_assessment"))
		self.assertEqual(response.status_code, 302)
		self.assertIn("/login/", response.url)

	def test_owner_sees_empty_state_then_current_score(self):
		self.client.force_login(self.owner)
		response = self.client.get(reverse("risk:risk_assessment"))
		self.assertEqual(response.status_code, 200)
		self.assertContains(response, "No risk assessment has been run")

		self.make_activity()
		self.make_anomaly(severity=AnomalySeverity.HIGH, tag="h1")
		self.client.post(reverse("risk:risk_assessment_run"))
		response = self.client.get(reverse("risk:risk_assessment"))
		self.assertEqual(response.status_code, 200)
		self.assertContains(response, "Ranked risk drivers")
		self.assertEqual(RiskAssessment.objects.filter(company=self.company).count(), 1)

	def test_run_ignores_client_supplied_company(self):
		self.client.force_login(self.owner)
		self.client.post(
			reverse("risk:risk_assessment_run"),
			{"company_id": self.other_company.pk, "period_year": ""},
		)
		assessment = RiskAssessment.objects.get()
		self.assertEqual(assessment.company, self.company)
		self.assertEqual(RiskAssessment.objects.filter(company=self.other_company).count(), 0)

	def test_auditor_can_view_but_not_run(self):
		auditor = User.objects.create_user(
			email="auditor@carbonpilot.test",
			password=self.password,
			role=User.Role.AUDITOR,
			status=User.Status.ACTIVE,
			company_membership=self.company,
		)
		self.client.force_login(auditor)
		self.assertEqual(self.client.get(reverse("risk:risk_assessment")).status_code, 200)
		response = self.client.post(reverse("risk:risk_assessment_run"))
		self.assertEqual(response.status_code, 403)
		self.assertEqual(RiskAssessment.objects.count(), 0)

	def test_run_records_history_for_each_run(self):
		self.client.force_login(self.owner)
		self.make_activity()
		self.client.post(reverse("risk:risk_assessment_run"))
		self.client.post(reverse("risk:risk_assessment_run"))
		self.assertEqual(RiskAssessment.objects.filter(company=self.company).count(), 2)

	def test_carbon_dashboard_shows_risk_widget(self):
		self.client.force_login(self.owner)
		self.make_activity()
		svc.run_assessment(self.company, actor=self.owner)
		response = self.client.get(reverse("carbon:dashboard"))
		self.assertEqual(response.status_code, 200)
		self.assertContains(response, "Carbon risk assessment")
