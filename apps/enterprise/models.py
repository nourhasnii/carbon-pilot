from django.conf import settings
from django.db import models
from django.core.validators import MaxValueValidator, RegexValidator


class Company(models.Model):
	class Sector(models.TextChoices):
		AGRICULTURE = "AGRICULTURE", "Agriculture"
		ENERGY = "ENERGY", "Energy"
		FINANCE = "FINANCE", "Finance"
		HEALTHCARE = "HEALTHCARE", "Healthcare"
		MANUFACTURING = "MANUFACTURING", "Manufacturing"
		RETAIL = "RETAIL", "Retail"
		TECHNOLOGY = "TECHNOLOGY", "Technology"
		TRANSPORT = "TRANSPORT", "Transport"
		OTHER = "OTHER", "Other"

	class Size(models.TextChoices):
		MICRO = "MICRO", "1-9 employees"
		SMALL = "SMALL", "10-49 employees"
		MEDIUM = "MEDIUM", "50-249 employees"
		LARGE = "LARGE", "250 or more employees"

	owner = models.OneToOneField(
		settings.AUTH_USER_MODEL,
		on_delete=models.CASCADE,
		related_name="company_profile",
	)
	name = models.CharField(max_length=255)
	is_active = models.BooleanField(default=True)
	legal_name = models.CharField(max_length=255, blank=True)
	siret = models.CharField(
		max_length=14,
		unique=True,
		blank=True,
		null=True,
		validators=[RegexValidator(r"^\d{14}$", "Enter a 14-digit SIRET number.")],
	)
	sector = models.CharField(max_length=20, choices=Sector.choices, blank=True)
	size = models.CharField(max_length=10, choices=Size.choices, blank=True)
	country = models.CharField(max_length=100, blank=True)
	city = models.CharField(max_length=100, blank=True)
	address = models.CharField(max_length=255, blank=True)
	website = models.URLField(max_length=255, blank=True)
	contact_email = models.EmailField(blank=True)
	reports_esg = models.BooleanField(default=False)
	sustainability_goal = models.TextField(blank=True)
	target_year = models.PositiveSmallIntegerField(
		blank=True,
		null=True,
		validators=[MaxValueValidator(2100)],
	)
	created_at = models.DateTimeField(auto_now_add=True)
	updated_at = models.DateTimeField(auto_now=True)

	class Meta:
		ordering = ("name",)

	def __str__(self):
		return self.name


class Department(models.Model):
	company = models.ForeignKey(
		Company,
		on_delete=models.CASCADE,
		related_name="departments",
	)
	name = models.CharField(max_length=255)
	code = models.CharField(max_length=50, blank=True)
	description = models.TextField(blank=True)
	is_active = models.BooleanField(
		default=True,
		help_text=(
			"Inactive departments stay linked to their historical records but are "
			"no longer offered when assigning new work."
		),
	)

	class Meta:
		ordering = ("name",)
		constraints = [
			models.UniqueConstraint(
				fields=("company", "name"),
				name="uniq_department_name_per_company",
			),
		]

	def __str__(self):
		return self.name
