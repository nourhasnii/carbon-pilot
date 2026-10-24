from decimal import Decimal, InvalidOperation

from django import forms

from apps.enterprise.models import Department

from .models import (
	DecarbonizationProject,
	ImpactAssessment,
	Investment,
	ProjectScenario,
)


DECIMAL_STEP = "0.01"
MULTIPLIER_MIN = Decimal("0.1")
MULTIPLIER_MAX = Decimal("3")


def _field_attrs(*, help_text, required=True, input_type=None, **attrs):
	"""Return consistent accessible HTML attributes for investment forms."""
	attributes = {
		"class": "field-input",
		"aria-describedby": "",
		"data-help-text": help_text,
		"data-error-id": "",
	}
	if required:
		attributes["required"] = "required"
	if input_type:
		attributes["type"] = input_type
	attributes.update({
		"inputmode": "decimal" if input_type == "number" else None,
	})
	attributes.update(attrs)
	return {key: value for key, value in attributes.items() if value is not None}


class InvestmentFormMixin:
	def _configure_accessibility(self):
		for name, field in self.fields.items():
			help_id = f"{self.prefix}-{name}-help" if self.prefix else f"{name}-help"
			error_id = f"{self.prefix}-{name}-error" if self.prefix else f"{name}-error"
			described_by = f"{help_id} {error_id}"
			field.error_messages["required"] = "Ce champ est obligatoire."
			field.help_text = field.help_text or "Saisissez une valeur valide."
			field.widget.attrs["aria-describedby"] = described_by
			field.widget.attrs["data-help-id"] = help_id
			field.widget.attrs["data-error-id"] = error_id

	def _strip(self, name):
		value = self.cleaned_data.get(name)
		if isinstance(value, str):
			value = value.strip()
			self.cleaned_data[name] = value
		return value

	def _decimal(self, name, *, minimum=Decimal("0"), strictly_positive=False):
		value = self.cleaned_data.get(name)
		if value in (None, ""):
			return value
		try:
			value = Decimal(value)
		except (InvalidOperation, TypeError, ValueError):
			raise forms.ValidationError("Saisissez un montant numérique valide.")
		if strictly_positive and value <= 0:
			raise forms.ValidationError("La valeur doit être strictement supérieure à zéro.")
		if not strictly_positive and value < minimum:
			raise forms.ValidationError("La valeur doit être supérieure ou égale à zéro.")
		self.cleaned_data[name] = value
		return value

	def _save_with_scope(self, instance, *, company=None, project=None, commit=True):
		if company is not None and hasattr(instance, "company_id"):
			instance.company = company
		if project is not None and hasattr(instance, "project_id"):
			instance.project = project
		if commit:
			instance.save()
		return instance


class DecarbonizationProjectForm(InvestmentFormMixin, forms.ModelForm):
	class Meta:
		model = DecarbonizationProject
		fields = (
			"department", "name", "project_type", "description", "status",
			"start_date", "end_date", "is_demo",
		)
		widgets = {
			"department": forms.Select(attrs={
				"data-help-text": "Sélectionnez un département de votre entreprise.",
			}),
			"name": forms.TextInput(attrs={
				"maxlength": "255",
				"pattern": r"\S(?:.*\S)?",
				"placeholder": "Ex. Installation de panneaux solaires",
			}),
			"description": forms.Textarea(attrs={
				"maxlength": "5000",
				"placeholder": "Décrivez le projet et ses objectifs.",
				"rows": 5,
			}),
			"start_date": forms.DateInput(attrs={"type": "date"}),
			"end_date": forms.DateInput(attrs={"type": "date"}),
			"is_demo": forms.CheckboxInput(),
		}

	def __init__(self, *args, company=None, user=None, **kwargs):
		super().__init__(*args, **kwargs)
		self.company = company
		self.user = user
		queryset = Department.objects.none()
		if company is not None:
			queryset = Department.objects.filter(company=company).order_by("name")
		if user is not None and getattr(user, "department", ""):
			if getattr(user, "role", None) == "DEPARTMENT_MANAGER":
				queryset = queryset.filter(name=user.department)
		self.fields["department"].queryset = queryset
		self.fields["department"].empty_label = "Tous les départements"
		self.fields["department"].error_messages["invalid_choice"] = (
			"Le département doit appartenir à votre entreprise."
		)
		self._configure_accessibility()

	def clean_department(self):
		department = self.cleaned_data.get("department")
		if department is not None and self.company is not None and department.company_id != self.company.pk:
			raise forms.ValidationError("Le département doit appartenir à votre entreprise.")
		return department

	def clean_name(self):
		name = self._strip("name")
		if not name:
			raise forms.ValidationError("Le nom du projet est obligatoire.")
		queryset = DecarbonizationProject.objects.filter(company=self.company, name__iexact=name)
		if self.instance.pk:
			queryset = queryset.exclude(pk=self.instance.pk)
		if queryset.exists():
			raise forms.ValidationError("Un projet portant ce nom existe déjà dans votre entreprise.")
		return name

	def clean_description(self):
		return self._strip("description") or ""

	def clean_project_type(self):
		return self.cleaned_data.get("project_type")

	def clean_status(self):
		return self.cleaned_data.get("status")

	def clean_start_date(self):
		value = self.cleaned_data.get("start_date")
		if value is None:
			raise forms.ValidationError("La date de début est obligatoire.")
		return value

	def clean_end_date(self):
		value = self.cleaned_data.get("end_date")
		if value is None:
			raise forms.ValidationError("La date de fin est obligatoire.")
		return value

	def clean_is_demo(self):
		return bool(self.cleaned_data.get("is_demo"))

	def clean(self):
		cleaned = super().clean()
		start_date = cleaned.get("start_date")
		end_date = cleaned.get("end_date")
		if start_date and end_date and end_date < start_date:
			self.add_error("end_date", "La date de fin doit être postérieure ou égale à la date de début.")
		return cleaned

	def save(self, commit=True):
		return self._save_with_scope(super().save(commit=False), company=self.company, commit=commit)


