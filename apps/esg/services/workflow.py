"""Indicator lifecycle: create, report, verify — every change is audited.

The indicator workflow is deliberately small and auditable:

* an :class:`~apps.esg.models.Indicator` is created once per catalog entry and
  can only be deactivated (soft archive), never deleted, so its history stays;
* a measurement moves ``DRAFT -> REPORTED -> VERIFIED`` and can be returned to
  ``DRAFT`` while it is reviewed;
* every transition appends one row to ``IndicatorHistory``.

Nothing here trusts a caller-supplied company: the tenant is always taken from
the parent object (the indicator), and cross-company references are rejected
before anything is written.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from ..models import (
	ELIGIBLE_STATUSES,
	HistoryAction,
	Indicator,
	IndicatorStatus,
	IndicatorValue,
)


# --------------------------------------------------------------------------- #
# Indicators
# --------------------------------------------------------------------------- #
@transaction.atomic
def create_indicator(*, company, catalog_entry, actor, owner=None, department=None, notes=""):
	"""Create one indicator for a catalog entry. Rejects duplicates/other tenants."""
	if company is None:
		raise ValidationError("A company is required to create an indicator.")
	if catalog_entry is None:
		raise ValidationError("Pick a catalog indicator to track.")
	if catalog_entry.company_id not in (None, company.pk):
		raise ValidationError("The catalog entry must be shared or belong to your company.")
	if Indicator.objects.filter(company=company, catalog_entry=catalog_entry).exists():
		raise ValidationError("Your company already tracks this indicator.")

	indicator = Indicator(
		company=company,
		catalog_entry=catalog_entry,
		owner=owner,
		department=department,
		notes=(notes or "").strip(),
		created_by=actor if getattr(actor, "is_authenticated", False) else None,
	)
	indicator.full_clean()
	indicator.save()
	indicator.log_event(
		actor=actor,
		action=HistoryAction.CREATED,
		summary=f"Started tracking {catalog_entry.code} — {catalog_entry.name}.",
	)
	return indicator


@transaction.atomic
def update_indicator(indicator, *, actor, owner=None, department=None, notes=""):
	"""Update ownership and notes. The tracked catalog entry never changes."""
	indicator.owner = owner
	indicator.department = department
	indicator.notes = (notes or "").strip()
	indicator.full_clean()
	indicator.save(update_fields=("owner", "department", "notes", "updated_at"))
	indicator.log_event(
		actor=actor,
		action=HistoryAction.UPDATED,
		summary="Indicator ownership or notes updated.",
	)
	return indicator


@transaction.atomic
def set_indicator_active(indicator, active, *, actor):
	"""Soft archive / reactivate an indicator (history is preserved either way)."""
	if indicator.is_active == active:
		return indicator
	indicator.is_active = active
	indicator.save(update_fields=("is_active", "updated_at"))
	indicator.log_event(
		actor=actor,
		action=HistoryAction.REACTIVATED if active else HistoryAction.ARCHIVED,
		summary=(
			"Indicator reactivated and counted towards completeness again."
			if active
			else "Indicator deactivated; it no longer counts towards completeness."
		),
	)
	return indicator


# --------------------------------------------------------------------------- #
# Measurements
# --------------------------------------------------------------------------- #
@transaction.atomic
def record_value(
	indicator,
	*,
	actor,
	period_year,
	value,
	unit="",
	evidence_reference="",
	note="",
	status=IndicatorStatus.DRAFT,
):
	"""Create or update the measurement of one reporting period.

	``value`` of ``None`` means the measurement is *missing*; ``Decimal("0")``
	is a genuine measured zero. A missing measurement can never be reported: a
	value must exist before the point can count as ESG data.
	"""
	if status not in (IndicatorStatus.DRAFT, IndicatorStatus.REPORTED):
		raise ValidationError("A measurement can be saved as a draft or reported.")
	if status == IndicatorStatus.REPORTED and value is None:
		raise ValidationError(
			"A value is required to report a measurement — leave it as a draft if it is missing."
		)

	existing = IndicatorValue.objects.filter(
		indicator=indicator, period_year=period_year
	).first()

	if existing is not None and existing.status == IndicatorStatus.VERIFIED:
		raise ValidationError(
			"This period is already verified. Return it to draft before editing it."
		)

	from_status = existing.status if existing else ""
	value_row = existing or IndicatorValue(indicator=indicator, period_year=period_year)
	# The tenant is always inherited from the indicator, never from the caller.
	value_row.company = indicator.company
	value_row.value = value
	value_row.unit = (unit or indicator.unit or "").strip()
	value_row.evidence_reference = (evidence_reference or "").strip()
	value_row.note = (note or "").strip()
	value_row.status = status
	if status == IndicatorStatus.REPORTED:
		value_row.reported_by = actor if getattr(actor, "is_authenticated", False) else None
		value_row.reported_at = timezone.now()
	value_row.full_clean()
	value_row.save()

	indicator.log_event(
		actor=actor,
		action=HistoryAction.VALUE_UPDATED if existing else HistoryAction.VALUE_RECORDED,
		summary=(
			f"{period_year} measurement "
			+ ("updated" if existing else "recorded")
			+ (" and reported." if status == IndicatorStatus.REPORTED else " as a draft.")
		),
		period_year=period_year,
		from_status=from_status,
		to_status=status,
	)
	return value_row


@transaction.atomic
def mark_reported(value, *, actor):
	"""DRAFT -> REPORTED, once a value actually exists."""
	if value.status != IndicatorStatus.DRAFT:
		raise ValidationError("Only a draft measurement can be reported.")
	if value.value is None:
		raise ValidationError("Record a value before reporting this measurement.")
	value.status = IndicatorStatus.REPORTED
	value.reported_by = actor if getattr(actor, "is_authenticated", False) else None
	value.reported_at = timezone.now()
	value.save(update_fields=("status", "reported_by", "reported_at", "updated_at"))
	value.indicator.log_event(
		actor=actor,
		action=HistoryAction.REPORTED,
		summary=f"{value.period_year} measurement reported.",
		period_year=value.period_year,
		from_status=IndicatorStatus.DRAFT,
		to_status=IndicatorStatus.REPORTED,
	)
	return value


@transaction.atomic
def verify_value(value, *, actor):
	"""REPORTED -> VERIFIED. Verification is a control function."""
	if value.status not in ELIGIBLE_STATUSES:
		raise ValidationError("Only a reported measurement can be verified.")
	if value.status == IndicatorStatus.VERIFIED:
		raise ValidationError("This measurement is already verified.")
	if value.value is None:
		raise ValidationError("A measurement without a value cannot be verified.")
	value.status = IndicatorStatus.VERIFIED
	value.verified_by = actor if getattr(actor, "is_authenticated", False) else None
	value.verified_at = timezone.now()
	value.save(update_fields=("status", "verified_by", "verified_at", "updated_at"))
	value.indicator.log_event(
		actor=actor,
		action=HistoryAction.VERIFIED,
		summary=f"{value.period_year} measurement verified.",
		period_year=value.period_year,
		from_status=IndicatorStatus.REPORTED,
		to_status=IndicatorStatus.VERIFIED,
	)
	return value


@transaction.atomic
def return_value_to_draft(value, *, actor, reason=""):
	"""VERIFIED/REPORTED -> DRAFT, so it can be corrected and reviewed again."""
	if value.status == IndicatorStatus.DRAFT:
		raise ValidationError("This measurement is already a draft.")
	from_status = value.status
	value.status = IndicatorStatus.DRAFT
	value.verified_by = None
	value.verified_at = None
	value.save(update_fields=("status", "verified_by", "verified_at", "updated_at"))
	value.indicator.log_event(
		actor=actor,
		action=HistoryAction.REJECTED,
		summary=(reason or "").strip()
		or f"{value.period_year} measurement returned to draft for correction.",
		period_year=value.period_year,
		from_status=from_status,
		to_status=IndicatorStatus.DRAFT,
	)
	return value
