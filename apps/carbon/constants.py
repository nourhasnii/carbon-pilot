"""Domain reference data for the carbon module.

These mappings keep scope / unit defaults consistent between the activity model,
the emission-factor recommendation service and (later) the creation wizard.
"""

from apps.carbon.models import ActivityCategory, Scope

# Default Greenhouse Gas Protocol scope inferred from an activity category.
# Companies can still override the scope explicitly when their inventory differs.
CATEGORY_DEFAULT_SCOPE = {
	ActivityCategory.ELECTRICITY: Scope.SCOPE_2,
	ActivityCategory.NATURAL_GAS: Scope.SCOPE_1,
	ActivityCategory.FUEL: Scope.SCOPE_1,
	ActivityCategory.BUSINESS_TRAVEL: Scope.SCOPE_3,
	ActivityCategory.TRANSPORTATION: Scope.SCOPE_3,
	ActivityCategory.PURCHASED_GOODS: Scope.SCOPE_3,
	ActivityCategory.WASTE: Scope.SCOPE_3,
	ActivityCategory.WATER: Scope.SCOPE_3,
	ActivityCategory.OTHER: Scope.SCOPE_3,
}

# Suggested denominator unit shown by default for each category.
CATEGORY_DEFAULT_UNIT = {
	ActivityCategory.ELECTRICITY: "kWh",
	ActivityCategory.NATURAL_GAS: "m3",
	ActivityCategory.FUEL: "L",
	ActivityCategory.BUSINESS_TRAVEL: "passenger-km",
	ActivityCategory.TRANSPORTATION: "tonne-km",
	ActivityCategory.PURCHASED_GOODS: "kg",
	ActivityCategory.WASTE: "kg",
	ActivityCategory.WATER: "m3",
	ActivityCategory.OTHER: "unit",
}

# Human-readable scope labels used by the API / UI layer.
SCOPE_LABELS = {
	Scope.SCOPE_1: "Scope 1",
	Scope.SCOPE_2: "Scope 2",
	Scope.SCOPE_3: "Scope 3",
}


def default_scope_for(category):
	return CATEGORY_DEFAULT_SCOPE.get(category, Scope.SCOPE_3)


def default_unit_for(category):
	return CATEGORY_DEFAULT_UNIT.get(category, "unit")
