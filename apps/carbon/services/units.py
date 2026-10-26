"""Small, dependency-free unit conversion helper for carbon calculations.

Only conversions between units of the same physical dimension are allowed.
Anything else raises :class:`UnitConversionError` so a wrong unit can never
silently produce a plausible-but-wrong CO2e figure.
"""

from decimal import Decimal, ROUND_HALF_UP

_CONVERSION_PRECISION = Decimal("0.000001")

# dimension -> {normalized unit: factor to the dimension base unit}
_DIMENSION_UNITS = {
	"mass": {  # base: kilogram
		"kg": "1",
		"kilogram": "1",
		"kilograms": "1",
		"g": "0.001",
		"gram": "0.001",
		"grams": "0.001",
		"mg": "0.000001",
		"t": "1000",
		"ton": "1000",
		"tons": "1000",
		"tonne": "1000",
		"tonnes": "1000",
		"lb": "0.45359237",
		"lbs": "0.45359237",
		"pound": "0.45359237",
		"pounds": "0.45359237",
		"oz": "0.0283495",
		"ounce": "0.0283495",
	},
	"energy": {  # base: kilowatt-hour
		"kwh": "1",
		"kilowatt-hour": "1",
		"kilowatthour": "1",
		"wh": "0.001",
		"watt-hour": "0.001",
		"mwh": "1000",
		"gwh": "1000000",
		"mj": "0.277777777778",
		"megajoule": "0.277777777778",
		"gj": "277.777777778",
		"gigajoule": "277.777777778",
		"tj": "277777.777778",
	},
	"volume": {  # base: litre
		"l": "1",
		"litre": "1",
		"liter": "1",
		"litres": "1",
		"liters": "1",
		"ml": "0.001",
		"m3": "1000",
		"m³": "1000",
		"cubicmetre": "1000",
		"cubicmeter": "1000",
		"gallon": "3.785411784",
		"gal": "3.785411784",
		"barrel": "158.987294928",
	},
	"distance": {  # base: kilometre
		"km": "1",
		"kilometre": "1",
		"kilometer": "1",
		"m": "0.001",
		"metre": "0.001",
		"meter": "0.001",
		"mi": "1.609344",
		"mile": "1.609344",
		"miles": "1.609344",
		"nmi": "1.852",
		"pkm": "1",
		"passenger-km": "1",
		"passengerkm": "1",
	},
	"tonne_distance": {  # base: tonne-kilometre
		"tkm": "1",
		"tonne-km": "1",
		"tonnekm": "1",
		"ton-km": "1",
		"tonkm": "1",
	},
	"count": {  # base: generic unit
		"unit": "1",
		"units": "1",
		"each": "1",
		"ea": "1",
		"item": "1",
		"items": "1",
		"piece": "1",
		"pieces": "1",
		"night": "1",
		"nights": "1",
		"room-night": "1",
		"roomnight": "1",
		"stay": "1",
		"person": "1",
		"persons": "1",
		"employee": "1",
		"employees": "1",
		"fte": "1",
		"occupant": "1",
	},
}


# Curated units surfaced as suggestions in the activity wizard.
COMMON_UNITS = (
	"kWh", "MWh", "L", "m3", "kg", "t",
	"km", "passenger-km", "tonne-km", "unit",
)


class UnitConversionError(ValueError):
	"""Raised when two units cannot be safely converted into one another."""


def _normalize(unit):
	if not unit:
		return ""
	return str(unit).strip().lower().replace("_", "-").replace(" ", "")


_UNIT_LOOKUP = {
	unit: (dimension, Decimal(factor))
	for dimension, units in _DIMENSION_UNITS.items()
	for unit, factor in units.items()
}


def canonical_unit(unit):
	"""Return the normalized (lower-case) form of a unit string."""
	return _normalize(unit)


def are_compatible(first_unit, second_unit):
	first = _UNIT_LOOKUP.get(_normalize(first_unit))
	second = _UNIT_LOOKUP.get(_normalize(second_unit))
	return first is not None and second is not None and first[0] == second[0]


def convert(value, from_unit, to_unit):
	"""Convert *value* from one unit to another of the same dimension.

	Raises :class:`UnitConversionError` for unknown units or dimension mismatch.
	"""
	value = value if isinstance(value, Decimal) else Decimal(str(value))
	normalized_from = _normalize(from_unit)
	normalized_to = _normalize(to_unit)

	if normalized_from == normalized_to:
		return value

	from_info = _UNIT_LOOKUP.get(normalized_from)
	if from_info is None:
		raise UnitConversionError(f"Unknown unit '{from_unit}'.")
	to_info = _UNIT_LOOKUP.get(normalized_to)
	if to_info is None:
		raise UnitConversionError(f"Unknown unit '{to_unit}'.")
	if from_info[0] != to_info[0]:
		raise UnitConversionError(
			f"Cannot convert '{from_unit}' to '{to_unit}': incompatible units."
		)

	base_value = value * from_info[1]
	converted = base_value / to_info[1]
	return converted.quantize(_CONVERSION_PRECISION, rounding=ROUND_HALF_UP)