class InvestmentForm(InvestmentFormMixin, forms.ModelForm):
	class Meta:
		model = Investment
		fields = ("planned_budget", "actual_cost", "committed_spend", "currency", "funding_source", "date")
		widgets = {
			"planned_budget": forms.NumberInput(attrs={
				"type": "number", "min": "0.01", "step": DECIMAL_STEP,
				"placeholder": "0,00",
			}),
			"actual_cost": forms.NumberInput(attrs={
				"type": "number", "min": "0", "step": DECIMAL_STEP,
				"placeholder": "0,00",
			}),
			"committed_spend": forms.NumberInput(attrs={
				"type": "number", "min": "0", "step": DECIMAL_STEP,
				"placeholder": "0,00",
			}),
			"currency": forms.TextInput(attrs={"maxlength": "3", "pattern": "[A-Za-z]{3}", "placeholder": "TND"}),
			"funding_source": forms.TextInput(attrs={"maxlength": "255", "placeholder": "Ex. Fonds propres"}),
			"date": forms.DateInput(attrs={"type": "date"}),
		}

	def __init__(self, *args, company=None, project=None, **kwargs):
		super().__init__(*args, **kwargs)
		self.company = company
		self.project = project
		self._configure_accessibility()

	def clean_planned_budget(self):
		return self._decimal("planned_budget", strictly_positive=True)

	def clean_actual_cost(self):
		return self._decimal("actual_cost")

	def clean_committed_spend(self):
		return self._decimal("committed_spend")

	def clean_currency(self):
		value = self._strip("currency")
		if not value or len(value) != 3 or not value.isalpha():
			raise forms.ValidationError("La devise doit comporter exactement trois lettres.")
		return value.upper()

	def clean_funding_source(self):
		value = self._strip("funding_source")
		if not value:
			raise forms.ValidationError("La source de financement est obligatoire.")
		return value

	def clean_date(self):
		value = self.cleaned_data.get("date")
		if value is None:
			raise forms.ValidationError("La date de l'investissement est obligatoire.")
		return value

	def clean(self):
		cleaned = super().clean()
		if self.company is not None and self.project is not None and self.project.company_id != self.company.pk:
			raise forms.ValidationError("Le projet doit appartenir à votre entreprise.")
		actual_cost = cleaned.get("actual_cost")
		committed_spend = cleaned.get("committed_spend")
		if actual_cost is not None and committed_spend is not None and committed_spend > actual_cost:
			self.add_error("committed_spend", "Les dépenses engagées ne peuvent pas dépasser le coût réel.")
		return cleaned

	def save(self, commit=True):
		return self._save_with_scope(super().save(commit=False), project=self.project, commit=commit)


class ImpactAssessmentForm(InvestmentFormMixin, forms.ModelForm):
	class Meta:
		model = ImpactAssessment
		fields = (
			"estimated_tco2e_per_year", "estimated_kwh_saved_per_year",
			"annual_savings", "lifetime_years", "assumptions",
			"actual_tco2e_per_year",
		)
		widgets = {
			"estimated_tco2e_per_year": forms.NumberInput(attrs={"type": "number", "min": "0.01", "step": DECIMAL_STEP}),
			"estimated_kwh_saved_per_year": forms.NumberInput(attrs={"type": "number", "min": "0", "step": DECIMAL_STEP}),
			"annual_savings": forms.NumberInput(attrs={"type": "number", "min": "0", "step": DECIMAL_STEP}),
			"lifetime_years": forms.NumberInput(attrs={"type": "number", "min": "1", "max": "50", "step": "1"}),
			"assumptions": forms.Textarea(attrs={"maxlength": "5000", "placeholder": "Décrivez les hypothèses retenues.", "rows": 5}),
			"actual_tco2e_per_year": forms.NumberInput(attrs={"type": "number", "min": "0", "step": DECIMAL_STEP}),
		}

	def __init__(self, *args, company=None, project=None, **kwargs):
		super().__init__(*args, **kwargs)
		self.company = company
		self.project = project
		self._configure_accessibility()

	def clean_estimated_tco2e_per_year(self):
		return self._decimal("estimated_tco2e_per_year", strictly_positive=True)

	def clean_estimated_kwh_saved_per_year(self):
		return self._decimal("estimated_kwh_saved_per_year")

	def clean_annual_savings(self):
		return self._decimal("annual_savings")

	def clean_lifetime_years(self):
		value = self.cleaned_data.get("lifetime_years")
		if value is None or not 1 <= value <= 50:
			raise forms.ValidationError("La durée de vie doit être comprise entre 1 et 50 ans.")
		return value

	def clean_assumptions(self):
		value = self._strip("assumptions")
		if not value:
			raise forms.ValidationError("Les hypothèses sont obligatoires.")
		return value

	def clean_actual_tco2e_per_year(self):
		return self._decimal("actual_tco2e_per_year")

	def clean(self):
		cleaned = super().clean()
		if self.company is not None and self.project is not None and self.project.company_id != self.company.pk:
			raise forms.ValidationError("Le projet doit appartenir à votre entreprise.")
		return cleaned

	def save(self, commit=True):
		return self._save_with_scope(super().save(commit=False), project=self.project, commit=commit)


