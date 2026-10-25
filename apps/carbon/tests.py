from datetime import date
from decimal import Decimal
import shutil
import tempfile

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db.models import Sum
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.accounts.models import Notification, User
from apps.enterprise.models import Company, Department

from .models import (
	ActivityAuditAction,
	ActivityCategory,
	ActivityStatus,
	AnomalyLog,
	AnomalySeverity,
	AnomalyStatus,
	AnomalyType,
	CarbonActivity,
	DataQualityBand,
	EmissionFactor,
	EmissionFactorCompanyState,
	Scope,
	VerificationStatus,
	activity_evidence_upload_to,
)
from .services import analytics, anomaly_detection, data_quality, tenancy, workflow
from .services.recommendation import (
	effective_factor_attrs,
	effective_is_active,
	factors_for_company,
	recommend_emission_factor,
	recommendation_candidates,
)
from .services.units import UnitConversionError, convert


class UnitConversionTests(TestCase):
	def test_same_unit_returns_unchanged_value(self):
		self.assertEqual(convert(Decimal("12.5"), "kWh", "kWh"), Decimal("12.5"))

	def test_compatible_units_are_converted(self):
		self.assertEqual(convert(Decimal("5"), "MWh", "kWh"), Decimal("5000.000000"))
		self.assertEqual(convert(Decimal("1"), "t", "kg"), Decimal("1000.000000"))
		self.assertEqual(convert(Decimal("1"), "m3", "L"), Decimal("1000.000000"))

	def test_incompatible_units_raise(self):
		with self.assertRaises(UnitConversionError):
			convert(Decimal("1"), "kWh", "kg")

	def test_unknown_unit_raises(self):
		with self.assertRaises(UnitConversionError):
			convert(Decimal("1"), "cubits", "kWh")


class CarbonTestCase(TestCase):
	password = "SecureTestPassword2026!"

	def setUp(self):
		self.owner = User.objects.create_user(
			email="owner@carbonpilot.test",
			password=self.password,
			first_name="Owner",
			last_name="Person",
			role=User.Role.COMPANY_OWNER,
			status=User.Status.ACTIVE,
		)
		self.company = Company.objects.create(
			owner=self.owner, name="Carbon Works", country="Tunisia"
		)
		self.department = Department.objects.create(company=self.company, name="Operations")
		self.other_company = Company.objects.create(
			owner=User.objects.create_user(
				email="other-owner@carbonpilot.test",
				password=self.password,
				role=User.Role.COMPANY_OWNER,
				status=User.Status.ACTIVE,
			),
			name="Other Works",
		)
		self.electricity_factor = EmissionFactor.objects.create(
			name="Electricity — Tunisia",
			category=ActivityCategory.ELECTRICITY,
			scope=Scope.SCOPE_2,
			value=Decimal("0.42"),
			unit="kWh",
			region="Tunisia",
			year=2026,
			source="Demo source",
			verification_status=VerificationStatus.DEMO,
		)

	def make_activity(self, **overrides):
		payload = {
			"company": self.company,
			"department": self.department,
			"name": "Electricity meter — January",
			"category": ActivityCategory.ELECTRICITY,
			"scope": Scope.SCOPE_2,
			"quantity": Decimal("5000"),
			"unit": "kWh",
			"period_start": date(2026, 1, 1),
			"period_end": date(2026, 1, 31),
			"emission_factor": self.electricity_factor,
			"created_by": self.owner,
		}
		payload.update(overrides)
		return CarbonActivity(**payload)


class CarbonActivityModelTests(CarbonTestCase):
	def test_default_status_is_draft(self):
		activity = self.make_activity()
		activity.save()
		self.assertEqual(activity.status, ActivityStatus.DRAFT)

	def test_co2e_is_calculated_and_stored(self):
		activity = self.make_activity()
		activity.save()
		activity.refresh_from_db()
		self.assertEqual(activity.co2e_kg, Decimal("2100.0000"))
		self.assertEqual(activity.co2e_tonnes, Decimal("2.1000"))
		self.assertEqual(activity.emission_factor_value, Decimal("0.420000"))
		self.assertEqual(activity.emission_factor_unit, "kWh")

	def test_co2e_uses_unit_conversion(self):
		activity = self.make_activity(quantity=Decimal("5"), unit="MWh")
		activity.save()
		self.assertEqual(activity.co2e_kg, Decimal("2100.0000"))

	def test_manual_co2e_value_is_overwritten(self):
		activity = self.make_activity()
		activity.co2e_kg = Decimal("999999")
		activity.save()
		activity.refresh_from_db()
		self.assertEqual(activity.co2e_kg, Decimal("2100.0000"))

	def test_incompatible_activity_unit_is_rejected(self):
		activity = self.make_activity(unit="kg")
		with self.assertRaises(ValidationError):
			activity.save()

	def test_scope_must_match_emission_factor(self):
		activity = self.make_activity(scope=Scope.SCOPE_1)
		with self.assertRaises(ValidationError):
			activity.save()

	def test_period_end_cannot_precede_start(self):
		activity = self.make_activity(period_start=date(2026, 2, 1), period_end=date(2026, 1, 1))
		with self.assertRaises(ValidationError):
			activity.save()

	def test_department_must_belong_to_company(self):
		foreign_department = Department.objects.create(company=self.other_company, name="Finance")
		activity = self.make_activity(department=foreign_department)
		with self.assertRaises(ValidationError):
			activity.save()

	def test_activity_without_factor_has_no_co2e(self):
		activity = self.make_activity(emission_factor=None)
		activity.save()
		self.assertIsNone(activity.co2e_kg)

	def test_inactive_factor_cannot_be_used_for_a_new_activity(self):
		self.electricity_factor.is_active = False
		self.electricity_factor.save(update_fields=("is_active", "updated_at"))
		with self.assertRaises(ValidationError):
			self.make_activity().save()

	def test_company_factor_cannot_be_used_by_another_tenant(self):
		self.electricity_factor.company = self.other_company
		self.electricity_factor.save(update_fields=("company", "updated_at"))
		with self.assertRaises(ValidationError):
			self.make_activity().save()

	def test_approved_activity_keeps_factor_snapshot_after_factor_edit(self):
		activity = self.make_activity(status=ActivityStatus.APPROVED)
		activity.save()
		self.electricity_factor.value = Decimal("0.99")
		self.electricity_factor.source = "Changed source"
		self.electricity_factor.year = 2027
		self.electricity_factor.save()
		activity.name = "Approved record retained"
		activity.save()
		activity.refresh_from_db()
		self.assertEqual(activity.co2e_kg, Decimal("2100.0000"))
		self.assertEqual(activity.emission_factor_value, Decimal("0.420000"))
		self.assertEqual(activity.emission_factor_source, "Demo source")
		self.assertEqual(activity.emission_factor_year, 2026)

	def test_evidence_path_is_scoped_per_company(self):
		activity = self.make_activity()
		path = activity_evidence_upload_to(activity, "Invoice.PDF")
		self.assertTrue(path.startswith(f"carbon/evidence/company_{self.company.pk}/2026/"))
		self.assertTrue(path.endswith(".pdf"))


class OfficialFootprintQueryTests(CarbonTestCase):
	def test_only_approved_activities_count(self):
		approved = self.make_activity(name="Approved", status=ActivityStatus.APPROVED)
		approved.save()
		draft = self.make_activity(name="Draft")
		draft.save()
		submitted = self.make_activity(name="Submitted", status=ActivityStatus.SUBMITTED)
		submitted.save()

		official = CarbonActivity.objects.for_company(self.company).approved()
		self.assertEqual(official.count(), 1)
		self.assertEqual(official.aggregate(total=Sum("co2e_kg"))["total"], Decimal("2100.0000"))

	def test_pending_review_query(self):
		self.make_activity(name="Submitted", status=ActivityStatus.SUBMITTED).save()
		self.make_activity(name="Draft").save()
		self.assertEqual(CarbonActivity.objects.pending_review().count(), 1)

	def test_activities_are_scoped_to_company(self):
		self.make_activity().save()
		self.assertEqual(CarbonActivity.objects.for_company(self.company).count(), 1)
		self.assertEqual(CarbonActivity.objects.for_company(self.other_company).count(), 0)


class EmissionFactorRecommendationTests(CarbonTestCase):
	def test_emission_factor_value_must_be_positive(self):
		factor = EmissionFactor(
			name="Zero factor",
			category=ActivityCategory.ELECTRICITY,
			scope=Scope.SCOPE_2,
			value=Decimal("0"),
			unit="kWh",
			region="Global",
			year=2026,
			source="Example source",
		)
		with self.assertRaises(ValidationError):
			factor.full_clean()

	def test_recommends_region_matching_factor(self):
		global_factor = EmissionFactor.objects.create(
			name="Electricity — Global average",
			category=ActivityCategory.ELECTRICITY,
			scope=Scope.SCOPE_2,
			value=Decimal("0.45"),
			unit="kWh",
			region="Global",
			year=2026,
		)
		recommended = recommend_emission_factor(
			category=ActivityCategory.ELECTRICITY, company=self.company, year=2026
		)
		self.assertEqual(recommended, self.electricity_factor)
		self.assertNotEqual(recommended, global_factor)

	def test_verified_factor_is_preferred_over_demo(self):
		verified = EmissionFactor.objects.create(
			name="Electricity — Tunisia (verified)",
			category=ActivityCategory.ELECTRICITY,
			scope=Scope.SCOPE_2,
			value=Decimal("0.40"),
			unit="kWh",
			region="Tunisia",
			year=2026,
			verification_status=VerificationStatus.VERIFIED,
		)
		recommended = recommend_emission_factor(
			category=ActivityCategory.ELECTRICITY, company=self.company, year=2026
		)
		self.assertEqual(recommended, verified)

	def test_inactive_factor_is_never_recommended(self):
		self.electricity_factor.is_active = False
		self.electricity_factor.save(update_fields=("is_active", "updated_at"))
		self.assertIsNone(
			recommend_emission_factor(category=ActivityCategory.ELECTRICITY, company=self.company)
		)

	def test_other_categories_return_no_candidates(self):
		self.assertEqual(
			recommendation_candidates(category=ActivityCategory.WASTE, company=self.company), []
		)

	def test_recommendations_include_only_shared_and_current_company_factors(self):
		own_factor = EmissionFactor.objects.create(
			name="Company electricity", category=ActivityCategory.ELECTRICITY,
			scope=Scope.SCOPE_2, value=Decimal("0.31"), unit="kWh", region="Tunisia",
			year=2026, company=self.company,
		)
		foreign_factor = EmissionFactor.objects.create(
			name="Other company electricity", category=ActivityCategory.ELECTRICITY,
			scope=Scope.SCOPE_2, value=Decimal("0.29"), unit="kWh", region="Tunisia",
			year=2026, company=self.other_company,
		)
		candidates = recommendation_candidates(category=ActivityCategory.ELECTRICITY, company=self.company)
		self.assertIn(own_factor, candidates)
		self.assertNotIn(foreign_factor, candidates)


