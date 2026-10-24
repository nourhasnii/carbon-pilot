"""Algorithmes purs d'optimisation de portefeuille d'investissements."""

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from itertools import combinations

from apps.investments.models import ProjectStatus
from apps.investments.services.ranking import project_indicators


class InsufficientData(Exception):
	"""Signale qu'un portefeuille ne contient pas assez de données exploitables."""


@dataclass(frozen=True)
class ProjectItem:
	id: int
	name: str
	cost: Decimal
	value: Decimal
	tco2e: Decimal
	savings: Decimal
	lifetime_years: Decimal = Decimal("1")


@dataclass(frozen=True)
class SolveResult:
	selected_ids: list[int]
	total_cost: Decimal
	total_value: Decimal
	total_tco2e: Decimal
	states_evaluated: int


def build_items(company, statuses, objective):
	"""Construit les projets éligibles d'une entreprise sans inventer de données."""
	if objective not in ("MAX_TCO2E", "MAX_ROI"):
		raise ValueError("L'objectif d'optimisation est invalide.")
	statuses = [status for status in statuses if status != ProjectStatus.CANCELLED]
	projects = (
		company.decarbonization_projects
		.filter(status__in=statuses)
		.exclude(status=ProjectStatus.CANCELLED)
		.prefetch_related("investments", "impacts")
	)
	items = []
	for project in projects:
		indicators = project_indicators(project)
		cost = indicators["net_cost"]
		tco2e = indicators["cumulative_tco2e"]
		savings = indicators["annual_savings"]
		lifetime = indicators["lifetime_years"]
		if cost is None or tco2e is None or savings is None or lifetime is None:
			continue
		value = tco2e if objective == "MAX_TCO2E" else savings * lifetime - cost
		if value <= 0:
			continue
		items.append(ProjectItem(
			id=project.pk,
			name=project.name,
			cost=cost,
			value=value,
			tco2e=tco2e,
			savings=savings,
			lifetime_years=Decimal(str(lifetime)),
		))
	if len(items) < 2:
		raise InsufficientData(
			"Données insuffisantes : il faut au moins 2 projets avec un coût et un impact renseignés"
		)
	return items


def _budget_units(budget):
	"""Convertit prudemment un budget en unités de 100 TND."""
	return int((Decimal(str(budget)) / Decimal("100")).to_integral_value(rounding=ROUND_FLOOR))


def _cost_units(cost):
	"""Arrondit chaque coût à l'unité de 100 TND supérieure."""
	return max(1, int((Decimal(cost) / Decimal("100")).to_integral_value(rounding=ROUND_CEILING)))


def _result(items, selected_indexes, states_evaluated):
	selected = [items[index] for index in selected_indexes]
	return SolveResult(
		selected_ids=[item.id for item in selected],
		total_cost=sum((item.cost for item in selected), Decimal("0")),
		total_value=sum((item.value for item in selected), Decimal("0")),
		total_tco2e=sum((item.tco2e for item in selected), Decimal("0")),
		states_evaluated=states_evaluated,
	)


def solve_exact(items, budget):
	"""Résout le sac à dos 0/1 par programmation dynamique.

	Les coûts sont arrondis à l'unité de 100 TND supérieure et le budget à
l'unité inférieure. Cette convention est prudente : la sélection ne dépasse
jamais le budget réel, au prix possible d'une solution légèrement moins remplie.
	"""
	budget_units = _budget_units(budget)
	cost_units = [_cost_units(item.cost) for item in items]
	dp = [None] * (budget_units + 1)
	dp[0] = (Decimal("0"), ())
	states = 0
	for index, item in enumerate(items):
		units = cost_units[index]
		for capacity in range(budget_units, units - 1, -1):
			states += 1
			previous = dp[capacity - units]
			if previous is None:
				continue
			candidate_value = previous[0] + item.value
			if dp[capacity] is None or candidate_value > dp[capacity][0]:
				dp[capacity] = (candidate_value, previous[1] + (index,))
	best = max(
		(value for value in dp if value is not None),
		key=lambda entry: (entry[0], -sum(cost_units[index] for index in entry[1])),
		default=(Decimal("0"), ()),
	)
	return _result(items, best[1], states)


def solve_greedy(items, budget):
	"""Sélectionne les meilleurs ratios sous la même convention prudente que le DP."""
	remaining_units = _budget_units(budget)
	cost_units = [_cost_units(item.cost) for item in items]
	selected = []
	states = 0
	ordered = sorted(
		range(len(items)),
		key=lambda index: items[index].value / items[index].cost,
		reverse=True,
	)
	for index in ordered:
		states += 1
		if cost_units[index] <= remaining_units:
			selected.append(index)
			remaining_units -= cost_units[index]
	return _result(items, selected, states)


def brute_force(items, budget):
	"""Énumère toutes les combinaisons ; refuse plus de 15 projets."""
	if len(items) > 15:
		raise ValueError("La recherche exhaustive est limitée à 15 projets.")
	budget_units = _budget_units(budget)
	cost_units = [_cost_units(item.cost) for item in items]
	best = ()
	best_value = Decimal("0")
	states = 0
	for size in range(len(items) + 1):
		for indexes in combinations(range(len(items)), size):
			states += 1
			value = sum((items[index].value for index in indexes), Decimal("0"))
			cost = sum((cost_units[index] for index in indexes), 0)
			if cost <= budget_units and value > best_value:
				best = indexes
				best_value = value
	return _result(items, best, states)


def explain(items, result, budget):
	"""Explique la sélection avec les coûts et valeurs effectivement calculés."""
	selected = {item.id: item for item in items if item.id in result.selected_ids}
	remaining = Decimal(str(budget)) - result.total_cost
	parts = [
		f"Le budget utilisé est de {result.total_cost:.2f} TND sur {Decimal(str(budget)):.2f} TND."
	]
	for item in items:
		if item.id in selected:
			parts.append(
				f"Le projet {item.name} est retenu car sa valeur calculée est de {item.value:.2f}."
			)
		elif item.cost > Decimal(str(budget)):
			parts.append(
				f"Le projet {item.name} est exclu car son coût de {item.cost:.2f} TND dépasse le budget."
			)
		else:
			parts.append(
				f"Le projet {item.name} est exclu car le budget restant de {remaining:.2f} TND est insuffisant."
			)
	return " ".join(parts)
