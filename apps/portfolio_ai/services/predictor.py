"""Prédiction locale de l'impact réel des projets terminés."""

from decimal import Decimal

import numpy as np
from sklearn.ensemble import RandomForestRegressor
from sklearn.linear_model import LinearRegression
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
from sklearn.model_selection import KFold, cross_val_score, train_test_split

from apps.investments.models import ProjectStatus

from .optimizer import InsufficientData


NUMERIC_FEATURES = (
	"planned_budget",
	"estimated_tco2e_per_year",
	"estimated_kwh_saved_per_year",
	"lifetime_years",
	"annual_savings",
)


def _rows(company):
	projects = (
		company.decarbonization_projects
		.filter(
			status=ProjectStatus.COMPLETED,
			is_demo=False,
			impacts__actual_tco2e_per_year__isnull=False,
		)
		.prefetch_related("investments", "impacts")
		.distinct()
	)
	rows = []
	for project in projects:
		impact = max(project.impacts.all(), key=lambda item: (item.created_at, item.pk))
		investment = project.investments.order_by("date", "pk").first()
		if investment is None:
			continue
		rows.append({
			"project_type": project.project_type,
			"planned_budget": investment.planned_budget,
			"estimated_tco2e_per_year": impact.estimated_tco2e_per_year,
			"estimated_kwh_saved_per_year": impact.estimated_kwh_saved_per_year,
			"lifetime_years": Decimal(str(impact.lifetime_years)),
			"annual_savings": impact.annual_savings,
			"target": impact.actual_tco2e_per_year,
		})
	if len(rows) < 10:
		raise InsufficientData(
			"Données insuffisantes : il faut au moins 10 projets terminés avec valeurs réelles"
		)
	return rows


def _models():
	return {
		"LinearRegression": LinearRegression(),
		"RandomForestRegressor": RandomForestRegressor(
			random_state=42,
			n_estimators=100,
		),
	}


def _split_features(rows):
	types = sorted({row["project_type"] for row in rows})
	features = [
		[
			Decimal("1") if row["project_type"] == project_type else Decimal("0")
			for project_type in types
		] + [row[key] for key in NUMERIC_FEATURES]
		for row in rows
	]
	target = np.array([row["target"] for row in rows], dtype=object)
	return np.array(features, dtype=object), target, types


def _validation(rows):
	features, target, _ = _split_features(rows)
	n_splits = min(5, len(rows))
	cv = KFold(n_splits=n_splits, shuffle=True, random_state=42)
	metrics = {}
	for name, model in _models().items():
		scores = cross_val_score(
			model,
			features,
			target,
			cv=cv,
			scoring="neg_root_mean_squared_error",
		)
		metrics[name] = {"rmse": Decimal(str(round(-scores.mean(), 2)))}
	return metrics


def predict_impact(company, project_features):
	"""Entraîne deux modèles locaux et retourne la prédiction du meilleur."""
	rows = _rows(company)
	metrics = _validation(rows)
	model_name = min(metrics, key=lambda name: metrics[name]["rmse"])
	features, target, types = _split_features(rows)
	model = _models()[model_name]
	model.fit(features, target)
	project_vector = [
		Decimal("1") if project_features["project_type"] == project_type else Decimal("0")
		for project_type in types
	] + [project_features[key] for key in NUMERIC_FEATURES]
	prediction = model.predict(np.array([project_vector], dtype=object))[0]
	return {
		"prediction": Decimal(str(round(max(0, prediction), 2))),
		"model": model_name,
		"metrics": metrics,
	}


def predictor_metrics(company):
	"""Calcule MAE, RMSE et R² sur un jeu de test reproductible."""
	rows = _rows(company)
	features, target, _ = _split_features(rows)
	train_x, test_x, train_y, test_y = train_test_split(
		features, target, test_size=0.2, random_state=42,
	)
	validation = _validation(rows)
	model_name = min(validation, key=lambda name: validation[name]["rmse"])
	model = _models()[model_name]
	model.fit(train_x, train_y)
	predicted = model.predict(test_x)
	return {
		"model": model_name,
		"mae": Decimal(str(round(mean_absolute_error(test_y, predicted), 2))),
		"rmse": Decimal(str(round(np.sqrt(mean_squared_error(test_y, predicted)), 2))),
		"r2": Decimal(str(round(r2_score(test_y, predicted), 2))),
	}
