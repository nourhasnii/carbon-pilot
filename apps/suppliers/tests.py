"""Tests for the supplier sustainability module.
Covers supplier CRUD (with soft deactivation), document upload/delete/download,
the derived ESG completeness status, priority-supplier ranking, the Scope 3
link between suppliers and carbon activities, tenant isolation (cross-tenant
access is a 404) and HTTP role permissions (auditors are read-only).
"""

import re
import shutil
import tempfile
from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
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
from apps.enterprise.models import Company

from .models import (
	ESGStatus,
	RiskLevel,
	Supplier,
	SupplierDocument,
	SupplierDocumentType,
)
from .services import (
	compute_esg_status,
	missing_document_types,
	priority_suppliers,
	scope3_tonnes_by_supplier,
)

def _upload(name="doc.pdf", content=b"supplier document"):
	return SimpleUploadedFile(name, content, content_type="application/pdf")


def _next_link(html):
	"""Return the href of the pagination "Next" link (rel="next"), or ""."""
	match = re.search(r'<a[^>]*href="([^"]*)"[^>]*rel="next"', html)
	return match.group(1) if match else ""


class SupplierTestCase(TestCase):
	password = "SecureTestPassword2026!"

	def setUp(self):
		# Each test gets its own media folder so uploads never touch the repo.
		self._media = tempfile.mkdtemp(prefix="supplier-tests-")
		self._media_override = override_settings(MEDIA_ROOT=self._media)
		self._media_override.enable()
		self.addCleanup(self._media_override.disable)
		self.addCleanup(shutil.rmtree, self._media, ignore_errors=True)

		self.owner = User.objects.create_user(
			email="supplier-owner@carbonpilot.test",
			password=self.password,
			first_name="Supplier",
			last_name="Owner",
			role=User.Role.COMPANY_OWNER,
			status=User.Status.ACTIVE,
		)
		self.company = Company.objects.create(
			owner=self.owner, name="Carbon Works", country="Tunisia"
		)
		self.manager = User.objects.create_user(
			email="esg-manager@carbonpilot.test",
			password=self.password,
			role=User.Role.ESG_REPORTING_MANAGER,
			status=User.Status.ACTIVE,
			company="Carbon Works",
			company_membership=self.company,
		)
		self.auditor = User.objects.create_user(
			email="auditor@carbonpilot.test",
			password=self.password,
			role=User.Role.AUDITOR,
			status=User.Status.ACTIVE,
			company="Carbon Works",
			company_membership=self.company,
		)

		# A completely separate tenant used to prove isolation.
		self.other_owner = User.objects.create_user(
			email="other-supplier-owner@carbonpilot.test",
			password=self.password,
			role=User.Role.COMPANY_OWNER,
			status=User.Status.ACTIVE,
		)
		self.other_company = Company.objects.create(
			owner=self.other_owner, name="Other Works", country="France"
		)

	def make_supplier(self, *, company=None, name="Nordwind Logistics", **overrides):
		payload = {
			"company": company or self.company,
			"name": name,
			"country": "Tunisia",
			"category": "Logistics",
			"created_by": self.owner,
		}
		payload.update(overrides)
		return Supplier.objects.create(**payload)

	def make_document(self, supplier, document_type=SupplierDocumentType.POLICY, title="Doc", company=None):
		return SupplierDocument.objects.create(
			supplier=supplier,
			company=company or supplier.company,
			title=title,
			document_type=document_type,
			file=_upload(),
			uploaded_by=self.owner,
		)

	def login(self, user):
		self.client.force_login(user)


