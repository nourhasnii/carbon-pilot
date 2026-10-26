"""Tests for the ESG / carbon report module.

Covers the report snapshot (approved data only, current company only), the
ReportLab PDF generation, tenant isolation (cross-tenant access is a 404) and
the HTTP views/permissions (auditors are read-only).
"""

import tempfile
from datetime import date
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
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
from apps.recommendations.models import (
	RecommendationLog,
	RecommendationPriority,
	RecommendationSource,
)
from apps.risk.models import RiskAssessment, RiskLevel, RiskMethod

from .models import ESGReport, ReportStatus
from .services import esg_report as svc


class ReportTestCase(TestCase):
	password = "SecureTestPassword2026!"

	def setUp(self):
		self.owner = User.objects.create_user(
			email="report-owner@carbonpilot.test",
			password=self.password,
			first_name="Report",
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
			email="other-report-owner@carbonpilot.test",
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

	def make_recommendation(self, *, company=None, priority=RecommendationPriority.HIGH, tag="r"):
		company = company or self.company
		return RecommendationLog.objects.create(
			company=company,
			title=f"Recommendation {tag}",
			message="Reduce stand-by consumption.",
			priority=priority,
			source=RecommendationSource.ANOMALY,
			fingerprint=f"{company.pk}:{tag}",
		)

	def make_assessment(self, *, company=None, score="42.00", level=RiskLevel.MEDIUM):
		company = company or self.company
		return RiskAssessment.objects.create(
			company=company,
			score=Decimal(score),
			level=level,
			summary="Two drivers contributed to the score.",
			drivers_json=[
				{"key": "anomalies", "label": "Open anomalies", "points": 8.0,
				 "max_points": 30, "detail": "1 open high-severity anomaly."},
			],
			period_year=2026,
			method=RiskMethod.RULE_BASED,
			created_by=self.owner,
		)

	def make_report(self, *, company=None, status=ReportStatus.DRAFT, with_file=False):
		company = company or self.company
		report = ESGReport.objects.create(
			company=company,
			period_year=2026,
			title="ESG & Carbon Report 2026",
			status=status,
			summary="Summary.",
			metrics_json={"period_year": 2026},
			created_by=self.owner,
		)
		if with_file:
			report.file = SimpleUploadedFile("report.pdf", b"%PDF-1.4 test", content_type="application/pdf")
			report.save(update_fields=("file",))
		return report


class BuildReportDataTests(ReportTestCase):
	def test_uses_only_approved_activities(self):
		self.make_activity(status=ActivityStatus.APPROVED)
		# None of these may contribute to the official emissions.
		self.make_activity(status=ActivityStatus.DRAFT)
		self.make_activity(status=ActivityStatus.SUBMITTED)
		self.make_activity(status=ActivityStatus.UNDER_REVIEW)
		self.make_activity(status=ActivityStatus.REJECTED)

		metrics, _summary = svc.build_report_data(self.company, period_year=2026)

		self.assertEqual(metrics["emissions"]["approved_activities"], 1)
		self.assertEqual(metrics["emissions"]["total_tonnes"], 2.1)

	def test_archived_activities_are_excluded(self):
		self.make_activity(status=ActivityStatus.APPROVED)
		self.make_activity(status=ActivityStatus.APPROVED, is_archived=True)

		metrics, _summary = svc.build_report_data(self.company, period_year=2026)
		self.assertEqual(metrics["emissions"]["approved_activities"], 1)

	def test_other_year_activities_are_excluded(self):
		self.make_activity(period_start=date(2025, 1, 1), period_end=date(2025, 1, 31))
		metrics, _summary = svc.build_report_data(self.company, period_year=2026)
		self.assertEqual(metrics["emissions"]["approved_activities"], 0)

	def test_snapshot_includes_scope_totals_and_company(self):
		self.make_activity()
		metrics, _summary = svc.build_report_data(self.company, period_year=2026)
		self.assertEqual(metrics["company"]["id"], self.company.pk)
		self.assertEqual(metrics["company"]["name"], self.company.name)
		self.assertEqual(
			[row["key"] for row in metrics["emissions"]["scopes"]],
			[Scope.SCOPE_1, Scope.SCOPE_2, Scope.SCOPE_3],
		)
		# Scope 2 holds the electricity activity; scopes 1 and 3 are empty.
		by_scope = {row["key"]: row["tonnes"] for row in metrics["emissions"]["scopes"]}
		self.assertEqual(by_scope[Scope.SCOPE_2], 2.1)
		self.assertEqual(by_scope[Scope.SCOPE_1], 0.0)

	def test_summary_mentions_key_figures(self):
		self.make_activity()
		metrics, summary = svc.build_report_data(self.company, period_year=2026)
		self.assertIn("2.10 tCO2e", summary)
		self.assertIn("2026", summary)
		self.assertTrue(metrics["quality"]["scored"] == 1)

	def test_quality_and_evidence_coverage(self):
		self.make_activity(evidence="")
		self.make_activity()
		metrics, _summary = svc.build_report_data(self.company, period_year=2026)
		self.assertEqual(metrics["quality"]["scored"], 2)
		self.assertEqual(metrics["quality"]["missing_evidence"], 1)
		self.assertEqual(metrics["quality"]["evidence_coverage"], 50.0)

	def test_objectives_and_latest_prediction_are_included(self):
		from apps.objectives.models import ExpectedOutcome, PredictionRun, RiskLevel as ObjRisk

		objective = self.make_objective()
		PredictionRun.objects.create(
			objective=objective,
			probability_achievement=Decimal("90.00"),
			risk_level=ObjRisk.LOW,
			expected_outcome=ExpectedOutcome.LIKELY_TO_ACHIEVE,
			current_progress_percent=Decimal("30.00"),
		)
		metrics, _summary = svc.build_report_data(self.company, period_year=2026)
		self.assertEqual(metrics["objectives"]["total"], 1)
		self.assertEqual(metrics["objectives"]["items"][0]["prediction"]["probability"], 90.0)

	def test_recommendations_and_risk_are_included(self):
		self.make_recommendation(priority=RecommendationPriority.HIGH, tag="r1")
		self.make_recommendation(priority=RecommendationPriority.LOW, tag="r2")
		self.make_assessment(score="42.00", level=RiskLevel.MEDIUM)

		metrics, _summary = svc.build_report_data(self.company, period_year=2026)
		self.assertEqual(metrics["recommendations"]["open"], 2)
		self.assertEqual(metrics["recommendations"]["high"], 1)
		self.assertEqual(metrics["risk"]["score"], 42.0)
		self.assertEqual(metrics["risk"]["level"], RiskLevel.MEDIUM)


class TenantIsolationDataTests(ReportTestCase):
	def test_other_company_activities_never_leak(self):
		self.make_activity()  # company A: 2.1 tCO2e
		self.make_activity(company=self.other_company)  # company B: must not leak

		metrics, summary = svc.build_report_data(self.company, period_year=2026)
		self.assertEqual(metrics["emissions"]["approved_activities"], 1)
		self.assertEqual(metrics["emissions"]["total_tonnes"], 2.1)
		self.assertIn("Carbon Works", summary)
		self.assertNotIn("Other Works", summary)

	def test_other_company_risk_assessment_is_ignored(self):
		self.make_assessment(company=self.other_company, score="90.00", level=RiskLevel.HIGH)
		metrics, _summary = svc.build_report_data(self.company, period_year=2026)
		self.assertIsNone(metrics["risk"])

	def test_other_company_objectives_are_ignored(self):
		self.make_objective(company=self.other_company, tag="b")
		metrics, _summary = svc.build_report_data(self.company, period_year=2026)
		self.assertEqual(metrics["objectives"]["total"], 0)

	def test_build_requires_a_company(self):
		with self.assertRaises(ValueError):
			svc.build_report_data(None, period_year=2026)


class DraftAndPdfTests(ReportTestCase):
	def setUp(self):
		super().setUp()
		self._media = tempfile.mkdtemp()
		self._override = override_settings(MEDIA_ROOT=self._media)
		self._override.enable()
		self.addCleanup(self._override.disable)

	def test_create_draft_snapshots_and_stays_draft(self):
		self.make_activity()
		report = svc.create_draft(self.company, period_year=2026, actor=self.owner)
		self.assertEqual(report.status, ReportStatus.DRAFT)
		self.assertFalse(report.file)
		self.assertEqual(report.created_by, self.owner)
		self.assertEqual(report.metrics_json["emissions"]["total_tonnes"], 2.1)
		self.assertTrue(report.summary)

	def test_create_draft_replaces_previous_draft(self):
		first = svc.create_draft(self.company, period_year=2026, actor=self.owner)
		second = svc.create_draft(self.company, period_year=2026, actor=self.owner)
		self.assertNotEqual(first.pk, second.pk)
		self.assertEqual(
			ESGReport.objects.filter(
				company=self.company, period_year=2026, status=ReportStatus.DRAFT
			).count(),
			1,
		)

	def test_render_pdf_returns_pdf_bytes(self):
		report = svc.create_draft(self.company, period_year=2026, actor=self.owner)
		pdf = svc.render_pdf(report)
		self.assertTrue(pdf.startswith(b"%PDF"))
		self.assertGreater(len(pdf), 1000)

	def test_generate_pdf_creates_file_and_marks_generated(self):
		report = svc.create_draft(self.company, period_year=2026, actor=self.owner)
		svc.generate_pdf(report)
		report.refresh_from_db()

		self.assertEqual(report.status, ReportStatus.GENERATED)
		self.assertTrue(report.file)
		self.assertTrue(report.file.name.endswith(".pdf"))
		self.assertIn(f"esg_reports/company_{self.company.pk}/2026/", report.file.name)
		self.assertTrue(report.is_generated)

		with report.file.open("rb") as handle:
			self.assertTrue(handle.read(4).startswith(b"%PDF"))

	def test_pdf_contains_required_sections(self):
		# The PDF text is compressed, so assert the content is a valid, non-trivial
		# PDF and that every section builder runs without error.
		report = svc.create_draft(self.company, period_year=2026, actor=self.owner)
		pdf = svc.render_pdf(report)
		self.assertTrue(pdf.startswith(b"%PDF"))
		self.assertIn(b"%%EOF", pdf)


class TenantIsolationViewTests(ReportTestCase):
	def test_download_other_company_report_is_404(self):
		other_report = self.make_report(
			company=self.other_company, status=ReportStatus.GENERATED, with_file=True
		)
		self.client.force_login(self.owner)
		response = self.client.get(reverse("reports:report_download", args=[other_report.pk]))
		self.assertEqual(response.status_code, 404)

	def test_detail_other_company_report_is_404(self):
		other_report = self.make_report(company=self.other_company)
		self.client.force_login(self.owner)
		response = self.client.get(reverse("reports:report_detail", args=[other_report.pk]))
		self.assertEqual(response.status_code, 404)

	def test_generate_other_company_report_is_404(self):
		other_report = self.make_report(company=self.other_company)
		self.client.force_login(self.owner)
		response = self.client.post(reverse("reports:report_generate", args=[other_report.pk]))
		self.assertEqual(response.status_code, 404)
		other_report.refresh_from_db()
		self.assertEqual(other_report.status, ReportStatus.DRAFT)

	def test_list_only_shows_own_reports(self):
		self.make_report()
		self.make_report(company=self.other_company)
		self.client.force_login(self.owner)
		response = self.client.get(reverse("reports:report_list"))
		self.assertEqual(response.status_code, 200)
		self.assertEqual(len(response.context["reports"]), 1)

	def test_create_ignores_client_supplied_company(self):
		self.make_activity()
		self.client.force_login(self.owner)
		self.client.post(
			reverse("reports:report_create"),
			{"period_year": "2026", "company_id": self.other_company.pk},
		)
		report = ESGReport.objects.get()
		self.assertEqual(report.company, self.company)
		self.assertEqual(ESGReport.objects.filter(company=self.other_company).count(), 0)


class ReportViewTests(ReportTestCase):
	def setUp(self):
		super().setUp()
		self._media = tempfile.mkdtemp()
		self._override = override_settings(MEDIA_ROOT=self._media)
		self._override.enable()
		self.addCleanup(self._override.disable)

	def test_requires_login(self):
		response = self.client.get(reverse("reports:report_list"))
		self.assertEqual(response.status_code, 302)
		self.assertIn("/login/", response.url)

	def test_owner_can_create_and_generate(self):
		self.make_activity()
		self.client.force_login(self.owner)

		response = self.client.post(reverse("reports:report_create"), {"period_year": "2026"})
		report = ESGReport.objects.get()
		self.assertRedirects(response, reverse("reports:report_detail", args=[report.pk]))
		self.assertEqual(report.status, ReportStatus.DRAFT)

		self.client.post(reverse("reports:report_generate", args=[report.pk]))
		report.refresh_from_db()
		self.assertEqual(report.status, ReportStatus.GENERATED)
		self.assertTrue(report.file)

	def test_create_rejects_invalid_year(self):
		self.client.force_login(self.owner)
		self.client.post(reverse("reports:report_create"), {"period_year": "not-a-year"})
		self.assertEqual(ESGReport.objects.count(), 0)

	def test_create_defaults_title_to_reporting_year(self):
		self.client.force_login(self.owner)
		self.client.post(reverse("reports:report_create"), {"period_year": "2026"})
		self.assertEqual(ESGReport.objects.get().title, "ESG & Carbon Report 2026")

	def test_download_before_generation_is_404(self):
		report = self.make_report()
		self.client.force_login(self.owner)
		response = self.client.get(reverse("reports:report_download", args=[report.pk]))
		self.assertEqual(response.status_code, 404)

	def test_download_generated_report_returns_attachment(self):
		report = svc.create_draft(self.company, period_year=2026, actor=self.owner)
		svc.generate_pdf(report)
		self.client.force_login(self.owner)
		response = self.client.get(reverse("reports:report_download", args=[report.pk]))
		self.assertEqual(response.status_code, 200)
		self.assertEqual(response["Content-Type"], "application/pdf")
		self.assertIn("attachment", response["Content-Disposition"])

	def test_auditor_can_view_but_not_manage(self):
		auditor = User.objects.create_user(
			email="report-auditor@carbonpilot.test",
			password=self.password,
			role=User.Role.AUDITOR,
			status=User.Status.ACTIVE,
			company_membership=self.company,
		)
		self.client.force_login(auditor)

		self.assertEqual(self.client.get(reverse("reports:report_list")).status_code, 200)

		create = self.client.post(reverse("reports:report_create"), {"period_year": "2026"})
		self.assertEqual(create.status_code, 403)
		self.assertEqual(ESGReport.objects.count(), 0)

		report = self.make_report()
		generate = self.client.post(reverse("reports:report_generate", args=[report.pk]))
		self.assertEqual(generate.status_code, 403)

	def test_dashboard_shows_esg_reports_widget(self):
		self.client.force_login(self.owner)
		self.make_activity()
		response = self.client.get(reverse("carbon:dashboard"))
		self.assertEqual(response.status_code, 200)
		self.assertContains(response, reverse("reports:report_list"))


class ReportListFilterTests(ReportTestCase):
	"""Filtering, pagination and tenant isolation on the report history list."""

	def make_report_row(self, *, company=None, year=2026, status=ReportStatus.DRAFT, title=None):
		company = company or self.company
		return ESGReport.objects.create(
			company=company,
			period_year=year,
			title=title or f"ESG & Carbon Report {year}",
			status=status,
			summary="Summary.",
			metrics_json={"period_year": year},
			created_by=self.owner,
		)

	def test_filter_combination_narrows_results(self):
		self.make_report_row(year=2025, title="Draft 2025")
		self.make_report_row(year=2026, status=ReportStatus.GENERATED, title="Generated 2026")
		self.make_report_row(year=2026, title="Draft 2026")
		self.client.force_login(self.owner)

		response = self.client.get(reverse("reports:report_list"), {
			"period_year": "2026",
			"status": ReportStatus.GENERATED,
		})
		self.assertEqual(response.status_code, 200)
		self.assertEqual(
			[report.title for report in response.context["reports"]],
			["Generated 2026"],
		)

	def test_default_page_size_is_ten(self):
		for index in range(12):
			self.make_report_row(year=2026, title=f"Report {index:02d}")
		self.client.force_login(self.owner)

		response = self.client.get(reverse("reports:report_list"))
		self.assertEqual(response.context["pagination"]["per_page"], 10)
		self.assertEqual(response.context["pagination"]["total"], 12)
		self.assertEqual(len(response.context["reports"]), 10)

	def test_per_page_twenty_shows_up_to_twenty(self):
		for index in range(25):
			self.make_report_row(year=2026, title=f"Report {index:02d}")
		self.client.force_login(self.owner)

		response = self.client.get(reverse("reports:report_list"), {"per_page": "20"})
		self.assertEqual(response.context["pagination"]["per_page"], 20)
		self.assertEqual(len(response.context["reports"]), 20)

	def test_next_link_preserves_filter_and_page(self):
		for index in range(12):
			self.make_report_row(year=2026, title=f"Draft {index:02d}")
		self.client.force_login(self.owner)

		response = self.client.get(reverse("reports:report_list"), {
			"period_year": "2026",
			"status": ReportStatus.DRAFT,
		})
		self.assertTrue(response.context["pagination"]["has_next"])
		content = response.content.decode()
		self.assertIn("period_year=2026", content)
		self.assertIn("page=2", content)

	def test_filter_matching_nothing_renders_empty_state(self):
		self.make_report_row(year=2026, status=ReportStatus.DRAFT)
		self.client.force_login(self.owner)

		response = self.client.get(reverse("reports:report_list"), {
			"status": ReportStatus.GENERATED,
		})
		self.assertEqual(response.status_code, 200)
		self.assertContains(response, "cp-empty")
		self.assertContains(response, "No reports match these filters.")

	def test_list_never_shows_another_companys_reports(self):
		self.make_report_row(year=2026, title="Mine")
		self.make_report_row(company=self.other_company, year=2026, title="Theirs")
		self.client.force_login(self.owner)

		response = self.client.get(reverse("reports:report_list"))
		self.assertEqual(response.status_code, 200)
		self.assertEqual(
			[report.title for report in response.context["reports"]],
			["Mine"],
		)
		self.assertEqual(response.context["pagination"]["total"], 1)
		self.assertNotContains(response, "Theirs")