class EmissionFactorLibraryTests(CarbonTestCase):
	def factor_payload(self, **overrides):
		payload = {
			"name": "Purchased electricity example",
			"category": ActivityCategory.ELECTRICITY,
			"scope": Scope.SCOPE_2,
			"value": "0.123456",
			"unit": "kWh",
			"region": "Tunisia",
			"year": "2026",
			"source": "Example publication",
			"methodology": "Location-based method",
			"verification_status": VerificationStatus.UNVERIFIED,
		}
		payload.update(overrides)
		return payload

	def test_owner_can_create_and_deactivate_a_company_factor(self):
		self.client.force_login(self.owner)
		response = self.client.post(reverse("carbon:factor_create"), self.factor_payload())
		self.assertEqual(response.status_code, 302)
		factor = EmissionFactor.objects.get(name="Purchased electricity example")
		self.assertEqual(factor.company, self.company)
		self.assertEqual(factor.created_by, self.owner)
		response = self.client.get(reverse("carbon:factor_library"))
		self.assertContains(response, "ACTIVE")
		self.assertContains(response, "View details")
		self.assertContains(response, "Edit")
		self.assertContains(response, "Deactivate")
		response = self.client.post(reverse("carbon:factor_toggle_status", args=[factor.pk]))
		self.assertEqual(response.status_code, 302)
		factor.refresh_from_db()
		self.assertFalse(factor.is_active)
		response = self.client.get(reverse("carbon:factor_library"))
		self.assertContains(response, "INACTIVE")
		self.assertContains(response, "Activate")

	def test_esg_reporting_manager_can_add_company_factor(self):
		manager = User.objects.create_user(
			email="admin-rse@carbonpilot.test", password=self.password,
			role=User.Role.ESG_REPORTING_MANAGER, status=User.Status.ACTIVE,
			company_membership=self.company,
		)
		self.client.force_login(manager)
		response = self.client.get(reverse("carbon:factor_library"))
		self.assertContains(response, "Add emission factor")
		response = self.client.post(reverse("carbon:factor_create"), self.factor_payload())
		self.assertEqual(response.status_code, 302)
		self.assertEqual(
			EmissionFactor.objects.get(name="Purchased electricity example").company,
			self.company,
		)

	def override_payload(self, **overrides):
		payload = {
			"value": "1.5",
			"unit": "kWh",
			"region": "Tunisia",
			"year": "2026",
			"source": "Company specific source",
			"methodology": "Company specific method",
		}
		payload.update(overrides)
		return payload

	def test_company_owner_edits_shared_factor_as_a_company_override(self):
		self.client.force_login(self.owner)
		edit_url = reverse("carbon:factor_edit", args=[self.electricity_factor.pk])
		self.assertEqual(self.client.get(edit_url).status_code, 200)
		response = self.client.post(edit_url, self.override_payload())
		self.assertEqual(response.status_code, 302)
		# The shared record is never touched...
		self.electricity_factor.refresh_from_db()
		self.assertEqual(self.electricity_factor.value, Decimal("0.42"))
		self.assertEqual(self.electricity_factor.source, "Demo source")
		# ...only the tenant-scoped override is stored.
		state = EmissionFactorCompanyState.objects.get(
			company=self.company, factor=self.electricity_factor
		)
		self.assertEqual(state.value, Decimal("1.500000"))
		self.assertEqual(state.source, "Company specific source")
		self.assertEqual(state.updated_by, self.owner)
		self.assertTrue(state.has_value_override)
		self.assertEqual(
			effective_factor_attrs(self.electricity_factor, self.company)["value"],
			Decimal("1.500000"),
		)

	def test_company_override_does_not_leak_to_other_tenants(self):
		self.client.force_login(self.owner)
		self.client.post(
			reverse("carbon:factor_edit", args=[self.electricity_factor.pk]),
			self.override_payload(),
		)
		self.assertEqual(
			effective_factor_attrs(self.electricity_factor, self.other_company)["value"],
			Decimal("0.42"),
		)
		# The platform view of the factor is also unchanged.
		self.assertEqual(
			effective_factor_attrs(self.electricity_factor, None)["value"],
			Decimal("0.42"),
		)

	def test_new_activity_uses_the_company_override_value(self):
		self.client.force_login(self.owner)
		self.client.post(
			reverse("carbon:factor_edit", args=[self.electricity_factor.pk]),
			self.override_payload(value="1.5"),
		)
		activity = self.make_activity()
		activity.save()
		activity.refresh_from_db()
		self.assertEqual(activity.co2e_kg, Decimal("7500.0000"))
		self.assertEqual(activity.emission_factor_value, Decimal("1.500000"))

	def test_library_and_detail_reflect_customized_values(self):
		self.client.force_login(self.owner)
		self.client.post(
			reverse("carbon:factor_edit", args=[self.electricity_factor.pk]),
			self.override_payload(),
		)
		response = self.client.get(reverse("carbon:factor_library"))
		self.assertContains(response, "Customized")
		listed = next(
			factor for factor in response.context["factors"]
			if factor.pk == self.electricity_factor.pk
		)
		self.assertEqual(listed.effective["value"], Decimal("1.500000"))
		detail = self.client.get(
			reverse("carbon:factor_detail", args=[self.electricity_factor.pk])
		)
		self.assertTrue(detail.context["has_override"])
		self.assertEqual(detail.context["effective"]["value"], Decimal("1.500000"))

	def test_super_admin_edit_updates_the_shared_record(self):
		super_admin = User.objects.create_user(
			email="platform-editor@carbonpilot.test", password=self.password,
			role=User.Role.SUPER_ADMIN, status=User.Status.ACTIVE,
		)
		self.client.force_login(super_admin)
		response = self.client.post(
			reverse("carbon:factor_edit", args=[self.electricity_factor.pk]),
			self.factor_payload(
				name="Electricity — Tunisia", value="0.5", unit="kWh",
				region="Tunisia", year="2026", source="Demo source", methodology="",
			),
		)
		self.assertEqual(response.status_code, 302)
		self.electricity_factor.refresh_from_db()
		self.assertEqual(self.electricity_factor.value, Decimal("0.500000"))
		self.assertFalse(
			EmissionFactorCompanyState.objects.filter(factor=self.electricity_factor).exists()
		)

	def test_auditor_cannot_edit_a_shared_factor(self):
		auditor = User.objects.create_user(
			email="edit-auditor@carbonpilot.test", password=self.password,
			role=User.Role.AUDITOR, status=User.Status.ACTIVE, company_membership=self.company,
		)
		self.client.force_login(auditor)
		response = self.client.post(
			reverse("carbon:factor_edit", args=[self.electricity_factor.pk]),
			self.override_payload(),
		)
		self.assertEqual(response.status_code, 403)
		self.assertFalse(
			EmissionFactorCompanyState.objects.filter(factor=self.electricity_factor).exists()
		)

	def test_company_owner_deactivates_shared_factor_for_own_company_only(self):
		self.client.force_login(self.owner)
		response = self.client.post(
			reverse("carbon:factor_toggle_status", args=[self.electricity_factor.pk])
		)
		self.assertEqual(response.status_code, 302)
		# The shared record is untouched; only a tenant-scoped override is stored.
		self.electricity_factor.refresh_from_db()
		self.assertTrue(self.electricity_factor.is_active)
		state = EmissionFactorCompanyState.objects.get(
			company=self.company, factor=self.electricity_factor
		)
		self.assertFalse(state.is_active)
		self.assertEqual(state.updated_by, self.owner)
		self.assertFalse(effective_is_active(self.electricity_factor, self.company))
		# Another tenant keeps using the shared factor.
		self.assertTrue(effective_is_active(self.electricity_factor, self.other_company))

	def test_deactivated_shared_factor_is_hidden_from_selection(self):
		self.client.force_login(self.owner)
		self.client.post(reverse("carbon:factor_toggle_status", args=[self.electricity_factor.pk]))
		self.assertNotIn(
			self.electricity_factor,
			recommendation_candidates(category=ActivityCategory.ELECTRICITY, company=self.company),
		)
		self.assertNotIn(
			self.electricity_factor,
			list(factors_for_company(company=self.company)),
		)
		self.assertIn(
			self.electricity_factor,
			list(factors_for_company(company=self.other_company)),
		)

	def test_company_owner_can_reactivate_a_shared_factor(self):
		self.client.force_login(self.owner)
		toggle_url = reverse("carbon:factor_toggle_status", args=[self.electricity_factor.pk])
		self.client.post(toggle_url)
		self.client.post(toggle_url)
		self.assertTrue(effective_is_active(self.electricity_factor, self.company))
		self.assertTrue(
			EmissionFactorCompanyState.objects.get(
				company=self.company, factor=self.electricity_factor
			).is_active
		)

	def test_library_badge_and_actions_reflect_the_company_override(self):
		self.client.force_login(self.owner)
		self.client.post(reverse("carbon:factor_toggle_status", args=[self.electricity_factor.pk]))
		response = self.client.get(reverse("carbon:factor_library"))
		self.assertContains(response, "INACTIVE")
		self.assertContains(response, "Activate")
		self.assertNotContains(response, "Deactivate")

	def test_super_admin_toggles_the_shared_record(self):
		super_admin = User.objects.create_user(
			email="platform-admin@carbonpilot.test", password=self.password,
			role=User.Role.SUPER_ADMIN, status=User.Status.ACTIVE,
		)
		self.client.force_login(super_admin)
		response = self.client.post(
			reverse("carbon:factor_toggle_status", args=[self.electricity_factor.pk])
		)
		self.assertEqual(response.status_code, 302)
		self.electricity_factor.refresh_from_db()
		self.assertFalse(self.electricity_factor.is_active)
		self.assertFalse(
			EmissionFactorCompanyState.objects.filter(factor=self.electricity_factor).exists()
		)

	def test_company_cannot_enable_a_globally_disabled_shared_factor(self):
		self.electricity_factor.is_active = False
		self.electricity_factor.save(update_fields=("is_active", "updated_at"))
		self.client.force_login(self.owner)
		response = self.client.post(
			reverse("carbon:factor_toggle_status", args=[self.electricity_factor.pk])
		)
		self.assertEqual(response.status_code, 403)
		self.assertFalse(
			EmissionFactorCompanyState.objects.filter(factor=self.electricity_factor).exists()
		)

	def test_factor_form_rejects_zero_value_invalid_year_and_missing_source(self):
		self.client.force_login(self.owner)
		response = self.client.post(
			reverse("carbon:factor_create"),
			self.factor_payload(value="0", year="2101", source=""),
		)
		self.assertEqual(response.status_code, 200)
		self.assertIn("value", response.context["form"].errors)
		self.assertIn("year", response.context["form"].errors)
		self.assertIn("source", response.context["form"].errors)

	def test_factor_detail_and_edit_pages_render(self):
		factor = EmissionFactor.objects.create(
			name="Local factor for rendering", category=ActivityCategory.ELECTRICITY,
			scope=Scope.SCOPE_2, value=Decimal("0.25"), unit="kWh", region="Tunisia",
			year=2026, source="Test publication", company=self.company,
		)
		self.client.force_login(self.owner)
		self.assertEqual(
			self.client.get(reverse("carbon:factor_detail", args=[factor.pk])).status_code,
			200,
		)
		self.assertEqual(
			self.client.get(reverse("carbon:factor_edit", args=[factor.pk])).status_code,
			200,
		)

	def test_auditor_can_view_but_cannot_manage_factors(self):
		auditor = User.objects.create_user(
			email="library-auditor@carbonpilot.test", password=self.password,
			role=User.Role.AUDITOR, status=User.Status.ACTIVE, company_membership=self.company,
		)
		self.client.force_login(auditor)
		self.assertEqual(self.client.get(reverse("carbon:factor_library")).status_code, 200)
		self.assertEqual(self.client.post(reverse("carbon:factor_create"), self.factor_payload()).status_code, 403)
		self.assertEqual(
			self.client.post(reverse("carbon:factor_toggle_status", args=[self.electricity_factor.pk])).status_code,
			403,
		)

	def test_company_factor_is_hidden_from_other_tenant(self):
		factor = EmissionFactor.objects.create(
			name="Private company factor", category=ActivityCategory.ELECTRICITY,
			scope=Scope.SCOPE_2, value=Decimal("0.21"), unit="kWh", region="Tunisia",
			year=2026, source="Tenant publication", company=self.company,
		)
		self.client.force_login(self.other_company.owner)
		self.assertEqual(self.client.get(reverse("carbon:factor_detail", args=[factor.pk])).status_code, 404)
		response = self.client.get(reverse("carbon:factor_library"))
		self.assertNotContains(response, "Private company factor")

	def test_library_filters_status_and_lists_inactive_factors(self):
		self.electricity_factor.is_active = False
		self.electricity_factor.save(update_fields=("is_active", "updated_at"))
		self.client.force_login(self.owner)
		response = self.client.get(reverse("carbon:factor_library"), {"status": "INACTIVE"})
		self.assertEqual(response.context["active_count"], 0)
		self.assertEqual(response.context["inactive_count"], 1)
		self.assertEqual([factor.pk for factor in response.context["factors"]], [self.electricity_factor.pk])


