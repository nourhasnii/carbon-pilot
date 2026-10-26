"""ESG intelligence hub views.

Tenant isolation mirrors the rest of CarbonPilot: the company is always
resolved from the authenticated user (never from the request body), every
queryset starts from that company, and a record from another tenant is a 404
rather than a 403 so IDs cannot be probed. Every state change is guarded
server-side by role and executed through a transactional service.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Q
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_http_methods, require_POST

from apps.accounts.permissions import role_required
from apps.carbon.views import _sidebar as carbon_sidebar
from apps.common.listing import paginate

from .forms import (
	IndicatorFilterForm,
	IndicatorForm,
	IndicatorValueForm,
	RecommendationConfirmForm,
)
from .models import (
	ESGPillar,
	AssessmentSnapshot,
	IndicatorCatalogEntry,
	IndicatorStatus,
	Readiness,
)
from .services import assessment as assessment_service
from .services import coach_ai
from .services import signals as signals_service
from .services import workflow
from .services.access import (
	VIEWER_ROLES,
	can_manage,
	can_verify,
	company_for_user,
	is_company_admin,
	visible_indicators,
	visible_values,
)

ACTIVE_NAV = "ESG intelligence"

# The last coaching run lives in the session (not the database): it is a
# read-only projection of the tenant's own data, produced on demand.
AI_SESSION_KEY = "esg_coach_analysis"

READINESS_BADGE = {
	Readiness.NOT_READY: "cp-badge-rejected",
	Readiness.NEEDS_REVIEW: "cp-badge-unverified",
	Readiness.READY_FOR_REPORTING: "cp-badge-approved",
}

STATUS_BADGE = {
	IndicatorStatus.DRAFT: "cp-badge-draft",
	IndicatorStatus.REPORTED: "cp-badge-submitted",
	IndicatorStatus.VERIFIED: "cp-badge-verified",
}

PILLAR_BADGE = {
	ESGPillar.ENVIRONMENTAL: "cp-badge-scope3",
	ESGPillar.SOCIAL: "cp-badge-scope2",
	ESGPillar.GOVERNANCE: "cp-badge-neutral",
}


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #
def _module_context(request, company):
	return {
		"company": company,
		"sidebar_nav": carbon_sidebar(ACTIVE_NAV, request.user),
	}


def _require_company(request):
	"""Resolve the tenant from the user; never trusts client input."""
	company = company_for_user(request.user)
	if company is None:
		messages.warning(request, "Set up your company before using the ESG hub.")
	return company


def _redirect_no_company():
	return redirect(f"{reverse('accounts:dashboard')}?section=company")


def _scoped_indicator_or_404(user, company, pk):
	indicator = (
		visible_indicators(user, company)
		.select_related("catalog_entry", "owner", "department")
		.filter(pk=pk)
		.first()
	)
	if indicator is None:
		raise Http404("Indicator not found.")
	return indicator


def _scoped_value_or_404(user, company, pk):
	value = (
		visible_values(user, company)
		.select_related("indicator__catalog_entry")
		.filter(pk=pk)
		.first()
	)
	if value is None:
		raise Http404("Measurement not found.")
	return value


def _pillar_rows(result):
	return [
		{
			"key": ESGPillar.ENVIRONMENTAL,
			"label": ESGPillar.ENVIRONMENTAL.label,
			"badge": PILLAR_BADGE[ESGPillar.ENVIRONMENTAL],
			"score": result.environmental_score,
		},
		{
			"key": ESGPillar.SOCIAL,
			"label": ESGPillar.SOCIAL.label,
			"badge": PILLAR_BADGE[ESGPillar.SOCIAL],
			"score": result.social_score,
		},
		{
			"key": ESGPillar.GOVERNANCE,
			"label": ESGPillar.GOVERNANCE.label,
			"badge": PILLAR_BADGE[ESGPillar.GOVERNANCE],
			"score": result.governance_score,
		},
	]


def _indicator_rows(indicators):
	"""Indicators paired with their latest measurement, without N+1 queries.

	``indicators`` is an already-evaluated page of indicators whose ``values``
	relation has been prefetched by the caller.
	"""
	rows = []
	for indicator in indicators:
		values = list(indicator.values.all())
		latest = values[0] if values else None
		rows.append(
			{
				"indicator": indicator,
				"latest": latest,
				"periods": len(values),
				"status_badge": STATUS_BADGE.get(latest.status if latest else "", "cp-badge-neutral"),
			}
		)
	return rows


def _generated_at_display(value):
	parsed = parse_datetime(value) if value else None
	if parsed is None:
		return value or ""
	return timezone.localtime(parsed).strftime("%d %b %Y, %H:%M")


def _remember_analysis(request, analysis):
	request.session[AI_SESSION_KEY] = {
		"company_id": analysis.company_id,
		"ai_available": analysis.ai_available,
		"model": analysis.model,
		"generated_at_display": _generated_at_display(analysis.generated_at),
		"message": analysis.message,
		"executive_summary": analysis.executive_summary,
		"priority_gaps": analysis.priority_gaps,
		"recommended_indicators": analysis.recommended_indicators,
		"management_next_steps": analysis.management_next_steps,
	}


def _ai_analysis(request, company):
	"""Last coaching run for the *current* tenant only, or ``None``."""
	stored = request.session.get(AI_SESSION_KEY)
	if not isinstance(stored, dict):
		return None
	if stored.get("company_id") != company.pk:
		# Never leak another tenant's analysis if the session user switched company.
		return None
	return stored


def _gap_keys(gaps):
	keys = set()
	for kind, items in (gaps or {}).items():
		for item in items:
			keys.add((kind, item.get("code")))
	return keys


def _gap_groups(gaps):
	"""Gaps grouped by kind, empty groups removed, in reporting order."""
	groups = []
	for kind, label in assessment_service.GAP_KINDS:
		items = (gaps or {}).get(kind) or []
		if items:
			groups.append({"kind": kind, "label": label, "items": items})
	return groups


# --------------------------------------------------------------------------- #
# Dashboard
# --------------------------------------------------------------------------- #
@login_required
@role_required(*VIEWER_ROLES)
def esg_dashboard(request):
	company = _require_company(request)
	if company is None:
		return _redirect_no_company()

	years = assessment_service.available_period_years(company)
	requested = request.GET.get("year")
	period_year = int(requested) if (requested or "").isdigit() else None
	result = assessment_service.assess_company(company, period_year=period_year)

	history = list(
		AssessmentSnapshot.objects.for_company(company).latest_first()[:8]
	)
	indicators = visible_indicators(request.user, company)
	period_values = visible_values(request.user, company).filter(
		period_year=result.period_year
	)
	counts = {
		"total": indicators.count(),
		"active": indicators.filter(is_active=True).count(),
		"reported": period_values.eligible().count(),
		"verified": period_values.filter(status=IndicatorStatus.VERIFIED).count(),
		"with_evidence": period_values.eligible().exclude(evidence_reference="").count(),
	}

	context = _module_context(request, company)
	context.update({
		"result": result,
		"period_year": result.period_year,
		"selected_year": str(period_year) if period_year else "",
		"years": years,
		"pillar_rows": _pillar_rows(result),
		"breakdown": result.breakdown,
		"gap_groups": _gap_groups(result.gaps),
		"gaps_total": assessment_service.gaps_count(result.gaps),
		"readiness_label": Readiness(result.readiness).label,
		"readiness_badge": READINESS_BADGE.get(result.readiness, "cp-badge-neutral"),
		"history": history,
		"signals": signals_service.company_signals(company),
		"counts": counts,
		"can_manage": can_manage(request.user, company),
		"can_run_ai": is_company_admin(request.user),
	})
	return render(request, "esg/dashboard.html", context)


# --------------------------------------------------------------------------- #
# Indicator catalog
# --------------------------------------------------------------------------- #
@login_required
@role_required(*VIEWER_ROLES)
def indicator_list(request):
	company = _require_company(request)
	if company is None:
		return _redirect_no_company()

	years = assessment_service.available_period_years(company)
	filter_form = IndicatorFilterForm(
		request.GET or None, company=company, years=years
	)
	queryset = visible_indicators(request.user, company).select_related(
		"catalog_entry", "owner", "department"
	)

	search = pillar = status = department = year = ""
	if filter_form.is_valid():
		search = filter_form.cleaned_data.get("q") or ""
		pillar = filter_form.cleaned_data.get("pillar") or ""
		status = filter_form.cleaned_data.get("status") or ""
		department = filter_form.cleaned_data.get("department") or ""
		year = filter_form.cleaned_data.get("year") or ""

	if search:
		queryset = queryset.filter(
			Q(catalog_entry__name__icontains=search)
			| Q(catalog_entry__code__icontains=search)
		)
	if pillar:
		queryset = queryset.filter(catalog_entry__pillar=pillar)
	if department.isdigit():
		queryset = queryset.filter(department_id=int(department))
	if year.isdigit():
		queryset = queryset.filter(values__period_year=int(year)).distinct()
	if status == "NONE":
		queryset = queryset.filter(values__isnull=True)
	elif status == "INACTIVE":
		queryset = queryset.filter(is_active=False)
	elif status in IndicatorStatus.values:
		queryset = queryset.filter(values__status=status).distinct()

	ordered = queryset.prefetch_related("values").order_by(
		"catalog_entry__pillar", "catalog_entry__sort_order"
	)
	pagination = paginate(request, ordered)
	rows = _indicator_rows(pagination["items"])

	context = _module_context(request, company)
	context.update({
		"rows": rows,
		"filter_form": filter_form,
		"pagination": pagination,
		"has_filters": any(
			request.GET.get(key)
			for key in ("q", "pillar", "status", "department", "year")
		),
		"selected": {
			"q": search, "pillar": pillar, "status": status,
			"department": department, "year": year,
		},
		"pillar_choices": ESGPillar.choices,
		"can_manage": can_manage(request.user, company),
		"total_count": visible_indicators(request.user, company).count(),
	})
	return render(request, "esg/indicator_list.html", context)


@login_required
@role_required(*VIEWER_ROLES)
def indicator_detail(request, pk):
	company = _require_company(request)
	if company is None:
		return _redirect_no_company()

	indicator = _scoped_indicator_or_404(request.user, company, pk)
	values = list(indicator.values.select_related("reported_by", "verified_by"))
	context = _module_context(request, company)
	context.update({
		"indicator": indicator,
		"values": values,
		"latest": values[0] if values else None,
		"history": list(indicator.history.select_related("actor")[:40]),
		"status_badge": STATUS_BADGE,
		"value_form": IndicatorValueForm(
			indicator=indicator,
			initial={"period_year": timezone.localdate().year},
			prefix="value",
		),
		"can_manage": can_manage(request.user, company, indicator),
		"can_verify": can_verify(request.user),
	})
	return render(request, "esg/indicator_detail.html", context)


@login_required
@role_required(*VIEWER_ROLES)
@require_http_methods(["GET", "POST"])
def indicator_create(request):
	company = _require_company(request)
	if company is None:
		return _redirect_no_company()
	if not can_manage(request.user, company):
		raise PermissionDenied

	form = IndicatorForm(request.POST or None, company=company)
	if request.method == "POST" and form.is_valid():
		try:
			indicator = workflow.create_indicator(
				company=company,
				catalog_entry=form.cleaned_data["catalog_entry"],
				actor=request.user,
				owner=form.cleaned_data.get("owner"),
				department=form.cleaned_data.get("department"),
				notes=form.cleaned_data.get("notes"),
			)
		except ValidationError as exc:
			messages.error(request, getattr(exc, "messages", [str(exc)])[0])
		else:
			messages.success(
				request, "Indicator created. Record its first measurement when ready."
			)
			return redirect("esg:indicator_detail", pk=indicator.pk)

	context = _module_context(request, company)
	context.update({"form": form, "form_title": "New indicator", "indicator": None})
	return render(request, "esg/indicator_form.html", context)


@login_required
@role_required(*VIEWER_ROLES)
@require_http_methods(["GET", "POST"])
def indicator_edit(request, pk):
	company = _require_company(request)
	if company is None:
		return _redirect_no_company()

	indicator = _scoped_indicator_or_404(request.user, company, pk)
	if not can_manage(request.user, company, indicator):
		raise PermissionDenied

	form = IndicatorForm(request.POST or None, instance=indicator, company=company)
	if request.method == "POST" and form.is_valid():
		try:
			workflow.update_indicator(
				indicator,
				actor=request.user,
				owner=form.cleaned_data.get("owner"),
				department=form.cleaned_data.get("department"),
				notes=form.cleaned_data.get("notes"),
			)
		except ValidationError as exc:
			messages.error(request, getattr(exc, "messages", [str(exc)])[0])
		else:
			messages.success(request, "Indicator updated.")
			return redirect("esg:indicator_detail", pk=indicator.pk)

	context = _module_context(request, company)
	context.update({"form": form, "form_title": "Edit indicator", "indicator": indicator})
	return render(request, "esg/indicator_form.html", context)


@login_required
@role_required(*VIEWER_ROLES)
@require_POST
def indicator_set_active(request, pk):
	company = _require_company(request)
	if company is None:
		return _redirect_no_company()

	indicator = _scoped_indicator_or_404(request.user, company, pk)
	if not can_manage(request.user, company, indicator):
		raise PermissionDenied

	active = request.POST.get("active") == "1"
	workflow.set_indicator_active(indicator, active, actor=request.user)
	messages.success(
		request,
		"Indicator reactivated." if active else "Indicator deactivated. Its history is kept.",
	)
	return redirect("esg:indicator_detail", pk=indicator.pk)


# --------------------------------------------------------------------------- #
# Measurements
# --------------------------------------------------------------------------- #
@login_required
@role_required(*VIEWER_ROLES)
@require_http_methods(["GET", "POST"])
def value_create(request, pk):
	company = _require_company(request)
	if company is None:
		return _redirect_no_company()

	indicator = _scoped_indicator_or_404(request.user, company, pk)
	if not can_manage(request.user, company, indicator):
		raise PermissionDenied

	form = IndicatorValueForm(
		request.POST or None,
		indicator=indicator,
		initial={"period_year": timezone.localdate().year},
	)
	if request.method == "POST" and form.is_valid():
		try:
			workflow.record_value(
				indicator,
				actor=request.user,
				period_year=form.cleaned_data["period_year"],
				value=form.cleaned_data.get("value"),
				unit=form.cleaned_data.get("unit"),
				evidence_reference=form.cleaned_data.get("evidence_reference"),
				note=form.cleaned_data.get("note"),
				status=form.cleaned_data["status"],
			)
		except ValidationError as exc:
			messages.error(request, getattr(exc, "messages", [str(exc)])[0])
		else:
			messages.success(request, "Measurement saved.")
			return redirect("esg:indicator_detail", pk=indicator.pk)

	context = _module_context(request, company)
	context.update({
		"form": form,
		"indicator": indicator,
		"value": None,
		"form_title": f"Record a measurement · {indicator.reference}",
	})
	return render(request, "esg/value_form.html", context)


@login_required
@role_required(*VIEWER_ROLES)
@require_http_methods(["GET", "POST"])
def value_edit(request, pk):
	company = _require_company(request)
	if company is None:
		return _redirect_no_company()

	value = _scoped_value_or_404(request.user, company, pk)
	indicator = value.indicator
	if not can_manage(request.user, company, indicator):
		raise PermissionDenied
	if value.status == IndicatorStatus.VERIFIED:
		messages.error(
			request,
			"A verified measurement must be returned to draft before it can be edited.",
		)
		return redirect("esg:indicator_detail", pk=indicator.pk)

	form = IndicatorValueForm(request.POST or None, instance=value, indicator=indicator)
	if request.method == "POST" and form.is_valid():
		try:
			workflow.record_value(
				indicator,
				actor=request.user,
				period_year=form.cleaned_data["period_year"],
				value=form.cleaned_data.get("value"),
				unit=form.cleaned_data.get("unit"),
				evidence_reference=form.cleaned_data.get("evidence_reference"),
				note=form.cleaned_data.get("note"),
				status=form.cleaned_data["status"],
			)
		except ValidationError as exc:
			messages.error(request, getattr(exc, "messages", [str(exc)])[0])
		else:
			messages.success(request, "Measurement updated.")
			return redirect("esg:indicator_detail", pk=indicator.pk)

	context = _module_context(request, company)
	context.update({
		"form": form,
		"indicator": indicator,
		"value": value,
		"form_title": f"Edit measurement · {indicator.reference} · {value.period_year}",
	})
	return render(request, "esg/value_form.html", context)


@login_required
@role_required(*VIEWER_ROLES)
@require_POST
def value_report(request, pk):
	company = _require_company(request)
	if company is None:
		return _redirect_no_company()

	value = _scoped_value_or_404(request.user, company, pk)
	if not can_manage(request.user, company, value.indicator):
		raise PermissionDenied
	try:
		workflow.mark_reported(value, actor=request.user)
	except ValidationError as exc:
		messages.error(request, getattr(exc, "messages", [str(exc)])[0])
	else:
		messages.success(request, "Measurement reported. It now counts towards completeness.")
	return redirect("esg:indicator_detail", pk=value.indicator_id)


@login_required
@role_required(*VIEWER_ROLES)
@require_POST
def value_verify(request, pk):
	company = _require_company(request)
	if company is None:
		return _redirect_no_company()

	value = _scoped_value_or_404(request.user, company, pk)
	if not can_verify(request.user):
		raise PermissionDenied
	try:
		workflow.verify_value(value, actor=request.user)
	except ValidationError as exc:
		messages.error(request, getattr(exc, "messages", [str(exc)])[0])
	else:
		messages.success(request, "Measurement verified.")
	return redirect("esg:indicator_detail", pk=value.indicator_id)


@login_required
@role_required(*VIEWER_ROLES)
@require_POST
def value_return(request, pk):
	company = _require_company(request)
	if company is None:
		return _redirect_no_company()

	value = _scoped_value_or_404(request.user, company, pk)
	if not can_verify(request.user):
		raise PermissionDenied
	try:
		workflow.return_value_to_draft(
			value, actor=request.user, reason=request.POST.get("reason", "")
		)
	except ValidationError as exc:
		messages.error(request, getattr(exc, "messages", [str(exc)])[0])
	else:
		messages.warning(request, "Measurement returned to draft for correction.")
	return redirect("esg:indicator_detail", pk=value.indicator_id)


# --------------------------------------------------------------------------- #
# Assessments
# --------------------------------------------------------------------------- #
@login_required
@role_required(*VIEWER_ROLES)
@require_POST
def assessment_run(request):
	company = _require_company(request)
	if company is None:
		return _redirect_no_company()
	if not is_company_admin(request.user):
		raise PermissionDenied

	requested = request.POST.get("year")
	period_year = int(requested) if (requested or "").isdigit() else None
	snapshot = assessment_service.run_assessment(
		company, actor=request.user, period_year=period_year
	)
	messages.success(
		request,
		f"Assessment saved for {snapshot.period_label} — completeness "
		f"{snapshot.overall_score}/100.",
	)
	return redirect("esg:assessment_detail", pk=snapshot.pk)


@login_required
@role_required(*VIEWER_ROLES)
def assessment_detail(request, pk):
	company = _require_company(request)
	if company is None:
		return _redirect_no_company()

	snapshot = (
		AssessmentSnapshot.objects.for_company(company).filter(pk=pk).first()
	)
	if snapshot is None:
		raise Http404("Assessment not found.")

	previous = assessment_service.previous_assessment(snapshot)
	pillar_deltas = []
	new_gaps = []
	resolved_gaps = []
	if previous is not None:
		previous_pillars = {
			row["key"]: row["score"] for row in previous.pillar_rows
		}
		for row in snapshot.pillar_rows:
			before = previous_pillars.get(row["key"])
			pillar_deltas.append({
				"label": row["label"],
				"badge": PILLAR_BADGE.get(row["key"], "cp-badge-neutral"),
				"before": before,
				"after": row["score"],
				"delta": (row["score"] - before) if before is not None else None,
			})
		current_keys = _gap_keys(snapshot.gaps)
		previous_keys = _gap_keys(previous.gaps)
		for kind, items in snapshot.gaps.items():
			for item in items:
				if (kind, item.get("code")) not in previous_keys:
					new_gaps.append(item)
		for kind, items in previous.gaps.items():
			for item in items:
				if (kind, item.get("code")) not in current_keys:
					resolved_gaps.append(item)

	context = _module_context(request, company)
	context.update({
		"snapshot": snapshot,
		"breakdown": snapshot.breakdown,
		"previous": previous,
		"pillar_deltas": pillar_deltas,
		"new_gaps": new_gaps,
		"resolved_gaps": resolved_gaps,
		"gap_kinds": assessment_service.GAP_KINDS,
		"gap_groups": _gap_groups(snapshot.gaps),
		"readiness_label": Readiness(snapshot.readiness).label,
		"readiness_badge": READINESS_BADGE.get(snapshot.readiness, "cp-badge-neutral"),
		"can_run_ai": is_company_admin(request.user),
	})
	return render(request, "esg/assessment_detail.html", context)


# --------------------------------------------------------------------------- #
# AI completeness coach
# --------------------------------------------------------------------------- #
@login_required
@role_required(*VIEWER_ROLES)
def coach(request):
	company = _require_company(request)
	if company is None:
		return _redirect_no_company()

	years = assessment_service.available_period_years(company)
	requested = request.GET.get("year")
	period_year = int(requested) if (requested or "").isdigit() else None
	result = assessment_service.assess_company(company, period_year=period_year)

	context = _module_context(request, company)
	context.update({
		"result": result,
		"period_year": result.period_year,
		"years": years,
		"pillar_rows": _pillar_rows(result),
		"gaps_total": assessment_service.gaps_count(result.gaps),
		"readiness_label": Readiness(result.readiness).label,
		"readiness_badge": READINESS_BADGE.get(result.readiness, "cp-badge-neutral"),
		"coach": _ai_analysis(request, company),
		"confirm_form": RecommendationConfirmForm(),
		"can_manage": can_manage(request.user, company),
		"can_run_ai": is_company_admin(request.user),
	})
	return render(request, "esg/coach.html", context)


@login_required
@role_required(*VIEWER_ROLES)
@require_POST
def coach_run(request):
	company = _require_company(request)
	if company is None:
		return _redirect_no_company()
	if not is_company_admin(request.user):
		raise PermissionDenied

	requested = request.POST.get("year")
	period_year = int(requested) if (requested or "").isdigit() else None
	analysis = coach_ai.run_coach(company, period_year=period_year)
	_remember_analysis(request, analysis)
	if analysis.ai_available:
		messages.success(request, analysis.message)
	else:
		messages.warning(request, analysis.message)
	return redirect("esg:coach")


@login_required
@role_required(*VIEWER_ROLES)
@require_POST
def coach_confirm_recommendation(request):
	"""Turn a confirmed AI recommendation into a draft indicator.

	The user must confirm and supply the measurement unit; CarbonPilot never
	invents a value or a unit for a suggested metric.
	"""
	company = _require_company(request)
	if company is None:
		return _redirect_no_company()
	if not can_manage(request.user, company):
		raise PermissionDenied

	form = RecommendationConfirmForm(request.POST)
	if not form.is_valid():
		messages.error(
			request,
			"Confirm the recommendation with a name and a measurement unit first.",
		)
		return redirect("esg:coach")

	name = form.cleaned_data["name"]
	pillar = form.cleaned_data["pillar"]
	justification = (request.POST.get("justification") or "").strip()[:500]

	entry = IndicatorCatalogEntry.objects.create(
		company=company,
		code=_unique_custom_code(company, pillar),
		name=name[:255],
		pillar=pillar,
		unit=form.cleaned_data["unit"],
		description=(
			"Adopted from AI ESG coaching. " + justification
		).strip()[:1000],
		is_required=bool(form.cleaned_data.get("make_required")),
		is_active=True,
		sort_order=900,
	)
	indicator = workflow.create_indicator(
		company=company,
		catalog_entry=entry,
		actor=request.user,
		notes="Created from an AI coaching recommendation after human confirmation.",
	)
	messages.success(
		request,
		f"Draft indicator “{indicator.name}” created. Record its first meas"
		"urement when the data is available.",
	)
	return redirect("esg:indicator_detail", pk=indicator.pk)


def _unique_custom_code(company, pillar):
	prefix = {
		ESGPillar.ENVIRONMENTAL: "ENV",
		ESGPillar.SOCIAL: "SOC",
		ESGPillar.GOVERNANCE: "GOV",
	}.get(pillar, "ESG")
	index = 1
	while True:
		code = f"{prefix}-X{index:02d}"
		if not IndicatorCatalogEntry.objects.filter(company=company, code=code).exists():
			return code
		index += 1
