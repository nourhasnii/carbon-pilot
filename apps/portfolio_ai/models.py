from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models

from apps.enterprise.models import Company
from apps.investments.models import ProjectStatus


class PortfolioRunObjective(models.TextChoices):
	MAX_TCO2E = "MAX_TCO2E", "Maximiser le tCO2e évité"
	MAX_ROI = "MAX_ROI", "Maximiser le ROI"


class PortfolioRunStatus(models.TextChoices):
	PENDING = "PENDING", "En attente"
	RUNNING = "RUNNING", "En cours"
	SUCCESS = "SUCCESS", "Réussi"
	FAILED = "FAILED", "Échoué"


class PortfolioRunQuerySet(models.QuerySet):
	def for_company(self, company):
		return self.filter(company=company)


class PortfolioRun(models.Model):
	company = models.ForeignKey(
		Company,
		on_delete=models.CASCADE,
		related_name="portfolio_runs",
	)
	created_by = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		null=True,
		blank=True,
		on_delete=models.SET_NULL,
		related_name="portfolio_runs_created",
	)
	budget = models.DecimalField(
		max_digits=14,
		decimal_places=2,
		validators=[MinValueValidator(Decimal("0.01"))],
	)
	objective = models.CharField(
		max_length=16,
		choices=PortfolioRunObjective.choices,
	)
	status = models.CharField(
		max_length=16,
		choices=PortfolioRunStatus.choices,
		default=PortfolioRunStatus.PENDING,
	)
	included_statuses = models.JSONField(default=list)
	selected_project_ids = models.JSONField(default=list)
	total_cost = models.DecimalField(
		max_digits=14,
		decimal_places=2,
		null=True,
		blank=True,
	)
	total_tco2e = models.DecimalField(
		max_digits=14,
		decimal_places=2,
		null=True,
		blank=True,
	)
	explanation = models.TextField(blank=True)
	metrics = models.JSONField(default=dict)
	duration_ms = models.PositiveIntegerField(null=True, blank=True)
	error_message = models.TextField(blank=True)
	created_at = models.DateTimeField(auto_now_add=True)

	objects = PortfolioRunQuerySet.as_manager()

	class Meta:
		ordering = ["-created_at"]

	def __str__(self):
		return f"Optimisation {self.get_objective_display()} — {self.get_status_display()}"

	def clean(self):
		errors = {}
		if not isinstance(self.included_statuses, list):
			errors["included_statuses"] = "Les statuts inclus doivent être une liste."
		elif any(status not in ProjectStatus.values for status in self.included_statuses):
			errors["included_statuses"] = "La liste contient un statut de projet invalide."
		if not isinstance(self.selected_project_ids, list):
			errors["selected_project_ids"] = "Les projets sélectionnés doivent être une liste."
		if not isinstance(self.metrics, dict):
			errors["metrics"] = "Les métriques doivent être un dictionnaire."
		if errors:
			raise ValidationError(errors)
