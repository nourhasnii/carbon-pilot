from datetime import date
from decimal import Decimal
from unittest import mock

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.carbon.models import ActivityCategory, ActivityStatus, CarbonActivity, EmissionFactor, Scope
from apps.enterprise.models import Company, Department

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