class SeedEmissionFactorsCommandTests(TestCase):
	def test_seed_is_idempotent_and_flagged_as_demo(self):
		call_command("seed_emission_factors")
		first_count = EmissionFactor.objects.count()
		call_command("seed_emission_factors")
		self.assertEqual(EmissionFactor.objects.count(), first_count)
		self.assertTrue(first_count > 0)
		self.assertFalse(
			EmissionFactor.objects.exclude(verification_status=VerificationStatus.DEMO).exists()
		)
		factor = EmissionFactor.objects.filter(category=ActivityCategory.ELECTRICITY).first()
		self.assertIn("not an official emission factor", factor.source.lower())


class ActivityWizardTests(CarbonTestCase):
	url = reverse("carbon:activity_wizard")

	def _member(self, *, role, email, company=None, department=""):
		return User.objects.create_user(
			email=email,
			password=self.password,
			role=role,
			status=User.Status.ACTIVE,
			company_membership=company,
			department=department,
		)

	def _advance_to_step_2(self):
		return self.client.post(self.url, {"step": "1", "category": "ELECTRICITY", "name": "Q1 electricity"})

	def _advance_to_step_3(self):
		self._advance_to_step_2()
		return self.client.post(
			self.url,
			{
				"step": "2",
				"quantity": "5000",
				"unit": "kWh",
				"period_start": "2026-01-01",
				"period_end": "2026-01-31",
				"department": self.department.pk,
			},
		)

	def _advance_to_step_4(self):
		self._advance_to_step_3()
		return self.client.post(
			self.url, {"step": "3", "emission_factor": str(self.electricity_factor.pk)}
		)

	# ---------- Access control ----------
	def test_login_is_required(self):
		response = self.client.get(self.url)
		self.assertEqual(response.status_code, 302)
		self.assertIn("login", response.url)

	def test_auditor_role_is_denied(self):
		auditor = self._member(role=User.Role.AUDITOR, email="auditor@carbonpilot.test", company=self.company)
		self.client.force_login(auditor)
		response = self.client.get(self.url)
		self.assertEqual(response.status_code, 403)

	def test_inactive_user_is_denied(self):
		inactive = User.objects.create_user(
			email="inactive@carbonpilot.test",
			password=self.password,
			role=User.Role.DEPARTMENT_MANAGER,
			status=User.Status.INACTIVE,
			company_membership=self.company,
		)
		# An inactive account has is_active=False, so Django treats it as anonymous.
		self.client.force_login(inactive)
		response = self.client.get(self.url)
		self.assertEqual(response.status_code, 302)
		self.assertIn("login", response.url)

	def test_creator_without_company_is_redirected(self):
		supplier = self._member(role=User.Role.SUPPLIER, email="supplier@carbonpilot.test")
		self.client.force_login(supplier)
		response = self.client.get(self.url)
		self.assertEqual(response.status_code, 302)
		self.assertIn("section=company", response.url)

	# ---------- Step gating ----------
	def test_cannot_skip_steps(self):
		self.client.force_login(self.owner)
		response = self.client.get(f"{self.url}?step=4")
		self.assertEqual(response.context["step"], 1)

	def test_review_step_renders_transparent_calculation(self):
		self.client.force_login(self.owner)
		self._advance_to_step_4()
		response = self.client.get(f"{self.url}?step=4")
		self.assertEqual(response.status_code, 200)
		preview = response.context["preview"]
		self.assertTrue(preview["available"])
		self.assertEqual(preview["co2e_kg"], Decimal("2100.0000"))
		self.assertEqual(preview["co2e_tonnes"], Decimal("2.1000"))

	def test_factor_step_lists_the_recommended_factor(self):
		self.client.force_login(self.owner)
		self._advance_to_step_2()
		self.client.post(
			self.url,
			{
				"step": "2",
				"quantity": "5000",
				"unit": "kWh",
				"period_start": "2026-01-01",
				"department": self.department.pk,
			},
		)
		response = self.client.get(f"{self.url}?step=3")
		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.context["recommended"], self.electricity_factor)
		self.assertContains(response, "Recommended")

	# ---------- Submission flows ----------
	def test_full_flow_submits_for_review(self):
		self.client.force_login(self.owner)
		self._advance_to_step_4()
		response = self.client.post(
			self.url, {"step": "4", "data_source": "January invoice", "action": "submit"}
		)
		self.assertEqual(response.status_code, 302)
		self.assertIn("created=", response.url)
		activity = CarbonActivity.objects.get(company=self.company)
		self.assertEqual(activity.status, ActivityStatus.SUBMITTED)
		self.assertIsNotNone(activity.submitted_at)
		self.assertEqual(activity.created_by, self.owner)
		self.assertEqual(activity.data_source, "January invoice")

	def test_submission_notifies_the_department_manager(self):
		manager = self._member(
			role=User.Role.DEPARTMENT_MANAGER,
			email="wizard-manager@carbonpilot.test",
			company=self.company,
			department="Operations",
		)
		self.client.force_login(self.owner)
		self._advance_to_step_4()
		self.client.post(self.url, {"step": "4", "action": "submit"})
		note = Notification.objects.get(recipient=manager)
		self.assertEqual(note.kind, Notification.Kind.ACTIVITY_SUBMITTED)
		self.assertIn("submitted", note.title.lower())

	def test_saving_a_draft_does_not_notify_the_department_manager(self):
		self._member(
			role=User.Role.DEPARTMENT_MANAGER,
			email="wizard-manager@carbonpilot.test",
			company=self.company,
			department="Operations",
		)
		self.client.force_login(self.owner)
		self._advance_to_step_4()
		self.client.post(self.url, {"step": "4", "action": "draft"})
		self.assertFalse(Notification.objects.exists())

	def test_save_draft_keeps_status_draft(self):
		self.client.force_login(self.owner)
		self._advance_to_step_4()
		self.client.post(self.url, {"step": "4", "action": "draft"})
		activity = CarbonActivity.objects.get(company=self.company)
		self.assertEqual(activity.status, ActivityStatus.DRAFT)
		self.assertIsNone(activity.submitted_at)

	def test_co2e_cannot_be_injected_from_the_client(self):
		self.client.force_login(self.owner)
		self._advance_to_step_4()
		self.client.post(
			self.url,
			{"step": "4", "action": "submit", "co2e_kg": "999999", "co2e_tonnes": "999"},
		)
		activity = CarbonActivity.objects.get(company=self.company)
		self.assertEqual(activity.co2e_kg, Decimal("2100.0000"))

	def test_missing_factor_blocks_submission(self):
		self.electricity_factor.delete()
		self.client.force_login(self.owner)
		self._advance_to_step_2()
		self.client.post(
			self.url,
			{
				"step": "2",
				"quantity": "5000",
				"unit": "kWh",
				"period_start": "2026-01-01",
				"department": self.department.pk,
			},
		)
		response = self.client.get(f"{self.url}?step=3")
		self.assertEqual(response.context["step"], 3)
		self.assertEqual(response.context["candidates"], [])
		self.client.post(self.url, {"step": "3", "emission_factor": ""})
		self.assertFalse(CarbonActivity.objects.exists())

	def test_stale_inactive_factor_in_session_cannot_be_submitted(self):
		self.electricity_factor.is_active = False
		self.electricity_factor.save(update_fields=("is_active", "updated_at"))
		self.client.force_login(self.owner)
		session = self.client.session
		session["carbon_activity_wizard"] = {
			"category": ActivityCategory.ELECTRICITY,
			"name": "Stale factor test",
			"quantity": "5000",
			"unit": "kWh",
			"period_start": "2026-01-01",
			"period_end": "",
			"department_id": self.department.pk,
			"emission_factor_id": self.electricity_factor.pk,
		}
		session.save()
		response = self.client.post(self.url, {"step": "4", "action": "submit"})
		self.assertEqual(response.status_code, 200)
		self.assertFalse(CarbonActivity.objects.exists())

	# ---------- Tenant isolation ----------
	def test_created_summary_is_scoped_to_the_owners_company(self):
		self.client.force_login(self.owner)
		self._advance_to_step_4()
		response = self.client.post(self.url, {"step": "4", "action": "submit"})
		created_id = response.url.split("created=")[-1]

		other_owner = self.other_company.owner
		self.client.force_login(other_owner)
		response = self.client.get(f"{self.url}?created={created_id}")
		self.assertNotIn("created", response.context)
		self.assertEqual(response.context["step"], 1)

	def test_wizard_writes_to_the_users_own_company(self):
		manager = self._member(
			role=User.Role.DEPARTMENT_MANAGER,
			email="manager@carbonpilot.test",
			company=self.company,
			department="Operations",
		)
		self.client.force_login(manager)
		self._advance_to_step_4()
		self.client.post(self.url, {"step": "4", "action": "submit"})
		activity = CarbonActivity.objects.get()
		self.assertEqual(activity.company, self.company)

	def test_department_manager_only_sees_own_department(self):
		Department.objects.create(company=self.company, name="Finance")
		manager = self._member(
			role=User.Role.DEPARTMENT_MANAGER,
			email="manager@carbonpilot.test",
			company=self.company,
			department="Operations",
		)
		self.client.force_login(manager)
		session = self.client.session
		session["carbon_activity_wizard"] = {"category": "ELECTRICITY"}
		session.save()
		response = self.client.get(f"{self.url}?step=2")
		names = list(response.context["form"].fields["department"].queryset.values_list("name", flat=True))
		self.assertEqual(names, ["Operations"])

	# ---------- Evidence ----------
	def test_evidence_upload_is_stored_per_company(self):
		media_root = tempfile.mkdtemp()
		self.addCleanup(shutil.rmtree, media_root, ignore_errors=True)
		with override_settings(MEDIA_ROOT=media_root):
			self.client.force_login(self.owner)
			self._advance_to_step_4()
			upload = SimpleUploadedFile("meter.pdf", b"%PDF-1.4 demo", content_type="application/pdf")
			self.client.post(
				self.url, {"step": "4", "action": "submit", "evidence": upload}
			)
			activity = CarbonActivity.objects.get(company=self.company)
			self.assertTrue(
				activity.evidence.name.startswith(
					f"carbon/evidence/company_{self.company.pk}/2026/"
				)
			)

	def test_partial_wizard_state_can_be_reset(self):
		self.client.force_login(self.owner)
		self._advance_to_step_2()
		response = self.client.get(f"{self.url}?reset=1")
		self.assertEqual(response.status_code, 302)
		self.assertNotIn("carbon_activity_wizard", self.client.session)


