"""Indicateurs et classements des projets d'investissement."""

from decimal import Decimal, InvalidOperation

from django.db.models import Prefetch
from django.db.models.query import prefetch_related_objects

from .finance import (
	cost_per_tco2e,
	cumulative_tco2e,
	net_cost,
	roi,
	simple_payback_years,
)


CRITERIA = {
	"reduction": "Réduction cumulée",
	"cost": "Coût net",
	"roi": "ROI",
	"cost_per_tco2e": "Coût par tCO2e",
	"payback": "Retour simple",
}

DEFAULT_DESCENDING = {
	"reduction": True,
	"cost": False,
	"roi": True,
	"cost_per_tco2e": False,
	"payback": False,
}


def _related_objects(project, relation):
	cache = getattr(project, "_prefetched_objects_cache", {})
	if relation in cache:
		return list(cache[relation])
	return list(getattr(project, relation).all())


def project_indicators(project):
	"""Construit les indicateurs financiers d'un projet sans inventer de données."""
	investments = _related_objects(project, "investments")
	impacts = _related_objects(project, "impacts")
	net_total = None
	if investments:
		net_total = sum(
			(net_cost(item.actual_cost, item.planned_budget) for item in investments),
			Decimal("0"),
		)
	impact = max(impacts, key=lambda item: (item.created_at, item.pk)) if impacts else None
	tco2e = impact.estimated_tco2e_per_year if impact else None
	lifetime = impact.lifetime_years if impact else None
	annual_savings = impact.annual_savings if impact else None
	cumulative = cumulative_tco2e(tco2e, lifetime)
	return {
		"id": project.pk,
		"name": project.name,
		"status": project.status,
		"net_cost": net_total,
		"tco2e_per_year": tco2e,
		"lifetime_years": lifetime,
		"cumulative_tco2e": cumulative,
		"annual_savings": annual_savings,
		"cost_per_tco2e": cost_per_tco2e(net_total, tco2e, lifetime),
		"payback": simple_payback_years(net_total, annual_savings),
		"roi": roi(net_total, annual_savings, lifetime),
	}


def compare_projects(projects):
	"""Retourne les indicateurs de projets avec leurs relations préchargées."""
	projects = list(projects)
	prefetch_related_objects(
		projects,
		Prefetch("investments"),
		Prefetch("impacts"),
	)
	return [project_indicators(project) for project in projects]


def rank_projects(rows, criterion, descending=None):
	"""Trie les lignes selon une liste blanche ; les valeurs absentes restent en dernier."""
	if criterion not in CRITERIA:
		raise ValueError("Critère de classement invalide.")
	if descending is None:
		descending = DEFAULT_DESCENDING[criterion]
	field = criterion if criterion != "reduction" else "cumulative_tco2e"
	available = [row for row in rows if row.get(field) is not None]
	missing = [row for row in rows if row.get(field) is None]
	available.sort(key=lambda row: row[field], reverse=descending)
	return available + missing


def filter_by_budget(rows, budget):
	"""Garde les projets dont le coût net est inférieur ou égal au budget."""
	try:
		budget = budget if isinstance(budget, Decimal) else Decimal(str(budget))
	except (InvalidOperation, TypeError, ValueError):
		return []
	return [row for row in rows if row.get("net_cost") is not None and row["net_cost"] <= budget]
