"""Supplier sustainability business logic.

Everything here is tenant-scoped: callers pass a company resolved from the
authenticated user, and every queryset starts from that company, so figures can
never include another tenant's suppliers or emissions.
"""

from decimal import Decimal

from django.db.models import Count, Sum

from apps.carbon.models import ActivityStatus, CarbonActivity, Scope

from .models import ESGStatus, RiskLevel, Supplier, SupplierDocumentType

# The documentation a supplier must provide to be considered ESG complete.
REQUIRED_DOCUMENT_TYPES = (
	SupplierDocumentType.POLICY,
	SupplierDocumentType.CERTIFICATE,
	SupplierDocumentType.EMISSIONS_REPORT,
)

KG_PER_TONNE = Decimal("1000")
TONNE_PLACES = Decimal("0.0001")


def to_tonnes(kg):
	"""Convert kilograms of CO2e to tonnes, rounded for display."""
	if kg is None:
		return Decimal("0")
	return (Decimal(kg) / KG_PER_TONNE).quantize(TONNE_PLACES)


def compute_esg_status(document_types):
	"""Derive ESG completeness from the document types a supplier has provided.

	* ``MISSING``  — no document at all.
	* ``COMPLETE`` — every required document type is present.
	* ``PARTIAL``  — some documentation exists but the required set is incomplete.
	"""
	present = {document_type for document_type in document_types if document_type}
	if not present:
		return ESGStatus.MISSING
	if set(REQUIRED_DOCUMENT_TYPES).issubset(present):
		return ESGStatus.COMPLETE
	return ESGStatus.PARTIAL


def missing_document_types(document_types):
	"""Required document types the supplier has not provided yet."""
	present = {document_type for document_type in document_types if document_type}
	return [doc for doc in REQUIRED_DOCUMENT_TYPES if doc not in present]


def refresh_esg_status(supplier):
	"""Recompute and persist a supplier's ESG status from its documents."""
	return supplier.recompute_esg_status()


def scope3_tonnes_by_supplier(company):
	"""Approved, non-archived Scope 3 emissions (tonnes) grouped by supplier id.

	Only activities explicitly linked to a supplier are counted, and the
	queryset is restricted to the caller's own company.
	"""
	rows = (
		CarbonActivity.objects.filter(
			company=company,
			supplier__isnull=False,
			scope=Scope.SCOPE_3,
			status=ActivityStatus.APPROVED,
			is_archived=False,
		)
		.values("supplier_id")
		.annotate(total_kg=Sum("co2e_kg"))
	)
	return {row["supplier_id"]: to_tonnes(row["total_kg"]) for row in rows}


def supplier_scope3_summary(supplier):
	"""Scope 3 activities linked to a supplier, with the approved total.

	The queryset starts from the supplier's own company, so it can never reach
	another tenant's activities.
	"""
	activities = (
		CarbonActivity.objects.filter(company=supplier.company, supplier=supplier)
		.select_related("department")
		.order_by("-period_start", "-id")
	)
	approved = activities.filter(status=ActivityStatus.APPROVED, is_archived=False)
	approved_kg = approved.aggregate(total=Sum("co2e_kg"))["total"]
	return {
		"activities": list(activities),
		"total_tonnes": to_tonnes(approved_kg),
		"approved_count": approved.count(),
		"count": activities.count(),
	}


def priority_suppliers(company, *, limit=8):
	"""Suppliers that need attention, ranked deterministically.

	A supplier is a priority when it is missing ESG documentation, carries a
	HIGH risk level, and/or accounts for a large share of the company's approved
	Scope 3 emissions. The ranking is rule-based (no machine learning) and
	always scoped to one company.
	"""
	suppliers = list(
		Supplier.objects.filter(company=company, is_active=True).annotate(
			doc_count=Count("documents")
		)
	)
	if not suppliers:
		return []

	totals = scope3_tonnes_by_supplier(company)
	max_tonnes = max(totals.values(), default=Decimal("0"))

	ranked = []
	for supplier in suppliers:
		tonnes = totals.get(supplier.pk, Decimal("0"))
		reasons = []
		score = 0.0

		if supplier.esg_status != ESGStatus.COMPLETE:
			score += 2
			reasons.append(
				"Missing ESG documentation"
				if supplier.esg_status == ESGStatus.MISSING
				else "Incomplete ESG documentation"
			)
		if supplier.risk_level == RiskLevel.HIGH:
			score += 2
			reasons.append("High risk supplier")
		elif supplier.risk_level == RiskLevel.MEDIUM:
			score += 1
			reasons.append("Medium risk supplier")
		if tonnes > 0 and max_tonnes > 0:
			score += float(tonnes / max_tonnes) * 3
			reasons.append(f"{tonnes:.2f} t CO2e in linked Scope 3 activity")

		if score <= 0:
			continue
		ranked.append(
			{
				"supplier": supplier,
				"score": round(score, 2),
				"reasons": reasons,
				"scope3_tonnes": tonnes,
				"document_count": supplier.doc_count,
			}
		)

	ranked.sort(
		key=lambda row: (
			-row["score"],
			-row["scope3_tonnes"],
			row["supplier"].name.lower(),
		)
	)
	return ranked[:limit]


def comparison_rows(company, suppliers):
	"""Build the comparison table rows for a set of the company's suppliers."""
	totals = scope3_tonnes_by_supplier(company)
	rows = []
	for supplier in suppliers:
		document_types = set(supplier.documents.values_list("document_type", flat=True))
		rows.append(
			{
				"supplier": supplier,
				"document_count": supplier.documents.count(),
				"missing_types": [
					SupplierDocumentType(doc).label
					for doc in missing_document_types(document_types)
				],
				"scope3_tonnes": totals.get(supplier.pk, Decimal("0")),
			}
		)
	return rows
