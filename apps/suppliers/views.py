"""Supplier sustainability views.

Tenant isolation mirrors the rest of CarbonPilot: the company is always
resolved from the authenticated user (never from the request body), and every
supplier or document is fetched with ``company=company`` so another tenant's
record is a 404 rather than a 403.

Managing suppliers (create/edit/deactivate/documents) is limited to the Company
Owner and the ESG Reporting Manager. Other company roles may view the module;
auditors are read-only.
"""

import os

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.http import FileResponse, Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods, require_POST

from apps.accounts.models import User
from apps.accounts.permissions import role_required
from apps.carbon.services.tenancy import company_for_user
from apps.carbon.views import _sidebar as carbon_sidebar
from apps.common.listing import paginate

from .forms import SupplierDocumentForm, SupplierFilterForm, SupplierForm
from .models import Supplier, SupplierDocument, SupplierDocumentType
from .services import (
	comparison_rows,
	missing_document_types,
	priority_suppliers,
	refresh_esg_status,
	supplier_scope3_summary,
)

# Roles that may view the supplier module (auditors are read-only).
VIEW_ROLES = (
	User.Role.COMPANY_OWNER,
	User.Role.DEPARTMENT_MANAGER,
	User.Role.ESG_REPORTING_MANAGER,
	User.Role.AUDITOR,
)

# Roles that may create/edit suppliers and manage their documents.
MANAGE_ROLES = (User.Role.COMPANY_OWNER, User.Role.ESG_REPORTING_MANAGER)

COMPARE_LIMIT = 6


def _sidebar(active, user):
	"""Reuse the carbon navigation and add the Suppliers entry to it."""
	items = carbon_sidebar(active, user)
	index = next(
		(i for i, item in enumerate(items) if item.get("url_name") == "carbon:dashboard"),
		0,
	)
	items.insert(index + 1, {
		"label": "Suppliers",
		"icon": "truck",
		"url_name": "suppliers:supplier_list",
		"active": active == "Suppliers",
	})
	return items


def _module_context(company, active, user):
	return {
		"company": company,
		"sidebar_nav": _sidebar(active, user),
	}


def _require_company(request):
	"""Resolve the tenant from the user; never trusts client input."""
	company = company_for_user(request.user)
	if company is None:
		messages.warning(request, "Set up your company before managing suppliers.")
	return company


def _can_manage(user):
	return user.role == User.Role.SUPER_ADMIN or user.role in MANAGE_ROLES


def _scoped_supplier_or_404(company, pk):
	"""Fetch a supplier only within the caller's own company (else 404)."""
	supplier = Supplier.objects.filter(company=company, pk=pk).first()
	if supplier is None:
		raise Http404("Supplier not found.")
	return supplier


def _scoped_document_or_404(company, pk):
	document = (
		SupplierDocument.objects.select_related("supplier")
		.filter(company=company, pk=pk)
		.first()
	)
	if document is None:
		raise Http404("Document not found.")
	return document


def _apply_filters(queryset, form):
	if not form.is_valid():
		return queryset
	data = form.cleaned_data
	term = (data.get("q") or "").strip()
	if term:
		queryset = queryset.filter(
			Q(name__icontains=term)
			| Q(legal_name__icontains=term)
			| Q(contact_name__icontains=term)
			| Q(contact_email__icontains=term)
		)
	if data.get("esg_status"):
		queryset = queryset.filter(esg_status=data["esg_status"])
	if data.get("risk_level"):
		queryset = queryset.filter(risk_level=data["risk_level"])
	if data.get("country"):
		queryset = queryset.filter(country=data["country"])
	if data.get("category"):
		queryset = queryset.filter(category=data["category"])
	if data.get("active") == "active":
		queryset = queryset.filter(is_active=True)
	elif data.get("active") == "inactive":
		queryset = queryset.filter(is_active=False)
	return queryset


