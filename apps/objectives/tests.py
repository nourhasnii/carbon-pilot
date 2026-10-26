import re
from datetime import date
from decimal import Decimal
from unittest import mock

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.analytics.models import ModelKind, ModelMetrics
from apps.carbon.models import ActivityCategory, ActivityStatus, CarbonActivity, EmissionFactor, Scope
from apps.enterprise.models import Company, Department

from .forms import YEAR_MAX_OFFSET, YEAR_MIN_OFFSET, ObjectiveForm
from .models import (
	ExpectedOutcome,
	Objective,
	ObjectiveStatus,
	PeriodType,
	PredictionRun,
	Quarter,
	RiskLevel,
)
from .services import can_manage, visible_objectives
from .services import objective_prediction

# Sentinel: "use the default department" so callers can pass ``department=None``
# to create a genuinely department-less activity.
_DEFAULT_DEPARTMENT = object()


class ObjectiveTestCase(TestCase):
	password = "SecureTestPassword2026!"

	def setUp(self):
		self.owner = User.objects.create_user(
			email="obj-owner@carbonpilot.test",
			password=self.password,
			role=User.Role.COMPANY_OWNER,
			status=User.Status.ACTIVE,
		)
		self.company = Company.objects.create(owner=self.owner, name="Objectives Works")
		self.operations = Department.objects.create(company=self.company, name="Operations")
		self.finance = Department.objects.create(company=self.company, name="Finance")

		self.other_owner = User.objects.create_user(
			email="obj-other-owner@carbonpilot.test",
			password=self.password,
			role=User.Role.COMPANY_OWNER,
			status=User.Status.ACTIVE,
		)
		self.other_company = Company.objects.create(owner=self.other_owner, name="Other Objectives")

		self.ops_manager = self._member(
			role=User.Role.DEPARTMENT_MANAGER,
			email="obj-ops-manager@carbonpilot.test",
			department="Operations",
		)
		self.auditor = self._member(
			role=User.Role.AUDITOR,
			email="obj-auditor@carbonpilot.test",
			department="",
		)
		# Value 1000 kg CO2e per unit: quantity N → N tonnes of CO2e.
		self.factor = EmissionFactor.objects.create(
			name="Objective test factor",
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

	def _objective(self, **overrides):
		payload = {
			"company": self.company,
			"name": "Cut emissions 20%",
			"year": 2026,
			"period_type": PeriodType.ANNUAL,
			"quarter": "",
			"baseline_tco2e": Decimal("100.00"),
			"target_tco2e": Decimal("80.00"),
			"created_by": self.owner,
		}
		payload.update(overrides)
		return Objective.objects.create(**payload)

	def _activity(self, tonnes, *, department=_DEFAULT_DEPARTMENT, company=None,
				  status=ActivityStatus.APPROVED, period_start=date(2026, 2, 1), is_archived=False):
		return CarbonActivity.objects.create(
			company=company or self.company,
			department=self.operations if department is _DEFAULT_DEPARTMENT else department,
			name=f"Activity {tonnes}t",
			category=ActivityCategory.ELECTRICITY,
			scope=Scope.SCOPE_2,
			quantity=Decimal(str(tonnes)),
			unit="unit",
			period_start=period_start,
			emission_factor=self.factor,
			status=status,
			is_archived=is_archived,
			created_by=self.owner,
		)


class ObjectiveModelTests(ObjectiveTestCase):
	def test_current_tco2e_uses_approved_activities_only(self):
		objective = self._objective()
		self._activity(10)
		self._activity(50, status=ActivityStatus.DRAFT)
		self._activity(30, status=ActivityStatus.SUBMITTED)
		self.assertEqual(objective.current_tco2e, Decimal("10.00"))

	def test_archived_activities_are_excluded(self):
		objective = self._objective()
		self._activity(10)
		self._activity(25, is_archived=True)
		self.assertEqual(objective.current_tco2e, Decimal("10.00"))

	def test_progress_is_share_of_the_committed_reduction(self):
		objective = self._objective(baseline_tco2e=Decimal("100"), target_tco2e=Decimal("80"))
		activity = self._activity(90)  # 100 → 90 = half of the 20 tCO2e reduction
		print("DEBUG co2e_kg", activity.co2e_kg, "status", activity.status, "archived", activity.is_archived)
		print("DEBUG current", objective.current_tco2e, "required", objective.reduction_required_tco2e)
		self.assertEqual(objective.progress_percent, 50.0)
		self.assertEqual(objective.gap_tco2e, Decimal("10.00"))

	def test_gap_to_target_is_never_negative(self):
		objective = self._objective(baseline_tco2e=Decimal("100"), target_tco2e=Decimal("80"))
		self._activity(95)
		self.assertEqual(objective.gap_tco2e, Decimal("15.00"))
		self.assertEqual(objective.progress_percent, 25.0)

	def test_progress_is_capped_at_one_hundred(self):
		objective = self._objective(baseline_tco2e=Decimal("100"), target_tco2e=Decimal("80"))
		self._activity(50)
		self.assertEqual(objective.progress_percent, 100.0)
		self.assertEqual(objective.gap_tco2e, Decimal("0.00"))

	def test_department_scope_only_counts_its_department(self):
		objective = self._objective(department=self.finance, baseline_tco2e=Decimal("100"), target_tco2e=Decimal("50"))
		self._activity(12, department=self.finance)
		self._activity(40, department=self.operations)
		self.assertEqual(objective.current_tco2e, Decimal("12.00"))

	def test_company_wide_scope_counts_every_department(self):
		objective = self._objective(department=None)
		self._activity(12, department=self.finance)
		self._activity(8, department=self.operations)
		self.assertEqual(objective.current_tco2e, Decimal("20.00"))

	def test_other_company_activities_are_never_counted(self):
		objective = self._objective()
		self._activity(10)
		self._activity(99, company=self.other_company, department=None)
		self.assertEqual(objective.current_tco2e, Decimal("10.00"))

	def test_quarterly_objective_only_counts_its_quarter(self):
		objective = self._objective(
			period_type=PeriodType.QUARTERLY, quarter=Quarter.Q1,
			baseline_tco2e=Decimal("100"), target_tco2e=Decimal("90"),
		)
		self._activity(6, period_start=date(2026, 3, 15))  # Q1
		self._activity(50, period_start=date(2026, 5, 10))  # Q2
		self.assertEqual(objective.current_tco2e, Decimal("6.00"))

	def test_period_bounds_for_quarterly_and_annual(self):
		annual = self._objective()
		self.assertEqual(annual.period_bounds(), (date(2026, 1, 1), date(2026, 12, 31)))
		quarterly = self._objective(period_type=PeriodType.QUARTERLY, quarter=Quarter.Q3)
		self.assertEqual(quarterly.period_bounds(), (date(2026, 7, 1), date(2026, 9, 30)))

	def test_refresh_status_marks_achieved_within_target(self):
		objective = self._objective(baseline_tco2e=Decimal("100"), target_tco2e=Decimal("80"))
		self._activity(70)
		self.assertEqual(objective.refresh_status(), ObjectiveStatus.ACHIEVED)
		objective.refresh_from_db()
		self.assertEqual(objective.status, ObjectiveStatus.ACHIEVED)

	def test_refresh_status_marks_missed_when_the_period_closed_above_target(self):
		objective = self._objective(
			year=2020, baseline_tco2e=Decimal("100"), target_tco2e=Decimal("80")
		)
		self._activity(90, period_start=date(2020, 5, 1))
		self.assertEqual(objective.refresh_status(), ObjectiveStatus.MISSED)

	def test_archived_objective_keeps_its_status_on_refresh(self):
		objective = self._objective(status=ObjectiveStatus.ARCHIVED)
		self.assertEqual(objective.refresh_status(), ObjectiveStatus.ARCHIVED)

	def test_quarter_is_required_for_a_quarterly_objective(self):
		objective = self._objective(period_type=PeriodType.QUARTERLY, quarter="")
		with self.assertRaises(ValidationError):
			objective.full_clean()

	def test_target_above_baseline_is_rejected(self):
		objective = self._objective(baseline_tco2e=Decimal("100"), target_tco2e=Decimal("120"))
		with self.assertRaises(ValidationError):
			objective.full_clean()

	def test_department_from_another_company_is_rejected(self):
		foreign = Department.objects.create(company=self.other_company, name="Remote")
		objective = self._objective(department=foreign)
		with self.assertRaises(ValidationError):
			objective.full_clean()


class ObjectiveScopingTests(ObjectiveTestCase):
	def test_owner_sees_every_company_objective(self):
		self._objective(department=self.finance)
		self._objective(department=None)
		self.assertEqual(visible_objectives(self.owner, self.company).count(), 2)

	def test_department_manager_sees_own_department_and_company_wide_only(self):
		self._objective(department=self.finance)
		self._objective(department=self.operations)
		self._objective(department=None)
		visible = visible_objectives(self.ops_manager, self.company)
		self.assertEqual(visible.count(), 2)
		self.assertFalse(visible.filter(department=self.finance).exists())

	def test_auditor_reads_every_objective(self):
		self._objective(department=self.finance)
		self.assertEqual(visible_objectives(self.auditor, self.company).count(), 1)

	def test_can_manage_rules(self):
		company_wide = self._objective(department=None)
		finance = self._objective(department=self.finance)
		self.assertTrue(can_manage(self.owner, self.company))
		self.assertTrue(can_manage(self.ops_manager, self.company))
		self.assertFalse(can_manage(self.ops_manager, self.company, company_wide))
		self.assertFalse(can_manage(self.ops_manager, self.company, finance))
		self.assertFalse(can_manage(self.auditor, self.company))


class ObjectiveViewTests(ObjectiveTestCase):
	def test_owner_creates_a_company_wide_objective(self):
		self.client.force_login(self.owner)
		response = self.client.post(reverse("objectives:objective_create"), {
			"name": "Halve our footprint",
			"department": "",
			"year": "2026",
			"period_type": PeriodType.ANNUAL,
			"quarter": "",
			"baseline_tco2e": "200",
			"target_tco2e": "100",
		})
		objective = Objective.objects.get(name="Halve our footprint")
		self.assertRedirects(response, reverse("objectives:objective_detail", args=[objective.pk]))
		self.assertEqual(objective.company, self.company)
		self.assertEqual(objective.created_by, self.owner)
		self.assertIsNone(objective.department)

	def test_department_manager_cannot_create_a_company_wide_objective(self):
		self.client.force_login(self.ops_manager)
		self.client.post(reverse("objectives:objective_create"), {
			"name": "Sneaky company-wide target",
			"department": "",
			"year": "2026",
			"period_type": PeriodType.ANNUAL,
			"quarter": "",
			"baseline_tco2e": "50",
			"target_tco2e": "40",
		})
		objective = Objective.objects.get(name="Sneaky company-wide target")
		self.assertEqual(objective.department, self.operations)

	def test_department_manager_cannot_edit_a_company_wide_objective(self):
		objective = self._objective(department=None)
		self.client.force_login(self.ops_manager)
		response = self.client.get(reverse("objectives:objective_edit", args=[objective.pk]))
		self.assertEqual(response.status_code, 403)

	def test_auditor_cannot_create_an_objective(self):
		self.client.force_login(self.auditor)
		response = self.client.post(reverse("objectives:objective_create"), {"name": "Nope"})
		self.assertEqual(response.status_code, 403)

	def test_cross_company_objective_is_not_found(self):
		foreign = Objective.objects.create(
			company=self.other_company,
			name="Foreign target",
			year=2026,
			baseline_tco2e=Decimal("10"),
			target_tco2e=Decimal("5"),
		)
		self.client.force_login(self.owner)
		response = self.client.get(reverse("objectives:objective_detail", args=[foreign.pk]))
		self.assertEqual(response.status_code, 404)

	def test_other_company_department_manager_cannot_see_the_objective(self):
		objective = self._objective(department=self.operations)
		outsider = User.objects.create_user(
			email="obj-outsider@carbonpilot.test",
			password=self.password,
			role=User.Role.DEPARTMENT_MANAGER,
			status=User.Status.ACTIVE,
			company_membership=self.other_company,
			department="Operations",
		)
		self.client.force_login(outsider)
		response = self.client.get(reverse("objectives:objective_detail", args=[objective.pk]))
		self.assertEqual(response.status_code, 404)

	def test_list_and_detail_render(self):
		objective = self._objective()
		self._activity(5)
		self.client.force_login(self.owner)
		list_response = self.client.get(reverse("objectives:objective_list"))
		self.assertEqual(list_response.status_code, 200)
		self.assertContains(list_response, "Cut emissions 20%")
		detail_response = self.client.get(reverse("objectives:objective_detail", args=[objective.pk]))
		self.assertEqual(detail_response.status_code, 200)
		self.assertContains(detail_response, "Current emissions")
		self.assertContains(detail_response, "Gap to target")

	def test_refresh_status_endpoint_updates_the_objective(self):
		objective = self._objective(baseline_tco2e=Decimal("100"), target_tco2e=Decimal("80"))
		self._activity(70)
		self.client.force_login(self.owner)
		self.client.post(reverse("objectives:objective_refresh_status", args=[objective.pk]))
		objective.refresh_from_db()
		self.assertEqual(objective.status, ObjectiveStatus.ACHIEVED)

	def test_objectives_widget_appears_on_the_carbon_dashboard(self):
		objective = self._objective(name="Dashboard widget target")
		self.client.force_login(self.owner)
		response = self.client.get(reverse("carbon:dashboard"))
		self.assertEqual(response.status_code, 200)
		self.assertContains(response, "Dashboard widget target")
		self.assertContains(response, reverse("objectives:objective_detail", args=[objective.pk]))


class ObjectivePredictionTestCase(ObjectiveTestCase):
	"""Shared fixtures for the ML achievement-prediction tests."""

	def _historical(self, outcome, *, tonnes, year):
		"""A resolved historical objective with a known final outcome."""
		objective = self._objective(
			name=f"{outcome} {year}",
			year=year,
			baseline_tco2e=Decimal("100"),
			target_tco2e=Decimal("80"),
			status=outcome,
		)
		self._activity(tonnes, period_start=date(year, 6, 1))
		return objective

	def _seed_history(self, *, achieved=4, missed=4, start_year=2010):
		"""Enough resolved history (both classes) for the model to train."""
		year = start_year
		for _ in range(achieved):
			self._historical(ObjectiveStatus.ACHIEVED, tonnes=70, year=year)
			year += 1
		for _ in range(missed):
			self._historical(ObjectiveStatus.MISSED, tonnes=95, year=year)
			year += 1

	def _target_objective(self, *, tonnes=70, **overrides):
		"""A live (ACTIVE) objective we want to score."""
		payload = {
			"name": "Live 2026 target",
			"year": 2026,
			"baseline_tco2e": Decimal("100"),
			"target_tco2e": Decimal("80"),
			"status": ObjectiveStatus.ACTIVE,
		}
		payload.update(overrides)
		objective = self._objective(**payload)
		self._activity(tonnes, period_start=date(2026, 6, 1))
		return objective


class PredictionServiceTests(ObjectivePredictionTestCase):
	def test_trains_and_predicts_with_sufficient_historical_data(self):
		self._seed_history()
		target = self._target_objective(tonnes=70)

		result = objective_prediction.predict_objective(target)

		self.assertTrue(result.ok)
		self.assertIsNotNone(result.run)
		run = PredictionRun.objects.get(objective=target)
		self.assertEqual(run.model_name, "RandomForestClassifier")
		self.assertEqual(run.model_version, objective_prediction.MODEL_VERSION)

	def test_training_run_stores_model_metrics(self):
		self._seed_history(achieved=4, missed=4)
		target = self._target_objective(tonnes=70)

		objective_prediction.predict_objective(target)

		metrics = ModelMetrics.latest(self.company, ModelKind.OBJECTIVE_PREDICTION)
		self.assertIsNotNone(metrics)
		self.assertFalse(metrics.insufficient_data)
		self.assertEqual(metrics.model_name, objective_prediction.MODEL_NAME)
		self.assertEqual(metrics.training_size, 8)
		self.assertEqual(metrics.class_balance, {"achieved": 4, "missed": 4})
		self.assertEqual(
			metrics.params["n_estimators"], objective_prediction.FOREST_ESTIMATORS
		)
		self.assertIsNotNone(metrics.created_at)
		# Real scikit-learn metrics, stored as 0–100 percentages.
		for value in (metrics.accuracy, metrics.precision, metrics.recall, metrics.f1):
			self.assertIsNotNone(value)
			self.assertGreaterEqual(float(value), 0.0)
			self.assertLessEqual(float(value), 100.0)

	def test_insufficient_history_records_an_honest_snapshot(self):
		# No resolved history at all → the run must record why, not a metric.
		target = self._target_objective()

		result = objective_prediction.predict_objective(target)

		self.assertTrue(result.insufficient)
		metrics = ModelMetrics.latest(self.company, ModelKind.OBJECTIVE_PREDICTION)
		self.assertIsNotNone(metrics)
		self.assertTrue(metrics.insufficient_data)
		self.assertEqual(metrics.training_size, 0)
		self.assertIsNone(metrics.accuracy)
		self.assertIsNone(metrics.f1)
		self.assertEqual(metrics.note, objective_prediction.INSUFFICIENT_DATA_MESSAGE)

	def test_model_metrics_never_leak_across_tenants(self):
		self._seed_history(achieved=4, missed=4)
		target = self._target_objective(tonnes=70)

		objective_prediction.predict_objective(target)

		self.assertIsNone(
			ModelMetrics.latest(self.other_company, ModelKind.OBJECTIVE_PREDICTION)
		)

	def test_probability_is_a_percentage_between_0_and_100(self):
		self._seed_history()
		target = self._target_objective(tonnes=70)

		run = objective_prediction.predict_objective(target).run

		self.assertGreaterEqual(float(run.probability_achievement), 0.0)
		self.assertLessEqual(float(run.probability_achievement), 100.0)

	def test_model_verdict_and_probability_agree_for_a_strong_candidate(self):
		self._seed_history()
		target = self._target_objective(tonnes=70)

		run = objective_prediction.predict_objective(target).run

		# Thousands of kWh of headroom → the model should back achievement.
		self.assertEqual(run.expected_outcome, ExpectedOutcome.LIKELY_TO_ACHIEVE)
		self.assertEqual(run.risk_level, RiskLevel.LOW)
		self.assertGreaterEqual(float(run.probability_achievement), 50.0)

	def test_fit_predict_and_predict_proba_are_the_ones_used(self):
		self._seed_history()
		target = self._target_objective(tonnes=70)
		calls = {}
		real_forest = objective_prediction.RandomForestClassifier

		class SpyForest(real_forest):
			def fit(self, X, y):
				calls["fit"] = True
				return super().fit(X, y)

			def predict(self, X):
				calls["predict"] = True
				return super().predict(X)

			def predict_proba(self, X):
				calls["predict_proba"] = True
				return super().predict_proba(X)

		with mock.patch.object(objective_prediction, "RandomForestClassifier", SpyForest):
			result = objective_prediction.predict_objective(target)

		self.assertTrue(result.ok)
		self.assertEqual(calls, {"fit": True, "predict": True, "predict_proba": True})

	def test_insufficient_data_does_not_create_a_prediction(self):
		# Only one of each class → far below any trustworthy training size.
		self._historical(ObjectiveStatus.ACHIEVED, tonnes=70, year=2020)
		self._historical(ObjectiveStatus.MISSED, tonnes=95, year=2021)
		target = self._target_objective()

		result = objective_prediction.predict_objective(target)

		self.assertFalse(result.ok)
		self.assertTrue(result.insufficient)
		self.assertEqual(result.message, objective_prediction.INSUFFICIENT_DATA_MESSAGE)
		self.assertEqual(PredictionRun.objects.count(), 0)

	def test_missing_class_does_not_train_a_model(self):
		# Ten resolved objectives, but every one of them ACHIEVED.
		for year in range(2010, 2020):
			self._historical(ObjectiveStatus.ACHIEVED, tonnes=70, year=year)
		target = self._target_objective()

		result = objective_prediction.predict_objective(target)

		self.assertTrue(result.insufficient)
		self.assertEqual(PredictionRun.objects.count(), 0)

	def test_only_approved_activities_feed_the_features(self):
		objective = self._objective(
			year=2020, baseline_tco2e=Decimal("100"), target_tco2e=Decimal("80")
		)
		self._activity(70, period_start=date(2020, 6, 1))
		# A draft outlier of 1000 t must never influence the features.
		self._activity(1000, status=ActivityStatus.DRAFT, period_start=date(2020, 7, 1))
		# An archived outlier must never influence the features either.
		self._activity(900, is_archived=True, period_start=date(2020, 8, 1))

		features = objective_prediction._feature_vector(objective)

		self.assertEqual(objective.current_tco2e, Decimal("70.00"))
		self.assertEqual(features[0], 100.0)  # progress_percent from approved only
		self.assertEqual(features[objective_prediction.FEATURE_NAMES.index("scope2_tco2e")], 70.0)

	def test_training_never_mixes_other_companies(self):
		self._seed_history()
		# Foreign-company history must be invisible to this tenant's model.
		foreign = Objective.objects.create(
			company=self.other_company,
			name="Foreign achieved",
			year=2019,
			baseline_tco2e=Decimal("100"),
			target_tco2e=Decimal("80"),
			status=ObjectiveStatus.ACHIEVED,
		)

		features, labels = objective_prediction._build_training_data(self.company)

		self.assertEqual(len(labels), 8)  # only this tenant's resolved objectives
		self.assertNotIn(foreign.pk, [o.pk for o in objective_prediction._training_rows(self.company)])

	def test_domain_risk_levels_are_deterministic(self):
		self.assertEqual(objective_prediction._risk_level(85.0), RiskLevel.LOW)
		self.assertEqual(objective_prediction._risk_level(55.0), RiskLevel.MEDIUM)
		self.assertEqual(objective_prediction._risk_level(10.0), RiskLevel.HIGH)

	def test_detection_is_deterministic_across_runs(self):
		self._seed_history()
		target = self._target_objective(tonnes=70)

		first = objective_prediction.predict_objective(target).run
		second = objective_prediction.predict_objective(target).run

		self.assertEqual(first.probability_achievement, second.probability_achievement)
		self.assertEqual(first.expected_outcome, second.expected_outcome)


class PredictionViewTests(ObjectivePredictionTestCase):
	def test_detail_renders_the_ai_prediction_card(self):
		objective = self._target_objective()
		self.client.force_login(self.owner)
		response = self.client.get(reverse("objectives:objective_detail", args=[objective.pk]))
		self.assertEqual(response.status_code, 200)
		self.assertContains(response, "AI Prediction")
		self.assertContains(response, "Run AI Prediction")

	def test_owner_runs_prediction_and_sees_the_result(self):
		self._seed_history()
		target = self._target_objective(tonnes=70)
		self.client.force_login(self.owner)

		response = self.client.post(
			reverse("objectives:objective_run_prediction", args=[target.pk]), follow=True
		)

		self.assertEqual(response.status_code, 200)
		self.assertEqual(PredictionRun.objects.filter(objective=target).count(), 1)
		self.assertContains(response, "AI prediction")
		# The evaluation panel shows the stored RandomForest metrics.
		self.assertContains(response, "Model evaluation")
		self.assertContains(response, "Accuracy")
		self.assertContains(response, "F1")
		self.assertContains(response, "last trained")

	def test_insufficient_data_surfaces_a_warning_message(self):
		target = self._target_objective()
		self.client.force_login(self.owner)

		response = self.client.post(
			reverse("objectives:objective_run_prediction", args=[target.pk]), follow=True
		)

		self.assertEqual(PredictionRun.objects.count(), 0)
		self.assertContains(response, objective_prediction.INSUFFICIENT_DATA_MESSAGE)

	def test_run_requires_post(self):
		target = self._target_objective()
		self.client.force_login(self.owner)
		response = self.client.get(
			reverse("objectives:objective_run_prediction", args=[target.pk])
		)
		self.assertEqual(response.status_code, 405)

	def test_auditor_cannot_run_a_prediction(self):
		self._seed_history()
		target = self._target_objective()
		self.client.force_login(self.auditor)
		response = self.client.post(
			reverse("objectives:objective_run_prediction", args=[target.pk])
		)
		self.assertEqual(response.status_code, 403)
		self.assertEqual(PredictionRun.objects.count(), 0)

	def test_department_manager_cannot_run_a_company_wide_prediction(self):
		self._seed_history()
		target = self._target_objective(department=None)
		self.client.force_login(self.ops_manager)
		response = self.client.post(
			reverse("objectives:objective_run_prediction", args=[target.pk])
		)
		self.assertEqual(response.status_code, 403)

	def test_cross_company_prediction_is_not_found(self):
		foreign = Objective.objects.create(
			company=self.other_company,
			name="Foreign target",
			year=2026,
			baseline_tco2e=Decimal("100"),
			target_tco2e=Decimal("80"),
			status=ObjectiveStatus.ACTIVE,
		)
		self.client.force_login(self.owner)
		response = self.client.post(
			reverse("objectives:objective_run_prediction", args=[foreign.pk])
		)
		self.assertEqual(response.status_code, 404)
		self.assertEqual(PredictionRun.objects.filter(objective=foreign).count(), 0)

	def test_anonymous_user_is_redirected_to_login(self):
		target = self._target_objective()
		response = self.client.post(
			reverse("objectives:objective_run_prediction", args=[target.pk])
		)
		self.assertEqual(response.status_code, 302)
		self.assertIn("/login", response.url)


class ObjectiveListFilterTests(ObjectiveTestCase):
	"""Shared filter bar + pagination behaviour on the objective list."""

	def _prediction(self, objective, *, probability, risk, outcome):
		return PredictionRun.objects.create(
			objective=objective,
			probability_achievement=Decimal(probability),
			risk_level=risk,
			expected_outcome=outcome,
			current_progress_percent=Decimal("50.00"),
		)

	def test_search_and_year_filters_narrow_the_result_set(self):
		self._objective(name="Cut fleet emissions", year=2026)
		self._objective(name="Reduce office power", year=2025)
		self.client.force_login(self.owner)
		response = self.client.get(
			reverse("objectives:objective_list"), {"q": "fleet", "year": "2026"}
		)
		self.assertEqual(response.status_code, 200)
		self.assertContains(response, "Cut fleet emissions")
		self.assertNotContains(response, "Reduce office power")

	def test_status_filter_narrows_the_result_set(self):
		self._objective(name="Active objective here")
		self._objective(name="Archived objective here", status=ObjectiveStatus.ARCHIVED)
		self.client.force_login(self.owner)
		response = self.client.get(
			reverse("objectives:objective_list"), {"status": ObjectiveStatus.ACTIVE}
		)
		self.assertContains(response, "Active objective here")
		self.assertNotContains(response, "Archived objective here")

	def test_department_filter_narrows_the_result_set(self):
		self._objective(name="Operations objective here", department=self.operations)
		self._objective(name="Finance objective here", department=self.finance)
		self.client.force_login(self.owner)
		response = self.client.get(
			reverse("objectives:objective_list"), {"department": self.operations.pk}
		)
		self.assertContains(response, "Operations objective here")
		self.assertNotContains(response, "Finance objective here")

	def test_at_risk_filter_uses_the_latest_prediction(self):
		risky = self._objective(name="Risky objective here")
		safe = self._objective(name="Safe objective here")
		self._prediction(risky, probability="20.00", risk=RiskLevel.HIGH, outcome=ExpectedOutcome.AT_RISK)
		self._prediction(safe, probability="90.00", risk=RiskLevel.LOW, outcome=ExpectedOutcome.LIKELY_TO_ACHIEVE)
		self.client.force_login(self.owner)
		response = self.client.get(reverse("objectives:objective_list"), {"at_risk": "1"})
		self.assertContains(response, "Risky objective here")
		self.assertNotContains(response, "Safe objective here")

	def test_at_risk_excludes_objectives_without_a_prediction(self):
		self._objective(name="No prediction objective here")
		self.client.force_login(self.owner)
		response = self.client.get(reverse("objectives:objective_list"), {"at_risk": "1"})
		self.assertEqual(response.status_code, 200)
		self.assertNotContains(response, "No prediction objective here")

	def test_at_risk_uses_only_the_most_recent_prediction(self):
		objective = self._objective(name="Recovered objective here")
		# Older run flagged HIGH, newest run is LOW → the objective is not at risk.
		self._prediction(objective, probability="15.00", risk=RiskLevel.HIGH, outcome=ExpectedOutcome.AT_RISK)
		self._prediction(objective, probability="95.00", risk=RiskLevel.LOW, outcome=ExpectedOutcome.LIKELY_TO_ACHIEVE)
		self.client.force_login(self.owner)
		response = self.client.get(reverse("objectives:objective_list"), {"at_risk": "1"})
		self.assertNotContains(response, "Recovered objective here")

	def test_default_page_size_is_ten(self):
		for index in range(15):
			self._objective(name=f"Paged objective {index:02d}")
		self.client.force_login(self.owner)
		response = self.client.get(reverse("objectives:objective_list"))
		self.assertEqual(response.status_code, 200)
		self.assertEqual(len(response.context["pagination"]["items"]), 10)
		self.assertEqual(response.context["pagination"]["total"], 15)

	def test_per_page_twenty_shows_more_rows(self):
		for index in range(15):
			self._objective(name=f"Paged objective {index:02d}")
		self.client.force_login(self.owner)
		response = self.client.get(
			reverse("objectives:objective_list"), {"per_page": "20"}
		)
		self.assertEqual(len(response.context["pagination"]["items"]), 15)

	def test_next_page_link_preserves_the_active_filter(self):
		for index in range(15):
			self._objective(name=f"Kept objective {index:02d}", year=2026)
		self._objective(name="Filtered out objective here", year=2025)
		self.client.force_login(self.owner)
		response = self.client.get(reverse("objectives:objective_list"), {"year": "2026"})
		self.assertTrue(response.context["pagination"]["has_next"])
		self.assertEqual(response.context["pagination"]["next_page"], 2)
		match = re.search(r'href="([^"]*)"[^>]*rel="next"', response.content.decode())
		self.assertIsNotNone(match)
		href = match.group(1)
		self.assertIn("year=2026", href)
		self.assertIn("page=2", href)

	def test_empty_result_set_renders_the_empty_state(self):
		self._objective(name="Only objective here")
		self.client.force_login(self.owner)
		response = self.client.get(
			reverse("objectives:objective_list"), {"q": "does-not-exist"}
		)
		self.assertEqual(response.status_code, 200)
		self.assertContains(response, "cp-empty")
		self.assertContains(response, "No objectives match these filters.")

	def test_other_company_objectives_never_appear(self):
		self._objective(name="Visible objective here")
		Objective.objects.create(
			company=self.other_company,
			name="Foreign objective here",
			year=2026,
			baseline_tco2e=Decimal("100"),
			target_tco2e=Decimal("80"),
		)
		self.client.force_login(self.owner)
		response = self.client.get(reverse("objectives:objective_list"))
		self.assertContains(response, "Visible objective here")
		self.assertNotContains(response, "Foreign objective here")


class ObjectiveFormValidationTests(ObjectiveTestCase):
	"""Server-side rules for section 1 — every message lands under its field."""

	def _payload(self, **overrides):
		current_year = timezone.now().year
		payload = {
			"name": "Cut emissions 20%",
			"department": "",
			"year": str(current_year),
			"period_type": PeriodType.ANNUAL,
			"quarter": "",
			"baseline_tco2e": "100.00",
			"target_tco2e": "80.00",
		}
		payload.update(overrides)
		return payload

	def _form(self, **overrides):
		return ObjectiveForm(
			data=self._payload(**overrides),
			company=self.company,
			instance=Objective(company=self.company),
		)

	def _min_year(self):
		return timezone.now().year + YEAR_MIN_OFFSET

	def _max_year(self):
		return timezone.now().year + YEAR_MAX_OFFSET

	def test_valid_objective_is_accepted(self):
		form = self._form()
		self.assertTrue(form.is_valid(), form.errors)
		self.assertEqual(form.cleaned_data["year"], timezone.now().year)

	def test_name_is_required(self):
		form = self._form(name="")
		self.assertIn("name", form.errors)
		self.assertIn("Give the objective a name.", form.errors["name"])

	def test_year_is_required(self):
		form = self._form(year="")
		self.assertIn("year", form.errors)
		self.assertIn("Enter the objective year.", form.errors["year"])

	def test_year_must_be_an_integer(self):
		form = self._form(year="twenty-twenty-six")
		self.assertIn("year", form.errors)
		self.assertIn("Enter the year as a whole number.", form.errors["year"])

	def test_year_below_the_window_is_rejected(self):
		form = self._form(year=str(self._min_year() - 5))
		self.assertIn("year", form.errors)
		self.assertIn(
			f"Year must be between {self._min_year()} and {self._max_year()}.",
			form.errors["year"],
		)

	def test_year_above_the_window_is_rejected(self):
		form = self._form(year=str(self._max_year() + 5))
		self.assertIn("year", form.errors)
		self.assertIn(
			f"Year must be between {self._min_year()} and {self._max_year()}.",
			form.errors["year"],
		)

	def test_baseline_cannot_be_negative(self):
		form = self._form(baseline_tco2e="-5")
		self.assertIn("baseline_tco2e", form.errors)
		self.assertIn("The baseline cannot be negative.", form.errors["baseline_tco2e"])

	def test_target_cannot_be_negative(self):
		form = self._form(baseline_tco2e="100", target_tco2e="-5")
		self.assertIn("target_tco2e", form.errors)
		self.assertIn("The target cannot be negative.", form.errors["target_tco2e"])

	def test_non_numeric_baseline_is_rejected(self):
		form = self._form(baseline_tco2e="lots")
		self.assertIn("baseline_tco2e", form.errors)
		self.assertIn("Enter a valid number for the baseline.", form.errors["baseline_tco2e"])

	def test_target_above_baseline_is_rejected_for_a_reduction(self):
		form = self._form(baseline_tco2e="80", target_tco2e="100")
		self.assertIn("target_tco2e", form.errors)
		self.assertIn(
			"Target must be lower than baseline for a reduction objective.",
			form.errors["target_tco2e"],
		)

	def test_quarter_is_required_for_a_quarterly_objective(self):
		form = self._form(period_type=PeriodType.QUARTERLY, quarter="")
		self.assertIn("quarter", form.errors)
		# The message must appear exactly once (model-only enforcement).
		self.assertEqual(len(form.errors["quarter"]), 1)
		self.assertIn("Pick a quarter for a quarterly objective.", form.errors["quarter"])

	def test_valid_quarterly_objective_is_accepted(self):
		form = self._form(period_type=PeriodType.QUARTERLY, quarter=Quarter.Q1)
		self.assertTrue(form.is_valid(), form.errors)

	def test_department_from_another_company_is_rejected(self):
		foreign_department = Department.objects.create(
			company=self.other_company, name="Foreign Ops"
		)
		form = self._form(department=str(foreign_department.pk))
		self.assertIn("department", form.errors)

	def test_department_of_the_same_company_is_accepted(self):
		form = self._form(department=str(self.operations.pk))
		self.assertTrue(form.is_valid(), form.errors)
		self.assertEqual(form.cleaned_data["department"], self.operations)
