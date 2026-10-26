from decimal import Decimal

from django import forms
from django.utils import timezone

from apps.accounts.models import User
from apps.enterprise.models import Department

from .models import Objective, ObjectiveStatus, PeriodType, Quarter

# An objective may be planned for the year that just closed (to record a
# baseline that has only now been consolidated) and up to ten years ahead.
# Anything outside that window is a typo, not a reduction target.
YEAR_MIN_OFFSET = -1
YEAR_MAX_OFFSET = 10


class ObjectiveFilterForm(forms.Form):
	"""Shared filter bar for the objective list.

	Every field is optional; an empty value simply means "no restriction".
	The department choices and searchable years are injected per company so a
	tenant can never widen its own scope through the filter bar.
	"""

	q = forms.CharField(
		required=False,
		label="Search",
		widget=forms.TextInput(
			attrs={"class": "field-input", "placeholder": "Search objective name…"}
		),
	)
	status = forms.ChoiceField(
		required=False,
		label="Status",
		choices=[("", "All statuses"), *ObjectiveStatus.choices],
		widget=forms.Select(attrs={"class": "field-input"}),
	)
	department = forms.ModelChoiceField(
		queryset=Department.objects.none(),
		required=False,
		label="Department",
		widget=forms.Select(attrs={"class": "field-input"}),
	)
	year = forms.ChoiceField(
		required=False, label="Year", widget=forms.Select(attrs={"class": "field-input"})
	)
	period_type = forms.ChoiceField(
		required=False,
		label="Period",
		choices=[("", "All periods"), *PeriodType.choices],
		widget=forms.Select(attrs={"class": "field-input"}),
	)
	at_risk = forms.ChoiceField(
		required=False,
		label="Risk",
		choices=[("", "All objectives"), ("1", "At risk only")],
		widget=forms.Select(attrs={"class": "field-input"}),
	)

	def __init__(self, *args, company=None, years=None, **kwargs):
		super().__init__(*args, **kwargs)
		self.fields["year"].choices = [
			("", "All years"),
			*[(str(year), str(year)) for year in (years or [])],
		]
		self.fields["department"].queryset = (
			Department.objects.filter(company=company).order_by("name")
			if company is not None
			else Department.objects.none()
		)
		self.fields["department"].empty_label = "All departments"


class ObjectiveForm(forms.ModelForm):
	"""Create/edit a reduction objective. Status is derived, never entered."""

	class Meta:
		model = Objective
		fields = (
			"name", "department", "year", "period_type", "quarter",
			"baseline_tco2e", "target_tco2e",
		)
		widgets = {
			"name": forms.TextInput(attrs={"class": "field-input", "placeholder": "e.g. Cut head office emissions 25%"}),
			"department": forms.Select(attrs={"class": "field-input"}),
			"year": forms.NumberInput(attrs={"class": "field-input", "step": "1"}),
			"period_type": forms.Select(attrs={"class": "field-input"}),
			"quarter": forms.Select(attrs={"class": "field-input"}),
			"baseline_tco2e": forms.NumberInput(attrs={"class": "field-input", "step": "0.01", "min": "0"}),
			"target_tco2e": forms.NumberInput(attrs={"class": "field-input", "step": "0.01", "min": "0"}),
		}

	def __init__(self, *args, company=None, user=None, **kwargs):
		super().__init__(*args, **kwargs)
		self.fields["quarter"].choices = [("", "Not applicable / annual"), *Quarter.choices]
		queryset = Department.objects.none()
		if company is not None:
			queryset = Department.objects.filter(company=company).order_by("name")
		# A department manager can only ever target the department they own.
		if user is not None and user.role == User.Role.DEPARTMENT_MANAGER and user.department:
			queryset = queryset.filter(name=user.department)
		self.fields["department"].queryset = queryset
		self.fields["department"].empty_label = "Company-wide (all departments)"
		self.fields["name"].error_messages["required"] = "Give the objective a name."

		current_year = timezone.now().year
		self.min_year = current_year + YEAR_MIN_OFFSET
		self.max_year = current_year + YEAR_MAX_OFFSET
		year_field = self.fields["year"]
		year_field.error_messages["required"] = "Enter the objective year."
		year_field.error_messages["invalid"] = "Enter the year as a whole number."
		year_field.widget.attrs["min"] = str(self.min_year)
		year_field.widget.attrs["max"] = str(self.max_year)

		for name, label in (("baseline_tco2e", "baseline"), ("target_tco2e", "target")):
			field = self.fields[name]
			field.error_messages["required"] = f"Enter the {label} in tCO2e."
			field.error_messages["invalid"] = f"Enter a valid number for the {label}."

	def clean_year(self):
		year = self.cleaned_data.get("year")
		if year is not None and not self.min_year <= year <= self.max_year:
			raise forms.ValidationError(
				f"Year must be between {self.min_year} and {self.max_year}."
			)
		return year

	def clean_baseline_tco2e(self):
		value = self.cleaned_data.get("baseline_tco2e")
		if value is not None and value < Decimal("0"):
			raise forms.ValidationError("The baseline cannot be negative.")
		return value

	def clean_target_tco2e(self):
		value = self.cleaned_data.get("target_tco2e")
		if value is not None and value < Decimal("0"):
			raise forms.ValidationError("The target cannot be negative.")
		return value

	# Period/quarter consistency and "target must be lower than baseline" are
	# enforced by ``Objective.clean()``, which Django runs right after this
	# form. Repeating them here would surface every such message twice.