def _option_values(company, field):
	return list(
		Supplier.objects.filter(company=company)
		.exclude(**{field: ""})
		.values_list(field, flat=True)
		.distinct()
		.order_by(field)
	)


# --------------------------------------------------------------------------- #
# Supplier CRUD
# --------------------------------------------------------------------------- #
@login_required
@role_required(*VIEW_ROLES)
def supplier_list(request):
	company = _require_company(request)
	if company is None:
		return redirect(f"{reverse('accounts:dashboard')}?section=company")

	filter_form = SupplierFilterForm(request.GET or None, company=company)
	suppliers = Supplier.objects.filter(company=company).annotate(
		document_count=Count("documents")
	)
	suppliers = _apply_filters(suppliers, filter_form)
	pagination = paginate(request, suppliers.order_by("name"))

	context = _module_context(company, "Suppliers", request.user)
	context.update({
		"filter_form": filter_form,
		"pagination": pagination,
		"has_filters": any(
			request.GET.get(key)
			for key in ("q", "esg_status", "risk_level", "country", "category", "active")
		),
		"can_manage": _can_manage(request.user),
		"priority": priority_suppliers(company),
		"country_options": _option_values(company, "country"),
		"category_options": _option_values(company, "category"),
	})
	return render(request, "suppliers/list.html", context)


@login_required
@role_required(*MANAGE_ROLES)
@require_http_methods(["GET", "POST"])
def supplier_create(request):
	company = _require_company(request)
	if company is None:
		return redirect(f"{reverse('accounts:dashboard')}?section=company")

	form = SupplierForm(request.POST or None, company=company)
	if request.method == "POST" and form.is_valid():
		supplier = form.save(commit=False)
		# The company is taken from the session, never from the form.
		supplier.company = company
		supplier.created_by = request.user
		supplier.save()
		messages.success(request, f"Supplier “{supplier.name}” added.")
		return redirect("suppliers:supplier_detail", pk=supplier.pk)

	context = _module_context(company, "Suppliers", request.user)
	context.update({
		"form": form,
		"form_title": "Add supplier",
		"is_edit": False,
		"country_options": _option_values(company, "country"),
		"category_options": _option_values(company, "category"),
	})
	return render(request, "suppliers/form.html", context)


@login_required
@role_required(*MANAGE_ROLES)
@require_http_methods(["GET", "POST"])
def supplier_edit(request, pk):
	company = _require_company(request)
	if company is None:
		return redirect(f"{reverse('accounts:dashboard')}?section=company")

	supplier = _scoped_supplier_or_404(company, pk)
	form = SupplierForm(request.POST or None, instance=supplier, company=company)
	if request.method == "POST" and form.is_valid():
		form.save()
		messages.success(request, f"Supplier “{supplier.name}” updated.")
		return redirect("suppliers:supplier_detail", pk=supplier.pk)

	context = _module_context(company, "Suppliers", request.user)
	context.update({
		"form": form,
		"form_title": f"Edit {supplier.name}",
		"supplier": supplier,
		"is_edit": True,
		"country_options": _option_values(company, "country"),
		"category_options": _option_values(company, "category"),
	})
	return render(request, "suppliers/form.html", context)


@login_required
@role_required(*VIEW_ROLES)
def supplier_detail(request, pk):
	company = _require_company(request)
	if company is None:
		return redirect(f"{reverse('accounts:dashboard')}?section=company")

	supplier = _scoped_supplier_or_404(company, pk)
	if request.user.role in MANAGE_ROLES:
		refresh_esg_status(supplier)

	scope3 = supplier_scope3_summary(supplier)
	context = _module_context(company, "Suppliers", request.user)
	context.update({
		"supplier": supplier,
		"documents": supplier.documents.all(),
		"document_form": SupplierDocumentForm(),
		"scope3_activities": scope3["activities"],
		"scope3_total_tonnes": scope3["total_tonnes"],
		"scope3_approved_count": scope3["approved_count"],
		"scope3_count": scope3["count"],
		"can_manage": _can_manage(request.user),
	})
	return render(request, "suppliers/detail.html", context)


