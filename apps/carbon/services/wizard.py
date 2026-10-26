"""Session-backed state for the multi-step "Add Carbon Activity" wizard."""

from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP

from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.carbon.constants import default_scope_for, default_unit_for
from apps.carbon.models import (
	ActivityAuditAction,
	ActivityStatus,
	CarbonActivity,
	EmissionFactor,
	calculate_co2e_kg,
)
from apps.carbon.services.recommendation import (
	effective_factor_attrs,
	factors_for_company,
)
from apps.carbon.services.workflow import notify_department_managers_of_activity

STATE_KEY = "carbon_activity_wizard"

STEP_LABELS = (
	(1, "Activity"),
	(2, "Data"),
	(3, "Emission factor"),
	(4, "Review"),
)
TOTAL_STEPS = len(STEP_LABELS)


def get_state(request):
	return request.session.get(STATE_KEY, {})


def update_state(request, **values):
	state = get_state(request)
	state.update(values)
	request.session[STATE_KEY] = state
	request.session.modified = True
	return state


def clear_state(request):
	request.session.pop(STATE_KEY, None)


def highest_reachable_step(state):
	"""The furthest step a user may open given the data already captured."""
	if not state.get("category"):
		return 1
	if not (state.get("quantity") and state.get("unit") and state.get("period_start")):
		return 2
	if not state.get("emission_factor_id"):
		return 3
	return 4


def _parse_date(value):
	if not value:
		return None
	if isinstance(value, date) and not isinstance(value, datetime):
		return value
	return datetime.fromisoformat(str(value)).date()


def period_year(state):
	start = _parse_date(state.get("period_start"))
	return start.year if start else timezone.localdate().year


def build_preview(state, *, company=None):
	"""Transparent calculation preview: quantity × factor = CO2e."""
	factor_id = state.get("emission_factor_id")
	factor = (
		factors_for_company(company=company)
		.filter(pk=factor_id, category=state.get("category"), scope=default_scope_for(state.get("category")))
		.first()
		if factor_id
		else None
	)
	if factor is None:
		return {"available": False, "message": "Select an emission factor to see the calculation preview."}
	attrs = effective_factor_attrs(factor, company)
	try:
		quantity = Decimal(str(state["quantity"]))
		co2e_kg = calculate_co2e_kg(
			quantity=quantity,
			activity_unit=state.get("unit"),
			factor_value=attrs["value"],
			factor_unit=attrs["unit"],
		)
	except (KeyError, ValidationError) as error:
		message = error.messages[0] if isinstance(error, ValidationError) else str(error)
		return {"available": False, "message": message}
	return {
		"available": True,
		"quantity": quantity,
		"unit": state.get("unit"),
		"factor": factor,
		"factor_attrs": attrs,
		"co2e_kg": co2e_kg,
		"co2e_tonnes": (co2e_kg / Decimal("1000")).quantize(
			Decimal("0.0001"), rounding=ROUND_HALF_UP
		),
	}


def create_activity(user, company, state, *, data_source="", evidence=None, submit=False):
	"""Persist the wizard result. CO2e is always computed by the model."""
	factor = factors_for_company(company=company).filter(
		pk=state.get("emission_factor_id"),
		category=state.get("category"),
		scope=default_scope_for(state.get("category")),
	).first()
	if factor is None:
		raise ValidationError("Select an active emission factor available to your company.")
	activity = CarbonActivity(
		company=company,
		department_id=state.get("department_id") or None,
		name=(state.get("name") or "").strip(),
		category=state["category"],
		scope=default_scope_for(state["category"]),
		quantity=Decimal(str(state["quantity"])),
		unit=state["unit"],
		period_start=_parse_date(state["period_start"]),
		period_end=_parse_date(state.get("period_end")),
		emission_factor=factor,
		data_source=data_source.strip(),
		created_by=user,
		status=ActivityStatus.SUBMITTED if submit else ActivityStatus.DRAFT,
	)
	if evidence:
		activity.evidence = evidence
	if submit:
		activity.submitted_at = timezone.now()
	activity.save()
	activity.log_event(
		actor=user, action=ActivityAuditAction.CREATED, to_status=ActivityStatus.DRAFT
	)
	if submit:
		activity.log_event(
			actor=user,
			action=ActivityAuditAction.SUBMITTED,
			from_status=ActivityStatus.DRAFT,
			to_status=ActivityStatus.SUBMITTED,
		)
		notify_department_managers_of_activity(activity, user)
	return activity


def suggested_unit(category):
	return default_unit_for(category)
