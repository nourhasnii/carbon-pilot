from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, MaxValueValidator
from django.db import models

from apps.enterprise.models import Company, Department


class ProjectType(models.TextChoices):
	SOLAR = "SOLAR", "Solaire"
	HVAC = "HVAC", "CVC"
	FLEET_ELECTRIFICATION = "FLEET_ELECTRIFICATION", "Électrification de la flotte"
	INSULATION = "INSULATION", "Isolation"
	LIGHTING = "LIGHTING", "Éclairage"
	OTHER = "OTHER", "Autre"


class ProjectStatus(models.TextChoices):
	PROPOSED = "PROPOSED", "Proposé"
	APPROVED = "APPROVED", "Approuvé"
	IN_PROGRESS = "IN_PROGRESS", "En cours"
	COMPLETED = "COMPLETED", "Terminé"
	CANCELLED = "CANCELLED", "Annulé"


class DecarbonizationProjectQuerySet(models.QuerySet):
	def for_company(self, company):
		return self.filter(company=company)


class InvestmentQuerySet(models.QuerySet):
	def for_company(self, company):
		return self.filter(project__company=company)


class ImpactAssessmentQuerySet(models.QuerySet):
	def for_company(self, company):
		return self.filter(project__company=company)


class ProjectScenarioQuerySet(models.QuerySet):
	def for_company(self, company):
		return self.filter(project__company=company)


class DecarbonizationProject(models.Model):
	company = models.ForeignKey(
		Company,
		on_delete=models.CASCADE,
		related_name="decarbonization_projects",
	)
	department = models.ForeignKey(
		Department,
		null=True,
		blank=True,
		on_delete=models.SET_NULL,
		related_name="decarbonization_projects",
	)
	name = models.CharField(max_length=255)
	project_type = models.CharField(max_length=32, choices=ProjectType.choices)
	description = models.TextField(blank=True)
	status = models.CharField(
		max_length=16,
		choices=ProjectStatus.choices,
		default=ProjectStatus.PROPOSED,
	)
	start_date = models.DateField()
	end_date = models.DateField()
	is_demo = models.BooleanField(default=False)
	created_by = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		null=True,
		blank=True,
		on_delete=models.SET_NULL,
		related_name="decarbonization_projects_created",
	)
	created_at = models.DateTimeField(auto_now_add=True)
	updated_at = models.DateTimeField(auto_now=True)

	objects = DecarbonizationProjectQuerySet.as_manager()

	class Meta:
		ordering = ("-start_date", "name")
		constraints = [
			models.UniqueConstraint(
				fields=("company", "name"),
				name="uniq_decarbonization_project_name_per_company",
			),
		]

	def __str__(self):
		return f"{self.name} ({self.get_status_display()})"

	def clean(self):
		errors = {}
		if self.start_date and self.end_date and self.end_date < self.start_date:
			errors["end_date"] = "La date de fin doit être postérieure ou égale à la date de début."
		if (
			self.department_id
			and self.company_id
			and self.department.company_id != self.company_id
		):
			errors["department"] = "Le département doit appartenir à l'entreprise du projet."
		if errors:
			raise ValidationError(errors)


class Investment(models.Model):
	project = models.ForeignKey(
		DecarbonizationProject,
		on_delete=models.CASCADE,
		related_name="investments",
	)
	planned_budget = models.DecimalField(
		max_digits=14,
		decimal_places=2,
		validators=[MinValueValidator(Decimal("0.01"))],
	)
	actual_cost = models.DecimalField(
		max_digits=14,
		decimal_places=2,
		null=True,
		blank=True,
		validators=[MinValueValidator(Decimal("0"))],
	)
	committed_spend = models.DecimalField(
		max_digits=14,
		decimal_places=2,
		default=Decimal("0"),
		validators=[MinValueValidator(Decimal("0"))],
	)
	currency = models.CharField(max_length=3, default="TND")
	funding_source = models.CharField(max_length=255)
	date = models.DateField()

	class Meta:
		ordering = ("-date", "-id")

	objects = InvestmentQuerySet.as_manager()

	def __str__(self):
		return f"Investissement {self.planned_budget} {self.currency} — {self.project.name}"

	def clean(self):
		if (
			self.actual_cost is not None
			and self.committed_spend is not None
			and self.committed_spend > self.actual_cost
		):
			raise ValidationError({
				"committed_spend": "Les dépenses engagées ne peuvent pas dépasser le coût réel."
			})


