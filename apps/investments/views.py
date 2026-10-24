from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods, require_POST

from apps.accounts.permissions import role_required
from apps.carbon.views import _sidebar as carbon_sidebar

from .forms import (
	DecarbonizationProjectForm,
	ImpactAssessmentForm,
	InvestmentForm,
	ProjectScenarioForm,
)
from .models import (
	DecarbonizationProject,
	ImpactAssessment,
	Investment,
	ProjectScenario,
	ProjectStatus,
	ProjectType,
)
from .services import VIEWER_ROLES, can_manage, company_for_user, visible_projects


def _module_context(request, company, active="Investments"):
	return {
		"company": company,
		"sidebar_nav": carbon_sidebar(active, request.user),
	}


def _require_company(request):
	company = company_for_user(request.user)
	if company is None:
		messages.warning(request, "Configurez votre entreprise avant de gérer les investissements.")
	return company


def _require_manager(request):
	if not can_manage(request.user):
		messages.error(request, "Vous n'avez pas les droits pour modifier les investissements.")
		raise PermissionDenied


def _project_or_404(user, company, pk):
	project = visible_projects(user, company).select_related("department").filter(pk=pk).first()
	if project is None:
		raise Http404("Projet introuvable.")
	return project


def _child_queryset(model, company, project):
	return model.objects.filter(project__company=company, project_id=project.pk)


def _child_or_404(model, company, project, child_pk):
	child = _child_queryset(model, company, project).filter(pk=child_pk).first()
	if child is None:
		raise Http404("Objet d'investissement introuvable.")
	return child


def _redirect_company_setup():
	return redirect(f"{reverse('accounts:dashboard')}?section=company")


@login_required
@role_required(*VIEWER_ROLES)
def project_list(request):
	company = _require_company(request)
	if company is None:
		return _redirect_company_setup()

	queryset = visible_projects(request.user, company)
	query = request.GET.get("q", "").strip()
	status = request.GET.get("status", "").strip()
	project_type = request.GET.get("project_type", "").strip()
	if query:
		queryset = queryset.filter(name__icontains=query)
	if status:
		queryset = queryset.filter(status=status)
	if project_type:
		queryset = queryset.filter(project_type=project_type)

	paginator = Paginator(queryset.select_related("department"), 10)
	page = paginator.get_page(request.GET.get("page"))
	all_projects = visible_projects(request.user, company)
	context = _module_context(request, company)
	context.update({
		"projects": page,
		"page_obj": page,
		"query": query,
		"selected_status": status,
		"selected_project_type": project_type,
		"status_choices": ProjectStatus.choices,
		"project_type_choices": ProjectType.choices,
		"project_count": all_projects.count(),
		"proposed_count": all_projects.filter(status=ProjectStatus.PROPOSED).count(),
		"approved_count": all_projects.filter(status=ProjectStatus.APPROVED).count(),
		"in_progress_count": all_projects.filter(status=ProjectStatus.IN_PROGRESS).count(),
		"completed_count": all_projects.filter(status=ProjectStatus.COMPLETED).count(),
		"can_manage": can_manage(request.user),
	})
	return render(request, "investments/project_list.html", context)


@login_required
@role_required(*VIEWER_ROLES)
def project_detail(request, pk):
	company = _require_company(request)
	if company is None:
		return _redirect_company_setup()
	project = _project_or_404(request.user, company, pk)
	context = _module_context(request, company)
	context.update({
		"project": project,
		"investments": _child_queryset(Investment, company, project).order_by("-date", "-id"),
		"impacts": _child_queryset(ImpactAssessment, company, project).order_by("-created_at", "-id"),
		"scenarios": _child_queryset(ProjectScenario, company, project).select_related(
			"investment", "impact"
		),
		"can_manage": can_manage(request.user),
	})
	return render(request, "investments/project_detail.html", context)


