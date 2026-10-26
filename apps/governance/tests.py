import re
from datetime import date, timedelta
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.enterprise.models import Company, Department
from apps.recommendations.services.ollama_client import OllamaResult

from .forms import PolicyForm
from .models import (
	ApprovalDecision,
	Policy,
	PolicyApproval,
	PolicyCategory,
	PolicyOwner,
	PolicyStatus,
	PolicyVersion,
	ResponsibilityRole,
)
from .services import (
	archive_policy,
	can_approve,
	can_manage,
	create_initial_version,
	create_new_version,
	record_decision,
	record_review,
	submit_for_review,
	visible_policies,
)
from .services import governance_ai


class PolicyTestCase(TestCase):
	password = "SecureTestPassword2026!"

	def setUp(self):
		self.owner = User.objects.create_user(
			email="gov-owner@carbonpilot.test",
			password=self.password,
			role=User.Role.COMPANY_OWNER,
			status=User.Status.ACTIVE,
		)
		self.company = Company.objects.create(owner=self.owner, name="Governance Works")
		self.operations = Department.objects.create(company=self.company, name="Operations")
		self.finance = Department.objects.create(company=self.company, name="Finance")

		self.other_owner = User.objects.create_user(
			email="gov-other-owner@carbonpilot.test",
			password=self.password,
			role=User.Role.COMPANY_OWNER,
			status=User.Status.ACTIVE,
		)
		self.other_company = Company.objects.create(owner=self.other_owner, name="Other Governance")

		self.esg_manager = self._member(
			role=User.Role.ESG_REPORTING_MANAGER,
			email="gov-esg@carbonpilot.test",
		)
		self.ops_manager = self._member(
			role=User.Role.DEPARTMENT_MANAGER,
			email="gov-ops-manager@carbonpilot.test",
			department="Operations",
		)
		self.reviewer = self._member(
			role=User.Role.AUDITOR,
			email="gov-reviewer@carbonpilot.test",
		)
		self.auditor = self._member(
			role=User.Role.AUDITOR,
			email="gov-auditor@carbonpilot.test",
		)
		self.outsider = User.objects.create_user(
			email="gov-outsider@carbonpilot.test",
			password=self.password,
			role=User.Role.DEPARTMENT_MANAGER,
			status=User.Status.ACTIVE,
			company_membership=self.other_company,
			department="Operations",
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
		policy = Policy.objects.create(**payload)
		create_initial_version(
			policy, self.owner, summary="First issue", content="Initial policy text."
		)
		return policy

	def _reviewed_draft(self, **overrides):
		"""A draft policy with the company owner as its sole named reviewer."""
		policy = self._policy(**overrides)
		PolicyOwner.objects.create(
			policy=policy, user=self.owner, responsibility_role=ResponsibilityRole.REVIEWER
		)
		return policy

	def _approved_policy(self, next_review_date=None, **overrides):
		policy = self._reviewed_draft(**overrides)
		submit_for_review(policy, self.owner)
		for approval in policy.approvals.filter(policy_version=policy.current_version):
			record_decision(approval, approval.approver, ApprovalDecision.APPROVED)
		policy.refresh_from_db()
		if next_review_date is not None:
			policy.next_review_date = next_review_date
			policy.save(update_fields=("next_review_date",))
		return policy


class PolicyModelTests(PolicyTestCase):
	def test_policy_belongs_to_one_company_and_gets_a_reference(self):
		policy = self._policy(code="GOV-001")
		self.assertEqual(policy.company, self.company)
		self.assertEqual(policy.reference, "GOV-001")
		self.assertEqual(policy.status, PolicyStatus.DRAFT)
		self.assertTrue(policy.is_editable)

	def test_reference_falls_back_to_the_identifier_when_no_code(self):
		policy = self._policy()
		self.assertEqual(policy.reference, f"POL-{policy.pk:04d}")

	def test_code_is_unique_per_company(self):
		self._policy(code="GOV-001")
		duplicate = Policy(
			company=self.company, title="Another policy", code="GOV-001"
		)
		with self.assertRaises(ValidationError):
			duplicate.full_clean()

	def test_same_code_is_allowed_in_another_company(self):
		self._policy(code="GOV-001")
		foreign = Policy.objects.create(
			company=self.other_company, title="Foreign policy", code="GOV-001"
		)
		foreign.full_clean()  # no cross-company clash

	def test_only_one_current_version_per_policy(self):
		policy = self._policy()
		with self.assertRaises(Exception):
			PolicyVersion.objects.create(policy=policy, version_number=2, is_current=True)

	def test_owner_row_pins_itself_to_the_policy_company(self):
		policy = self._policy()
		row = PolicyOwner.objects.create(policy=policy, department=self.operations)
		self.assertEqual(row.company, self.company)

	def test_owner_department_from_another_company_is_rejected(self):
		policy = self._policy()
		foreign_department = Department.objects.create(company=self.other_company, name="Remote")
		row = PolicyOwner(policy=policy, department=foreign_department)
		with self.assertRaises(ValidationError):
			row.full_clean()

	def test_owner_user_from_another_company_is_rejected(self):
		policy = self._policy()
		row = PolicyOwner(policy=policy, user=self.outsider)
		with self.assertRaises(ValidationError):
			row.full_clean()

	def test_owner_row_needs_a_user_or_a_department(self):
		policy = self._policy()
		row = PolicyOwner(policy=policy)
		with self.assertRaises(ValidationError):
			row.full_clean()

	def test_due_for_review_flag(self):
		overdue = self._approved_policy(next_review_date=timezone.localdate() - timedelta(days=3))
		self.assertTrue(overdue.is_due_for_review)

		future = self._approved_policy(
			title="Future review", next_review_date=timezone.localdate() + timedelta(days=30)
		)
		self.assertFalse(future.is_due_for_review)

		draft = self._policy(
			title="Draft", next_review_date=timezone.localdate() - timedelta(days=3)
		)
		self.assertFalse(draft.is_due_for_review)


class PolicyWorkflowTests(PolicyTestCase):
	def test_submit_for_review_creates_pending_approvals(self):
		policy = self._policy()
		PolicyOwner.objects.create(
			policy=policy, user=self.reviewer, responsibility_role=ResponsibilityRole.REVIEWER
		)
		submit_for_review(policy, self.owner)

		policy.refresh_from_db()
		self.assertEqual(policy.status, PolicyStatus.IN_REVIEW)
		approval = policy.approvals.get(approver=self.reviewer)
		self.assertEqual(approval.decision, ApprovalDecision.PENDING)
		self.assertIsNone(approval.decided_at)

	def test_submit_falls_back_to_governance_roles_without_named_reviewers(self):
		policy = self._policy()
		submit_for_review(policy, self.owner)
		approvers = set(policy.approvals.values_list("approver_id", flat=True))
		self.assertIn(self.owner.pk, approvers)
		self.assertIn(self.esg_manager.pk, approvers)

	def test_all_approvals_approve_the_policy_and_set_a_review_date(self):
		policy = self._policy()
		PolicyOwner.objects.create(
			policy=policy, user=self.owner, responsibility_role=ResponsibilityRole.REVIEWER
		)
		submit_for_review(policy, self.owner)
		approval = policy.approvals.get(approver=self.owner)
		record_decision(approval, self.owner, ApprovalDecision.APPROVED)

		policy.refresh_from_db()
		self.assertEqual(policy.status, PolicyStatus.APPROVED)
		self.assertIsNotNone(policy.next_review_date)
		self.assertTrue(policy.current_version.is_current)

	def test_a_partial_approval_keeps_the_policy_in_review(self):
		policy = self._policy()
		PolicyOwner.objects.create(
			policy=policy, user=self.reviewer, responsibility_role=ResponsibilityRole.REVIEWER
		)
		PolicyOwner.objects.create(
			policy=policy, user=self.auditor, responsibility_role=ResponsibilityRole.REVIEWER
		)
		submit_for_review(policy, self.owner)

		first = policy.approvals.get(approver=self.reviewer)
		record_decision(first, self.reviewer, ApprovalDecision.APPROVED)
		policy.refresh_from_db()
		self.assertEqual(policy.status, PolicyStatus.IN_REVIEW)

		second = policy.approvals.get(approver=self.auditor)
		record_decision(second, self.auditor, ApprovalDecision.APPROVED)
		policy.refresh_from_db()
		self.assertEqual(policy.status, PolicyStatus.APPROVED)

	def test_rejection_returns_the_policy_to_draft(self):
		policy = self._policy()
		submit_for_review(policy, self.owner)
		approval = policy.approvals.get(approver=self.owner)
		record_decision(approval, self.owner, ApprovalDecision.REJECTED, "Needs work")

		policy.refresh_from_db()
		self.assertEqual(policy.status, PolicyStatus.DRAFT)
		approval.refresh_from_db()
		self.assertEqual(approval.comment, "Needs work")
		self.assertIsNotNone(approval.decided_at)

	def test_resubmitting_reopens_a_rejected_approval(self):
		policy = self._policy()
		submit_for_review(policy, self.owner)
		approval = policy.approvals.get(approver=self.owner)
		record_decision(approval, self.owner, ApprovalDecision.REJECTED)

		submit_for_review(policy, self.owner)
		approval.refresh_from_db()
		self.assertEqual(approval.decision, ApprovalDecision.PENDING)
		self.assertIsNone(approval.decided_at)

	def test_cannot_submit_a_policy_that_is_not_a_draft(self):
		policy = self._approved_policy()
		with self.assertRaises(ValidationError):
			submit_for_review(policy, self.owner)

	def test_archive_requires_an_approved_policy(self):
		draft = self._policy()
		with self.assertRaises(ValidationError):
			archive_policy(draft)

		approved = self._approved_policy(title="Archivable")
		archive_policy(approved)
		approved.refresh_from_db()
		self.assertEqual(approved.status, PolicyStatus.ARCHIVED)
		self.assertFalse(approved.is_active)

	def test_new_version_only_from_an_approved_policy(self):
		draft = self._policy()
		with self.assertRaises(ValidationError):
			create_new_version(draft, self.owner)

	def test_new_version_keeps_history_and_returns_to_draft(self):
		policy = self._approved_policy()
		first_version = policy.current_version

		version = create_new_version(
			policy, self.owner, summary="Second issue", content="Updated text.",
			change_notes="Updated scope",
		)

		policy.refresh_from_db()
		first_version.refresh_from_db()
		self.assertEqual(version.version_number, 2)
		self.assertTrue(version.is_current)
		self.assertFalse(first_version.is_current)
		self.assertEqual(policy.current_version, version)
		self.assertEqual(policy.status, PolicyStatus.DRAFT)
		self.assertIsNone(policy.next_review_date)
		self.assertEqual(policy.versions.count(), 2)

	def test_record_review_reschedules_the_next_review(self):
		policy = self._approved_policy(next_review_date=timezone.localdate() - timedelta(days=10))
		record_review(policy)
		policy.refresh_from_db()
		self.assertFalse(policy.is_due_for_review)
		self.assertGreater(policy.next_review_date, timezone.localdate())

	def test_due_for_review_queryset(self):
		self._approved_policy(next_review_date=timezone.localdate() - timedelta(days=1))
		self._approved_policy(
			title="On time", next_review_date=timezone.localdate() + timedelta(days=5)
		)
		self.assertEqual(Policy.objects.due_for_review().count(), 1)


class PolicyScopingTests(PolicyTestCase):
	def test_owner_sees_every_company_policy(self):
		self._policy()
		self._policy(title="Second")
		self.assertEqual(visible_policies(self.owner, self.company).count(), 2)

	def test_auditor_reads_but_cannot_manage(self):
		policy = self._policy()
		self.assertEqual(visible_policies(self.auditor, self.company).count(), 1)
		self.assertFalse(can_manage(self.auditor, self.company, policy))

	def test_other_company_policies_are_never_visible(self):
		Policy.objects.create(company=self.other_company, title="Foreign")
		self.assertEqual(visible_policies(self.owner, self.company).count(), 0)

	def test_department_manager_contributes_only_when_responsible(self):
		policy = self._policy()
		self.assertTrue(can_manage(self.ops_manager, self.company))
		self.assertFalse(can_manage(self.ops_manager, self.company, policy))

		PolicyOwner.objects.create(policy=policy, user=self.ops_manager)
		self.assertTrue(can_manage(self.ops_manager, self.company, policy))

	def test_can_approve_rules(self):
		policy = self._policy()
		self.assertTrue(can_approve(self.owner, self.company, policy))
		self.assertFalse(can_approve(self.auditor, self.company, policy))

		PolicyOwner.objects.create(
			policy=policy, user=self.reviewer, responsibility_role=ResponsibilityRole.REVIEWER
		)
		self.assertTrue(can_approve(self.reviewer, self.company, policy))


class PolicyViewTests(PolicyTestCase):
	def _owner_formset_data(self, rows, total=None):
		total = len(rows) if total is None else total
		data = {
			"owners-TOTAL_FORMS": str(total),
			"owners-INITIAL_FORMS": "0",
			"owners-MIN_NUM_FORMS": "0",
			"owners-MAX_NUM_FORMS": "1000",
		}
		for index, row in enumerate(rows):
			data[f"owners-{index}-responsibility_role"] = row.get(
				"role", ResponsibilityRole.OWNER
			)
			data[f"owners-{index}-department"] = row.get("department", "")
			data[f"owners-{index}-user"] = row.get("user", "")
			data[f"owners-{index}-id"] = row.get("id", "")
			if row.get("delete"):
				data[f"owners-{index}-DELETE"] = "on"
		return data

	def _create_payload(self, **overrides):
		payload = {
			"title": "Data Protection Policy",
			"code": "GOV-100",
			"category": PolicyCategory.GOVERNANCE,
			"description": "How we handle personal data.",
			"next_review_date": "",
			"version-summary": "First issue",
			"version-content": "Full text.",
			"version-change_notes": "Initial",
			"version-effective_from": "",
		}
		payload.update(overrides)
		return payload

	def test_owner_creates_a_policy_with_owners_and_a_first_version(self):
		self.client.force_login(self.owner)
		data = self._create_payload()
		data.update(
			self._owner_formset_data(
				[
					{"role": ResponsibilityRole.OWNER, "department": str(self.operations.pk)},
					{"role": ResponsibilityRole.REVIEWER, "user": str(self.reviewer.pk)},
				]
			)
		)
		response = self.client.post(reverse("governance:policy_create"), data)

		policy = Policy.objects.get(title="Data Protection Policy")
		self.assertRedirects(
			response, reverse("governance:policy_detail", args=[policy.pk])
		)
		self.assertEqual(policy.company, self.company)
		self.assertEqual(policy.created_by, self.owner)
		self.assertEqual(policy.status, PolicyStatus.DRAFT)
		self.assertEqual(policy.owners.count(), 2)
		self.assertEqual(policy.current_version.version_number, 1)
		self.assertEqual(policy.current_version.summary, "First issue")

	def test_create_rejects_a_cross_company_department(self):
		foreign_department = Department.objects.create(company=self.other_company, name="Remote")
		self.client.force_login(self.owner)
		data = self._create_payload(title="Sneaky policy")
		data.update(
			self._owner_formset_data(
				[{"role": ResponsibilityRole.OWNER, "department": ""}], total=1
			)
		)
		# Force a foreign id into the field payload.
		data["owners-0-department"] = str(foreign_department.pk)
		response = self.client.post(reverse("governance:policy_create"), data)

		self.assertEqual(response.status_code, 200)  # re-rendered with errors
		self.assertFalse(Policy.objects.filter(title="Sneaky policy").exists())

	def test_auditor_cannot_create_a_policy(self):
		self.client.force_login(self.auditor)
		response = self.client.post(reverse("governance:policy_create"), self._create_payload())
		self.assertEqual(response.status_code, 403)

	def test_only_draft_policies_can_be_edited(self):
		policy = self._approved_policy()
		self.client.force_login(self.owner)
		response = self.client.get(reverse("governance:policy_edit", args=[policy.pk]))
		self.assertRedirects(
			response, reverse("governance:policy_detail", args=[policy.pk])
		)

	def test_owner_edits_a_draft_policy(self):
		policy = self._policy(code="GOV-EDIT")
		self.client.force_login(self.owner)
		data = self._create_payload(title="Renamed policy")
		data.update(
			self._owner_formset_data(
				[{"role": ResponsibilityRole.OWNER, "user": str(self.owner.pk)}]
			)
		)
		response = self.client.post(
			reverse("governance:policy_edit", args=[policy.pk]), data
		)
		self.assertRedirects(
			response, reverse("governance:policy_detail", args=[policy.pk])
		)
		policy.refresh_from_db()
		self.assertEqual(policy.title, "Renamed policy")
		self.assertEqual(policy.owners.count(), 1)

	def test_department_manager_cannot_edit_a_policy_they_do_not_own(self):
		policy = self._policy()
		self.client.force_login(self.ops_manager)
		response = self.client.get(reverse("governance:policy_edit", args=[policy.pk]))
		self.assertEqual(response.status_code, 403)

	def test_submit_for_review_endpoint(self):
		policy = self._policy()
		self.client.force_login(self.owner)
		response = self.client.post(
			reverse("governance:policy_submit_review", args=[policy.pk])
		)
		self.assertRedirects(
			response, reverse("governance:policy_detail", args=[policy.pk])
		)
		policy.refresh_from_db()
		self.assertEqual(policy.status, PolicyStatus.IN_REVIEW)
		self.assertTrue(policy.approvals.exists())

	def test_approve_and_reject_endpoints(self):
		policy = self._reviewed_draft()
		self.client.force_login(self.owner)
		self.client.post(reverse("governance:policy_submit_review", args=[policy.pk]))

		self.client.post(
			reverse("governance:policy_decide", args=[policy.pk]),
			{"decision": ApprovalDecision.REJECTED, "comment": "Not yet"},
		)
		policy.refresh_from_db()
		self.assertEqual(policy.status, PolicyStatus.DRAFT)

		self.client.post(reverse("governance:policy_submit_review", args=[policy.pk]))
		self.client.post(
			reverse("governance:policy_decide", args=[policy.pk]),
			{"decision": ApprovalDecision.APPROVED, "comment": "Good"},
		)
		policy.refresh_from_db()
		self.assertEqual(policy.status, PolicyStatus.APPROVED)

	def test_auditor_cannot_record_a_decision(self):
		policy = self._policy()
		submit_for_review(policy, self.owner)
		self.client.force_login(self.auditor)
		response = self.client.post(
			reverse("governance:policy_decide", args=[policy.pk]),
			{"decision": ApprovalDecision.APPROVED},
		)
		self.assertEqual(response.status_code, 403)

	def test_archive_endpoint(self):
		policy = self._approved_policy()
		self.client.force_login(self.owner)
		self.client.post(reverse("governance:policy_archive", args=[policy.pk]))
		policy.refresh_from_db()
		self.assertEqual(policy.status, PolicyStatus.ARCHIVED)

	def test_new_version_endpoint(self):
		policy = self._approved_policy()
		self.client.force_login(self.owner)
		response = self.client.post(
			reverse("governance:policy_new_version", args=[policy.pk]),
			{
				"version-summary": "Second issue",
				"version-content": "Updated.",
				"version-change_notes": "Tweaks",
				"version-effective_from": "",
			},
		)
		self.assertRedirects(
			response, reverse("governance:policy_detail", args=[policy.pk])
		)
		policy.refresh_from_db()
		self.assertEqual(policy.status, PolicyStatus.DRAFT)
		self.assertEqual(policy.versions.count(), 2)

	def test_record_review_endpoint(self):
		policy = self._approved_policy(next_review_date=timezone.localdate() - timedelta(days=5))
		self.client.force_login(self.owner)
		self.client.post(reverse("governance:policy_record_review", args=[policy.pk]))
		policy.refresh_from_db()
		self.assertFalse(policy.is_due_for_review)

	def test_list_and_detail_render(self):
		policy = self._approved_policy()
		self.client.force_login(self.owner)
		list_response = self.client.get(reverse("governance:policy_list"))
		self.assertEqual(list_response.status_code, 200)
		self.assertContains(list_response, "Supplier Code of Conduct")

		detail_response = self.client.get(
			reverse("governance:policy_detail", args=[policy.pk])
		)
		self.assertEqual(detail_response.status_code, 200)
		self.assertContains(detail_response, "Version history")
		self.assertContains(detail_response, "Approvals")
		self.assertContains(detail_response, "Responsibility matrix")

	def test_due_for_review_is_flagged_and_filterable(self):
		due = self._approved_policy(
			title="Overdue policy", next_review_date=timezone.localdate() - timedelta(days=2)
		)
		on_time = self._approved_policy(
			title="Fresh policy", next_review_date=timezone.localdate() + timedelta(days=90)
		)
		self.client.force_login(self.owner)

		response = self.client.get(reverse("governance:policy_list"), {"due": "due"})
		self.assertContains(response, "Overdue policy")
		self.assertNotContains(response, "Fresh policy")

		detail = self.client.get(reverse("governance:policy_detail", args=[due.pk]))
		self.assertContains(detail, "Due for review")

		fresh_detail = self.client.get(
			reverse("governance:policy_detail", args=[on_time.pk])
		)
		self.assertNotContains(fresh_detail, "Due for review")

	def test_list_filters_by_status_and_category(self):
		self._policy(title="Draft environmental", category=PolicyCategory.ENVIRONMENT)
		self._approved_policy(title="Approved governance", category=PolicyCategory.GOVERNANCE)
		self.client.force_login(self.owner)

		response = self.client.get(reverse("governance:policy_list"), {"status": PolicyStatus.APPROVED})
		self.assertContains(response, "Approved governance")
		self.assertNotContains(response, "Draft environmental")

		response = self.client.get(reverse("governance:policy_list"), {"category": PolicyCategory.ENVIRONMENT})
		self.assertContains(response, "Draft environmental")
		self.assertNotContains(response, "Approved governance")

	def test_cross_company_policy_is_not_found(self):
		foreign = Policy.objects.create(
			company=self.other_company, title="Foreign policy", created_by=self.other_owner
		)
		self.client.force_login(self.owner)
		response = self.client.get(
			reverse("governance:policy_detail", args=[foreign.pk])
		)
		self.assertEqual(response.status_code, 404)

	def test_other_company_member_cannot_open_the_policy(self):
		policy = self._policy()
		self.client.force_login(self.outsider)
		response = self.client.get(
			reverse("governance:policy_detail", args=[policy.pk])
		)
		self.assertEqual(response.status_code, 404)

	def test_anonymous_user_is_redirected_to_login(self):
		response = self.client.get(reverse("governance:policy_list"))
		self.assertEqual(response.status_code, 302)
		self.assertIn("/login", response.url)

	def test_sidebar_entry_is_exposed(self):
		self.client.force_login(self.owner)
		response = self.client.get(reverse("governance:policy_list"))
		self.assertContains(response, reverse("governance:policy_list"))


class GovernanceAiContextTests(PolicyTestCase):
	"""The factual context Django supplies to Qwen must be tenant-scoped."""

	def test_build_context_is_tenant_scoped(self):
		self._policy(title="Our environment policy", category=PolicyCategory.ENVIRONMENT)
		Policy.objects.create(
			company=self.other_company,
			title="Foreign governance policy",
			category=PolicyCategory.GOVERNANCE,
			created_by=self.other_owner,
		)

		context = governance_ai.build_context(self.company)

		titles = [row["title"] for row in context["policies"]]
		self.assertEqual(titles, ["Our environment policy"])
		coverage = context["policy_coverage"]
		self.assertEqual(coverage["covered_categories"], [PolicyCategory.ENVIRONMENT])
		self.assertNotIn(PolicyCategory.ENVIRONMENT, coverage["uncovered_categories"])
		self.assertIn(PolicyCategory.GOVERNANCE, coverage["uncovered_categories"])

	def test_build_context_without_company_is_none(self):
		self.assertIsNone(governance_ai.build_context(None))

	def test_prompt_contains_only_this_company_facts(self):
		self._policy(title="Our environment policy", category=PolicyCategory.ENVIRONMENT)
		Policy.objects.create(
			company=self.other_company,
			title="Foreign governance policy",
			category=PolicyCategory.GOVERNANCE,
			created_by=self.other_owner,
		)
		context = governance_ai.build_context(self.company)
		prompt = governance_ai.build_missing_policies_prompt(context)
		self.assertIn("Our environment policy", prompt)
		self.assertNotIn("Foreign governance policy", prompt)


class GovernanceAiValidationTests(PolicyTestCase):
	"""Values returned by the model are validated before they reach the UI."""

	def test_unknown_category_is_rejected(self):
		self.assertIsNone(
			governance_ai.validate_missing_item(
				{
					"suggested_title": "Bad category",
					"category": "NOT_A_CATEGORY",
					"priority": "HIGH",
					"why": "The supplied facts justify this.",
					"related_signals": ["coverage"],
				}
			)
		)

	def test_unknown_priority_is_rejected(self):
		self.assertIsNone(
			governance_ai.validate_missing_item(
				{
					"suggested_title": "Bad priority",
					"category": PolicyCategory.SOCIAL,
					"priority": "URGENT",
					"why": "The supplied facts justify this.",
					"related_signals": ["coverage"],
				}
			)
		)

	def test_valid_item_is_cleaned_and_labelled(self):
		item = governance_ai.validate_missing_item(
			{
				"suggested_title": "  Environmental Management Policy  ",
				"category": "environment",
				"priority": "high",
				"why": "No environment policy is on record.",
				"related_signals": ["Coverage", "coverage", ""],
			}
		)
		self.assertEqual(item["category"], PolicyCategory.ENVIRONMENT)
		self.assertEqual(item["category_label"], "Environment")
		self.assertEqual(item["priority"], "HIGH")
		self.assertEqual(item["suggested_title"], "Environmental Management Policy")
		self.assertEqual(item["related_signals"], ["coverage"])

	def test_duplicate_titles_are_collapsed(self):
		items = [
			{
				"suggested_title": "Same title",
				"category": PolicyCategory.ESG,
				"priority": "LOW",
				"why": "First occurrence of this suggestion.",
				"related_signals": [],
			},
			{
				"suggested_title": "same title",
				"category": PolicyCategory.ESG,
				"priority": "HIGH",
				"why": "Duplicate occurrence of the suggestion.",
				"related_signals": [],
			},
		]
		cleaned, rejected = governance_ai.validate_missing_policies(items)
		self.assertEqual(len(cleaned), 1)
		self.assertEqual(rejected, 1)

	def test_invalid_json_returns_none(self):
		self.assertIsNone(governance_ai.parse_missing_policies("not json at all"))
		self.assertIsNone(governance_ai.parse_summary("not json at all"))

	def test_embedded_json_is_recovered(self):
		text = 'Sure, here it is: {"missing_policies": []} — done.'
		self.assertEqual(governance_ai.parse_missing_policies(text), [])


class GovernanceAiAnalysisTests(PolicyTestCase):
	"""``run_analysis`` never raises and degrades cleanly when Qwen is down."""

	def setUp(self):
		super().setUp()
		# A signal must exist: one covered category leaves the rest uncovered.
		self._policy(category=PolicyCategory.GOVERNANCE)

	def test_ollama_unavailable_falls_back_with_a_message(self):
		failure = OllamaResult(ok=False, error="URLError", error_category="network_or_request_error")
		with patch.object(governance_ai.ollama_client, "generate", return_value=failure):
			result = governance_ai.run_analysis(self.company)
		self.assertTrue(result.ok)
		self.assertFalse(result.ai_available)
		self.assertEqual(result.message, governance_ai.UNAVAILABLE_MESSAGE)
		self.assertEqual(result.missing_policies, [])

	def test_invalid_json_from_model_is_handled(self):
		reply = OllamaResult(ok=True, text="I am not JSON")
		with patch.object(governance_ai.ollama_client, "generate", return_value=reply):
			result = governance_ai.run_analysis(self.company)
		self.assertTrue(result.ok)
		self.assertFalse(result.ai_available)
		self.assertEqual(result.message, governance_ai.INVALID_RESPONSE_MESSAGE)

	def test_successful_analysis_cleans_and_reports_rejections(self):
		missing = OllamaResult(
			ok=True,
			text=(
				'{"missing_policies": ['
				'{"suggested_title": "Environmental Management Policy",'
				' "category": "ENVIRONMENT", "priority": "HIGH",'
				' "why": "No environment category policy is on record.",'
				' "related_signals": ["coverage"]},'
				'{"suggested_title": "Nonsense", "category": "NONSENSE",'
				' "priority": "HIGH", "why": "Rejected by the category check.",'
				' "related_signals": []}'
				"]}"
			),
		)
		summary = OllamaResult(
			ok=True,
			text=(
				'{"executive_summary": "Coverage is partial.",'
				' "strengths": ["Owner named"],'
				' "gaps": ["Environment policy missing"],'
				' "management_attention": ["Approve the draft"]}'
			),
		)
		with patch.object(
			governance_ai.ollama_client, "generate", side_effect=[missing, summary]
		):
			result = governance_ai.run_analysis(self.company)

		self.assertTrue(result.ai_available, result.message)
		self.assertEqual(len(result.missing_policies), 1)
		self.assertEqual(
			result.missing_policies[0]["category"], PolicyCategory.ENVIRONMENT
		)
		self.assertIn("1 suggestion(s) failed validation", result.message)
		self.assertEqual(result.summary["executive_summary"], "Coverage is partial.")
		self.assertEqual(result.summary["management_attention"], ["Approve the draft"])

	def test_a_failed_summary_keeps_the_missing_policy_findings(self):
		missing = OllamaResult(
			ok=True,
			text=(
				'{"missing_policies": [{"suggested_title": "Supplier ESG Policy",'
				' "category": "SOCIAL", "priority": "MEDIUM",'
				' "why": "Suppliers show missing ESG documentation.",'
				' "related_signals": ["supplier"]}]}'
			),
		)
		failure = OllamaResult(ok=False, error="TimeoutError", error_category="timeout")
		with patch.object(
			governance_ai.ollama_client, "generate", side_effect=[missing, failure]
		):
			result = governance_ai.run_analysis(self.company)
		self.assertTrue(result.ok)
		self.assertTrue(result.ai_available)
		self.assertEqual(len(result.missing_policies), 1)
		self.assertEqual(result.summary, {})
		self.assertIn("summary could not be retrieved", result.message)


class GovernanceAiViewTests(PolicyTestCase):
	"""The POST endpoint keeps tenant isolation and never crashes on failure."""

	def test_ai_analysis_requires_post(self):
		self.client.force_login(self.owner)
		response = self.client.get(reverse("governance:policy_ai_analysis"))
		self.assertEqual(response.status_code, 405)

	def test_auditor_cannot_trigger_the_analysis(self):
		self.client.force_login(self.auditor)
		response = self.client.post(reverse("governance:policy_ai_analysis"))
		self.assertEqual(response.status_code, 403)

	def test_anonymous_user_is_redirected_to_login(self):
		response = self.client.post(reverse("governance:policy_ai_analysis"))
		self.assertEqual(response.status_code, 302)
		self.assertIn("/login", response.url)

	def test_owner_runs_analysis_and_sees_the_cards(self):
		self._policy(category=PolicyCategory.GOVERNANCE)
		missing = OllamaResult(
			ok=True,
			text=(
				'{"missing_policies": [{"suggested_title": "Environmental Policy",'
				' "category": "ENVIRONMENT", "priority": "HIGH",'
				' "why": "No environment policy is on record.",'
				' "related_signals": ["coverage"]}]}'
			),
		)
		summary = OllamaResult(
			ok=True,
			text=(
				'{"executive_summary": "Coverage is partial.",'
				' "strengths": ["A governance policy exists"],'
				' "gaps": ["Environment policy missing"],'
				' "management_attention": ["Draft the environment policy"]}'
			),
		)
		self.client.force_login(self.owner)
		with patch.object(
			governance_ai.ollama_client, "generate", side_effect=[missing, summary]
		):
			response = self.client.post(
				reverse("governance:policy_ai_analysis"), follow=True
			)

		self.assertRedirects(response, reverse("governance:policy_list"))
		self.assertContains(response, "AI · QWEN")
		self.assertContains(response, "Environmental Policy")
		self.assertContains(response, "Coverage is partial.")

	def test_ollama_down_shows_a_fallback_message(self):
		self._policy(category=PolicyCategory.GOVERNANCE)
		failure = OllamaResult(ok=False, error="URLError", error_category="network_or_request_error")
		self.client.force_login(self.owner)
		with patch.object(governance_ai.ollama_client, "generate", return_value=failure):
			response = self.client.post(
				reverse("governance:policy_ai_analysis"), follow=True
			)
		self.assertContains(response, "unavailable")

	def test_analysis_is_not_shown_to_another_tenant(self):
		"""A stored analysis belongs to one company; it is never rendered elsewhere."""
		self._policy(category=PolicyCategory.GOVERNANCE)
		missing = OllamaResult(ok=True, text='{"missing_policies": []}')
		summary = OllamaResult(
			ok=True,
			text='{"executive_summary": "Only for the first company.", "strengths": [], "gaps": [], "management_attention": []}',
		)
		self.client.force_login(self.owner)
		with patch.object(
			governance_ai.ollama_client, "generate", side_effect=[missing, summary]
		):
			self.client.post(reverse("governance:policy_ai_analysis"))

		# The same browser session now acts as a member of another company.
		self.client.force_login(self.outsider)
		response = self.client.get(reverse("governance:policy_list"))
		self.assertNotContains(response, "Only for the first company.")

	def test_create_form_prefills_a_suggested_policy(self):
		self.client.force_login(self.owner)
		response = self.client.get(
			reverse("governance:policy_create"),
			{"title": "Environmental Policy", "category": "ENVIRONMENT"},
		)
		self.assertContains(response, "Environmental Policy")
		self.assertContains(response, 'value="ENVIRONMENT" selected')


@override_settings(
	PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"]
)
class PolicyListPaginationTests(PolicyTestCase):
	"""The policy list uses the shared filter bar and pagination helper."""

	def test_filter_combination_narrows_results(self):
		self._policy(title="Draft environmental", category=PolicyCategory.ENVIRONMENT)
		self._approved_policy(
			title="Approved environmental", category=PolicyCategory.ENVIRONMENT
		)
		self.client.force_login(self.owner)

		response = self.client.get(
			reverse("governance:policy_list"),
			{"status": PolicyStatus.APPROVED, "category": PolicyCategory.ENVIRONMENT},
		)

		self.assertEqual(response.status_code, 200)
		self.assertContains(response, "Approved environmental")
		self.assertNotContains(response, "Draft environmental")

	def test_default_page_size_is_ten(self):
		for index in range(12):
			self._policy(title=f"Policy {index:02d}")
		self.client.force_login(self.owner)

		response = self.client.get(reverse("governance:policy_list"))

		self.assertEqual(len(response.context["pagination"]["items"]), 10)
		self.assertEqual(response.context["pagination"]["total"], 12)

	def test_per_page_twenty_shows_up_to_twenty(self):
		for index in range(25):
			self._policy(title=f"Policy {index:02d}")
		self.client.force_login(self.owner)

		response = self.client.get(
			reverse("governance:policy_list"), {"per_page": "20"}
		)

		self.assertEqual(len(response.context["pagination"]["items"]), 20)
		self.assertEqual(response.context["pagination"]["total"], 25)

	def test_next_link_preserves_active_filters(self):
		for index in range(15):
			self._policy(
				title=f"Env {index:02d}", category=PolicyCategory.ENVIRONMENT
			)
		self.client.force_login(self.owner)

		response = self.client.get(
			reverse("governance:policy_list"),
			{"category": PolicyCategory.ENVIRONMENT},
		)
		content = response.content.decode()
		match = re.search(r'href="([^"]*page=2[^"]*)"', content)

		self.assertIsNotNone(match)
		href = match.group(1)
		self.assertIn("page=2", href)
		self.assertIn(f"category={PolicyCategory.ENVIRONMENT}", href)

	def test_empty_result_renders_empty_state(self):
		self._policy(title="A live draft")
		self.client.force_login(self.owner)

		response = self.client.get(
			reverse("governance:policy_list"), {"status": PolicyStatus.ARCHIVED}
		)

		self.assertEqual(response.status_code, 200)
		self.assertContains(response, "cp-empty")
		self.assertEqual(response.context["pagination"]["total"], 0)

	def test_other_tenant_policies_never_appear(self):
		self._policy(title="Our internal policy")
		Policy.objects.create(
			company=self.other_company,
			title="Foreign secret policy",
			created_by=self.other_owner,
		)
		self.client.force_login(self.owner)

		response = self.client.get(reverse("governance:policy_list"))

		self.assertContains(response, "Our internal policy")
		self.assertNotContains(response, "Foreign secret policy")


class PolicyFormValidationTests(PolicyTestCase):
	"""Server-side rules for section 5 — field-level messages only, never twice."""

	def _payload(self, **overrides):
		payload = {
			"title": "Supplier Code of Conduct",
			"code": "GOV-001",
			"category": PolicyCategory.GOVERNANCE,
			"description": "Sets the minimum standards expected from our suppliers.",
			"next_review_date": "",
		}
		payload.update(overrides)
		return payload

	def _form(self, instance=None, **overrides):
		return PolicyForm(data=self._payload(**overrides), company=self.company, instance=instance)

	def test_valid_policy_is_accepted(self):
		form = self._form()
		self.assertTrue(form.is_valid(), form.errors)

	def test_title_is_required(self):
		form = self._form(title="")
		self.assertIn("title", form.errors)
		self.assertIn("Give the policy a title.", form.errors["title"])

	def test_category_is_required(self):
		form = self._form(category="")
		self.assertIn("category", form.errors)
		self.assertIn("Pick a policy category.", form.errors["category"])

	def test_description_is_required(self):
		form = self._form(description="")
		self.assertIn("description", form.errors)
		self.assertIn("Describe what this policy covers.", form.errors["description"])

	def test_short_description_is_rejected(self):
		form = self._form(description="Too short")
		self.assertIn("description", form.errors)
		self.assertIn(
			"Description must be at least 10 characters.", form.errors["description"]
		)

	def test_invalid_next_review_date_is_rejected(self):
		form = self._form(next_review_date="not-a-date")
		self.assertIn("next_review_date", form.errors)
		self.assertIn(
			"Enter a valid date (for example 2026-01-31).",
			form.errors["next_review_date"],
		)

	def test_duplicate_code_is_rejected_once(self):
		self._policy(title="Existing policy", code="GOV-001")
		form = self._form()
		self.assertIn("code", form.errors)
		self.assertEqual(len(form.errors["code"]), 1)
		self.assertIn(
			"Another policy of your company already uses this code.", form.errors["code"]
		)

	def test_editing_a_policy_keeps_its_own_code(self):
		policy = self._policy(title="Editable policy", code="GOV-777")
		form = self._form(instance=policy, title="Editable policy renamed", code="GOV-777")
		self.assertTrue(form.is_valid(), form.errors)

	def test_same_code_is_allowed_in_another_company(self):
		Policy.objects.create(
			company=self.other_company,
			title="Foreign policy",
			code="GOV-001",
			created_by=self.other_owner,
		)
		form = self._form()
		self.assertTrue(form.is_valid(), form.errors)