class ImpactAssessment(models.Model):
	project = models.ForeignKey(
		DecarbonizationProject,
		on_delete=models.CASCADE,
		related_name="impacts",
	)
	estimated_tco2e_per_year = models.DecimalField(
		max_digits=14,
		decimal_places=2,
		validators=[MinValueValidator(Decimal("0.01"))],
	)
	estimated_kwh_saved_per_year = models.DecimalField(
		max_digits=14,
		decimal_places=2,
		validators=[MinValueValidator(Decimal("0"))],
	)
	annual_savings = models.DecimalField(
		max_digits=14,
		decimal_places=2,
		validators=[MinValueValidator(Decimal("0"))],
	)
	lifetime_years = models.PositiveSmallIntegerField(
		validators=[MinValueValidator(1), MaxValueValidator(50)],
	)
	assumptions = models.TextField()
	actual_tco2e_per_year = models.DecimalField(
		max_digits=14,
		decimal_places=2,
		null=True,
		blank=True,
		validators=[MinValueValidator(Decimal("0"))],
	)
	created_at = models.DateTimeField(auto_now_add=True)

	class Meta:
		ordering = ("-created_at", "-id")

	objects = ImpactAssessmentQuerySet.as_manager()

	def __str__(self):
		return f"Évaluation d'impact — {self.project.name}"

	def clean(self):
		if not self.assumptions or not self.assumptions.strip():
			raise ValidationError({"assumptions": "Les hypothèses sont obligatoires."})


class ProjectScenario(models.Model):
	project = models.ForeignKey(
		DecarbonizationProject,
		on_delete=models.CASCADE,
		related_name="scenarios",
	)
	name = models.CharField(max_length=255)
	investment = models.ForeignKey(
		Investment,
		on_delete=models.CASCADE,
		related_name="scenarios",
	)
	impact = models.ForeignKey(
		ImpactAssessment,
		on_delete=models.CASCADE,
		related_name="scenarios",
	)
	cost_multiplier = models.DecimalField(
		max_digits=4,
		decimal_places=2,
		default=Decimal("1"),
		validators=[MinValueValidator(Decimal("0.1")), MaxValueValidator(Decimal("3"))],
	)
	reduction_multiplier = models.DecimalField(
		max_digits=4,
		decimal_places=2,
		default=Decimal("1"),
		validators=[MinValueValidator(Decimal("0.1")), MaxValueValidator(Decimal("3"))],
	)
	energy_price = models.DecimalField(
		max_digits=14,
		decimal_places=2,
		validators=[MinValueValidator(Decimal("0"))],
	)

	class Meta:
		ordering = ("project", "name")
		constraints = [
			models.UniqueConstraint(
				fields=("project", "name"),
				name="uniq_project_scenario_name",
			),
		]

	objects = ProjectScenarioQuerySet.as_manager()

	def __str__(self):
		return f"Scénario {self.name} — {self.project.name}"

	def clean(self):
		errors = {}
		if self.investment_id and self.project_id:
			if self.investment.project_id != self.project_id:
				errors["investment"] = "L'investissement doit appartenir au même projet."
		if self.impact_id and self.project_id:
			if self.impact.project_id != self.project_id:
				errors["impact"] = "L'évaluation d'impact doit appartenir au même projet."
		if errors:
			raise ValidationError(errors)