@login_required
@role_required(*VIEWER_ROLES)
@require_http_methods(["GET", "POST"])
def project_create(request):
	company = _require_company(request)
	if company is None:
		return _redirect_company_setup()
	_require_manager(request)
	form = DecarbonizationProjectForm(
		request.POST or None, company=company, user=request.user
	)
	if request.method == "POST" and form.is_valid():
		project = form.save(commit=False)
		project.company = company
		project.created_by = request.user
		try:
			with transaction.atomic():
				project.save()
		except IntegrityError:
			form.add_error("name", "Un projet portant ce nom existe déjà dans votre entreprise.")
		else:
			messages.success(request, "Le projet a été créé.")
			return redirect("investments:project_detail", pk=project.pk)
	context = _module_context(request, company)
	context.update({"form": form, "form_title": "Nouveau projet", "project": None})
	return render(request, "investments/project_form.html", context)


@login_required
@role_required(*VIEWER_ROLES)
@require_http_methods(["GET", "POST"])
def project_edit(request, pk):
	company = _require_company(request)
	if company is None:
		return _redirect_company_setup()
	_require_manager(request)
	project = _project_or_404(request.user, company, pk)
	form = DecarbonizationProjectForm(
		request.POST or None, instance=project, company=company, user=request.user
	)
	if request.method == "POST" and form.is_valid():
		try:
			with transaction.atomic():
				project = form.save()
		except IntegrityError:
			form.add_error("name", "Un projet portant ce nom existe déjà dans votre entreprise.")
		else:
			messages.success(request, "Le projet a été modifié.")
			return redirect("investments:project_detail", pk=project.pk)
	context = _module_context(request, company)
	context.update({"form": form, "form_title": "Modifier le projet", "project": project})
	return render(request, "investments/project_form.html", context)


@login_required
@role_required(*VIEWER_ROLES)
def project_delete_confirm(request, pk):
	company = _require_company(request)
	if company is None:
		return _redirect_company_setup()
	_require_manager(request)
	project = _project_or_404(request.user, company, pk)
	context = _module_context(request, company)
	context.update({"project": project, "delete_action": "investments:project_delete"})
	return render(request, "investments/project_confirm_delete.html", context)


@login_required
@role_required(*VIEWER_ROLES)
@require_POST
def project_delete(request, pk):
	company = _require_company(request)
	if company is None:
		return _redirect_company_setup()
	_require_manager(request)
	project = _project_or_404(request.user, company, pk)
	if project.status == ProjectStatus.IN_PROGRESS:
		messages.error(request, "Un projet en cours ne peut pas être supprimé.")
		return redirect("investments:project_detail", pk=project.pk)
	try:
		with transaction.atomic():
			project.delete()
	except IntegrityError:
		messages.error(request, "Le projet ne peut pas être supprimé pour le moment.")
	else:
		messages.success(request, "Le projet a été supprimé.")
	return redirect("investments:project_list")


def _child_context(request, company, project, form, form_title, child=None):
	context = _module_context(request, company)
	context.update({
		"project": project,
		"form": form,
		"form_title": form_title,
		"child": child,
	})
	return context


@login_required
@role_required(*VIEWER_ROLES)
@require_http_methods(["GET", "POST"])
def investment_create(request, pk):
	return _child_create(request, pk, Investment, InvestmentForm, "Nouvel investissement")


@login_required
@role_required(*VIEWER_ROLES)
@require_http_methods(["GET", "POST"])
def investment_edit(request, pk, child_pk):
	return _child_edit(request, pk, child_pk, Investment, InvestmentForm, "Modifier l'investissement")


@login_required
@role_required(*VIEWER_ROLES)
def investment_delete_confirm(request, pk, child_pk):
	return _child_delete_confirm(request, pk, child_pk, Investment, "investissement_delete")


@login_required
@role_required(*VIEWER_ROLES)
@require_POST
def investment_delete(request, pk, child_pk):
	return _child_delete(request, pk, child_pk, Investment, "investissement")


@login_required
@role_required(*VIEWER_ROLES)
@require_http_methods(["GET", "POST"])
def impact_create(request, pk):
	return _child_create(request, pk, ImpactAssessment, ImpactAssessmentForm, "Nouvelle évaluation d'impact")


@login_required
@role_required(*VIEWER_ROLES)
@require_http_methods(["GET", "POST"])
def impact_edit(request, pk, child_pk):
	return _child_edit(request, pk, child_pk, ImpactAssessment, ImpactAssessmentForm, "Modifier l'évaluation d'impact")


