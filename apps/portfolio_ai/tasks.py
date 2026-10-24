import logging
import time
from decimal import Decimal

try:
	from celery import shared_task
except ImportError:  # pragma: no cover
	def shared_task(function):
		class LocalTask:
			def __call__(self, *args, **kwargs):
				return function(*args, **kwargs)

			def delay(self, *args, **kwargs):
				return function(*args, **kwargs)
		return LocalTask()

from .models import PortfolioRun, PortfolioRunStatus
from .services.evaluation import metrics_for_optimizer
from .services.optimizer import InsufficientData, build_items, explain, solve_exact
from .services.predictor import predict_impact
from .services.sensitivity import run_sensitivity


logger = logging.getLogger(__name__)
GENERIC_ERROR = "Le calcul a échoué. Réessayez ou contactez un administrateur."


def _json_value(value):
	if isinstance(value, Decimal):
		return str(value)
	if isinstance(value, dict):
		return {key: _json_value(item) for key, item in value.items()}
	if isinstance(value, (list, tuple)):
		return [_json_value(item) for item in value]
	return value


def _prediction_features(project):
	impact = project.impacts.order_by("-created_at", "-id").first()
	investment = project.investments.order_by("-date", "-id").first()
	if not impact or not investment:
		return None
	return {
		"project_type": project.project_type,
		"planned_budget": investment.planned_budget,
		"estimated_tco2e_per_year": impact.estimated_tco2e_per_year,
		"estimated_kwh_saved_per_year": impact.estimated_kwh_saved_per_year,
		"lifetime_years": Decimal(str(impact.lifetime_years)),
		"annual_savings": impact.annual_savings,
	}


def _sensitivity_for_display(sensitivity):
	cases = sensitivity["cases"]
	max_value = max((abs(Decimal(str(case["total_value"]))) for case in cases), default=Decimal("0"))
	for case in cases:
		value = abs(Decimal(str(case["total_value"])))
		case["chart_width"] = (
			int((value / max_value * Decimal("240")).quantize(Decimal("1")))
			if max_value
			else 0
		)
	return sensitivity


@shared_task
def run_portfolio_task(run_id):
	start = time.perf_counter()
	run = PortfolioRun.objects.select_related("company").get(pk=run_id)
	run.status = PortfolioRunStatus.RUNNING
	run.error_message = ""
	run.save(update_fields=("status", "error_message"))
	try:
		items = build_items(run.company, run.included_statuses, run.objective)
		result = solve_exact(items, run.budget)
		metrics = metrics_for_optimizer(items, run.budget, run.objective)
		sensitivity = _sensitivity_for_display(
			run_sensitivity(items, run.budget, run.objective)
		)
		prediction = {"status": "insufficient_data", "message": ""}
		selected_project = run.company.decarbonization_projects.filter(
			pk__in=result.selected_ids,
		).prefetch_related("investments", "impacts").first()
		features = _prediction_features(selected_project) if selected_project else None
		if features:
			try:
				prediction = predict_impact(run.company, features)
			except InsufficientData as exc:
				prediction["message"] = str(exc)
		else:
			prediction["message"] = (
				"Données insuffisantes : il faut au moins 10 projets terminés avec valeurs réelles"
			)
		metrics["sensitivity"] = {
			"stability_score": sensitivity["stability_score"],
			"comment": sensitivity["comment"],
			"cases": sensitivity["cases"],
		}
		metrics["prediction"] = prediction
		run.status = PortfolioRunStatus.SUCCESS
		run.selected_project_ids = result.selected_ids
		run.total_cost = result.total_cost
		run.total_tco2e = result.total_tco2e
		run.explanation = explain(items, result, run.budget)
		run.metrics = _json_value(metrics)
		run.duration_ms = int((time.perf_counter() - start) * 1000)
		run.save()
	except InsufficientData as exc:
		run.status = PortfolioRunStatus.FAILED
		run.error_message = str(exc)
		run.metrics = {"insufficient_data": True}
		run.duration_ms = int((time.perf_counter() - start) * 1000)
		run.save(update_fields=("status", "error_message", "metrics", "duration_ms"))
	except Exception:
		logger.exception("Échec du calcul du portefeuille run_id=%s", run_id)
		run.status = PortfolioRunStatus.FAILED
		run.error_message = GENERIC_ERROR
		run.duration_ms = int((time.perf_counter() - start) * 1000)
		run.save(update_fields=("status", "error_message", "duration_ms"))
	return run.status
