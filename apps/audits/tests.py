"""Tests for the internal audit & findings manager.

The suite covers the module's contract: model integrity, the finding and
remediation workflows, overdue escalation, the dashboard aggregation, strict
tenant isolation (cross-company access is a 404, never a 403) and the role
matrix (Company Owner / ESG manage everything, a Department Manager only their
own department, auditors read-only).
"""

import json
import re
import shutil
import tempfile
from datetime import timedelta
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.enterprise.models import Company, Department
from apps.governance.models import Policy, PolicyCategory
from apps.recommendations.services.ollama_client import OllamaResult

from . import ai
from .forms import AuditForm
from .models import (
	Audit,
	AuditEvidence,
	AuditStatus,
	AuditType,
	Finding,
	FindingSeverity,
	FindingStatus,
	FindingTheme,
	RemediationPlan,
	RemediationStatus,
)
from .services import (
	can_manage_audit,
	can_manage_finding,
	dashboard_counts,
	escalated_findings,
	severity_breakdown,
	transition_audit,
	transition_finding,
	transition_remediation,
	user_department,
	visible_audits,
	visible_findings,
)


class AuditFixtureMixin:
	"""Shared fixtures: one company with departments and one other tenant."""

	password = "SecureAuditPassword2026!"

	def setUp(self):
		self.owner = User.objects.create_user(
			email="audit-owner@carbonpilot.test",
			password=self.password,
			role=User.Role.COMPANY_OWNER,
			status=User.Status.ACTIVE,
		)
		self.company = Company.objects.create(owner=self.owner, name="Audit Works")
		self.operations = Department.objects.create(
			company=self.company, name="Operations"
		)
		self.finance = Department.objects.create(company=self.company, name="Finance")

		self.esg_manager = self._member(
			role=User.Role.ESG_REPORTING_MANAGER, email="audit-esg@carbonpilot.test"
		)
		self.ops_manager = self._member(
			role=User.Role.DEPARTMENT_MANAGER,
			email="audit-ops-manager@carbonpilot.test",
			department="Operations",
		)
		self.finance_manager = self._member(
			role=User.Role.DEPARTMENT_MANAGER,
			email="audit-finance-manager@carbonpilot.test",
			department="Finance",
		)
		self.auditor = self._member(
			role=User.Role.AUDITOR, email="audit-readonly@carbonpilot.test"
		)

		# A second, fully isolated tenant.
		self.other_owner = User.objects.create_user(
			email="audit-other-owner@carbonpilot.test",
			password=self.password,
			role=User.Role.COMPANY_OWNER,
			status=User.Status.ACTIVE,
		)
		self.other_company = Company.objects.create(
			owner=self.other_owner, name="Other Audit Works"
		)
		self.other_audit = Audit.objects.create(
			company=self.other_company,
			title="Other tenant audit",
			start_date=timezone.localdate(),
			created_by=self.other_owner,
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

	def _policy(self, **overrides):
		payload = {
			"company": self.company,
			"title": "Supplier Code of Conduct",
			"category": PolicyCategory.GOVERNANCE,
			"created_by": self.owner,
		}
		payload.update(overrides)
		return Policy.objects.create(**payload)

	def _audit(self, **overrides):
		payload = {
			"company": self.company,
			"title": "Q3 internal control review",
			"audit_type": AuditType.INTERNAL,
			"start_date": timezone.localdate(),
			"created_by": self.owner,
		}
		payload.update(overrides)
		return Audit.objects.create(**payload)

	def _finding(self, audit=None, **overrides):
		payload = {
			"audit": audit or self._audit(),
			"title": "Missing documented approval",
			"severity": FindingSeverity.MEDIUM,
		}
		payload.update(overrides)
		payload.setdefault("company", payload["audit"].company)
		return Finding.objects.create(**payload)


class AuditModelTests(AuditFixtureMixin, TestCase):
	def test_audit_defaults_and_reference(self):
		audit = self._audit()
		self.assertEqual(audit.status, AuditStatus.PLANNED)
		self.assertTrue(audit.is_open)
		self.assertFalse(audit.is_completed)
		self.assertEqual(audit.reference, f"AUD-{audit.pk:04d}")

	def test_audit_clean_rejects_cross_company_department(self):
		foreign = Department.objects.create(company=self.other_company, name="Foreign")
		audit = Audit(
			company=self.company,
			title="Bad scope",
			start_date=timezone.localdate(),
			department=foreign,
		)
		with self.assertRaises(ValidationError):
			audit.full_clean()

	def test_audit_clean_rejects_end_before_start(self):
		today = timezone.localdate()
		audit = Audit(
			company=self.company,
			title="Bad dates",
			start_date=today,
			end_date=today - timedelta(days=1),
		)
		with self.assertRaises(ValidationError):
			audit.full_clean()

	def test_finding_company_is_pinned_from_audit(self):
		finding = self._finding(company=self.other_company)
		self.assertEqual(finding.company_id, finding.audit.company_id)

	def test_closing_a_finding_stamps_closed_at(self):
		finding = self._finding()
		self.assertIsNone(finding.closed_at)
		finding.status = FindingStatus.CLOSED
		finding.save()
		finding.refresh_from_db()
		self.assertIsNotNone(finding.closed_at)
		self.assertTrue(finding.is_closed)

	def test_reopening_a_finding_clears_closed_at(self):
		finding = self._finding()
		finding.status = FindingStatus.CLOSED
		finding.save()
		finding.status = FindingStatus.IN_PROGRESS
		finding.save()
		finding.refresh_from_db()
		self.assertIsNone(finding.closed_at)

	def test_finding_clean_rejects_cross_company_department(self):
		finding = Finding(
			audit=self._audit(),
			company=self.company,
			title="Bad department",
			department=Department.objects.create(
				company=self.other_company, name="Foreign"
			),
		)
		with self.assertRaises(ValidationError):
			finding.full_clean()

	def test_overdue_finding_flags_escalation(self):
		finding = self._finding(
			due_date=timezone.localdate() - timedelta(days=5),
			status=FindingStatus.OPEN,
		)
		self.assertTrue(finding.is_overdue)
		self.assertTrue(finding.escalated)
		self.assertEqual(finding.days_overdue, 5)

	def test_closed_finding_is_never_overdue(self):
		finding = self._finding(
			due_date=timezone.localdate() - timedelta(days=5),
			status=FindingStatus.CLOSED,
		)
		self.assertFalse(finding.is_overdue)
		self.assertFalse(finding.escalated)
		self.assertEqual(finding.days_overdue, 0)

	def test_finding_without_due_date_is_not_overdue(self):
		finding = self._finding()
		self.assertFalse(finding.is_overdue)

	def test_remediation_company_and_completion_are_pinned(self):
		finding = self._finding()
		plan = RemediationPlan.objects.create(
			company=self.other_company,
			finding=finding,
			title="Document the approval",
			status=RemediationStatus.DONE,
		)
		self.assertEqual(plan.company_id, finding.company_id)
		self.assertIsNotNone(plan.completed_at)
		plan.refresh_from_db()
		self.assertTrue(plan.is_done)

	def test_remediation_reopen_clears_completed_at(self):
		finding = self._finding()
		plan = RemediationPlan.objects.create(
			company=finding.company,
			finding=finding,
			title="Action",
			status=RemediationStatus.DONE,
		)
		plan.status = RemediationStatus.IN_PROGRESS
		plan.save()
		plan.refresh_from_db()
		self.assertIsNone(plan.completed_at)

	def test_evidence_derives_company_and_audit_from_finding(self):
		finding = self._finding()
		evidence = AuditEvidence.objects.create(
			company=self.other_company,
			finding=finding,
			title="Signed email",
			file=SimpleUploadedFile("note.txt", b"hello", content_type="text/plain"),
		)
		self.assertEqual(evidence.company_id, finding.company_id)
		self.assertEqual(evidence.audit_id, finding.audit_id)

	def test_evidence_requires_an_audit_or_a_finding(self):
		evidence = AuditEvidence(company=self.company, title="Orphan")
		with self.assertRaises(ValidationError):
			evidence.full_clean()


class AuditWorkflowTests(AuditFixtureMixin, TestCase):
	def test_audit_transitions_forward_only(self):
		audit = self._audit()
		transition_audit(audit, AuditStatus.IN_PROGRESS)
		self.assertEqual(audit.status, AuditStatus.IN_PROGRESS)
		transition_audit(audit, AuditStatus.COMPLETED)
		self.assertEqual(audit.status, AuditStatus.COMPLETED)
		# Completing stamps the end date.
		self.assertIsNotNone(audit.end_date)
		with self.assertRaises(ValidationError):
			transition_audit(audit, AuditStatus.IN_PROGRESS)

	def test_planned_audit_cannot_skip_to_completed(self):
		audit = self._audit()
		with self.assertRaises(ValidationError):
			transition_audit(audit, AuditStatus.COMPLETED)

	def test_finding_open_to_in_progress_to_closed(self):
		finding = self._finding()
		transition_finding(finding, FindingStatus.IN_PROGRESS)
		self.assertEqual(finding.status, FindingStatus.IN_PROGRESS)
		transition_finding(finding, FindingStatus.CLOSED)
		finding.refresh_from_db()
		self.assertEqual(finding.status, FindingStatus.CLOSED)
		self.assertIsNotNone(finding.closed_at)

	def test_closed_finding_cannot_reopen(self):
		finding = self._finding(status=FindingStatus.CLOSED)
		with self.assertRaises(ValidationError):
			transition_finding(finding, FindingStatus.OPEN)

	def test_finding_same_status_is_rejected(self):
		finding = self._finding()
		with self.assertRaises(ValidationError):
			transition_finding(finding, FindingStatus.OPEN)

	def test_remediation_planned_to_in_progress_to_done(self):
		finding = self._finding()
		plan = RemediationPlan.objects.create(
			company=finding.company, finding=finding, title="Action"
		)
		transition_remediation(plan, RemediationStatus.IN_PROGRESS)
		transition_remediation(plan, RemediationStatus.DONE)
		plan.refresh_from_db()
		self.assertEqual(plan.status, RemediationStatus.DONE)
		self.assertIsNotNone(plan.completed_at)

	def test_remediation_cannot_restart_after_done(self):
		finding = self._finding()
		plan = RemediationPlan.objects.create(
			company=finding.company,
			finding=finding,
			title="Action",
			status=RemediationStatus.DONE,
		)
		with self.assertRaises(ValidationError):
			transition_remediation(plan, RemediationStatus.PLANNED)


class AuditAggregationTests(AuditFixtureMixin, TestCase):
	def test_dashboard_counts_and_severity_breakdown(self):
		audit = self._audit()
		today = timezone.localdate()
		Finding.objects.create(
			company=self.company,
			audit=audit,
			title="Critical overdue",
			severity=FindingSeverity.CRITICAL,
			due_date=today - timedelta(days=3),
		)
		Finding.objects.create(
			company=self.company,
			audit=audit,
			title="High",
			severity=FindingSeverity.HIGH,
		)
		Finding.objects.create(
			company=self.company,
			audit=audit,
			title="Closed low",
			severity=FindingSeverity.LOW,
			status=FindingStatus.CLOSED,
		)
		# A finding from the other tenant must never be counted.
		other = Audit.objects.create(
			company=self.other_company,
			title="Other",
			start_date=today,
		)
		Finding.objects.create(
			company=self.other_company,
			audit=other,
			title="Foreign critical",
			severity=FindingSeverity.CRITICAL,
		)

		counts = dashboard_counts(self.company)
		self.assertEqual(counts["audits_total"], 1)
		self.assertEqual(counts["findings_total"], 3)
		self.assertEqual(counts["open_findings"], 2)
		self.assertEqual(counts["overdue_findings"], 1)
		self.assertEqual(counts["high_critical_findings"], 2)
		self.assertEqual(counts["critical_findings"], 1)
		self.assertEqual(counts["high_findings"], 1)
		self.assertEqual(counts["closed_findings"], 1)

		severity = {row["value"]: row["count"] for row in severity_breakdown(self.company)}
		self.assertEqual(severity[FindingSeverity.CRITICAL], 1)
		self.assertEqual(severity[FindingSeverity.HIGH], 1)
		self.assertEqual(severity[FindingSeverity.LOW], 0)

	def test_escalated_findings_are_company_scoped_and_sorted(self):
		audit = self._audit()
		today = timezone.localdate()
		most_overdue = Finding.objects.create(
			company=self.company,
			audit=audit,
			title="Oldest",
			due_date=today - timedelta(days=10),
		)
		recent = Finding.objects.create(
			company=self.company,
			audit=audit,
			title="Recent",
			due_date=today - timedelta(days=2),
		)
		rows = escalated_findings(self.company)
		self.assertEqual([row.pk for row in rows], [most_overdue.pk, recent.pk])

	def test_visible_audits_scoped_to_company(self):
		mine = self._audit()
		rows = list(visible_audits(self.owner, self.company))
		self.assertEqual(rows, [mine])

	def test_visible_findings_scoped_to_company(self):
		finding = self._finding()
		rows = list(visible_findings(self.owner, self.company))
		self.assertEqual(rows, [finding])

	def test_user_department_resolves_by_name(self):
		self.assertEqual(user_department(self.company, self.ops_manager), self.operations)
		self.assertIsNone(user_department(self.company, self.auditor))


class AuditPermissionTests(AuditFixtureMixin, TestCase):
	def test_company_admin_manages_everything(self):
		self.assertTrue(can_manage_audit(self.owner, self.company))
		self.assertTrue(can_manage_audit(self.esg_manager, self.company))
		self.assertTrue(can_manage_finding(self.esg_manager, self.company))

	def test_auditor_is_read_only(self):
		self.assertFalse(can_manage_audit(self.auditor, self.company))
		self.assertFalse(can_manage_finding(self.auditor, self.company))

	def test_department_manager_scoped_to_own_department(self):
		own = self._finding(department=self.operations)
		foreign = self._finding(department=self.finance)
		self.assertTrue(can_manage_finding(self.ops_manager, self.company, own))
		self.assertFalse(can_manage_finding(self.ops_manager, self.company, foreign))
		# A department manager may raise new findings against their department.
		self.assertTrue(can_manage_finding(self.ops_manager, self.company))
		# But not administer engagements.
		self.assertFalse(can_manage_audit(self.ops_manager, self.company))

	def test_department_manager_without_department_cannot_manage(self):
		manager = self._member(
			role=User.Role.DEPARTMENT_MANAGER, email="audit-nodep@carbonpilot.test"
		)
		self.assertFalse(can_manage_finding(manager, self.company))

	def test_finding_owner_can_manage_even_outside_department(self):
		finding = self._finding(department=self.finance, owner=self.ops_manager)
		self.assertTrue(can_manage_finding(self.ops_manager, self.company, finding))


class AuditViewAccessTests(AuditFixtureMixin, TestCase):
	def test_anonymous_is_redirected_to_login(self):
		response = self.client.get(reverse("audits:audit_list"))
		self.assertEqual(response.status_code, 302)
		self.assertIn("/login", response.url)

	def test_supplier_role_is_denied(self):
		supplier = self._member(
			role=User.Role.SUPPLIER, email="audit-supplier@carbonpilot.test"
		)
		self.client.force_login(supplier)
		response = self.client.get(reverse("audits:audit_list"))
		self.assertEqual(response.status_code, 403)

	def test_owner_sees_only_their_company_audits(self):
		mine = self._audit(title="Mine")
		self.client.force_login(self.owner)
		response = self.client.get(reverse("audits:audit_list"))
		self.assertEqual(response.status_code, 200)
		self.assertContains(response, mine.title)
		self.assertNotContains(response, self.other_audit.title)

	def test_sidebar_exposes_internal_audit(self):
		self.client.force_login(self.owner)
		response = self.client.get(reverse("audits:audit_list"))
		labels = [item["label"] for item in response.context["sidebar_nav"]]
		self.assertIn("Internal audit", labels)

	def test_cross_tenant_audit_detail_is_404(self):
		self.client.force_login(self.owner)
		response = self.client.get(
			reverse("audits:audit_detail", args=[self.other_audit.pk])
		)
		self.assertEqual(response.status_code, 404)

	def test_cross_tenant_finding_detail_is_404(self):
		foreign_finding = Finding.objects.create(
			company=self.other_company,
			audit=self.other_audit,
			title="Foreign finding",
		)
		self.client.force_login(self.owner)
		response = self.client.get(
			reverse("audits:finding_detail", args=[foreign_finding.pk])
		)
		self.assertEqual(response.status_code, 404)


class AuditViewCrudTests(AuditFixtureMixin, TestCase):
	def test_owner_creates_audit_with_company_from_session(self):
		self.client.force_login(self.owner)
		response = self.client.post(
			reverse("audits:audit_create"),
			{
				"title": "New compliance audit",
				"audit_type": AuditType.COMPLIANCE,
				"scope_summary": "Review supplier onboarding.",
				"start_date": timezone.localdate().isoformat(),
				# A forged company id must be ignored: the form has no such field.
				"company": self.other_company.pk,
			},
		)
		self.assertEqual(response.status_code, 302)
		audit = Audit.objects.get(title="New compliance audit")
		self.assertEqual(audit.company_id, self.company.pk)
		self.assertEqual(audit.created_by_id, self.owner.pk)
		self.assertEqual(audit.status, AuditStatus.PLANNED)

	def test_department_manager_cannot_create_audit(self):
		self.client.force_login(self.ops_manager)
		response = self.client.get(reverse("audits:audit_create"))
		self.assertEqual(response.status_code, 403)

	def test_auditor_cannot_create_audit(self):
		self.client.force_login(self.auditor)
		response = self.client.get(reverse("audits:audit_create"))
		self.assertEqual(response.status_code, 403)

	def test_owner_creates_finding_on_audit(self):
		audit = self._audit()
		self.client.force_login(self.owner)
		response = self.client.post(
			reverse("audits:finding_create", args=[audit.pk]),
			{
				"title": "Undocumented approval",
				"description": "No approval on file.",
				"severity": FindingSeverity.HIGH,
				"due_date": (timezone.localdate() + timedelta(days=7)).isoformat(),
				"company": self.other_company.pk,
			},
		)
		self.assertEqual(response.status_code, 302)
		finding = Finding.objects.get(title="Undocumented approval")
		self.assertEqual(finding.company_id, self.company.pk)
		self.assertEqual(finding.audit_id, audit.pk)
		self.assertEqual(finding.status, FindingStatus.OPEN)

	def test_department_manager_finding_is_confined_to_own_department(self):
		audit = self._audit()
		self.client.force_login(self.ops_manager)
		response = self.client.post(
			reverse("audits:finding_create", args=[audit.pk]),
			{
				"title": "Ops gap",
				"severity": FindingSeverity.MEDIUM,
				# Attempting to file against another department must be ignored.
				"department": self.finance.pk,
			},
		)
		self.assertEqual(response.status_code, 302)
		finding = Finding.objects.get(title="Ops gap")
		self.assertEqual(finding.department_id, self.operations.pk)

	def test_finding_status_change_closes_and_stamps(self):
		finding = self._finding()
		self.client.force_login(self.owner)
		response = self.client.post(
			reverse("audits:finding_set_status", args=[finding.pk]),
			{"status": FindingStatus.CLOSED},
		)
		self.assertEqual(response.status_code, 302)
		finding.refresh_from_db()
		self.assertEqual(finding.status, FindingStatus.CLOSED)
		self.assertIsNotNone(finding.closed_at)

	def test_invalid_finding_transition_is_reported_not_applied(self):
		finding = self._finding(status=FindingStatus.CLOSED)
		self.client.force_login(self.owner)
		response = self.client.post(
			reverse("audits:finding_set_status", args=[finding.pk]),
			{"status": FindingStatus.OPEN},
			follow=True,
		)
		self.assertEqual(response.status_code, 200)
		finding.refresh_from_db()
		self.assertEqual(finding.status, FindingStatus.CLOSED)

	def test_auditor_cannot_change_finding_status(self):
		finding = self._finding()
		self.client.force_login(self.auditor)
		response = self.client.post(
			reverse("audits:finding_set_status", args=[finding.pk]),
			{"status": FindingStatus.CLOSED},
		)
		self.assertEqual(response.status_code, 403)

	def test_department_manager_cannot_touch_other_department_finding(self):
		finding = self._finding(department=self.finance)
		self.client.force_login(self.ops_manager)
		response = self.client.post(
			reverse("audits:finding_set_status", args=[finding.pk]),
			{"status": FindingStatus.IN_PROGRESS},
		)
		self.assertEqual(response.status_code, 403)

	def test_remediation_workflow_through_views(self):
		finding = self._finding()
		self.client.force_login(self.owner)
		create = self.client.post(
			reverse("audits:remediation_create", args=[finding.pk]),
			{"title": "Retrain the buyer"},
		)
		self.assertEqual(create.status_code, 302)
		plan = RemediationPlan.objects.get(finding=finding)
		self.assertEqual(plan.company_id, self.company.pk)
		self.assertEqual(plan.status, RemediationStatus.PLANNED)

		self.client.post(
			reverse("audits:remediation_set_status", args=[plan.pk]),
			{"status": RemediationStatus.IN_PROGRESS},
		)
		self.client.post(
			reverse("audits:remediation_set_status", args=[plan.pk]),
			{"status": RemediationStatus.DONE},
		)
		plan.refresh_from_db()
		self.assertEqual(plan.status, RemediationStatus.DONE)
		self.assertIsNotNone(plan.completed_at)


class AuditViewRenderTests(AuditFixtureMixin, TestCase):
	"""Every page must render for an authorised user without template errors."""

	def test_audit_pages_render(self):
		audit = self._audit(department=self.operations, related_policy=self._policy())
		finding = self._finding(
			audit=audit,
			department=self.operations,
			owner=self.owner,
			due_date=timezone.localdate() + timedelta(days=3),
		)
		plan = RemediationPlan.objects.create(
			company=self.company, finding=finding, title="Action"
		)
		pages = [
			reverse("audits:audit_list"),
			reverse("audits:audit_create"),
			reverse("audits:audit_detail", args=[audit.pk]),
			reverse("audits:audit_edit", args=[audit.pk]),
			reverse("audits:finding_create", args=[audit.pk]),
			reverse("audits:finding_detail", args=[finding.pk]),
			reverse("audits:finding_edit", args=[finding.pk]),
			reverse("audits:remediation_create", args=[finding.pk]),
			reverse("audits:remediation_edit", args=[plan.pk]),
		]
		self.client.force_login(self.owner)
		for url in pages:
			with self.subTest(url=url):
				response = self.client.get(url)
				self.assertEqual(response.status_code, 200)

	def test_overdue_finding_renders_escalation_badge(self):
		audit = self._audit()
		finding = self._finding(
			audit=audit, due_date=timezone.localdate() - timedelta(days=4)
		)
		self.client.force_login(self.owner)
		response = self.client.get(reverse("audits:finding_detail", args=[finding.pk]))
		self.assertEqual(response.status_code, 200)
		self.assertContains(response, "Escalated")


@override_settings()
class AuditEvidenceUploadTests(AuditFixtureMixin, TestCase):
	def setUp(self):
		super().setUp()
		media_root = tempfile.mkdtemp(prefix="audit-evidence-tests-")
		override = override_settings(MEDIA_ROOT=media_root)
		override.enable()
		self.addCleanup(override.disable)
		self.addCleanup(shutil.rmtree, media_root, True)

	def _upload(self, finding, name="evidence.txt"):
		return self.client.post(
			reverse("audits:finding_evidence_upload", args=[finding.pk]),
			{
				"title": "Signed approval",
				"file": SimpleUploadedFile(
					name, b"approval content", content_type="text/plain"
				),
			},
		)

	def test_owner_uploads_finding_evidence(self):
		finding = self._finding()
		self.client.force_login(self.owner)
		response = self._upload(finding)
		self.assertEqual(response.status_code, 302)
		evidence = AuditEvidence.objects.get(finding=finding)
		self.assertEqual(evidence.company_id, self.company.pk)
		self.assertEqual(evidence.audit_id, finding.audit_id)
		self.assertEqual(evidence.uploaded_by_id, self.owner.pk)
		self.assertTrue(evidence.file.name.startswith(f"audits/company_{self.company.pk}/"))

	def test_download_is_scoped_to_company(self):
		finding = self._finding()
		self.client.force_login(self.owner)
		self._upload(finding)
		evidence = AuditEvidence.objects.get(finding=finding)
		response = self.client.get(
			reverse("audits:evidence_download", args=[evidence.pk])
		)
		self.assertEqual(response.status_code, 200)
		disposition = response["Content-Disposition"]
		self.assertTrue(disposition.startswith("attachment;"))
		self.assertTrue(disposition.endswith('.txt"'))

	def test_cross_tenant_evidence_download_is_404(self):
		foreign_finding = Finding.objects.create(
			company=self.other_company,
			audit=self.other_audit,
			title="Foreign",
		)
		foreign = AuditEvidence.objects.create(
			company=self.other_company,
			audit=self.other_audit,
			finding=foreign_finding,
			title="Foreign evidence",
			file=SimpleUploadedFile("f.txt", b"secret", content_type="text/plain"),
		)
		self.client.force_login(self.owner)
		response = self.client.get(
			reverse("audits:evidence_download", args=[foreign.pk])
		)
		self.assertEqual(response.status_code, 404)

	def test_auditor_cannot_upload_evidence(self):
		finding = self._finding()
		self.client.force_login(self.auditor)
		response = self._upload(finding)
		self.assertEqual(response.status_code, 403)
		self.assertFalse(AuditEvidence.objects.filter(finding=finding).exists())


class AuditAiContextTests(AuditFixtureMixin, TestCase):
	"""The facts Django hands to Qwen are always one company's own rows."""

	def test_build_context_is_tenant_scoped(self):
		audit = self._audit()
		self._finding(audit=audit, title="Our data gap")
		Finding.objects.create(
			company=self.other_company, audit=self.other_audit, title="Foreign finding"
		)

		context = ai.build_context(self.company)

		self.assertEqual(context["company"]["id"], self.company.pk)
		self.assertEqual([row["title"] for row in context["findings"]], ["Our data gap"])

	def test_build_context_without_company_is_none(self):
		self.assertIsNone(ai.build_context(None))

	def test_theme_prompt_only_contains_this_company_facts(self):
		audit = self._audit()
		self._finding(audit=audit, title="Our evidence gap")
		Finding.objects.create(
			company=self.other_company, audit=self.other_audit, title="Foreign finding"
		)
		prompt = ai.build_theme_prompt(ai.build_context(self.company))
		self.assertIn("Our evidence gap", prompt)
		self.assertNotIn("Foreign finding", prompt)

	def test_closing_context_counts_the_engagement(self):
		audit = self._audit()
		self._finding(
			audit=audit,
			severity=FindingSeverity.HIGH,
			due_date=timezone.localdate() - timedelta(days=2),
		)
		self._finding(audit=audit, title="Closed", status=FindingStatus.CLOSED)

		context = ai.build_closing_context(self.company, audit)

		self.assertEqual(context["audit"]["audit_id"], audit.pk)
		self.assertEqual(context["findings"]["total"], 2)
		self.assertEqual(context["findings"]["closed"], 1)
		self.assertEqual(context["findings"]["overdue"], 1)
		self.assertEqual(context["findings"]["high_or_critical"], 1)


class AuditAiValidationTests(AuditFixtureMixin, TestCase):
	"""Model values are validated before they ever reach the UI."""

	def test_valid_classification_is_cleaned_and_labelled(self):
		item = ai.validate_classification(
			{
				"finding_id": "7",
				"theme": "evidence_control",
				"confidence": "high",
				"reason": "  Evidence is missing for this control.  ",
			},
			{7},
		)
		self.assertEqual(item["finding_id"], 7)
		self.assertEqual(item["theme"], FindingTheme.EVIDENCE_CONTROL)
		self.assertEqual(item["theme_label"], "Evidence control")
		self.assertEqual(item["confidence"], "HIGH")
		self.assertEqual(item["reason"], "Evidence is missing for this control.")

	def test_unknown_theme_is_rejected(self):
		self.assertIsNone(
			ai.validate_classification(
				{
					"finding_id": 1,
					"theme": "NOT_A_THEME",
					"confidence": "HIGH",
					"reason": "Long enough reason here.",
				},
				{1},
			)
		)

	def test_unknown_confidence_is_rejected(self):
		self.assertIsNone(
			ai.validate_classification(
				{
					"finding_id": 1,
					"theme": "DATA_QUALITY",
					"confidence": "CERTAIN",
					"reason": "Long enough reason here.",
				},
				{1},
			)
		)

	def test_finding_id_from_another_company_is_rejected(self):
		# Only ids this company owns are accepted, so a foreign row can never appear.
		self.assertIsNone(
			ai.validate_classification(
				{
					"finding_id": 999999,
					"theme": "DATA_QUALITY",
					"confidence": "HIGH",
					"reason": "Long enough reason here.",
				},
				{1, 2, 3},
			)
		)

	def test_duplicate_classifications_are_collapsed(self):
		items = [
			{
				"finding_id": 1,
				"theme": "DATA_QUALITY",
				"confidence": "HIGH",
				"reason": "First classification here.",
			},
			{
				"finding_id": 1,
				"theme": "OTHER",
				"confidence": "LOW",
				"reason": "Duplicate classification here.",
			},
		]
		cleaned, rejected = ai.validate_classifications(items, {1})
		self.assertEqual(len(cleaned), 1)
		self.assertEqual(rejected, 1)

	def test_delay_explanations_reject_unknown_ids(self):
		cleaned, rejected = ai.validate_delay_explanations(
			[
				{"finding_id": 1, "reasons": ["The due date has passed."]},
				{"finding_id": 999, "reasons": ["Foreign finding."]},
			],
			[1],
		)
		self.assertEqual([row["finding_id"] for row in cleaned], [1])
		self.assertEqual(rejected, 1)

	def test_closing_note_requires_a_summary(self):
		self.assertIsNone(ai.validate_closing_note({"executive_summary": "short"}))
		self.assertIsNone(ai.validate_closing_note("not a dict"))

	def test_closing_note_is_cleaned(self):
		note = ai.validate_closing_note(
			{
				"closing_note_title": "Closing",
				"executive_summary": "The engagement completed with two findings.",
				"key_findings": ["A", "B"],
				"remediation_status": "One action is done.",
				"residual_risks": ["One overdue finding"],
				"conclusion": "Close the audit.",
			}
		)
		self.assertEqual(note["closing_note_title"], "Closing")
		self.assertEqual(note["key_findings"], ["A", "B"])
		self.assertEqual(note["residual_risks"], ["One overdue finding"])

	def test_invalid_json_returns_none(self):
		self.assertIsNone(ai.parse_classifications("not json at all"))
		self.assertIsNone(ai.parse_delay_explanations("not json at all"))
		self.assertIsNone(ai.parse_closing_note("not json at all"))

	def test_embedded_json_is_recovered(self):
		self.assertEqual(
			ai.parse_classifications('Sure: {"classifications": []} done'), []
		)
		self.assertEqual(
			ai.parse_closing_note('Here {"executive_summary": "ok ok ok"}'),
			{"executive_summary": "ok ok ok"},
		)


class AuditAiDelayRiskTests(AuditFixtureMixin, TestCase):
	"""The delay-risk rules are deterministic and never fabricate a metric."""

	def test_overdue_open_finding_is_high_risk(self):
		finding = self._finding(due_date=timezone.localdate() - timedelta(days=3))
		entry = ai.score_delay_risk(finding)
		self.assertEqual(entry["finding_id"], finding.pk)
		self.assertEqual(entry["delay_risk"], "HIGH")
		self.assertEqual(entry["method"], "RULE_BASED")
		self.assertTrue(entry["reasons"])

	def test_finding_due_soon_is_medium_risk(self):
		finding = self._finding(due_date=timezone.localdate() + timedelta(days=5))
		entry = ai.score_delay_risk(finding)
		self.assertEqual(entry["delay_risk"], "MEDIUM")

	def test_finding_with_no_due_date_and_no_plan_is_low_risk(self):
		finding = self._finding()
		entry = ai.score_delay_risk(finding)
		self.assertEqual(entry["delay_risk"], "LOW")

	def test_started_remediation_is_not_penalised(self):
		finding = self._finding(due_date=timezone.localdate() + timedelta(days=10))
		RemediationPlan.objects.create(
			company=self.company,
			finding=finding,
			title="Action",
			status=RemediationStatus.IN_PROGRESS,
		)
		entry = ai.score_delay_risk(finding)
		self.assertEqual(entry["delay_risk"], "LOW")

	def test_closed_finding_has_no_delay_risk(self):
		finding = self._finding(status=FindingStatus.CLOSED)
		self.assertIsNone(ai.score_delay_risk(finding))

	def test_batch_risk_is_keyed_by_finding_id(self):
		first = self._finding(due_date=timezone.localdate() - timedelta(days=1))
		second = self._finding(title="Second")
		rows = ai.delay_risk_for_findings([first, second])
		self.assertEqual(set(rows), {first.pk, second.pk})


class AuditAiAnalysisTests(AuditFixtureMixin, TestCase):
	"""``run_analysis`` degrades cleanly and never raises."""

	def test_no_findings_returns_a_message(self):
		result = ai.run_analysis(self.other_company)
		self.assertTrue(result.ok)
		self.assertTrue(result.ai_available)
		self.assertEqual(result.message, ai.NO_FINDINGS_MESSAGE)

	def test_ollama_unavailable_falls_back_with_a_message(self):
		self._finding()
		failure = OllamaResult(
			ok=False, error="URLError", error_category="network_or_request_error"
		)
		with patch.object(ai.ollama_client, "generate", return_value=failure):
			result = ai.run_analysis(self.company)
		self.assertTrue(result.ok)
		self.assertFalse(result.ai_available)
		self.assertEqual(result.message, ai.UNAVAILABLE_MESSAGE)
		self.assertEqual(result.classifications, [])

	def test_invalid_json_from_model_is_handled(self):
		self._finding()
		reply = OllamaResult(ok=True, text="I am not JSON")
		with patch.object(ai.ollama_client, "generate", return_value=reply):
			result = ai.run_analysis(self.company)
		self.assertTrue(result.ok)
		self.assertFalse(result.ai_available)
		self.assertEqual(result.message, ai.INVALID_RESPONSE_MESSAGE)

	def test_a_raising_client_never_breaks_the_analysis(self):
		self._finding()
		with patch.object(
			ai.ollama_client, "generate", side_effect=RuntimeError("boom")
		):
			result = ai.run_analysis(self.company)
		self.assertTrue(result.ok)
		self.assertFalse(result.ai_available)
		self.assertEqual(result.message, ai.UNAVAILABLE_MESSAGE)

	def test_successful_analysis_rejects_foreign_ids_and_marks_hybrid(self):
		finding = self._finding(due_date=timezone.localdate() - timedelta(days=3))
		theme = OllamaResult(
			ok=True,
			text=json.dumps(
				{
					"classifications": [
						{
							"finding_id": finding.pk,
							"theme": "EVIDENCE_CONTROL",
							"confidence": "HIGH",
							"reason": "Evidence is missing for this control.",
						},
						{
							"finding_id": 999999,
							"theme": "DATA_QUALITY",
							"confidence": "LOW",
							"reason": "A finding the company does not own.",
						},
					]
				}
			),
		)
		delay = OllamaResult(
			ok=True,
			text=json.dumps(
				{
					"explanations": [
						{
							"finding_id": finding.pk,
							"reasons": ["The due date has already passed."],
						}
					]
				}
			),
		)
		with patch.object(ai.ollama_client, "generate", side_effect=[theme, delay]):
			result = ai.run_analysis(self.company)

		self.assertTrue(result.ai_available, result.message)
		self.assertEqual(len(result.classifications), 1)
		self.assertEqual(
			result.classifications[0]["theme"], FindingTheme.EVIDENCE_CONTROL
		)
		self.assertEqual(result.rejected, 1)
		self.assertEqual(len(result.delay_risk), 1)
		self.assertEqual(result.delay_risk[0]["delay_risk"], "HIGH")
		self.assertEqual(result.delay_risk[0]["method"], "HYBRID")

	def test_delay_explanation_failure_keeps_rule_based_risk(self):
		finding = self._finding(due_date=timezone.localdate() + timedelta(days=5))
		theme = OllamaResult(
			ok=True,
			text=json.dumps(
				{
					"classifications": [
						{
							"finding_id": finding.pk,
							"theme": "OTHER",
							"confidence": "LOW",
							"reason": "Cannot be determined from the facts.",
						}
					]
				}
			),
		)
		failure = OllamaResult(ok=False, error="TimeoutError", error_category="timeout")
		with patch.object(ai.ollama_client, "generate", side_effect=[theme, failure]):
			result = ai.run_analysis(self.company)
		self.assertTrue(result.ai_available)
		self.assertEqual(result.delay_risk[0]["method"], "RULE_BASED")
		self.assertIn("could not be retrieved", result.message)


class AuditAiClosingNoteTests(AuditFixtureMixin, TestCase):
	"""The closing note is drafted from real counts and degrades cleanly."""

	def _note_reply(self, **overrides):
		payload = {
			"closing_note_title": "Q3 closing note",
			"executive_summary": "The engagement completed with one finding.",
			"key_findings": ["A missing approval was raised."],
			"remediation_status": "Remediation is in progress.",
			"residual_risks": ["One finding remains open."],
			"conclusion": "Close the engagement with follow-up.",
		}
		payload.update(overrides)
		return OllamaResult(ok=True, text=json.dumps(payload))

	def test_ollama_unavailable_falls_back(self):
		audit = self._audit()
		failure = OllamaResult(
			ok=False, error="URLError", error_category="network_or_request_error"
		)
		with patch.object(ai.ollama_client, "generate", return_value=failure):
			note = ai.run_closing_note(self.company, audit)
		self.assertTrue(note.ok)
		self.assertFalse(note.ai_available)
		self.assertEqual(note.message, ai.UNAVAILABLE_MESSAGE)
		self.assertEqual(note.note, {})

	def test_invalid_json_is_handled(self):
		audit = self._audit()
		reply = OllamaResult(ok=True, text="not json")
		with patch.object(ai.ollama_client, "generate", return_value=reply):
			note = ai.run_closing_note(self.company, audit)
		self.assertFalse(note.ai_available)
		self.assertEqual(note.message, ai.INVALID_RESPONSE_MESSAGE)

	def test_successful_closing_note_is_validated(self):
		audit = self._audit()
		with patch.object(ai.ollama_client, "generate", return_value=self._note_reply()):
			note = ai.run_closing_note(self.company, audit)
		self.assertTrue(note.ai_available)
		self.assertEqual(note.audit_id, audit.pk)
		self.assertEqual(note.note["key_findings"], ["A missing approval was raised."])

	def test_a_foreign_audit_is_rejected(self):
		# Defensive: an audit from another tenant can never be used.
		note = ai.run_closing_note(self.company, self.other_audit)
		self.assertFalse(note.ok)
		self.assertFalse(note.ai_available)

	def test_none_company_or_audit_is_handled(self):
		self.assertFalse(ai.run_closing_note(None, None).ok)
		self.assertFalse(ai.run_closing_note(self.company, None).ok)


class AuditAiViewTests(AuditFixtureMixin, TestCase):
	"""The AI endpoints keep tenant isolation and never crash the pages."""

	def _theme_reply(self, finding, theme="EVIDENCE_CONTROL", confidence="HIGH"):
		return OllamaResult(
			ok=True,
			text=json.dumps(
				{
					"classifications": [
						{
							"finding_id": finding.pk,
							"theme": theme,
							"confidence": confidence,
							"reason": "Evidence is missing for this control.",
						}
					]
				}
			),
		)

	def _delay_reply(self, finding):
		return OllamaResult(
			ok=True,
			text=json.dumps(
				{
					"explanations": [
						{
							"finding_id": finding.pk,
							"reasons": ["The due date has already passed."],
						}
					]
				}
			),
		)

	def test_ai_analysis_requires_post(self):
		self.client.force_login(self.owner)
		response = self.client.get(reverse("audits:audit_ai_analysis"))
		self.assertEqual(response.status_code, 405)

	def test_auditor_cannot_trigger_the_analysis(self):
		self.client.force_login(self.auditor)
		response = self.client.post(reverse("audits:audit_ai_analysis"))
		self.assertEqual(response.status_code, 403)

	def test_anonymous_user_is_redirected_to_login(self):
		response = self.client.post(reverse("audits:audit_ai_analysis"))
		self.assertEqual(response.status_code, 302)
		self.assertIn("/login", response.url)

	def test_owner_runs_analysis_and_sees_the_cards(self):
		finding = self._finding(due_date=timezone.localdate() - timedelta(days=2))
		self.client.force_login(self.owner)
		with patch.object(
			ai.ollama_client,
			"generate",
			side_effect=[self._theme_reply(finding), self._delay_reply(finding)],
		):
			response = self.client.post(
				reverse("audits:audit_ai_analysis"), follow=True
			)
		self.assertRedirects(response, reverse("audits:audit_list"))
		self.assertContains(response, "AI · QWEN")
		self.assertContains(response, "Evidence control")
		self.assertContains(response, finding.title)

	def test_ollama_down_shows_a_fallback_message(self):
		self._finding()
		failure = OllamaResult(
			ok=False, error="URLError", error_category="network_or_request_error"
		)
		self.client.force_login(self.owner)
		with patch.object(ai.ollama_client, "generate", return_value=failure):
			response = self.client.post(
				reverse("audits:audit_ai_analysis"), follow=True
			)
		self.assertEqual(response.status_code, 200)
		self.assertContains(response, "unavailable")

	def test_analysis_is_not_shown_to_another_tenant(self):
		finding = self._finding()
		self.client.force_login(self.owner)
		with patch.object(
			ai.ollama_client,
			"generate",
			side_effect=[self._theme_reply(finding), self._delay_reply(finding)],
		):
			self.client.post(reverse("audits:audit_ai_analysis"))

		# The same browser session now acts as the owner of another company.
		self.client.force_login(self.other_owner)
		response = self.client.get(reverse("audits:audit_list"))
		self.assertEqual(response.status_code, 200)
		self.assertNotContains(response, "Evidence control")

	def test_detail_button_returns_to_the_audit(self):
		audit = self._audit()
		finding = self._finding(audit=audit)
		self.client.force_login(self.owner)
		with patch.object(
			ai.ollama_client,
			"generate",
			side_effect=[self._theme_reply(finding), self._delay_reply(finding)],
		):
			response = self.client.post(
				reverse("audits:audit_ai_analysis"), {"next_audit": audit.pk}
			)
		self.assertRedirects(response, reverse("audits:audit_detail", args=[audit.pk]))

	def test_finding_detail_shows_rule_based_delay_risk(self):
		finding = self._finding(due_date=timezone.localdate() - timedelta(days=2))
		self.client.force_login(self.owner)
		response = self.client.get(reverse("audits:finding_detail", args=[finding.pk]))
		self.assertEqual(response.status_code, 200)
		self.assertContains(response, "Delay risk high")

	def test_closing_note_requires_post(self):
		audit = self._audit()
		self.client.force_login(self.owner)
		response = self.client.get(
			reverse("audits:audit_closing_note", args=[audit.pk])
		)
		self.assertEqual(response.status_code, 405)

	def test_auditor_cannot_generate_a_closing_note(self):
		audit = self._audit()
		self.client.force_login(self.auditor)
		response = self.client.post(
			reverse("audits:audit_closing_note", args=[audit.pk])
		)
		self.assertEqual(response.status_code, 403)

	def test_foreign_audit_closing_note_is_404(self):
		audit = self._audit()
		self.client.force_login(self.other_owner)
		response = self.client.post(
			reverse("audits:audit_closing_note", args=[audit.pk])
		)
		self.assertEqual(response.status_code, 404)

	def test_owner_generates_a_closing_note(self):
		audit = self._audit(status=AuditStatus.IN_PROGRESS)
		self._finding(audit=audit)
		returned = OllamaResult(ok=True, text=json.dumps({
			"closing_note_title": "Q3 closing note",
			"executive_summary": "The engagement completed with one finding.",
			"key_findings": ["A missing approval was raised."],
			"remediation_status": "Remediation is in progress.",
			"residual_risks": ["One finding remains open."],
			"conclusion": "Close the engagement with follow-up.",
		}))
		self.client.force_login(self.owner)
		with patch.object(ai.ollama_client, "generate", return_value=returned):
			response = self.client.post(
				reverse("audits:audit_closing_note", args=[audit.pk]), follow=True
			)
		self.assertRedirects(response, reverse("audits:audit_detail", args=[audit.pk]))
		self.assertContains(response, "Q3 closing note")
		self.assertContains(response, "AI · QWEN")

	def test_closing_note_fallback_when_ollama_is_down(self):
		audit = self._audit(status=AuditStatus.COMPLETED)
		failure = OllamaResult(
			ok=False, error="URLError", error_category="network_or_request_error"
		)
		self.client.force_login(self.owner)
		with patch.object(ai.ollama_client, "generate", return_value=failure):
			response = self.client.post(
				reverse("audits:audit_closing_note", args=[audit.pk]), follow=True
			)
		self.assertEqual(response.status_code, 200)
		self.assertContains(response, "unavailable")


@override_settings(
	PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"]
)
class AuditListPaginationTests(AuditFixtureMixin, TestCase):
	"""The audit list uses the shared filter bar and pagination helper."""

	def test_filter_combination_narrows_results(self):
		self._audit(
			title="Ops supplier review",
			audit_type=AuditType.SUPPLIER,
			department=self.operations,
		)
		self._audit(
			title="Finance supplier review",
			audit_type=AuditType.SUPPLIER,
			department=self.finance,
		)
		self.client.force_login(self.owner)

		response = self.client.get(
			reverse("audits:audit_list"),
			{"audit_type": AuditType.SUPPLIER, "department": str(self.operations.pk)},
		)

		self.assertEqual(response.status_code, 200)
		self.assertContains(response, "Ops supplier review")
		self.assertNotContains(response, "Finance supplier review")

	def test_default_page_size_is_ten(self):
		for index in range(12):
			self._audit(title=f"Audit {index:02d}")
		self.client.force_login(self.owner)

		response = self.client.get(reverse("audits:audit_list"))

		self.assertEqual(len(response.context["pagination"]["items"]), 10)
		self.assertEqual(response.context["pagination"]["total"], 12)

	def test_per_page_twenty_shows_up_to_twenty(self):
		for index in range(25):
			self._audit(title=f"Audit {index:02d}")
		self.client.force_login(self.owner)

		response = self.client.get(reverse("audits:audit_list"), {"per_page": "20"})

		self.assertEqual(len(response.context["pagination"]["items"]), 20)
		self.assertEqual(response.context["pagination"]["total"], 25)

	def test_next_link_preserves_active_filters(self):
		for index in range(15):
			self._audit(
				title=f"Supplier audit {index:02d}", audit_type=AuditType.SUPPLIER
			)
		self.client.force_login(self.owner)

		response = self.client.get(
			reverse("audits:audit_list"), {"audit_type": AuditType.SUPPLIER}
		)
		content = response.content.decode()
		match = re.search(r'href="([^"]*page=2[^"]*)"', content)

		self.assertIsNotNone(match)
		href = match.group(1)
		self.assertIn("page=2", href)
		self.assertIn(f"audit_type={AuditType.SUPPLIER}", href)

	def test_empty_result_renders_empty_state(self):
		self._audit(title="Only planned")
		self.client.force_login(self.owner)

		response = self.client.get(
			reverse("audits:audit_list"), {"status": AuditStatus.CANCELLED}
		)

		self.assertEqual(response.status_code, 200)
		self.assertContains(response, "cp-empty")
		self.assertEqual(response.context["pagination"]["total"], 0)

	def test_pagination_with_annotation_and_tenant_isolation(self):
		for index in range(11):
			self._audit(title=f"Filler {index:02d}")
		audit = self._audit(title="Annotated audit")
		self._finding(audit=audit, title="First finding")
		self._finding(audit=audit, title="Second finding")
		self.client.force_login(self.owner)

		response = self.client.get(reverse("audits:audit_list"))
		items = response.context["pagination"]["items"]

		# annotate(Count(...)) + slicing still paginates without duplicating rows.
		self.assertEqual(len(items), 10)
		self.assertEqual(response.context["pagination"]["total"], 12)
		annotated = next(row for row in items if row.title == "Annotated audit")
		self.assertEqual(annotated.finding_total, 2)
		# Another tenant's engagement is never rendered.
		self.assertNotContains(response, self.other_audit.title)


class AuditFormValidationTests(AuditFixtureMixin, TestCase):
	"""Server-side validation of the audit creation/edit form."""

	def _form(self, **overrides):
		payload = {
			"title": "Q3 internal control review",
			"audit_type": AuditType.INTERNAL,
			"scope_summary": "",
			"department": "",
			"related_policy": "",
			"start_date": timezone.localdate().isoformat(),
			"end_date": "",
			"lead_auditor": "",
		}
		payload.update(overrides)
		return AuditForm(
			data=payload,
			company=self.company,
			instance=Audit(company=self.company, created_by=self.owner),
		)

	def test_valid_audit_is_accepted(self):
		form = self._form()
		self.assertTrue(form.is_valid(), form.errors)

	def test_title_is_required(self):
		form = self._form(title="")
		self.assertFalse(form.is_valid())
		self.assertIn("Give the audit a title.", form.errors["title"])

	def test_audit_type_is_required(self):
		form = self._form(audit_type="")
		self.assertFalse(form.is_valid())
		self.assertIn("Pick an audit type.", form.errors["audit_type"])

	def test_start_date_is_required(self):
		form = self._form(start_date="")
		self.assertFalse(form.is_valid())
		self.assertIn("Enter the audit start date.", form.errors["start_date"])

	def test_invalid_start_date_format_is_rejected(self):
		form = self._form(start_date="not-a-date")
		self.assertFalse(form.is_valid())
		self.assertIn(
			"Enter a valid date (for example 2026-01-31).", form.errors["start_date"]
		)

	def test_end_date_before_start_date_is_rejected(self):
		today = timezone.localdate()
		form = self._form(
			start_date=today.isoformat(),
			end_date=(today - timedelta(days=1)).isoformat(),
		)
		self.assertFalse(form.is_valid())
		# The message is emitted once, under the end date field.
		self.assertEqual(
			form.errors["end_date"], ["End date must be on or after start date."]
		)

	def test_end_date_on_start_date_is_accepted(self):
		today = timezone.localdate()
		form = self._form(start_date=today.isoformat(), end_date=today.isoformat())
		self.assertTrue(form.is_valid(), form.errors)

	def test_foreign_department_is_rejected(self):
		foreign = Department.objects.create(company=self.other_company, name="Foreign")
		form = self._form(department=str(foreign.pk))
		self.assertFalse(form.is_valid())
		self.assertIn("department", form.errors)

	def test_foreign_policy_is_rejected(self):
		foreign = Policy.objects.create(
			company=self.other_company,
			title="Foreign policy",
			category=PolicyCategory.GOVERNANCE,
			created_by=self.other_owner,
		)
		form = self._form(related_policy=str(foreign.pk))
		self.assertFalse(form.is_valid())
		self.assertIn("related_policy", form.errors)

	def test_foreign_lead_auditor_is_rejected(self):
		form = self._form(lead_auditor=str(self.other_owner.pk))
		self.assertFalse(form.is_valid())
		self.assertIn("lead_auditor", form.errors)

	def test_own_department_and_lead_auditor_are_accepted(self):
		form = self._form(
			department=str(self.operations.pk),
			lead_auditor=str(self.ops_manager.pk),
		)
		self.assertTrue(form.is_valid(), form.errors)
