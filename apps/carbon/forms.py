from decimal import Decimal

from django import forms

from apps.accounts.models import User
from apps.enterprise.models import Department

from .models import (
	ActivityCategory,
	ActivityStatus,
	AnomalySeverity,
	AnomalyStatus,
	EmissionFactor,
	Scope,
)
from .services.recommendation import recommendation_candidates


class EmissionFactorForm(forms.ModelForm):
	class Meta:
		model = EmissionFactor
		fields = (
			"name", "category", "scope", "value", "unit", "region", "year",
			"source", "methodology", "verification_status",
		)
		widgets = {
			"name": forms.TextInput(attrs={"class": "field-input"}),
			"category": forms.Select(attrs={"class": "field-input"}),
			"scope": forms.Select(attrs={"class": "field-input"}),
			"value": forms.NumberInput(attrs={"class": "field-input", "step": "0.000001", "min": "0.000001"}),
			"unit": forms.TextInput(attrs={"class": "field-input"}),
			"region": forms.TextInput(attrs={"class": "field-input"}),
			"year": forms.NumberInput(attrs={"class": "field-input", "min": "1900", "max": "2100", "step": "1"}),
			"source": forms.TextInput(attrs={"class": "field-input"}),
			"methodology": forms.Textarea(attrs={"class": "field-input", "rows": 4}),
			"verification_status": forms.Select(attrs={"class": "field-input"}),
		}

	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)
		self.fields["value"].min_value = Decimal("0.000001")
		self.fields["year"].min_value = 1900
		self.fields["year"].max_value = 2100
		self.fields["source"].required = True
		self.fields["region"].required = True

	def clean(self):
		cleaned = super().clean()
		for field in ("name", "unit", "region", "source"):
			value = cleaned.get(field)
			if value is not None and not value.strip():
				self.add_error(field, "This field cannot be empty.")
		return cleaned


class EmissionFactorOverrideForm(forms.Form):
	"""Company-specific values for a shared factor.

	Name, category and scope are intentionally excluded: they identify the factor
	and drive activity matching, so a tenant only customises the values it uses.
	"""

	value = forms.DecimalField(
		max_digits=18, decimal_places=6, min_value=Decimal("0.000001"),
		widget=forms.NumberInput(attrs={"class": "field-input", "step": "0.000001", "min": "0.000001"}),
	)
	unit = forms.CharField(
		max_length=32, widget=forms.TextInput(attrs={"class": "field-input", "list": "unit-options"})
	)
	region = forms.CharField(max_length=120, widget=forms.TextInput(attrs={"class": "field-input"}))
	year = forms.IntegerField(
		min_value=1900, max_value=2100,
		widget=forms.NumberInput(attrs={"class": "field-input", "min": "1900", "max": "2100", "step": "1"}),
	)
	source = forms.CharField(max_length=255, widget=forms.TextInput(attrs={"class": "field-input"}))
	methodology = forms.CharField(
		required=False,
		widget=forms.Textarea(attrs={"class": "field-input", "rows": 4}),
	)

	def clean(self):
		cleaned = super().clean()
		for field in ("unit", "region", "source"):
			value = cleaned.get(field)
			if value is not None and not value.strip():
				self.add_error(field, "This field cannot be empty.")
		return cleaned


class ActivityCategoryForm(forms.Form):
	"""Step 1 — pick the activity category/source."""

	category = forms.ChoiceField(
		choices=ActivityCategory.choices,
		widget=forms.RadioSelect(attrs={"class": "choice-input"}),
	)
	name = forms.CharField(
		max_length=255,
		required=False,
		widget=forms.TextInput(attrs={"class": "field-input", "placeholder": "e.g. Head office electricity — Q1"}),
	)


class ActivityDataForm(forms.Form):
	"""Step 2 — quantity, unit, reporting period and department."""

	quantity = forms.DecimalField(
		max_digits=18,
		decimal_places=4,
		min_value=0,
		error_messages={
			"required": "Enter the quantity you measured.",
			"invalid": "Enter a valid number for the quantity.",
			"min_value": "The quantity cannot be negative.",
		},
		widget=forms.NumberInput(attrs={"class": "field-input", "step": "0.0001", "min": "0"}),
	)
	unit = forms.CharField(
		max_length=32,
		error_messages={"required": "Pick the unit the quantity is expressed in."},
		widget=forms.TextInput(attrs={"class": "field-input", "list": "unit-options"}),
	)
	period_start = forms.DateField(
		error_messages={
			"required": "Enter the reporting period start date.",
			"invalid": "Enter a valid date (for example 2026-01-31).",
		},
		widget=forms.DateInput(attrs={"class": "field-input", "type": "date"}),
	)
	period_end = forms.DateField(
		required=False,
		error_messages={"invalid": "Enter a valid date (for example 2026-01-31)."},
		widget=forms.DateInput(attrs={"class": "field-input", "type": "date"}),
	)
	department = forms.ModelChoiceField(
		queryset=Department.objects.none(),
		required=False,
		error_messages={
			"invalid_choice": "The department must belong to your company."
		},
		widget=forms.Select(attrs={"class": "field-input"}),
	)

	def __init__(self, *args, company=None, user=None, **kwargs):
		super().__init__(*args, **kwargs)
		self.company = company
		queryset = Department.objects.none()
		if company is not None:
			queryset = Department.objects.filter(company=company).order_by("name")
		# A Department Manager can only file activity for the department they own.
		if user is not None and user.role == User.Role.DEPARTMENT_MANAGER and user.department:
			queryset = queryset.filter(name=user.department)
		self.fields["department"].queryset = queryset
		self.fields["department"].empty_label = "No department"

	def clean(self):
		cleaned_data = super().clean()
		start = cleaned_data.get("period_start")
		end = cleaned_data.get("period_end")
		if start and end and end < start:
			self.add_error("period_end", "End date must be on or after start date.")
		department = cleaned_data.get("department")
		if department is not None and self.company is not None:
			if department.company_id != self.company.pk:
				self.add_error("department", "The department must belong to your company.")
		return cleaned_data