class WorkflowTestCase(CarbonTestCase):
	"""Shared actors for the validation-flow tests."""

	def setUp(self):
		super().setUp()
		self.finance = Department.objects.create(company=self.company, name="Finance")
		self.employee = self._member(
			role=User.Role.SUPPLIER, email="employee@carbonpilot.test", department="Operations"
		)
		self.ops_manager = self._member(
			role=User.Role.DEPARTMENT_MANAGER,
			email="ops-manager@carbonpilot.test",
			department="Operations",
		)
		self.finance_manager = self._member(
			role=User.Role.DEPARTMENT_MANAGER,
			email="finance-manager@carbonpilot.test",
			department="Finance",
		)
		self.esg_manager = self._member(
			role=User.Role.ESG_REPORTING_MANAGER, email="esg@carbonpilot.test"
		)
		self.auditor = self._member(
			role=User.Role.AUDITOR, email="auditor@carbonpilot.test"
		)

	def _member(self, *, role, email, department="", company=None):
		return User.objects.create_user(
			email=email,
			password=self.password,
			role=role,
			status=User.Status.ACTIVE,
			company_membership=company if company is not None else self.company,
			department=department,
		)

	def _draft(self, **overrides):
		payload = {"created_by": self.employee, "department": self.department}
		payload.update(overrides)
		activity = self.make_activity(**payload)
		activity.save()
		activity.log_event(actor=activity.created_by, action=ActivityAuditAction.CREATED)
		return activity