class ProjectScenarioForm(InvestmentFormMixin, forms.ModelForm):
	class Meta:
		model = ProjectScenario
		fields = ("name", "investment", "impact", "cost_multiplier", "reduction_multiplier", "energy_price")
		widgets = {
			"name": forms.TextInput(attrs={"maxlength": "255", "pattern": r"\S(?:.*\S)?", "placeholder": "Ex. Hausse du prix de l'énergie"}),
			"cost_multiplier": forms.NumberInput(attrs={"type": "number", "min": "0.1", "max": "3", "step": DECIMAL_STEP}),
			"reduction_multiplier": forms.NumberInput(attrs={"type": "number", "min": "0.1", "max": "3", "step": DECIMAL_STEP}),
			"energy_price": forms.NumberInput(attrs={"type": "number", "min": "0", "step": DECIMAL_STEP, "placeholder": "0,00"}),
		}

	def __init__(self, *args, company=None, project=None, **kwargs):
		super().__init__(*args, **kwargs)
		self.company = company
		self.project = project
		self.fields["investment"].error_messages["invalid_choice"] = (
			"L'investissement doit appartenir au projet sélectionné."
		)
		self.fields["impact"].error_messages["invalid_choice"] = (
			"L'évaluation d'impact doit appartenir au projet sélectionné."
		)
		self.fields["investment"].queryset = (
			Investment.objects.filter(project=project).order_by("-date", "-id")
			if project is not None else Investment.objects.none()
		)
		self.fields["impact"].queryset = (
			ImpactAssessment.objects.filter(project=project).order_by("-created_at", "-id")
			if project is not None else ImpactAssessment.objects.none()
		)
		self._configure_accessibility()

	def clean_name(self):
		name = self._strip("name")
		if not name:
			raise forms.ValidationError("Le nom du scénario est obligatoire.")
		queryset = ProjectScenario.objects.filter(project=self.project, name__iexact=name)
		if self.instance.pk:
			queryset = queryset.exclude(pk=self.instance.pk)
		if queryset.exists():
			raise forms.ValidationError("Un scénario portant ce nom existe déjà pour ce projet.")
		return name

	def clean_investment(self):
		investment = self.cleaned_data.get("investment")
		if investment is not None and self.project is not None and investment.project_id != self.project.pk:
			raise forms.ValidationError("L'investissement doit appartenir au projet sélectionné.")
		return investment

	def clean_impact(self):
		impact = self.cleaned_data.get("impact")
		if impact is not None and self.project is not None and impact.project_id != self.project.pk:
			raise forms.ValidationError("L'évaluation d'impact doit appartenir au projet sélectionné.")
		return impact

	def clean_cost_multiplier(self):
		return self._multiplier("cost_multiplier")

	def clean_reduction_multiplier(self):
		return self._multiplier("reduction_multiplier")

	def _multiplier(self, name):
		value = self._decimal(name, minimum=MULTIPLIER_MIN)
		if value is not None and not MULTIPLIER_MIN <= value <= MULTIPLIER_MAX:
			raise forms.ValidationError("Le multiplicateur doit être compris entre 0,1 et 3.")
		return value

	def clean_energy_price(self):
		return self._decimal("energy_price")

	def clean(self):
		cleaned = super().clean()
		if self.company is not None and self.project is not None and self.project.company_id != self.company.pk:
			raise forms.ValidationError("Le projet doit appartenir à votre entreprise.")
		investment = cleaned.get("investment")
		impact = cleaned.get("impact")
		if self.project is not None:
			if investment is not None and investment.project_id != self.project.pk:
				self.add_error("investment", "L'investissement doit appartenir au projet sélectionné.")
			if impact is not None and impact.project_id != self.project.pk:
				self.add_error("impact", "L'évaluation d'impact doit appartenir au projet sélectionné.")
		return cleaned

	def save(self, commit=True):
		return self._save_with_scope(super().save(commit=False), project=self.project, commit=commit)