class ESGStatusLogicTests(SupplierTestCase):
	def test_no_documents_is_missing(self):
		self.assertEqual(compute_esg_status(set()), ESGStatus.MISSING)
		self.assertEqual(self.make_supplier().esg_status, ESGStatus.MISSING)

	def test_some_documents_is_partial(self):
		self.assertEqual(
			compute_esg_status({SupplierDocumentType.POLICY}), ESGStatus.PARTIAL
		)

	def test_only_other_documents_is_partial(self):
		self.assertEqual(
			compute_esg_status({SupplierDocumentType.OTHER}), ESGStatus.PARTIAL
		)

	def test_all_required_documents_is_complete(self):
		status = compute_esg_status(
			{
				SupplierDocumentType.POLICY,
				SupplierDocumentType.CERTIFICATE,
				SupplierDocumentType.EMISSIONS_REPORT,
			}
		)
		self.assertEqual(status, ESGStatus.COMPLETE)

	def test_missing_document_types_lists_required_only(self):
		missing = missing_document_types({SupplierDocumentType.POLICY})
		self.assertEqual(
			missing,
			[SupplierDocumentType.CERTIFICATE, SupplierDocumentType.EMISSIONS_REPORT],
		)

	def test_recompute_reflects_uploaded_documents(self):
		supplier = self.make_supplier()
		self.make_document(supplier, SupplierDocumentType.POLICY)
		self.assertEqual(supplier.recompute_esg_status(), ESGStatus.PARTIAL)
		supplier.refresh_from_db()
		self.assertEqual(supplier.esg_status, ESGStatus.PARTIAL)

		self.make_document(supplier, SupplierDocumentType.CERTIFICATE)
		self.make_document(supplier, SupplierDocumentType.EMISSIONS_REPORT)
		self.assertEqual(supplier.recompute_esg_status(), ESGStatus.COMPLETE)
		supplier.refresh_from_db()
		self.assertEqual(supplier.esg_status, ESGStatus.COMPLETE)


class SupplierModelTests(SupplierTestCase):
	def test_document_company_is_forced_from_supplier(self):
		supplier = self.make_supplier()
		# A wrong company passed by a caller must be corrected on save.
		document = self.make_document(supplier, company=self.other_company)
		self.assertEqual(document.company_id, self.company.pk)

	def test_unique_name_per_company_but_not_across_tenants(self):
		self.make_supplier(name="Shared Name")
		other = self.make_supplier(company=self.other_company, name="Shared Name")
		self.assertEqual(other.company_id, self.other_company.pk)

	def test_str_uses_name(self):
		supplier = self.make_supplier(name="Acme Metals")
		self.assertIn("Acme Metals", str(supplier))


class Scope3LinkTests(SupplierTestCase):
	def setUp(self):
		super().setUp()
		self.factor = EmissionFactor.objects.create(
			name="Purchased goods — demo",
			category=ActivityCategory.PURCHASED_GOODS,
			scope=Scope.SCOPE_3,
			value=Decimal("1.5"),
			unit="kg",
			region="Global",
			year=2026,
			source="Demo source",
			verification_status=VerificationStatus.VERIFIED,
		)

	def make_activity(self, *, supplier=None, status=ActivityStatus.APPROVED, company=None, **overrides):
		payload = {
			"company": company or self.company,
			"supplier": supplier,
			"name": "Procured steel",
			"category": ActivityCategory.PURCHASED_GOODS,
			"scope": Scope.SCOPE_3,
			"quantity": Decimal("1000"),
			"unit": "kg",
			"period_start": date(2026, 2, 1),
			"emission_factor": self.factor,
			"status": status,
			"created_by": self.owner,
		}
		payload.update(overrides)
		activity = CarbonActivity(**payload)
		activity.save()
		return activity

	def test_only_approved_scope3_activities_are_totalled(self):
		supplier = self.make_supplier()
		self.make_activity(supplier=supplier)
		self.make_activity(supplier=supplier, status=ActivityStatus.DRAFT)
		totals = scope3_tonnes_by_supplier(self.company)
		# 1000 kg × 1.5 = 1500 kg = 1.5 t (draft excluded).
		self.assertEqual(totals[supplier.pk], Decimal("1.5000"))

	def test_totals_do_not_leak_across_tenants(self):
		other_supplier = self.make_supplier(company=self.other_company, name="Far Away Ltd")
		self.make_activity(supplier=other_supplier, company=self.other_company)
		self.assertEqual(scope3_tonnes_by_supplier(self.company), {})

	def test_activity_supplier_must_belong_to_same_company(self):
		other_supplier = self.make_supplier(company=self.other_company, name="Foreign")
		activity = CarbonActivity(
			company=self.company,
			supplier=other_supplier,
			name="Invalid",
			category=ActivityCategory.PURCHASED_GOODS,
			scope=Scope.SCOPE_3,
			quantity=Decimal("10"),
			unit="kg",
			period_start=date(2026, 2, 1),
			emission_factor=self.factor,
			status=ActivityStatus.DRAFT,
		)
		with self.assertRaises(ValidationError):
			activity.save()


