"""Mesure des performances des optimiseurs et du prédicteur."""

import logging
import time
from decimal import Decimal

from .optimizer import brute_force, solve_exact, solve_greedy
from .predictor import predictor_metrics


logger = logging.getLogger(__name__)


def metrics_for_optimizer(items, budget, objective):
	"""Compare les solveurs et mesure leurs temps sans journaliser de données métier."""
	start = time.perf_counter()
	exact = solve_exact(items, budget)
	exact_ms = (time.perf_counter() - start) * 1000
	start = time.perf_counter()
	greedy = solve_greedy(items, budget)
	greedy_ms = (time.perf_counter() - start) * 1000
	logger.info("Résolution portefeuille objective=%s exact_ms=%.2f greedy_ms=%.2f", objective, exact_ms, greedy_ms)
	gap = Decimal("0")
	if exact.total_value:
		gap = (exact.total_value - greedy.total_value) / exact.total_value * Decimal("100")
	validated = None
	brute_states = None
	if len(items) <= 15:
		brute = brute_force(items, budget)
		brute_states = brute.states_evaluated
		validated = brute.total_value == exact.total_value
	return {
		"greedy_gap_percent": max(Decimal("0"), gap),
		"dp_states": exact.states_evaluated,
		"brute_force_combinations": brute_states,
		"exact_ms": Decimal(str(round(exact_ms, 2))),
		"greedy_ms": Decimal(str(round(greedy_ms, 2))),
		"validated_against_brute_force": validated,
	}


def metrics_for_predictor(company):
	"""Retourne les métriques de test du prédicteur pour l'entreprise."""
	return predictor_metrics(company)