@login_required
@role_required(*MANAGE_ROLES)
@require_POST
def supplier_toggle_active(request, pk):
	company = _require_company(request)
	if company is None:
		return redirect(f"{reverse('accounts:dashboard')}?section=company")

	supplier = _scoped_supplier_or_404(company, pk)
	supplier.is_active = not supplier.is_active
	supplier.save(update_fields=["is_active", "updated_at"])
	state = "activated" if supplier.is_active else "deactivated"
	messages.success(request, f"Supplier “{supplier.name}” {state}.")
	return redirect("suppliers:supplier_detail", pk=supplier.pk)


# --------------------------------------------------------------------------- #
# Documents
# --------------------------------------------------------------------------- #
@login_required
@role_required(*MANAGE_ROLES)
@require_POST
def supplier_document_upload(request, pk):
	company = _require_company(request)
	if company is None:
		return redirect(f"{reverse('accounts:dashboard')}?section=company")

	supplier = _scoped_supplier_or_404(company, pk)
	form = SupplierDocumentForm(request.POST, request.FILES)
	if form.is_valid():
		document = form.save(commit=False)
		document.supplier = supplier
		document.company = company
		document.uploaded_by = request.user
		document.save()
		refresh_esg_status(supplier)
		messages.success(request, f"Document “{document.title}” uploaded.")
	else:
		message = " ".join(
			error for errors in form.errors.values() for error in errors
		)
		messages.error(request, message or "Upload a valid document.")
	return redirect("suppliers:supplier_detail", pk=supplier.pk)


@login_required
@role_required(*MANAGE_ROLES)
@require_POST
def supplier_document_delete(request, pk):
	company = _require_company(request)
	if company is None:
		return redirect(f"{reverse('accounts:dashboard')}?section=company")

	document = _scoped_document_or_404(company, pk)
	supplier = document.supplier
	title = document.title
	document.file.delete(save=False)
	document.delete()
	refresh_esg_status(supplier)
	messages.success(request, f"Document “{title}” deleted.")
	return redirect("suppliers:supplier_detail", pk=supplier.pk)


@login_required
@role_required(*VIEW_ROLES)
def supplier_document_download(request, pk):
	company = _require_company(request)
	if company is None:
		raise Http404("Document not found.")

	# Cross-tenant access is impossible: the document is fetched within the
	# caller's own company, so another company's file is a 404.
	document = _scoped_document_or_404(company, pk)
	try:
		handle = document.file.open("rb")
	except (FileNotFoundError, OSError):
		raise Http404("The document file is no longer available.")
	return FileResponse(
		handle, as_attachment=True, filename=os.path.basename(document.file.name)
	)


# --------------------------------------------------------------------------- #
# Comparison
# --------------------------------------------------------------------------- #
@login_required
@role_required(*VIEW_ROLES)
def supplier_compare(request):
	company = _require_company(request)
	if company is None:
		return redirect(f"{reverse('accounts:dashboard')}?section=company")

	raw_ids = ",".join(request.GET.getlist("ids"))
	ids = []
	for value in raw_ids.split(","):
		value = value.strip()
		if value.isdigit() and int(value) not in ids:
			ids.append(int(value))
	# Filtering by company makes another tenant's id simply "not found".
	suppliers = list(
		Supplier.objects.filter(company=company, pk__in=ids).order_by("name")[:COMPARE_LIMIT]
	)
	rows = comparison_rows(company, suppliers)

	context = _module_context(company, "Suppliers", request.user)
	context.update({
		"rows": rows,
		"selected_ids": [supplier.pk for supplier in suppliers],
		"all_suppliers": Supplier.objects.filter(company=company).order_by("name"),
		"compare_limit": COMPARE_LIMIT,
	})
	return render(request, "suppliers/compare.html", context)