class PrioritySupplierTests(SupplierTestCase):
	def _make_complete(self, name, **overrides):
		supplier = self.make_supplier(name=name, **overrides)
		for doc_type in (
			SupplierDocumentType.POLICY,
			SupplierDocumentType.CERTIFICATE,
			SupplierDocumentType.EMISSIONS_REPORT,
		):
			self.make_document(supplier, doc_type)
		supplier.recompute_esg_status()
		return supplier

	def test_suppliers_needing_attention_are_ranked(self):
		# Complete + low risk + no linked emissions => not a priority.
		self._make_complete("Low Risk Co", risk_level=RiskLevel.LOW)
		# Missing documentation + HIGH risk => top priority.
		self.make_supplier(name="Risky Co", risk_level=RiskLevel.HIGH)
		# Complete documentation but MEDIUM risk => still worth attention.
		self._make_complete("Medium Risk Co", risk_level=RiskLevel.MEDIUM)

		ranked = priority_suppliers(self.company)
		names = [row["supplier"].name for row in ranked]
		self.assertEqual(names, ["Risky Co", "Medium Risk Co"])

	def test_complete_low_risk_supplier_is_not_a_priority(self):
		self._make_complete("Clean Co", risk_level=RiskLevel.LOW)
		self.assertEqual(priority_suppliers(self.company), [])

	def test_other_tenant_suppliers_are_never_ranked(self):
		self.make_supplier(company=self.other_company, name="Foreign Supplier")
		self.assertEqual(priority_suppliers(self.company), [])


