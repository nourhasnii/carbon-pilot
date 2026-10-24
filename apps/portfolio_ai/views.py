from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.http import Http404, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_http_methods

from apps.accounts.permissions import role_required
from apps.carbon.views import _sidebar as carbon_sidebar
from apps.investments.services import VIEWER_ROLES, WRITE_ROLES, company_for_user
from apps.investments.models import DecarbonizationProject

from .forms import PortfolioRunForm
from .models import PortfolioRun, PortfolioRunStatus
from .tasks import run_portfolio_task


def _company(request):
	company = company_for_user(request.user)
	if company is None:
		messages.warning(request, "Configurez votre entreprise avant de lancer une simulation.")
	return company


def _run_or_404(company, pk):
	run = PortfolioRun.objects.for_company(company).filter(pk=pk).first()
	if run is None:
		raise Http404("Simulation introuvable.")
	return run


def _context(request, company, **extra):
	context = {
		"company": company,
		"sidebar_nav": carbon_sidebar("Portfolio AI", request.user),
	}
	context.update(extra)
	return context


@login_required
@role_required(*VIEWER_ROLES)
@require_http_methods(["GET", "POST"])
def run_create(request):
	company = _company(request)
	if company is None:
		return redirect("accounts:dashboard")
	if request.user.role not in WRITE_ROLES and request.user.role != request.user.Role.SUPER_ADMIN:
		raise PermissionDenied
	form = PortfolioRunForm(request.POST or None)
	if request.method == "POST" and form.is_valid():
		run = form.save(commit=False)
		run.company = company
		run.created_by = request.user
		run.save()
		run_portfolio_task.delay(run.pk)
		return redirect("portfolio_ai:run_detail", pk=run.pk)
	return render(request, "portfolio_ai/run_form.html", _context(request, company, form=form))


@login_required
@role_required(*VIEWER_ROLES)
def run_detail(request, pk):
	company = _company(request)
	if company is None:
		return redirect("accounts:dashboard")
	run = _run_or_404(company, pk)
	selected_projects = DecarbonizationProject.objects.for_company(company).filter(
		pk__in=run.selected_project_ids,
	)
	return render(request, "portfolio_ai/run_detail.html", _context(
		request,
		company,
		run=run,
		selected_projects=selected_projects,
		budget_remaining=(run.budget - run.total_cost) if run.total_cost is not None else None,
	))


@login_required
@role_required(*VIEWER_ROLES)
@require_GET
def run_status(request, pk):
	company = _company(request)
	if company is None:
		return JsonResponse({"status": "FAILED", "error": "Entreprise non configurée.", "done": True}, status=404)
	run = _run_or_404(company, pk)
	done = run.status in (PortfolioRunStatus.SUCCESS, PortfolioRunStatus.FAILED)
	return JsonResponse({
		"status": run.status,
		"error": run.error_message if run.status == PortfolioRunStatus.FAILED else "",
		"done": done,
	})


@login_required
@role_required(*VIEWER_ROLES)
def run_history(request):
	company = _company(request)
	if company is None:
		return redirect("accounts:dashboard")
	page = Paginator(PortfolioRun.objects.for_company(company), 10).get_page(
		request.GET.get("page")
	)
	return render(request, "portfolio_ai/run_history.html", _context(
		request, company, runs=page, page_obj=page,
	))
