"""The default, configurable ESG indicator catalog.

These entries are seeded as **shared platform defaults** (``company=None``) by
the data migration, giving every company a documented, comparable starting
point. They are ordinary :class:`~apps.esg.models.IndicatorCatalogEntry` rows:
a tenant can deactivate one, add its own, or flag an entry optional, so the
required-indicator catalog stays fully configurable.
"""

from ..models import ESGPillar

# (code, name, pillar, unit, description, is_required, sort_order)
DEFAULT_CATALOG = (
	# --- Environmental --------------------------------------------------- #
	(
		"ENV-001",
		"GHG emissions · Scope 1",
		ESGPillar.ENVIRONMENTAL,
		"tCO2e",
		"Direct greenhouse-gas emissions from owned or controlled sources.",
		True,
		10,
	),
	(
		"ENV-002",
		"GHG emissions · Scope 2",
		ESGPillar.ENVIRONMENTAL,
		"tCO2e",
		"Indirect emissions from purchased electricity, heat or steam.",
		True,
		20,
	),
	(
		"ENV-003",
		"GHG emissions · Scope 3",
		ESGPillar.ENVIRONMENTAL,
		"tCO2e",
		"Other indirect emissions across the value chain.",
		True,
		30,
	),
	(
		"ENV-004",
		"Total energy consumption",
		ESGPillar.ENVIRONMENTAL,
		"MWh",
		"Total energy consumed across the reporting period.",
		True,
		40,
	),
	(
		"ENV-005",
		"Renewable energy share",
		ESGPillar.ENVIRONMENTAL,
		"%",
		"Share of energy consumption from renewable sources.",
		True,
		50,
	),
	(
		"ENV-006",
		"Total waste generated",
		ESGPillar.ENVIRONMENTAL,
		"tonnes",
		"Total waste generated across the reporting period.",
		True,
		60,
	),
	(
		"ENV-007",
		"Waste diverted from disposal",
		ESGPillar.ENVIRONMENTAL,
		"%",
		"Share of waste reused, recycled or recovered instead of disposed.",
		True,
		70,
	),
	(
		"ENV-008",
		"Total water withdrawal",
		ESGPillar.ENVIRONMENTAL,
		"m3",
		"Total water withdrawn across the reporting period.",
		True,
		80,
	),
	# --- Social ---------------------------------------------------------- #
	(
		"SOC-001",
		"Training hours per employee",
		ESGPillar.SOCIAL,
		"hours",
		"Average training hours delivered per employee.",
		True,
		10,
	),
	(
		"SOC-002",
		"Recordable injury rate",
		ESGPillar.SOCIAL,
		"per 1M hours",
		"Workplace safety incident rate per one million hours worked.",
		True,
		20,
	),
	(
		"SOC-003",
		"Employee turnover rate",
		ESGPillar.SOCIAL,
		"%",
		"Share of the workforce that left during the reporting period.",
		True,
		30,
	),
	(
		"SOC-004",
		"Women in workforce",
		ESGPillar.SOCIAL,
		"%",
		"Share of women across the total workforce.",
		True,
		40,
	),
	# --- Governance ------------------------------------------------------ #
	(
		"GOV-001",
		"Active policies",
		ESGPillar.GOVERNANCE,
		"count",
		"Number of internal ESG/business policies in force.",
		True,
		10,
	),
	(
		"GOV-002",
		"Policies reviewed in period",
		ESGPillar.GOVERNANCE,
		"count",
		"Number of policies formally reviewed during the reporting period.",
		True,
		20,
	),
	(
		"GOV-003",
		"Open audit findings",
		ESGPillar.GOVERNANCE,
		"count",
		"Audit findings still open or in progress at period end.",
		True,
		30,
	),
	(
		"GOV-004",
		"Governance controls tested",
		ESGPillar.GOVERNANCE,
		"count",
		"Number of internal governance controls tested in the period.",
		True,
		40,
	),
)


def default_entries():
	"""Yield catalog dicts ready for ``IndicatorCatalogEntry.objects.create``."""
	for code, name, pillar, unit, description, is_required, sort_order in DEFAULT_CATALOG:
		yield {
			"code": code,
			"name": name,
			"pillar": pillar,
			"unit": unit,
			"description": description,
			"is_required": is_required,
			"sort_order": sort_order,
		}