class ValidationWorkflowTests(WorkflowTestCase):
	def _action_url(self, activity):
		return reverse("carbon:activity_action", args=[activity.pk])

	def test_submit_moves_draft_to_submitted(self):
		activity = self._draft()
		workflow.submit_activity(activity, self.employee)
		activity.refresh_from_db()
		self.assertEqual(activity.status, ActivityStatus.SUBMITTED)
		self.assertIsNotNone(activity.submitted_at)

	def test_department_manager_can_review_own_department_only(self):
		activity = self._draft(status=ActivityStatus.SUBMITTED)
		self.assertTrue(workflow.can_review(self.ops_manager, activity))
		self.assertFalse(workflow.can_review(self.finance_manager, activity))

	def test_start_review_is_logged(self):
		activity = self._draft(status=ActivityStatus.SUBMITTED)
		workflow.start_review(activity, self.ops_manager, "Checking the invoice")
		activity.refresh_from_db()
		self.assertEqual(activity.status, ActivityStatus.UNDER_REVIEW)
		self.assertEqual(activity.reviewed_by, self.ops_manager)
		self.assertTrue(activity.audit_log.filter(action=ActivityAuditAction.REVIEW_STARTED).exists())

	def test_approval_is_scoped_to_the_department_manager(self):
		activity = self._draft(status=ActivityStatus.SUBMITTED)
		# The department's own manager may approve into the official footprint.
		self.assertTrue(workflow.can_approve(self.ops_manager, activity))
		# Another department's manager may not.
		self.assertFalse(workflow.can_approve(self.finance_manager, activity))
		# Company-wide approvers can.
		self.assertTrue(workflow.can_approve(self.esg_manager, activity))
		self.assertTrue(workflow.can_approve(self.owner, activity))
		with self.assertRaises(workflow.WorkflowError):
			workflow.approve_activity(activity, self.finance_manager)

	def test_approve_sets_official_status_and_timestamps(self):
		activity = self._draft(status=ActivityStatus.SUBMITTED)
		workflow.approve_activity(activity, self.esg_manager, "Looks good")
		activity.refresh_from_db()
		self.assertEqual(activity.status, ActivityStatus.APPROVED)
		self.assertEqual(activity.approved_by, self.esg_manager)
		self.assertIsNotNone(activity.approved_at)
		self.assertEqual(activity.review_comment, "Looks good")

	def test_reject_requires_a_comment(self):
		activity = self._draft(status=ActivityStatus.SUBMITTED)
		with self.assertRaises(workflow.WorkflowError):
			workflow.reject_activity(activity, self.ops_manager, "   ")
		workflow.reject_activity(activity, self.ops_manager, "Wrong meter")
		activity.refresh_from_db()
		self.assertEqual(activity.status, ActivityStatus.REJECTED)
		self.assertEqual(activity.review_comment, "Wrong meter")

	def test_request_correction_returns_to_draft(self):
		activity = self._draft(status=ActivityStatus.UNDER_REVIEW)
		with self.assertRaises(workflow.WorkflowError):
			workflow.request_correction(activity, self.ops_manager, "")
		workflow.request_correction(activity, self.ops_manager, "Attach the invoice")
		activity.refresh_from_db()
		self.assertEqual(activity.status, ActivityStatus.DRAFT)
		self.assertTrue(
			activity.audit_log.filter(action=ActivityAuditAction.CORRECTION_REQUESTED).exists()
		)

	def test_auditor_cannot_review_or_approve(self):
		activity = self._draft(status=ActivityStatus.SUBMITTED)
		self.assertEqual(workflow.actionable(self.auditor, activity), set())
		with self.assertRaises(workflow.WorkflowError):
			workflow.start_review(activity, self.auditor)

	def test_unknown_action_is_rejected(self):
		activity = self._draft()
		with self.assertRaises(workflow.WorkflowError):
			workflow.perform("explode", activity, self.owner)

	def test_action_endpoint_enforces_permissions(self):
		activity = self._draft(status=ActivityStatus.SUBMITTED)
		# Auditors are read-only: the endpoint denies the role entirely.
		self.client.force_login(self.auditor)
		response = self.client.post(self._action_url(activity), {"action": "approve"})
		activity.refresh_from_db()
		self.assertEqual(response.status_code, 403)
		self.assertEqual(activity.status, ActivityStatus.SUBMITTED)

	def test_ops_manager_approves_own_department_via_the_endpoint(self):
		activity = self._draft(status=ActivityStatus.SUBMITTED)
		self.client.force_login(self.ops_manager)
		response = self.client.post(self._action_url(activity), {"action": "approve"})
		self.assertEqual(response.status_code, 302)
		activity.refresh_from_db()
		self.assertEqual(activity.status, ActivityStatus.APPROVED)

	def test_cross_department_action_is_not_found(self):
		activity = self._draft(status=ActivityStatus.SUBMITTED)
		self.client.force_login(self.finance_manager)
		response = self.client.post(self._action_url(activity), {"action": "approve"})
		self.assertEqual(response.status_code, 404)
		activity.refresh_from_db()
		self.assertEqual(activity.status, ActivityStatus.SUBMITTED)

	def test_action_endpoint_records_the_decision(self):
		activity = self._draft(status=ActivityStatus.SUBMITTED)
		self.client.force_login(self.esg_manager)
		response = self.client.post(
			self._action_url(activity), {"action": "approve", "comment": "Approved"}
		)
		self.assertEqual(response.status_code, 302)
		activity.refresh_from_db()
		self.assertEqual(activity.status, ActivityStatus.APPROVED)
		self.assertTrue(activity.audit_log.filter(action=ActivityAuditAction.APPROVED).exists())

	def test_audit_log_is_append_only(self):
		activity = self._draft()
		entry = activity.log_event(actor=self.employee, action=ActivityAuditAction.CREATED)
		entry.comment = "tampered"
		with self.assertRaises(ValidationError):
			entry.save()

	def test_full_chain_employee_to_approval(self):
		activity = self._draft()
		workflow.submit_activity(activity, self.employee)
		workflow.start_review(activity, self.ops_manager)
		workflow.approve_activity(activity, self.esg_manager)
		activity.refresh_from_db()
		self.assertEqual(activity.status, ActivityStatus.APPROVED)
		actions = list(
			activity.audit_log.values_list("action", flat=True)
		)
		self.assertIn(ActivityAuditAction.CREATED, actions)
		self.assertIn(ActivityAuditAction.SUBMITTED, actions)
		self.assertIn(ActivityAuditAction.REVIEW_STARTED, actions)
		self.assertIn(ActivityAuditAction.APPROVED, actions)


class DataQualityTests(WorkflowTestCase):
	def _complete_activity(self):
		verified = EmissionFactor.objects.create(
			name="Electricity — verified",
			category=ActivityCategory.ELECTRICITY,
			scope=Scope.SCOPE_2,
			value=Decimal("0.42"),
			unit="kWh",
			region="Tunisia",
			year=2026,
			source="Official grid factor",
			verification_status=VerificationStatus.VERIFIED,
		)
		activity = self.make_activity(
			emission_factor=verified,
			data_source="January invoice",
			evidence=SimpleUploadedFile("meter.pdf", b"%PDF demo"),
		)
		activity.save()
		return activity

	def test_complete_activity_scores_high(self):
		result = data_quality.score_activity(self._complete_activity())
		self.assertEqual(result["score"], 100)
		self.assertEqual(result["band"], DataQualityBand.HIGH)
		self.assertTrue(all(check["ok"] for check in result["checks"]))

	def test_missing_information_lowers_the_score(self):
		activity = self.make_activity(data_source="")
		activity.save()
		result = data_quality.score_activity(activity)
		self.assertLess(result["score"], 100)
		self.assertEqual(result["band"], DataQualityBand.LOW)
		failed = {check["key"] for check in result["checks"] if not check["ok"]}
		self.assertIn("data_source", failed)
		self.assertIn("evidence", failed)

	def test_average_score_helper(self):
		self._complete_activity()
		incomplete = self.make_activity(name="Sparse", data_source="")
		incomplete.save()
		average = data_quality.average_score(CarbonActivity.objects.all())
		self.assertTrue(0 < average < 100)


class AnalyticsTests(WorkflowTestCase):
	def setUp(self):
		super().setUp()
		self.fuel_factor = EmissionFactor.objects.create(
			name="Natural gas",
			category=ActivityCategory.NATURAL_GAS,
			scope=Scope.SCOPE_1,
			value=Decimal("2"),
			unit="m3",
			region="Tunisia",
			year=2026,
		)

	def _approved(self, **overrides):
		payload = {
			"status": ActivityStatus.APPROVED,
			"created_by": self.employee,
			"department": self.department,
		}
		payload.update(overrides)
		activity = self.make_activity(**payload)
		activity.save()
		return activity

	def test_total_uses_approved_only(self):
		self._approved()
		self.make_activity(name="Draft").save()
		self.make_activity(name="Pending", status=ActivityStatus.SUBMITTED).save()
		queryset = tenancy.visible_activities(self.owner, self.company).approved()
		self.assertEqual(analytics.total_tonnes(queryset), Decimal("2.10"))

	def test_scope_breakdown_sums_to_total(self):
		self._approved()
		self._approved(
			category=ActivityCategory.NATURAL_GAS,
			scope=Scope.SCOPE_1,
			unit="m3",
			quantity=Decimal("500"),
			emission_factor=self.fuel_factor,
		)
		queryset = tenancy.visible_activities(self.owner, self.company).approved()
		scopes = {row["key"]: row for row in analytics.scope_breakdown(queryset)}
		self.assertEqual(scopes[Scope.SCOPE_1]["tonnes"], Decimal("1.00"))
		self.assertEqual(scopes[Scope.SCOPE_2]["tonnes"], Decimal("2.10"))

	def test_category_and_department_breakdowns(self):
		self._approved()
		queryset = tenancy.visible_activities(self.owner, self.company).approved()
		categories = analytics.category_breakdown(queryset)
		departments = analytics.department_breakdown(queryset)
		self.assertEqual(categories[0]["key"], ActivityCategory.ELECTRICITY)
		self.assertEqual(departments[0]["label"], "Operations")

	def test_monthly_series_has_twelve_buckets(self):
		self._approved()
		series = analytics.monthly_series(
			tenancy.visible_activities(self.owner, self.company).approved(), 2026
		)
		self.assertEqual(len(series), 12)
		self.assertEqual(series[0]["month"], 1)
		self.assertEqual(series[0]["tonnes"], Decimal("2.10"))

	def test_year_comparison_reports_delta(self):
		self._approved()
		self._approved(period_start=date(2025, 1, 1), period_end=date(2025, 1, 31), name="2025")
		queryset = tenancy.visible_activities(self.owner, self.company).approved()
		comparison = analytics.year_comparison(queryset, 2026)
		self.assertEqual(comparison["current"], Decimal("2.10"))
		self.assertEqual(comparison["previous"], Decimal("2.10"))
		self.assertEqual(comparison["delta_percent"], 0.0)

	def test_available_years_are_derived_from_data(self):
		self._approved()
		self._approved(period_start=date(2025, 6, 1), period_end=date(2025, 6, 30), name="2025")
		queryset = tenancy.visible_activities(self.owner, self.company).approved()
		self.assertEqual(analytics.available_years(queryset, 2030), [2026, 2025])


class DashboardAndFootprintViewTests(WorkflowTestCase):
	def test_dashboard_shows_real_totals(self):
		self.make_activity(
			status=ActivityStatus.APPROVED, created_by=self.employee
		).save()
		self.client.force_login(self.owner)
		response = self.client.get(reverse("carbon:dashboard"))
		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.context["total_tonnes"], Decimal("2.10"))

	def test_dashboard_excludes_unapproved_activities(self):
		self.make_activity(name="Draft").save()
		self.client.force_login(self.owner)
		response = self.client.get(reverse("carbon:dashboard"))
		self.assertEqual(response.context["total_tonnes"], Decimal("0.00"))

	def test_footprint_filters_by_scope(self):
		self.make_activity(status=ActivityStatus.APPROVED, created_by=self.employee).save()
		self.client.force_login(self.owner)
		response = self.client.get(reverse("carbon:footprint"), {"scope": Scope.SCOPE_3})
		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.context["total_tonnes"], Decimal("0.00"))

	def test_reviewer_sees_only_actionable_items_in_queue(self):
		self.make_activity(
			status=ActivityStatus.SUBMITTED, created_by=self.employee, department=self.department
		).save()
		self.client.force_login(self.finance_manager)
		response = self.client.get(reverse("carbon:review_queue"))
		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.context["open_count"], 0)

	def test_auditor_cannot_open_the_review_queue(self):
		self.client.force_login(self.auditor)
		response = self.client.get(reverse("carbon:review_queue"))
		self.assertEqual(response.status_code, 403)