class ActivityFactorForm(forms.Form):
	"""Step 3 — confirm the recommended factor or pick another one."""

	emission_factor = forms.ChoiceField(widget=forms.RadioSelect(attrs={"class": "choice-input"}))

	def __init__(self, *args, candidates=None, **kwargs):
		super().__init__(*args, **kwargs)
		self.candidates = list(candidates or [])
		self.fields["emission_factor"].choices = [
			(str(factor.pk), factor.name) for factor in self.candidates
		]

	def clean_emission_factor(self):
		value = self.cleaned_data["emission_factor"]
		return int(value)

	def factor(self):
		selected_id = self.cleaned_data.get("emission_factor")
		for factor in self.candidates:
			if factor.pk == selected_id:
				return factor
		return None


class ActivitySubmitForm(forms.Form):
	"""Step 4 — evidence, source and the draft/submit decision."""

	data_source = forms.CharField(
		max_length=255,
		required=False,
		widget=forms.TextInput(
			attrs={"class": "field-input", "placeholder": "e.g. January electricity invoice"}
		),
	)
	evidence = forms.FileField(
		required=False,
		widget=forms.ClearableFileInput(attrs={"class": "field-input"}),
	)


class ActivityReviewForm(forms.Form):
	"""Reviewer decision comment."""

	comment = forms.CharField(
		required=False,
		widget=forms.Textarea(
			attrs={"class": "field-input", "rows": 3, "placeholder": "Add a review comment…"}
		),
	)


class ActivityFilterForm(forms.Form):
	"""Shared filter bar for the activity list and footprint pages."""

	q = forms.CharField(
		required=False,
		label="Search",
		widget=forms.TextInput(
			attrs={"class": "field-input", "placeholder": "Search activity name…"}
		),
	)
	year = forms.ChoiceField(
		required=False, label="Year", widget=forms.Select(attrs={"class": "field-input"})
	)
	scope = forms.ChoiceField(
		required=False,
		label="Scope",
		choices=[("", "All scopes"), *Scope.choices],
		widget=forms.Select(attrs={"class": "field-input"}),
	)
	category = forms.ChoiceField(
		required=False,
		label="Category",
		choices=[("", "All categories"), *ActivityCategory.choices],
		widget=forms.Select(attrs={"class": "field-input"}),
	)
	department = forms.ModelChoiceField(
		queryset=Department.objects.none(),
		required=False,
		label="Department",
		widget=forms.Select(attrs={"class": "field-input"}),
	)
	status = forms.ChoiceField(
		required=False,
		label="Status",
		choices=[("", "All statuses"), *ActivityStatus.choices],
		widget=forms.Select(attrs={"class": "field-input"}),
	)

	def __init__(self, *args, company=None, years=None, include_status=True, **kwargs):
		super().__init__(*args, **kwargs)
		year_choices = [("", "All years"), *[(str(year), str(year)) for year in (years or [])]]
		self.fields["year"].choices = year_choices
		self.fields["department"].queryset = (
			Department.objects.filter(company=company).order_by("name")
			if company is not None
			else Department.objects.none()
		)
		self.fields["department"].empty_label = "All departments"
		if not include_status:
			self.fields.pop("status")


