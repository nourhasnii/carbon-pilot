"""Calculs financiers purs pour les projets d'investissement."""

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


CENT = Decimal("0.01")


def _decimal(value):
	"""Convertit une valeur numérique en Decimal sans passer par un float."""
	if value is None:
		return None
	if isinstance(value, Decimal):
		return value
	try:
		return Decimal(str(value))
	except (InvalidOperation, TypeError, ValueError):
		return None


def _rounded(value):
	return value.quantize(CENT, rounding=ROUND_HALF_UP)


def net_cost(actual_cost, planned_budget):
	"""Retourne le coût réel s'il existe, sinon le budget prévu."""
	value = _decimal(actual_cost)
	if value is None:
		value = _decimal(planned_budget)
	return _rounded(value) if value is not None else None


def cumulative_tco2e(tco2e_per_year, lifetime_years):
	"""Calcule le tCO2e cumulé ; renvoie None si une valeur manque ou vaut zéro."""
	annual = _decimal(tco2e_per_year)
	lifetime = _decimal(lifetime_years)
	if annual is None or lifetime is None or annual == 0 or lifetime == 0:
		return None
	return _rounded(annual * lifetime)


def cost_per_tco2e(net_cost_value, tco2e_per_year, lifetime_years):
	"""Calcule le coût par tCO2e ; renvoie None en cas de division par zéro."""
	cost = _decimal(net_cost_value)
	cumulative = cumulative_tco2e(tco2e_per_year, lifetime_years)
	if cost is None or cumulative in (None, Decimal("0")):
		return None
	return _rounded(cost / cumulative)


def simple_payback_years(net_cost_value, annual_savings):
	"""Calcule le retour simple en années ; renvoie None si les économies valent zéro."""
	cost = _decimal(net_cost_value)
	savings = _decimal(annual_savings)
	if cost is None or savings in (None, Decimal("0")):
		return None
	return _rounded(cost / savings)


def roi(net_cost_value, annual_savings, lifetime_years):
	"""Calcule le ROI ; renvoie None si le coût net vaut zéro."""
	cost = _decimal(net_cost_value)
	savings = _decimal(annual_savings)
	lifetime = _decimal(lifetime_years)
	if cost in (None, Decimal("0")) or savings is None or lifetime is None:
		return None
	return _rounded((savings * lifetime - cost) / cost)


def planned_vs_actual(estimated, actual):
	"""Retourne les écarts absolu et relatif, ou None si le réel est absent.

	L'écart absolu est toujours positif et le relatif est rapporté à l'estimation.
	Le résultat est un dictionnaire ``{"absolute": ..., "relative": ...}``.
	Si l'estimation vaut zéro, le relatif vaut None pour éviter une division par zéro.
	"""
	estimated_value = _decimal(estimated)
	actual_value = _decimal(actual)
	if actual_value is None or estimated_value is None:
		return None
	absolute = _rounded(abs(actual_value - estimated_value))
	relative = None
	if estimated_value != 0:
		relative = _rounded(absolute / abs(estimated_value))
	return {"absolute": absolute, "relative": relative}