class ActivityDetailAndSecurityTests(WorkflowTestCase):
	def _detail_url(self, activity):
		return reverse("carbon:activity_detail", args=[activity.pk])

	def test_detail_page_renders_timeline(self):
		activity = self._draft()
		activity.log_event(actor=self.employee, action=ActivityAuditAction.CREATED)
		self.client.force_login(self.owner)
		response = self.client.get(self._detail_url(activity))
		self.assertEqual(response.status_code, 200)
		self.assertContains(response, "Audit timeline")
		self.assertContains(response, "Created")

	def test_department_manager_cannot_open_another_department_activity(self):
		activity = self._draft(department=self.department)
		self.client.force_login(self.finance_manager)
		response = self.client.get(self._detail_url(activity))
		self.assertEqual(response.status_code, 404)

	def test_other_company_cannot_open_the_activity(self):
		activity = self._draft()
		self.client.force_login(self.other_company.owner)
		response = self.client.get(self._detail_url(activity))
		self.assertEqual(response.status_code, 404)

	def test_supplier_only_sees_own_activities(self):
		self._draft()
		other_supplier = self._member(
			role=User.Role.SUPPLIER, email="other-supplier@carbonpilot.test", department="Operations"
		)
		self.client.force_login(other_supplier)
		response = self.client.get(reverse("carbon:activity_list"))
		self.assertEqual(response.context["pagination"]["total"], 0)

	def test_list_queryset_cannot_leak_across_tenants(self):
		self._draft()
		self.client.force_login(self.other_company.owner)
		response = self.client.get(reverse("carbon:activity_list"))
		self.assertEqual(response.context["pagination"]["total"], 0)

	def test_auditor_cannot_edit_an_activity(self):
		activity = self._draft()
		self.client.force_login(self.auditor)
		response = self.client.get(reverse("carbon:activity_edit", args=[activity.pk]))
		self.assertEqual(response.status_code, 403)

	def test_approved_activity_is_not_editable(self):
		activity = self._draft(status=ActivityStatus.APPROVED)
		self.client.force_login(self.owner)
		response = self.client.get(reverse("carbon:activity_edit", args=[activity.pk]))
		self.assertEqual(response.status_code, 302)
		self.assertIn(str(activity.pk), response.url)


class EvidenceDownloadTests(WorkflowTestCase):
	def setUp(self):
		super().setUp()
		self.media_root = tempfile.mkdtemp()
		self.addCleanup(shutil.rmtree, self.media_root, ignore_errors=True)
		self.settings_override = override_settings(MEDIA_ROOT=self.media_root)
		self.settings_override.enable()
		self.addCleanup(self.settings_override.disable)

	def _activity_with_evidence(self):
		activity = self.make_activity(
			created_by=self.employee,
			evidence=SimpleUploadedFile("invoice.pdf", b"%PDF invoice"),
		)
		activity.save()
		return activity

	def test_owner_can_download_own_company_evidence(self):
		activity = self._activity_with_evidence()
		self.client.force_login(self.owner)
		response = self.client.get(reverse("carbon:activity_evidence", args=[activity.pk]))
		self.assertEqual(response.status_code, 200)
		disposition = response["Content-Disposition"]
		self.assertTrue(disposition.startswith('attachment; filename="'))
		self.assertTrue(disposition.endswith('.pdf"'))

	def test_other_company_cannot_download_evidence(self):
		activity = self._activity_with_evidence()
		self.client.force_login(self.other_company.owner)
		response = self.client.get(reverse("carbon:activity_evidence", args=[activity.pk]))
		self.assertEqual(response.status_code, 404)

	def test_activity_without_evidence_returns_404(self):
		activity = self.make_activity(created_by=self.employee)
		activity.save()
		self.client.force_login(self.owner)
		response = self.client.get(reverse("carbon:activity_evidence", args=[activity.pk]))
		self.assertEqual(response.status_code, 404)

	def test_auditor_can_download_within_the_company(self):
		activity = self._activity_with_evidence()
		self.client.force_login(self.auditor)
		response = self.client.get(reverse("carbon:activity_evidence", args=[activity.pk]))
		self.assertEqual(response.status_code, 200)


class ActivityActionsTests(WorkflowTestCase):
	"""View / edit / delete / archive behaviour of the activities table."""

	def _url(self, name, activity):
		return reverse(f"carbon:{name}", args=[activity.pk])

	def test_list_renders_an_actions_column(self):
		self._draft()
		self.client.force_login(self.owner)
		response = self.client.get(reverse("carbon:activity_list"))
		self.assertEqual(response.status_code, 200)
		self.assertContains(response, "Actions")
		self.assertContains(response, "View")

	def test_a_draft_offers_delete_but_not_archive(self):
		activity = self._draft()
		self.client.force_login(self.owner)
		response = self.client.get(reverse("carbon:activity_list"))
		self.assertContains(response, self._url("activity_delete", activity))
		self.assertNotContains(response, self._url("activity_archive", activity))

	def test_owner_can_delete_a_draft(self):
		activity = self._draft()
		self.client.force_login(self.owner)
		response = self.client.post(self._url("activity_delete", activity))
		self.assertRedirects(response, reverse("carbon:activity_list"))
		self.assertFalse(CarbonActivity.objects.filter(pk=activity.pk).exists())

	def test_delete_requires_post(self):
		activity = self._draft()
		self.client.force_login(self.owner)
		response = self.client.get(self._url("activity_delete", activity))
		self.assertEqual(response.status_code, 405)

	def test_a_submitted_activity_cannot_be_deleted(self):
		activity = self._draft(status=ActivityStatus.SUBMITTED)
		self.client.force_login(self.owner)
		response = self.client.post(self._url("activity_delete", activity))
		self.assertEqual(response.status_code, 302)
		self.assertTrue(CarbonActivity.objects.filter(pk=activity.pk).exists())

	def test_a_submitted_activity_can_be_archived(self):
		activity = self._draft(status=ActivityStatus.SUBMITTED)
		self.client.force_login(self.owner)
		response = self.client.post(self._url("activity_archive", activity))
		self.assertEqual(response.status_code, 302)
		activity.refresh_from_db()
		self.assertTrue(activity.is_archived)
		self.assertEqual(activity.archived_by, self.owner)
		self.assertIsNotNone(activity.archived_at)
		self.assertTrue(activity.audit_log.filter(action=ActivityAuditAction.ARCHIVED).exists())

	def test_archived_activity_leaves_the_live_list_and_the_footprint(self):
		activity = self._draft(status=ActivityStatus.APPROVED)
		self.client.force_login(self.owner)
		self.client.post(self._url("activity_archive", activity))

		live = self.client.get(reverse("carbon:activity_list"))
		self.assertEqual(live.context["pagination"]["total"], 0)
		archived = self.client.get(f"{reverse('carbon:activity_list')}?archived=1")
		self.assertEqual(archived.context["pagination"]["total"], 1)
		dashboard = self.client.get(reverse("carbon:dashboard"))
		self.assertEqual(dashboard.context["total_tonnes"], Decimal("0.00"))

	def test_restore_returns_the_activity_to_the_live_inventory(self):
		activity = self._draft(status=ActivityStatus.APPROVED)
		self.client.force_login(self.owner)
		self.client.post(self._url("activity_archive", activity))
		response = self.client.post(self._url("activity_restore", activity))
		self.assertEqual(response.status_code, 302)
		activity.refresh_from_db()
		self.assertFalse(activity.is_archived)
		self.assertIsNone(activity.archived_at)
		self.assertTrue(activity.audit_log.filter(action=ActivityAuditAction.RESTORED).exists())

	def test_edit_recalculates_co2e_server_side(self):
		activity = self._draft()  # 5000 kWh × 0.42 = 2100 kg
		self.client.force_login(self.owner)
		response = self.client.post(
			self._url("activity_edit", activity),
			{
				"name": activity.name,
				"quantity": "10000",
				"unit": "kWh",
				"period_start": "2026-01-01",
				"period_end": "2026-01-31",
				"department": self.department.pk,
				"emission_factor": self.electricity_factor.pk,
				"data_source": "Meter reading",
			},
		)
		self.assertEqual(response.status_code, 302)
		activity.refresh_from_db()
		self.assertEqual(activity.quantity, Decimal("10000.0000"))
		self.assertEqual(activity.co2e_kg, Decimal("4200.0000"))

	def test_auditor_cannot_delete_or_archive(self):
		draft = self._draft()
		submitted = self._draft(status=ActivityStatus.SUBMITTED)
		self.client.force_login(self.auditor)
		self.assertEqual(self.client.post(self._url("activity_delete", draft)).status_code, 403)
		self.assertEqual(self.client.post(self._url("activity_archive", submitted)).status_code, 403)

	def test_another_company_cannot_delete_or_archive(self):
		activity = self._draft()
		self.client.force_login(self.other_company.owner)
		self.assertEqual(self.client.post(self._url("activity_delete", activity)).status_code, 404)
		self.assertEqual(self.client.post(self._url("activity_archive", activity)).status_code, 404)

	def test_department_manager_cannot_manage_another_department(self):
		activity = self._draft(department=self.department)
		self.client.force_login(self.finance_manager)
		self.assertEqual(self.client.post(self._url("activity_archive", activity)).status_code, 404)

	def test_the_reporter_can_archive_their_own_activity(self):
		activity = self._draft(status=ActivityStatus.SUBMITTED)  # reported by employee
		self.client.force_login(self.employee)
		response = self.client.post(self._url("activity_archive", activity))
		self.assertEqual(response.status_code, 302)
		activity.refresh_from_db()
		self.assertTrue(activity.is_archived)

	def test_detail_hides_the_lifecycle_card_from_read_only_roles(self):
		activity = self._draft()
		self.client.force_login(self.auditor)
		response = self.client.get(self._url("activity_detail", activity))
		self.assertEqual(response.status_code, 200)
		self.assertNotContains(response, "Activity lifecycle")