class AnomalyFilterForm(forms.Form):
	"""Filter bar for the anomaly list: severity, status, department, category, period."""

	severity = forms.ChoiceField(
		required=False,
		label="Severity",
		choices=[("", "All severities"), *AnomalySeverity.choices],
		widget=forms.Select(attrs={"class": "field-input"}),
	)
	status = forms.ChoiceField(
		required=False,
		label="Status",
		choices=[("", "All statuses"), *AnomalyStatus.choices],
		widget=forms.Select(attrs={"class": "field-input"}),
	)
	category = forms.ChoiceField(
		required=False,
		label="Category",
		choices=[("", "All categories"), *ActivityCategory.choices],
		widget=forms.Select(attrs={"class": "field-input"}),
	)
	department = forms.ModelChoiceField(
		queryset=Department.objects.none(),
		required=False,
		label="Department",
		widget=forms.Select(attrs={"class": "field-input"}),
	)
	year = forms.ChoiceField(
		required=False, label="Period", widget=forms.Select(attrs={"class": "field-input"})
	)

	def __init__(self, *args, company=None, years=None, **kwargs):
		super().__init__(*args, **kwargs)
		self.fields["year"].choices = [
			("", "All periods"),
			*[(str(year), str(year)) for year in (years or [])],
		]
		self.fields["department"].queryset = (
			Department.objects.filter(company=company).order_by("name")
			if company is not None
			else Department.objects.none()
		)
		self.fields["department"].empty_label = "All departments"


class ActivityEditForm(forms.Form):
	"""Correct a draft or returned activity without touching the CO2e by hand."""

	name = forms.CharField(
		max_length=255, required=False,
		widget=forms.TextInput(attrs={"class": "field-input"}),
	)
	quantity = forms.DecimalField(
		max_digits=18, decimal_places=4, min_value=0,
		error_messages={
			"required": "Enter the quantity you measured.",
			"invalid": "Enter a valid number for the quantity.",
			"min_value": "The quantity cannot be negative.",
		},
		widget=forms.NumberInput(attrs={"class": "field-input", "step": "0.0001", "min": "0"}),
	)
	unit = forms.CharField(
		max_length=32,
		error_messages={"required": "Pick the unit the quantity is expressed in."},
		widget=forms.TextInput(attrs={"class": "field-input", "list": "unit-options"}),
	)
	period_start = forms.DateField(
		error_messages={
			"required": "Enter the reporting period start date.",
			"invalid": "Enter a valid date (for example 2026-01-31).",
		},
		widget=forms.DateInput(attrs={"class": "field-input", "type": "date"}),
	)
	period_end = forms.DateField(
		required=False,
		error_messages={"invalid": "Enter a valid date (for example 2026-01-31)."},
		widget=forms.DateInput(attrs={"class": "field-input", "type": "date"}),
	)
	department = forms.ModelChoiceField(
		queryset=Department.objects.none(), required=False,
		error_messages={
			"invalid_choice": "The department must belong to your company."
		},
		widget=forms.Select(attrs={"class": "field-input"}),
	)
	supplier = forms.ModelChoiceField(
		queryset=None, required=False,
		widget=forms.Select(attrs={"class": "field-input"}),
	)
	emission_factor = forms.ModelChoiceField(
		queryset=None, widget=forms.Select(attrs={"class": "field-input"})
	)
	data_source = forms.CharField(
		max_length=255, required=False,
		widget=forms.TextInput(attrs={"class": "field-input"}),
	)
	evidence = forms.FileField(
		required=False, widget=forms.ClearableFileInput(attrs={"class": "field-input"})
	)

	def __init__(self, *args, instance=None, company=None, user=None, **kwargs):
		super().__init__(*args, **kwargs)
		self.instance = instance
		self.company = company
		# Imported lazily so the carbon app never imports suppliers at load time.
		from apps.suppliers.models import Supplier

		queryset = Department.objects.none()
		suppliers = Supplier.objects.none()
		if company is not None:
			queryset = Department.objects.filter(company=company).order_by("name")
			suppliers = Supplier.objects.filter(
				company=company, is_active=True
			).order_by("name")
		if user is not None and user.role == User.Role.DEPARTMENT_MANAGER and user.department:
			queryset = queryset.filter(name=user.department)
		self.fields["department"].queryset = queryset
		self.fields["department"].empty_label = "No department"
		self.fields["supplier"].queryset = suppliers
		self.fields["supplier"].empty_label = "No supplier"
		candidates = (
			recommendation_candidates(category=instance.category, scope=instance.scope, company=company)
			if instance is not None
			else []
		)
		candidate_ids = [factor.pk for factor in candidates]
		# A ModelChoiceField needs a queryset, not the recommendation list.
		self.fields["emission_factor"].queryset = EmissionFactor.objects.filter(
			pk__in=candidate_ids
		)

	def clean(self):
		cleaned = super().clean()
		instance = self.instance
		factor = cleaned.get("emission_factor")
		if factor is not None and instance is not None and factor.category != instance.category:
			self.add_error(
				"emission_factor",
				"The selected emission factor does not match this activity's category.",
			)
		if factor is not None and instance is not None and factor.scope != instance.scope:
			self.add_error(
				"emission_factor",
				"The selected emission factor does not match this activity's scope.",
			)
		start = cleaned.get("period_start")
		end = cleaned.get("period_end")
		if start and end and end < start:
			self.add_error("period_end", "End date must be on or after start date.")
		department = cleaned.get("department")
		if department is not None and self.company is not None:
			if department.company_id != self.company.pk:
				self.add_error("department", "The department must belong to your company.")
		return cleaned