class SupplierViewTests(SupplierTestCase):
	def test_list_requires_login(self):
		response = self.client.get(reverse("suppliers:supplier_list"))
		self.assertEqual(response.status_code, 302)
		self.assertIn("/login", response.url)

	def test_supplier_role_cannot_open_the_module(self):
		supplier_user = User.objects.create_user(
			email="supplier-role@carbonpilot.test",
			password=self.password,
			role=User.Role.SUPPLIER,
			status=User.Status.ACTIVE,
			company="Carbon Works",
			company_membership=self.company,
		)
		self.login(supplier_user)
		response = self.client.get(reverse("suppliers:supplier_list"))
		self.assertEqual(response.status_code, 403)

	def test_owner_sees_only_their_suppliers(self):
		self.make_supplier(name="Visible Ltd")
		self.make_supplier(company=self.other_company, name="Hidden Ltd")
		self.login(self.owner)
		response = self.client.get(reverse("suppliers:supplier_list"))
		self.assertContains(response, "Visible Ltd")
		self.assertNotContains(response, "Hidden Ltd")

	def test_create_supplier_uses_session_company(self):
		self.login(self.owner)
		response = self.client.post(
			reverse("suppliers:supplier_create"),
			{
				"name": "New Vendor",
				"country": "Tunisia",
				"category": "Raw materials",
				"risk_level": "",
				"is_active": "on",
			},
		)
		supplier = Supplier.objects.get(name="New Vendor")
		self.assertEqual(response.status_code, 302)
		self.assertEqual(supplier.company_id, self.company.pk)
		self.assertEqual(supplier.created_by, self.owner)
		self.assertEqual(supplier.esg_status, ESGStatus.MISSING)

	def test_create_rejects_duplicate_name_in_same_company(self):
		self.make_supplier(name="Duplicate Ltd")
		self.login(self.owner)
		response = self.client.post(
			reverse("suppliers:supplier_create"),
			{"name": "Duplicate Ltd", "country": "Tunisia", "category": "Logistics", "is_active": "on"},
		)
		self.assertEqual(response.status_code, 200)
		self.assertEqual(Supplier.objects.filter(name="Duplicate Ltd").count(), 1)

	def test_edit_supplier(self):
		supplier = self.make_supplier()
		self.login(self.owner)
		response = self.client.post(
			reverse("suppliers:supplier_edit", args=[supplier.pk]),
			{
				"name": supplier.name,
				"country": "Tunisia",
				"category": "Logistics",
				"city": "Tunis",
				"risk_level": RiskLevel.MEDIUM,
				"is_active": "on",
			},
		)
		supplier.refresh_from_db()
		self.assertEqual(response.status_code, 302)
		self.assertEqual(supplier.city, "Tunis")
		self.assertEqual(supplier.risk_level, RiskLevel.MEDIUM)

	def test_soft_deactivate_and_reactivate(self):
		supplier = self.make_supplier()
		self.login(self.owner)

		self.client.post(reverse("suppliers:supplier_toggle_active", args=[supplier.pk]))
		supplier.refresh_from_db()
		self.assertFalse(supplier.is_active)
		# The row is kept, never hard-deleted.
		self.assertTrue(Supplier.objects.filter(pk=supplier.pk).exists())

		self.client.post(reverse("suppliers:supplier_toggle_active", args=[supplier.pk]))
		supplier.refresh_from_db()
		self.assertTrue(supplier.is_active)

	def test_document_upload_updates_esg_status(self):
		supplier = self.make_supplier()
		self.login(self.owner)
		for doc_type in (
			SupplierDocumentType.POLICY,
			SupplierDocumentType.CERTIFICATE,
			SupplierDocumentType.EMISSIONS_REPORT,
		):
			response = self.client.post(
				reverse("suppliers:supplier_document_upload", args=[supplier.pk]),
				{"title": f"{doc_type} doc", "document_type": doc_type, "file": _upload()},
			)
			self.assertEqual(response.status_code, 302)
		supplier.refresh_from_db()
		self.assertEqual(supplier.esg_status, ESGStatus.COMPLETE)
		self.assertEqual(supplier.documents.count(), 3)

	def test_document_delete_recomputes_status(self):
		supplier = self.make_supplier()
		document = self.make_document(supplier, SupplierDocumentType.POLICY)
		supplier.recompute_esg_status()
		self.login(self.owner)
		response = self.client.post(
			reverse("suppliers:supplier_document_delete", args=[document.pk])
		)
		supplier.refresh_from_db()
		self.assertEqual(response.status_code, 302)
		self.assertEqual(supplier.documents.count(), 0)
		self.assertEqual(supplier.esg_status, ESGStatus.MISSING)

	def test_document_download_streams_the_file(self):
		supplier = self.make_supplier()
		document = self.make_document(supplier)
		self.login(self.owner)
		response = self.client.get(
			reverse("suppliers:supplier_document_download", args=[document.pk])
		)
		self.assertEqual(response.status_code, 200)
		self.assertEqual(response["Content-Disposition"].split(";")[0], "attachment")

	def test_detail_shows_scope3_activities(self):
		factor = EmissionFactor.objects.create(
			name="Purchased goods — demo",
			category=ActivityCategory.PURCHASED_GOODS,
			scope=Scope.SCOPE_3,
			value=Decimal("2"),
			unit="kg",
			region="Global",
			year=2026,
			source="Demo source",
			verification_status=VerificationStatus.VERIFIED,
		)
		supplier = self.make_supplier()
		activity = CarbonActivity(
			company=self.company,
			supplier=supplier,
			name="Procured steel",
			category=ActivityCategory.PURCHASED_GOODS,
			scope=Scope.SCOPE_3,
			quantity=Decimal("1000"),
			unit="kg",
			period_start=date(2026, 2, 1),
			emission_factor=factor,
			status=ActivityStatus.APPROVED,
			created_by=self.owner,
		)
		activity.save()
		self.login(self.owner)
		response = self.client.get(reverse("suppliers:supplier_detail", args=[supplier.pk]))
		self.assertContains(response, "Procured steel")
		self.assertEqual(response.context["scope3_total_tonnes"], Decimal("2.0000"))

	def test_auditor_is_read_only(self):
		supplier = self.make_supplier()
		self.login(self.auditor)
		self.assertEqual(
			self.client.get(reverse("suppliers:supplier_list")).status_code, 200
		)
		self.assertEqual(
			self.client.get(reverse("suppliers:supplier_detail", args=[supplier.pk])).status_code,
			200,
		)
		self.assertEqual(
			self.client.post(
				reverse("suppliers:supplier_toggle_active", args=[supplier.pk])
			).status_code,
			403,
		)
		self.assertEqual(
			self.client.get(reverse("suppliers:supplier_create")).status_code, 403
		)

	def test_esg_manager_can_manage_suppliers(self):
		supplier = self.make_supplier()
		self.login(self.manager)
		response = self.client.post(
			reverse("suppliers:supplier_toggle_active", args=[supplier.pk])
		)
		supplier.refresh_from_db()
		self.assertEqual(response.status_code, 302)
		self.assertFalse(supplier.is_active)

	def test_cross_tenant_detail_is_404(self):
		foreign = self.make_supplier(company=self.other_company, name="Foreign Ltd")
		self.login(self.owner)
		response = self.client.get(reverse("suppliers:supplier_detail", args=[foreign.pk]))
		self.assertEqual(response.status_code, 404)

	def test_cross_tenant_document_download_is_404(self):
		foreign = self.make_supplier(company=self.other_company, name="Foreign Ltd")
		document = self.make_document(foreign)
		self.login(self.owner)
		response = self.client.get(
			reverse("suppliers:supplier_document_download", args=[document.pk])
		)
		self.assertEqual(response.status_code, 404)

	def test_cross_tenant_cannot_deactivate(self):
		foreign = self.make_supplier(company=self.other_company, name="Foreign Ltd")
		self.login(self.owner)
		response = self.client.post(
			reverse("suppliers:supplier_toggle_active", args=[foreign.pk])
		)
		foreign.refresh_from_db()
		self.assertEqual(response.status_code, 404)
		self.assertTrue(foreign.is_active)

	def test_comparison_only_includes_own_suppliers(self):
		mine = self.make_supplier(name="Mine Ltd")
		foreign = self.make_supplier(company=self.other_company, name="Foreign Ltd")
		self.login(self.owner)
		response = self.client.get(
			reverse("suppliers:supplier_compare"),
			{"ids": [mine.pk, foreign.pk]},
		)
		self.assertEqual(response.status_code, 200)
		self.assertContains(response, "Mine Ltd")
		self.assertNotContains(response, "Foreign Ltd")

	def test_filter_by_esg_status(self):
		missing = self.make_supplier(name="Missing Docs Ltd")
		partial = self.make_supplier(name="Partial Docs Ltd")
		self.make_document(partial, SupplierDocumentType.POLICY)
		partial.recompute_esg_status()

		self.login(self.owner)
		response = self.client.get(reverse("suppliers:supplier_list"), {"esg_status": ESGStatus.PARTIAL})
		items = response.context["pagination"]["items"]
		self.assertEqual([supplier.name for supplier in items], ["Partial Docs Ltd"])