class DepartmentManagerWorkspaceTests(WorkflowTestCase):
	"""The Department Manager reviews and approves their own department only."""

	def test_review_queue_lists_only_own_department_submissions(self):
		mine = self._draft(status=ActivityStatus.SUBMITTED)
		other = self.make_activity(
			created_by=self.finance_manager,
			department=self.finance,
			status=ActivityStatus.SUBMITTED,
		)
		other.save()
		self.client.force_login(self.ops_manager)
		response = self.client.get(reverse("carbon:review_queue"))
		self.assertEqual(response.status_code, 200)
		submitted = list(response.context["submitted"])
		self.assertIn(mine, submitted)
		self.assertNotIn(other, submitted)

	def test_review_queue_is_closed_to_plain_members(self):
		self.client.force_login(self.employee)
		self.assertEqual(self.client.get(reverse("carbon:review_queue")).status_code, 403)

	def test_manager_can_approve_from_the_queue_and_feed_the_kpis(self):
		activity = self._draft(status=ActivityStatus.SUBMITTED)
		self.client.force_login(self.ops_manager)
		response = self.client.post(
			reverse("carbon:activity_action", args=[activity.pk]),
			{"action": "approve", "next": reverse("carbon:review_queue")},
		)
		self.assertRedirects(response, reverse("carbon:review_queue"))
		activity.refresh_from_db()
		self.assertEqual(activity.status, ActivityStatus.APPROVED)
		official = tenancy.visible_activities(self.owner, self.company).approved()
		self.assertIn(activity, list(official))
		dashboard = self.client.get(reverse("carbon:dashboard"))
		self.assertEqual(dashboard.context["total_tonnes"], Decimal("2.10"))

	def test_manager_can_reject_with_a_comment(self):
		activity = self._draft(status=ActivityStatus.SUBMITTED)
		self.client.force_login(self.ops_manager)
		response = self.client.post(
			reverse("carbon:activity_action", args=[activity.pk]),
			{"action": "reject", "comment": "Meter reading unclear"},
		)
		self.assertEqual(response.status_code, 302)
		activity.refresh_from_db()
		self.assertEqual(activity.status, ActivityStatus.REJECTED)
		self.assertEqual(activity.review_comment, "Meter reading unclear")

	def test_manager_dashboard_is_department_scoped(self):
		self.make_activity(status=ActivityStatus.APPROVED, created_by=self.employee).save()
		self.client.force_login(self.ops_manager)
		response = self.client.get(reverse("accounts:dashboard"))
		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.context["carbon_summary"]["total_tonnes"], Decimal("2.10"))
		self.assertContains(response, "Review queue")


class ActivityNotificationTests(WorkflowTestCase):
	"""In-app notifications for submissions and decisions."""

	def test_submission_notifies_the_department_manager(self):
		activity = self._draft()
		workflow.submit_activity(activity, self.employee)
		note = Notification.objects.get(recipient=self.ops_manager)
		self.assertEqual(note.kind, Notification.Kind.ACTIVITY_SUBMITTED)
		self.assertIn("submitted", note.title.lower())

	def test_submission_does_not_notify_other_departments(self):
		activity = self._draft()
		workflow.submit_activity(activity, self.employee)
		self.assertFalse(Notification.objects.filter(recipient=self.finance_manager).exists())

	def test_approval_notifies_the_activity_creator(self):
		activity = self._draft(status=ActivityStatus.SUBMITTED)
		workflow.approve_activity(activity, self.ops_manager)
		note = Notification.objects.get(
			recipient=self.employee, kind=Notification.Kind.ACTIVITY_APPROVED
		)
		self.assertIn("approved", note.title.lower())

	def test_rejection_notifies_the_creator_with_the_reason(self):
		activity = self._draft(status=ActivityStatus.SUBMITTED)
		workflow.reject_activity(activity, self.ops_manager, "Wrong meter")
		note = Notification.objects.get(
			recipient=self.employee, kind=Notification.Kind.ACTIVITY_REJECTED
		)
		self.assertEqual(note.message, "Wrong meter")

	def test_top_bar_bell_surfaces_the_unread_count(self):
		activity = self._draft()
		workflow.submit_activity(activity, self.employee)
		self.client.force_login(self.ops_manager)
		response = self.client.get(reverse("accounts:dashboard"))
		self.assertContains(response, "cp-notif-count")
		self.assertContains(response, "Mark all read")

	def test_mark_all_read_clears_the_unread_badge(self):
		activity = self._draft()
		workflow.submit_activity(activity, self.employee)
		self.client.force_login(self.ops_manager)
		self.client.post(reverse("accounts:notifications_mark_read"))
		self.assertFalse(
			Notification.objects.filter(recipient=self.ops_manager, is_read=False).exists()
		)


class AnomalyDetectionTestCase(CarbonTestCase):
	"""Shared fixtures for the IsolationForest anomaly-detection feature."""

	def _approved_same_group(self, quantities, **overrides):
		"""Create APPROVED activities in one group (department + category + year)."""
		activities = []
		for index, quantity in enumerate(quantities):
			payload = {
				"company": self.company,
				"department": self.department,
				"category": ActivityCategory.ELECTRICITY,
				"scope": Scope.SCOPE_2,
				"quantity": Decimal(str(quantity)),
				"unit": "kWh",
				"emission_factor": self.electricity_factor,
				"period_start": date(2026, 1, 1),
				"period_end": date(2026, 1, 31),
				"status": ActivityStatus.APPROVED,
				"created_by": self.owner,
				"name": f"Electricity reading {index}",
			}
			payload.update(overrides)
			activity = self.make_activity(**payload)
			activity.save()
			activities.append(activity)
		return activities

	def _member(self, *, role, email, department="", company=None):
		return User.objects.create_user(
			email=email,
			password=self.password,
			role=role,
			status=User.Status.ACTIVE,
			company_membership=company if company is not None else self.company,
			department=department,
		)


class AnomalyScoringTests(AnomalyDetectionTestCase):
	"""The documented, deterministic scoring rules."""

	def test_classify_type_and_severity_thresholds(self):
		classify = anomaly_detection._classify
		# Ratio above the spike threshold with a high score is a HIGH spike.
		self.assertEqual(
			classify(2.5, 0.9, False, False), (AnomalyType.SPIKE, AnomalySeverity.HIGH)
		)
		# A spike ratio with a modest score is still at least MEDIUM.
		self.assertEqual(
			classify(2.0, 0.4, False, False), (AnomalyType.SPIKE, AnomalySeverity.MEDIUM)
		)
		# A small deviation with a low score is a LOW outlier.
		self.assertEqual(
			classify(1.1, 0.2, False, False), (AnomalyType.OUTLIER, AnomalySeverity.LOW)
		)
		# A supported rising trend on the latest value is TREND_INCREASE.
		self.assertEqual(
			classify(1.2, 0.6, True, True),
			(AnomalyType.TREND_INCREASE, AnomalySeverity.MEDIUM),
		)
		# The same trend on a non-latest value stays an OUTLIER.
		self.assertEqual(
			classify(1.2, 0.6, True, False), (AnomalyType.OUTLIER, AnomalySeverity.MEDIUM)
		)

	def test_increasing_trend_detection(self):
		self.assertTrue(anomaly_detection._has_increasing_trend([1, 2, 3, 4, 5]))
		self.assertFalse(anomaly_detection._has_increasing_trend([5, 4, 3, 2, 1]))
		self.assertFalse(anomaly_detection._has_increasing_trend([2, 2, 2, 2, 2]))
		self.assertFalse(anomaly_detection._has_increasing_trend([1, 2]))


class AnomalyInputSelectionTests(AnomalyDetectionTestCase):
	"""Only approved, non-archived, same-company activities are ever analysed."""

	def test_only_approved_and_unarchived_are_selected(self):
		approved = self._approved_same_group([1000] * 9 + [50000])
		draft = self.make_activity(quantity=Decimal("90000"), name="Draft outlier")
		draft.save()
		submitted = self.make_activity(
			quantity=Decimal("90000"), status=ActivityStatus.SUBMITTED, name="Submitted"
		)
		submitted.save()
		archived = self.make_activity(
			quantity=Decimal("90000"), status=ActivityStatus.APPROVED, name="Archived"
		)
		archived.is_archived = True
		archived.save()

		selected = set(
			anomaly_detection._approved_activity_queryset(self.company)
			.values_list("pk", flat=True)
		)
		self.assertEqual(selected, {activity.pk for activity in approved})

	def test_archived_outlier_is_never_flagged(self):
		self._approved_same_group([1000] * 9 + [50000])
		archived = self.make_activity(
			quantity=Decimal("90000"), status=ActivityStatus.APPROVED, name="Archived outlier"
		)
		archived.is_archived = True
		archived.save()

		anomaly_detection.detect_anomalies(self.company)
		self.assertFalse(
			AnomalyLog.objects.filter(activity=archived).exists()
		)

	def test_detection_never_reads_another_companys_activities(self):
		self._approved_same_group([1000] * 9 + [50000])
		# A different tenant's data is never part of the analysed queryset.
		foreign = set(
			anomaly_detection._approved_activity_queryset(self.other_company)
			.values_list("pk", flat=True)
		)
		self.assertEqual(foreign, set())


