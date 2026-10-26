"""Emission-factor recommendation for a given activity context."""

from django.db.models import Q

from apps.carbon.constants import default_scope_for
from apps.carbon.models import (
	EmissionFactor,
	EmissionFactorCompanyState,
	VerificationStatus,
)

# Regions that describe a non-geographic, generic factor.
_GENERIC_REGIONS = {"", "global", "international", "world", "worldwide", "generic"}


def _matches_region(factor_region, region):
	if not region:
		return factor_region.strip().lower() in _GENERIC_REGIONS
	return factor_region.strip().lower() == region.strip().lower()


def _is_generic_region(factor_region):
	return factor_region.strip().lower() in _GENERIC_REGIONS


def company_factor_states(company):
	"""Return ``{factor_id: EmissionFactorCompanyState}`` for a tenant."""
	if company is None:
		return {}
	return {
		state.factor_id: state
		for state in EmissionFactorCompanyState.objects.filter(company=company)
	}


def effective_is_active(factor, company, *, states=None):
	"""Resolve whether a factor may be used by a company, honouring overrides.

	Company-owned factors use their own flag. Shared factors fall back to the
	tenant override, which can only further restrict the platform value.
	"""
	if factor.company_id is not None:
		return factor.is_active
	if company is None:
		return factor.is_active
	states = company_factor_states(company) if states is None else states
	state = states.get(factor.pk)
	if state is None:
		return factor.is_active
	return factor.is_active and state.is_active


# Value attributes a company may override on a shared factor. Name, category and
# scope stay platform-owned because they identify the factor and drive matching.
OVERRIDABLE_FIELDS = ("value", "unit", "region", "year", "source", "methodology")


def effective_factor_attrs(factor, company, *, states=None):
	"""Return the value attributes a company should actually use for a factor.

	Company-owned factors and shared factors without an override return their
	platform values. A shared factor the tenant customised returns the tenant's
	own values, falling back to the platform value for any blank override field.
	"""
	attrs = {field: getattr(factor, field) for field in OVERRIDABLE_FIELDS}
	if factor.company_id is not None or company is None:
		return attrs
	states = company_factor_states(company) if states is None else states
	state = states.get(factor.pk)
	if state is None or not state.has_value_override:
		return attrs
	for field in OVERRIDABLE_FIELDS:
		override = getattr(state, field)
		if override:
			attrs[field] = override
	return attrs


def set_company_factor_overrides(*, factor, company, data, user=None):
	"""Store a tenant's own values for a shared factor (never the shared record)."""
	state, _ = EmissionFactorCompanyState.objects.update_or_create(
		company=company,
		factor=factor,
		defaults={
			**{field: data[field] for field in OVERRIDABLE_FIELDS},
			"updated_by": user if getattr(user, "is_authenticated", False) else None,
		},
	)
	return state


def set_company_factor_active(*, factor, company, is_active, user=None):
	"""Deactivate/reactivate a shared factor for a single tenant only."""
	state, _ = EmissionFactorCompanyState.objects.update_or_create(
		company=company,
		factor=factor,
		defaults={
			"is_active": is_active,
			"updated_by": user if getattr(user, "is_authenticated", False) else None,
		},
	)
	return state


def company_restricted_factor_ids(company):
	"""Ids of shared factors a tenant has deactivated for itself."""
	if company is None:
		return []
	return list(
		EmissionFactorCompanyState.objects.filter(
			company=company, is_active=False, factor__company__isnull=True
		).values_list("factor_id", flat=True)
	)


def factors_for_company(*, company=None, active_only=True):
	"""Return shared factors plus factors owned by the selected tenant.

	When ``active_only`` is set, shared factors a tenant has deactivated for
	itself are excluded, so they cannot be picked for new activities.
	"""
	queryset = EmissionFactor.objects.all()
	if company is None:
		queryset = queryset.filter(company__isnull=True)
	else:
		queryset = queryset.filter(Q(company__isnull=True) | Q(company=company))
	if not active_only:
		return queryset
	queryset = queryset.filter(is_active=True)
	restricted_ids = company_restricted_factor_ids(company)
	if restricted_ids:
		queryset = queryset.exclude(pk__in=restricted_ids)
	return queryset


def _score(factor, *, region, year, scope):
	score = 0
	# Geographic specificity is the strongest signal.
	if _matches_region(factor.region, region):
		score += 100
	elif region and _is_generic_region(factor.region):
		score += 25
	# Prefer an exact reporting year, then the most recent factor not newer than it.
	if year:
		if factor.year == year:
			score += 40
		elif factor.year < year:
			score += 40 - min(39, year - factor.year)
		else:
			score -= 20  # a factor from the future is less trustworthy
	# Verified factors are preferred over unverified or demo entries.
	if factor.verification_status == VerificationStatus.VERIFIED:
		score += 30
	elif factor.verification_status == VerificationStatus.DEMO:
		score -= 10
	if scope and factor.scope == scope:
		score += 5
	return score


def recommendation_candidates(*, category, scope=None, region=None, year=None, company=None):
	"""Return active factors for a category, best recommendation first."""
	if region is None and company is not None:
		region = getattr(company, "country", "") or ""
	region = region or ""
	scope = scope or default_scope_for(category)
	queryset = factors_for_company(company=company).filter(category=category)
	if scope:
		queryset = queryset.filter(scope=scope)
	candidates = list(queryset)
	candidates.sort(
		key=lambda factor: (_score(factor, region=region, year=year, scope=scope), factor.year, factor.pk),
		reverse=True,
	)
	return candidates


def recommend_emission_factor(*, category, scope=None, region=None, year=None, company=None):
	"""Return the single best-matching active emission factor, or ``None``."""
	candidates = recommendation_candidates(
		category=category, scope=scope, region=region, year=year, company=company
	)
	return candidates[0] if candidates else None