@login_required
@role_required(*VIEWER_ROLES)
def impact_delete_confirm(request, pk, child_pk):
	return _child_delete_confirm(request, pk, child_pk, ImpactAssessment, "impact_delete")


@login_required
@role_required(*VIEWER_ROLES)
@require_POST
def impact_delete(request, pk, child_pk):
	return _child_delete(request, pk, child_pk, ImpactAssessment, "évaluation d'impact")


@login_required
@role_required(*VIEWER_ROLES)
@require_http_methods(["GET", "POST"])
def scenario_create(request, pk):
	return _child_create(request, pk, ProjectScenario, ProjectScenarioForm, "Nouveau scénario")


@login_required
@role_required(*VIEWER_ROLES)
@require_http_methods(["GET", "POST"])
def scenario_edit(request, pk, child_pk):
	return _child_edit(request, pk, child_pk, ProjectScenario, ProjectScenarioForm, "Modifier le scénario")


@login_required
@role_required(*VIEWER_ROLES)
def scenario_delete_confirm(request, pk, child_pk):
	return _child_delete_confirm(request, pk, child_pk, ProjectScenario, "scenario_delete")


@login_required
@role_required(*VIEWER_ROLES)
@require_POST
def scenario_delete(request, pk, child_pk):
	return _child_delete(request, pk, child_pk, ProjectScenario, "scénario")


def _child_form_kwargs(model, project):
	if model is ProjectScenario:
		return {"project": project, "company": project.company}
	return {"project": project, "company": project.company}


def _child_create(request, pk, model, form_class, title):
	company = _require_company(request)
	if company is None:
		return _redirect_company_setup()
	_require_manager(request)
	project = _project_or_404(request.user, company, pk)
	form = form_class(request.POST or None, **_child_form_kwargs(model, project))
	if request.method == "POST" and form.is_valid():
		try:
			with transaction.atomic():
				child = form.save()
		except IntegrityError:
			messages.error(request, "Cet élément existe déjà pour ce projet.")
		else:
			messages.success(request, "L'élément a été créé.")
			return redirect("investments:project_detail", pk=project.pk)
	return render(
		request,
		"investments/child_form.html",
		_child_context(request, company, project, form, title),
	)


def _child_edit(request, pk, child_pk, model, form_class, title):
	company = _require_company(request)
	if company is None:
		return _redirect_company_setup()
	_require_manager(request)
	project = _project_or_404(request.user, company, pk)
	child = _child_or_404(model, company, project, child_pk)
	form = form_class(
		request.POST or None,
		instance=child,
		**_child_form_kwargs(model, project),
	)
	if request.method == "POST" and form.is_valid():
		try:
			with transaction.atomic():
				child = form.save()
		except IntegrityError:
			messages.error(request, "Cet élément existe déjà pour ce projet.")
		else:
			messages.success(request, "L'élément a été modifié.")
			return redirect("investments:project_detail", pk=project.pk)
	return render(
		request,
		"investments/child_form.html",
		_child_context(request, company, project, form, title, child),
	)


def _child_delete_confirm(request, pk, child_pk, model, action_name):
	company = _require_company(request)
	if company is None:
		return _redirect_company_setup()
	_require_manager(request)
	project = _project_or_404(request.user, company, pk)
	child = _child_or_404(model, company, project, child_pk)
	context = _module_context(request, company)
	context.update({
		"project": project,
		"child": child,
		"delete_action": f"investments:{action_name}",
	})
	return render(request, "investments/child_confirm_delete.html", context)


def _child_delete(request, pk, child_pk, model, label):
	company = _require_company(request)
	if company is None:
		return _redirect_company_setup()
	_require_manager(request)
	project = _project_or_404(request.user, company, pk)
	child = _child_or_404(model, company, project, child_pk)
	try:
		with transaction.atomic():
			child.delete()
	except IntegrityError:
		messages.error(request, f"{label.capitalize()} ne peut pas être supprimé pour le moment.")
	else:
		messages.success(request, f"{label.capitalize()} a été supprimé.")
	return redirect("investments:project_detail", pk=project.pk)