class AnomalyDetectionServiceTests(AnomalyDetectionTestCase):
	"""End-to-end behaviour of the IsolationForest service."""

	def test_insufficient_data_creates_no_false_anomalies(self):
		self._approved_same_group([1000, 1200, 50000])  # only 3 observations
		result = anomaly_detection.detect_anomalies(self.company, actor=self.owner)
		self.assertTrue(result.insufficient)
		self.assertEqual(result.message, "Insufficient historical data for anomaly detection.")
		self.assertEqual(result.detected, 0)
		self.assertFalse(AnomalyLog.objects.exists())

	def test_isolation_forest_flags_the_clear_outlier(self):
		activities = self._approved_same_group([1000] * 9 + [50000])
		outlier = activities[-1]
		result = anomaly_detection.detect_anomalies(self.company, actor=self.owner)
		self.assertGreaterEqual(result.groups_analyzed, 1)
		log = AnomalyLog.objects.get(activity=outlier)
		self.assertEqual(log.company, self.company)
		self.assertEqual(log.department, self.department)
		self.assertEqual(log.severity, AnomalySeverity.HIGH)
		self.assertEqual(log.anomaly_type, AnomalyType.SPIKE)
		self.assertEqual(log.status, AnomalyStatus.NEW)
		self.assertEqual(log.detected_by, self.owner)
		self.assertIsNotNone(log.period_start)
		self.assertIn("historical median", log.message)
		# The normal members of the cluster are not turned into spikes.
		normal_logs = AnomalyLog.objects.exclude(activity=outlier)
		self.assertFalse(
			normal_logs.filter(severity=AnomalySeverity.HIGH).exists()
		)

	def test_detection_is_deterministic(self):
		self._approved_same_group([1000] * 9 + [50000])
		first = anomaly_detection.detect_anomalies(self.company, actor=self.owner)
		first_scores = dict(
			AnomalyLog.objects.values_list("activity_id", "score")
		)
		second = anomaly_detection.detect_anomalies(self.company, actor=self.owner)
		second_scores = dict(
			AnomalyLog.objects.values_list("activity_id", "score")
		)
		self.assertEqual(first_scores, second_scores)
		self.assertEqual(first.detected, second.detected)

	def test_repeated_runs_do_not_duplicate_logs(self):
		self._approved_same_group([1000] * 9 + [50000])
		anomaly_detection.detect_anomalies(self.company, actor=self.owner)
		count = AnomalyLog.objects.count()
		self.assertGreater(count, 0)
		result = anomaly_detection.detect_anomalies(self.company, actor=self.owner)
		self.assertEqual(AnomalyLog.objects.count(), count)
		self.assertEqual(result.created, 0)
		self.assertEqual(result.updated, count)
		# Fingerprints are unique per (company, activity, period).
		self.assertEqual(
			AnomalyLog.objects.values("fingerprint").distinct().count(), count
		)

	def test_reviewer_decision_survives_a_rerun(self):
		activities = self._approved_same_group([1000] * 9 + [50000])
		outlier = activities[-1]
		anomaly_detection.detect_anomalies(self.company, actor=self.owner)
		log = AnomalyLog.objects.get(activity=outlier)
		log.status = AnomalyStatus.REVIEWED
		log.save(update_fields=("status", "updated_at"))
		anomaly_detection.detect_anomalies(self.company, actor=self.owner)
		log.refresh_from_db()
		self.assertEqual(log.status, AnomalyStatus.REVIEWED)

	def test_anomaly_counts_aggregate_by_severity_and_status(self):
		self._approved_same_group([1000] * 9 + [50000])
		anomaly_detection.detect_anomalies(self.company, actor=self.owner)
		counts = anomaly_detection.anomaly_counts(AnomalyLog.objects.for_company(self.company))
		self.assertEqual(counts["total"], AnomalyLog.objects.count())
		self.assertEqual(counts["high"], AnomalyLog.objects.filter(severity=AnomalySeverity.HIGH).count())
		self.assertGreaterEqual(counts["new"], 1)


class AnomalyTenantIsolationTests(AnomalyDetectionTestCase):
	"""Cross-tenant access is impossible, server-side."""

	def test_detection_is_scoped_to_the_selected_company(self):
		self._approved_same_group([1000] * 9 + [50000])
		anomaly_detection.detect_anomalies(self.company, actor=self.owner)
		# Running detection for another tenant never produces rows for it here.
		anomaly_detection.detect_anomalies(self.other_company, actor=self.other_company.owner)
		self.assertFalse(
			AnomalyLog.objects.filter(company=self.other_company).exists()
		)

	def test_other_company_cannot_view_anomaly_list(self):
		self._approved_same_group([1000] * 9 + [50000])
		anomaly_detection.detect_anomalies(self.company, actor=self.owner)
		self.client.force_login(self.other_company.owner)
		response = self.client.get(reverse("carbon:anomaly_list"))
		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.context["pagination"]["total"], 0)

	def test_other_company_cannot_open_an_anomaly_by_id(self):
		self._approved_same_group([1000] * 9 + [50000])
		anomaly_detection.detect_anomalies(self.company, actor=self.owner)
		log = AnomalyLog.objects.first()
		self.client.force_login(self.other_company.owner)
		response = self.client.get(reverse("carbon:anomaly_detail", args=[log.pk]))
		self.assertEqual(response.status_code, 404)

	def test_department_manager_only_sees_own_department_anomalies(self):
		finance = Department.objects.create(company=self.company, name="Finance")
		manager = self._member(
			role=User.Role.DEPARTMENT_MANAGER,
			email="dm-anomaly@carbonpilot.test",
			department="Finance",
		)
		# Anomalies belong to the Operations department; the Finance DM sees none.
		self._approved_same_group([1000] * 9 + [50000])
		anomaly_detection.detect_anomalies(self.company, actor=self.owner)
		self.assertTrue(AnomalyLog.objects.exists())
		self.client.force_login(manager)
		response = self.client.get(reverse("carbon:anomaly_list"))
		self.assertEqual(response.context["pagination"]["total"], 0)
		log = AnomalyLog.objects.first()
		self.assertEqual(
			self.client.get(reverse("carbon:anomaly_detail", args=[log.pk])).status_code, 404
		)
		self.assertTrue(finance)


class AnomalyViewAndPermissionTests(AnomalyDetectionTestCase):
	"""The run/list/detail/status endpoints enforce login and roles."""

	def test_login_is_required(self):
		response = self.client.get(reverse("carbon:anomaly_list"))
		self.assertEqual(response.status_code, 302)
		self.assertIn("login", response.url)

	def test_auditor_cannot_run_detection(self):
		auditor = self._member(role=User.Role.AUDITOR, email="anomaly-auditor@carbonpilot.test")
		self.client.force_login(auditor)
		response = self.client.post(reverse("carbon:anomaly_run"))
		self.assertEqual(response.status_code, 403)

	def test_department_manager_cannot_run_detection(self):
		manager = self._member(
			role=User.Role.DEPARTMENT_MANAGER,
			email="anomaly-dm@carbonpilot.test",
			department="Operations",
		)
		self.client.force_login(manager)
		response = self.client.post(reverse("carbon:anomaly_run"))
		self.assertEqual(response.status_code, 403)

	def test_supplier_cannot_run_detection(self):
		supplier = self._member(role=User.Role.SUPPLIER, email="anomaly-supplier@carbonpilot.test")
		self.client.force_login(supplier)
		response = self.client.post(reverse("carbon:anomaly_run"))
		self.assertEqual(response.status_code, 403)

	def test_owner_runs_detection_and_sees_the_count(self):
		self._approved_same_group([1000] * 9 + [50000])
		self.client.force_login(self.owner)
		response = self.client.post(reverse("carbon:anomaly_run"), follow=True)
		self.assertRedirects(response, reverse("carbon:anomaly_list"))
		self.assertTrue(AnomalyLog.objects.exists())
		# The success message names how many anomalies were detected.
		texts = [str(message) for message in response.context["messages"]]
		self.assertTrue(any("detected" in text for text in texts), texts)

	def test_run_detection_requires_post(self):
		self.client.force_login(self.owner)
		response = self.client.get(reverse("carbon:anomaly_run"))
		self.assertEqual(response.status_code, 405)

	def test_list_shows_severity_counts_and_detail_link(self):
		self._approved_same_group([1000] * 9 + [50000])
		anomaly_detection.detect_anomalies(self.company, actor=self.owner)
		log = AnomalyLog.objects.first()
		self.client.force_login(self.owner)
		response = self.client.get(reverse("carbon:anomaly_list"))
		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.context["counts"]["total"], AnomalyLog.objects.count())
		self.assertContains(response, reverse("carbon:anomaly_detail", args=[log.pk]))

	def test_detail_respects_status_permissions(self):
		self._approved_same_group([1000] * 9 + [50000])
		anomaly_detection.detect_anomalies(self.company, actor=self.owner)
		log = AnomalyLog.objects.first()
		auditor = self._member(role=User.Role.AUDITOR, email="status-auditor@carbonpilot.test")

		self.client.force_login(self.owner)
		response = self.client.post(
			reverse("carbon:anomaly_status", args=[log.pk]), {"status": AnomalyStatus.REVIEWED}
		)
		self.assertEqual(response.status_code, 302)
		log.refresh_from_db()
		self.assertEqual(log.status, AnomalyStatus.REVIEWED)

		# An auditor may view but not triage.
		self.client.force_login(auditor)
		response = self.client.post(
			reverse("carbon:anomaly_status", args=[log.pk]), {"status": AnomalyStatus.DISMISSED}
		)
		self.assertEqual(response.status_code, 403)

	def test_status_rejects_an_unknown_value(self):
		self._approved_same_group([1000] * 9 + [50000])
		anomaly_detection.detect_anomalies(self.company, actor=self.owner)
		log = AnomalyLog.objects.first()
		self.client.force_login(self.owner)
		self.client.post(reverse("carbon:anomaly_status", args=[log.pk]), {"status": "BOGUS"})
		log.refresh_from_db()
		self.assertEqual(log.status, AnomalyStatus.NEW)

	def test_detail_page_renders(self):
		self._approved_same_group([1000] * 9 + [50000])
		anomaly_detection.detect_anomalies(self.company, actor=self.owner)
		log = AnomalyLog.objects.first()
		self.client.force_login(self.owner)
		response = self.client.get(reverse("carbon:anomaly_detail", args=[log.pk]))
		self.assertEqual(response.status_code, 200)
		self.assertContains(response, "What the model observed")
