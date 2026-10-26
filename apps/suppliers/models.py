"""Supplier sustainability.

A :class:`Supplier` is a company's supplier (vendor). Every row belongs to
exactly one company and is never shared across tenants. A supplier can be
linked to that company's carbon activities (typically Scope 3 purchased goods
and services) through ``CarbonActivity.supplier``.

Its environmental, social and governance documentation is tracked with
:class:`SupplierDocument` rows; the resulting ESG completeness is summarised by
``esg_status`` (``COMPLETE`` / ``PARTIAL`` / ``MISSING``).
"""

import os
import uuid

from django.conf import settings
from django.db import models

from apps.enterprise.models import Company


class ESGStatus(models.TextChoices):
	COMPLETE = "COMPLETE", "Complete"
	PARTIAL = "PARTIAL", "Partial"
	MISSING = "MISSING", "Missing"


class RiskLevel(models.TextChoices):
	LOW = "LOW", "Low"
	MEDIUM = "MEDIUM", "Medium"
	HIGH = "HIGH", "High"


class SupplierDocumentType(models.TextChoices):
	POLICY = "POLICY", "Policy"
	CERTIFICATE = "CERTIFICATE", "Certificate"
	EMISSIONS_REPORT = "EMISSIONS_REPORT", "Emissions report"
	OTHER = "OTHER", "Other"


def supplier_document_upload_to(instance, filename):
	"""Store each document under its own company/supplier folder.

	Tenants never share a folder, and a document always lives next to the
	company that owns it, never next to the uploader.
	"""
	company_id = instance.company_id or "unassigned"
	supplier_id = instance.supplier_id or "unassigned"
	extension = os.path.splitext(filename)[1].lower()
	return f"suppliers/company_{company_id}/supplier_{supplier_id}/{uuid.uuid4().hex}{extension}"


class SupplierQuerySet(models.QuerySet):
	def for_company(self, company):
		return self.filter(company=company)

	def active(self):
		return self.filter(is_active=True)


class Supplier(models.Model):
	"""A supplier/vendor of one company, tracked for Scope 3 sustainability."""

	class Meta:
		ordering = ("name", "id")
		verbose_name = "Supplier"
		constraints = [
			models.UniqueConstraint(
				fields=("company", "name"),
				name="uniq_supplier_company_name",
			),
		]
		indexes = [
			models.Index(fields=("company", "is_active")),
			models.Index(fields=("company", "esg_status")),
			models.Index(fields=("company", "risk_level")),
		]

	company = models.ForeignKey(
		Company, on_delete=models.CASCADE, related_name="suppliers"
	)
	name = models.CharField(max_length=255)
	legal_name = models.CharField(max_length=255, blank=True)
	country = models.CharField(max_length=120)
	city = models.CharField(max_length=120, blank=True)
	category = models.CharField(
		max_length=120,
		help_text="Supplier category or sector, e.g. Raw materials, Logistics.",
	)
	contact_name = models.CharField(max_length=255, blank=True)
	contact_email = models.EmailField(blank=True)
	contact_phone = models.CharField(max_length=32, blank=True)
	website = models.URLField(blank=True)
	esg_status = models.CharField(
		max_length=16,
		choices=ESGStatus.choices,
		default=ESGStatus.MISSING,
		help_text="Derived from the supplier's uploaded documentation.",
	)
	risk_level = models.CharField(
		max_length=8,
		choices=RiskLevel.choices,
		blank=True,
		help_text="Optional manual risk rating for the supplier.",
	)
	notes = models.TextField(blank=True)
	# Soft disable: an inactive supplier leaves the working list but keeps its
	# documents, linked activities and history intact.
	is_active = models.BooleanField(default=True)
	created_by = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		null=True,
		blank=True,
		on_delete=models.SET_NULL,
		related_name="suppliers_created",
	)
	created_at = models.DateTimeField(auto_now_add=True)
	updated_at = models.DateTimeField(auto_now=True)

	objects = SupplierQuerySet.as_manager()

	def __str__(self):
		return f"{self.name} · company {self.company_id}"

	@property
	def status_label(self):
		return "Active" if self.is_active else "Inactive"

	@property
	def display_legal_name(self):
		return self.legal_name or self.name

	@property
	def location_label(self):
		parts = [self.city, self.country]
		return ", ".join(part for part in parts if part)

	@property
	def has_scope3(self):
		"""True when at least one Scope 3 activity references this supplier."""
		return self.activities.filter(scope="SCOPE_3").exists()

	def recompute_esg_status(self, *, save=True):
		"""Recalculate ESG completeness from the supplier's documents.

		Returns the new status. The derivation itself lives in
		``apps.suppliers.services`` so it stays in one place and is reusable.
		"""
		from apps.suppliers.services import compute_esg_status

		document_types = set(self.documents.values_list("document_type", flat=True))
		status = compute_esg_status(document_types)
		if status != self.esg_status:
			self.esg_status = status
			if save and self.pk:
				type(self).objects.filter(pk=self.pk).update(esg_status=status)
		return status


class SupplierDocument(models.Model):
	"""A supporting document (policy, certificate, emissions report…) of a supplier."""

	class Meta:
		ordering = ("-uploaded_at", "-id")
		verbose_name = "Supplier document"
		indexes = [
			models.Index(fields=("company", "supplier")),
			models.Index(fields=("company", "document_type")),
		]

	supplier = models.ForeignKey(
		Supplier, on_delete=models.CASCADE, related_name="documents"
	)
	# Stored explicitly for tenant isolation: every document can be filtered by
	# company without joining through the supplier.
	company = models.ForeignKey(
		Company, on_delete=models.CASCADE, related_name="supplier_documents"
	)
	title = models.CharField(max_length=255)
	document_type = models.CharField(
		max_length=24,
		choices=SupplierDocumentType.choices,
		default=SupplierDocumentType.OTHER,
	)
	file = models.FileField(upload_to=supplier_document_upload_to)
	uploaded_by = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		null=True,
		blank=True,
		on_delete=models.SET_NULL,
		related_name="supplier_documents_uploaded",
	)
	uploaded_at = models.DateTimeField(auto_now_add=True)

	def __str__(self):
		return f"{self.title} ({self.get_document_type_display()})"

	@property
	def filename(self):
		return os.path.basename(self.file.name) if self.file else ""

	def save(self, *args, **kwargs):
		# The owning company is always derived from the supplier — never from
		# the request — so a document can never be filed against another tenant.
		if self.supplier_id:
			self.company_id = self.supplier.company_id
		super().save(*args, **kwargs)
