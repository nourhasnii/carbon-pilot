"""ESG / carbon report views.

Tenant isolation mirrors the rest of CarbonPilot: the company is always
resolved from the authenticated user (never from the request body), and every
report is fetched with ``company=company`` so another tenant's report is a 404
rather than a 403. A role check guards who may create/generate a report;
auditors and other viewers can read and download.
"""

import os

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.accounts.models import User
from apps.accounts.permissions import role_required
from apps.carbon.services.tenancy import company_for_user
from apps.carbon.views import CARBON_VIEWER_ROLES
from apps.carbon.views import _sidebar as carbon_sidebar
from apps.common.listing import paginate

from .forms import ReportFilterForm
from .models import ESGReport, ReportStatus
from .permissions import MANAGE_ROLES as REPORT_MANAGE_ROLES
from .services import esg_report

REPORT_VIEWER_ROLES = CARBON_VIEWER_ROLES


def _module_context(request, company, active):
	return {
		"company": company,
		"sidebar_nav": carbon_sidebar(active, request.user),
	}


def _require_company(request):
	"""Resolve the tenant from the user; never trusts client input."""
	company = company_for_user(request.user)
	if company is None:
		messages.warning(request, "Set up your company before building an ESG report.")
	return company


def _can_manage(user):
	return user.role == User.Role.SUPER_ADMIN or user.role in REPORT_MANAGE_ROLES


def _scoped_report_or_404(company, pk):
	"""Fetch a report only within the caller's own company (else 404)."""
	report = ESGReport.objects.filter(company=company, pk=pk).first()
	if report is None:
		raise Http404("Report not found.")
	return report


def _apply_report_filters(queryset, form):
	"""Narrow the report history by the shared filter bar.

	An invalid form (unknown year/status) leaves the queryset untouched so bad
	GET values are ignored rather than erroring.
	"""
	if not form.is_valid():
		return queryset
	data = form.cleaned_data
	if data.get("period_year"):
		queryset = queryset.filter(period_year=data["period_year"])
	if data.get("status"):
		queryset = queryset.filter(status=data["status"])
	return queryset


@login_required
@role_required(*REPORT_VIEWER_ROLES)
def report_list(request):
	company = _require_company(request)
	if company is None:
		return redirect(f"{reverse('accounts:dashboard')}?section=company")

	# Every row read here is filtered by the caller's own company.
	reports = ESGReport.objects.filter(company=company).latest_first()
	form = ReportFilterForm(request.GET or None, company=company)
	pagination = paginate(request, _apply_report_filters(reports, form))

	years = sorted(
		set(
			ESGReport.objects.filter(company=company)
			.values_list("period_year", flat=True)
			.distinct()
		),
		reverse=True,
	) or [timezone.localdate().year]
	context = _module_context(request, company, "ESG reports")
	context.update({
		"reports": pagination["items"],
		"pagination": pagination,
		"filter_form": form,
		"has_filters": any(
			request.GET.get(key) for key in ("period_year", "status")
		),
		"latest": reports.first(),
		"years": years,
		"current_year": timezone.localdate().year,
		"can_manage": _can_manage(request.user),
	})
	return render(request, "reports/list.html", context)


@login_required
@role_required(*REPORT_VIEWER_ROLES)
@require_POST
def report_create(request):
	company = _require_company(request)
	if company is None:
		return redirect(f"{reverse('accounts:dashboard')}?section=company")
	if not _can_manage(request.user):
		raise PermissionDenied

	# period_year is the only client value used, and it is validated as a year;
	# the company is never taken from the request.
	raw_year = (request.POST.get("period_year") or "").strip()
	if not (raw_year.isdigit() and 1900 <= int(raw_year) <= 2100):
		messages.error(request, "Choose a valid reporting year.")
		return redirect("reports:report_list")
	title = (request.POST.get("title") or "").strip() or None

	report = esg_report.create_draft(
		company, period_year=int(raw_year), actor=request.user, title=title
	)
	messages.success(
		request,
		f"Preview ready for {report.period_year}. Review the summary, then "
		"generate the PDF.",
	)
	return redirect("reports:report_detail", pk=report.pk)


@login_required
@role_required(*REPORT_VIEWER_ROLES)
def report_detail(request, pk):
	company = _require_company(request)
	if company is None:
		return redirect(f"{reverse('accounts:dashboard')}?section=company")

	report = _scoped_report_or_404(company, pk)
	context = _module_context(request, company, "ESG reports")
	context.update({"report": report, "can_manage": _can_manage(request.user)})
	return render(request, "reports/detail.html", context)


@login_required
@role_required(*REPORT_VIEWER_ROLES)
@require_POST
def report_generate(request, pk):
	company = _require_company(request)
	if company is None:
		return redirect(f"{reverse('accounts:dashboard')}?section=company")
	if not _can_manage(request.user):
		raise PermissionDenied

	report = _scoped_report_or_404(company, pk)
	esg_report.generate_pdf(report)
	messages.success(request, f"PDF generated for {report.title}.")
	return redirect("reports:report_detail", pk=report.pk)


@login_required
@role_required(*REPORT_VIEWER_ROLES)
def report_download(request, pk):
	company = _require_company(request)
	if company is None:
		raise Http404("Report not found.")

	# Cross-tenant access is impossible: the report is fetched within the
	# caller's own company, so another company's report is a 404.
	report = _scoped_report_or_404(company, pk)
	if report.status != ReportStatus.GENERATED or not report.file:
		raise Http404("This report has not been generated yet.")
	try:
		handle = report.file.open("rb")
	except (FileNotFoundError, OSError):
		raise Http404("The report file is no longer available.")

	return FileResponse(
		handle, as_attachment=True, filename=os.path.basename(report.file.name)
	)