class SupplierListFilterPaginationTests(SupplierTestCase):
	"""Shared filtering, search, pagination and empty states on the list."""

	def _make_many(self, count, *, category="Logistics"):
		return [
			self.make_supplier(name=f"Supplier {index:02d}", category=category)
			for index in range(count)
		]

	def test_filter_combination_narrows_the_result_set(self):
		self.make_supplier(name="Nordwind Logistics", category="Logistics", country="Tunisia")
		self.make_supplier(name="Solaris Energy", category="Energy", country="Tunisia")
		self.login(self.owner)
		response = self.client.get(
			reverse("suppliers:supplier_list"),
			{"category": "Logistics", "country": "Tunisia"},
		)
		self.assertEqual(response.status_code, 200)
		pagination = response.context["pagination"]
		self.assertEqual(pagination["total"], 1)
		self.assertEqual(
			[supplier.name for supplier in pagination["items"]], ["Nordwind Logistics"]
		)

	def test_default_page_size_is_ten(self):
		self._make_many(12)
		self.login(self.owner)
		response = self.client.get(reverse("suppliers:supplier_list"))
		pagination = response.context["pagination"]
		self.assertEqual(pagination["per_page"], 10)
		self.assertEqual(len(pagination["items"]), 10)
		self.assertEqual(pagination["pages"], 2)

	def test_per_page_twenty_shows_more_rows(self):
		self._make_many(15)
		self.login(self.owner)
		response = self.client.get(reverse("suppliers:supplier_list"), {"per_page": "20"})
		pagination = response.context["pagination"]
		self.assertEqual(pagination["per_page"], 20)
		self.assertEqual(len(pagination["items"]), 15)

	def test_next_link_preserves_active_filters(self):
		self._make_many(12)
		self.login(self.owner)
		response = self.client.get(
			reverse("suppliers:supplier_list"), {"category": "Logistics"}
		)
		pagination = response.context["pagination"]
		self.assertTrue(pagination["has_next"])
		self.assertEqual(pagination["next_page"], 2)
		href = _next_link(response.content.decode())
		self.assertIn("category=Logistics", href)
		self.assertIn("page=2", href)

	def test_filter_with_no_match_renders_empty_state(self):
		self.make_supplier(name="Nordwind Logistics")
		self.login(self.owner)
		response = self.client.get(
			reverse("suppliers:supplier_list"), {"q": "does-not-exist"}
		)
		self.assertEqual(response.status_code, 200)
		self.assertContains(response, "cp-empty")
		self.assertContains(response, "No suppliers match these filters.")

	def test_cross_tenant_rows_never_appear(self):
		self.make_supplier(name="Visible Ltd")
		self.make_supplier(company=self.other_company, name="Hidden Ltd")
		self.login(self.owner)
		response = self.client.get(reverse("suppliers:supplier_list"))
		names = [supplier.name for supplier in response.context["pagination"]["items"]]
		self.assertIn("Visible Ltd", names)
		self.assertNotIn("Hidden Ltd", names)
		self.assertNotContains(response, "Hidden Ltd")
