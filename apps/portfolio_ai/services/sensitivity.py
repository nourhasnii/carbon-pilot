"""Analyse de sensibilité du portefeuille optimisé."""

from decimal import Decimal

from .optimizer import ProjectItem, solve_exact


FACTORS = (Decimal("0.8"), Decimal("0.9"), Decimal("1.0"), Decimal("1.1"), Decimal("1.2"))


def _items_for(items, objective, cost_factor=Decimal("1"), reduction_factor=Decimal("1"), price_factor=Decimal("1")):
	result = []
	for item in items:
		cost = item.cost * cost_factor
		tco2e = item.tco2e * reduction_factor
		savings = item.savings * price_factor
		if objective == "MAX_TCO2E":
			value = tco2e
		elif objective == "MAX_ROI":
			value = savings * item.lifetime_years - cost
		else:
			raise ValueError("L'objectif d'optimisation est invalide.")
		if value > 0:
			result.append(ProjectItem(
				id=item.id,
				name=item.name,
				cost=cost,
				value=value,
				tco2e=tco2e,
				savings=savings,
				lifetime_years=item.lifetime_years,
			))
	return result


def _case(items, base_ids, budget, objective, parameter, factor, applicable, cost_factor=Decimal("1"), reduction_factor=Decimal("1"), price_factor=Decimal("1")):
	scenario_items = _items_for(items, objective, cost_factor, reduction_factor, price_factor)
	result = solve_exact(scenario_items, budget)
	return {
		"parameter": parameter,
		"factor": factor,
		"applicable": applicable,
		"selected_ids": result.selected_ids,
		"total_cost": result.total_cost,
		"total_value": result.total_value,
		"projects_in": [project_id for project_id in result.selected_ids if project_id not in base_ids],
		"projects_out": [project_id for project_id in base_ids if project_id not in result.selected_ids],
	}


def run_sensitivity(items, budget, objective):
	"""Recalcule les choix selon les coûts, réductions et prix de l'énergie.

	Le facteur de prix de l'énergie ne modifie que MAX_ROI, car les économies
	annuelles entrent dans cette valeur ; il est marqué non applicable pour
	MAX_TCO2E. La stabilité est la part des cas applicables identiques à la base.
	"""
	base_items = _items_for(items, objective)
	base_result = solve_exact(base_items, budget)
	base = {
		"selected_ids": base_result.selected_ids,
		"total_cost": base_result.total_cost,
		"total_value": base_result.total_value,
	}
	base_ids = set(base["selected_ids"])
	cases = []
	for factor in FACTORS:
		cases.append(_case(items, base_ids, budget, objective, "cost_factor", factor, True, cost_factor=factor))
	for factor in FACTORS:
		cases.append(_case(items, base_ids, budget, objective, "reduction_factor", factor, True, reduction_factor=factor))
	for factor in (Decimal("0.8"), Decimal("1.2")):
		applicable = objective == "MAX_ROI"
		cases.append(_case(
			items,
			base_ids,
			budget,
			objective,
			"energy_price",
			factor,
			applicable,
			price_factor=factor,
		))
	applicable_cases = [case for case in cases if case["applicable"]]
	stable = sum(
		case["selected_ids"] == base["selected_ids"] for case in applicable_cases
	)
	stability_score = (
		Decimal(stable) / Decimal(len(applicable_cases))
		if applicable_cases else Decimal("0")
	)
	comment = (
		f"{stable} cas sur {len(applicable_cases)} conservent la sélection de référence ; "
		f"le score de stabilité est {stability_score:.2f}."
	)
	return {
		"base": base,
		"cases": cases,
		"stability_score": stability_score,
		"comment": comment,
	}
