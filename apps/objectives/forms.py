from decimal import Decimal

from django import forms

from apps.accounts.models import User
from apps.enterprise.models import Department

from .models import Objective, PeriodType, Quarter


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
			"year": forms.NumberInput(attrs={"class": "field-input", "min": "1900", "max": "2100", "step": "1"}),
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
		self.fields["year"].min_value = 1900
		self.fields["year"].max_value = 2100

	def clean(self):
		cleaned = super().clean()
		period_type = cleaned.get("period_type")
		quarter = cleaned.get("quarter")
		baseline = cleaned.get("baseline_tco2e")
		target = cleaned.get("target_tco2e")
		if period_type == PeriodType.QUARTERLY and not quarter:
			self.add_error("quarter", "Pick a quarter for a quarterly objective.")
		if period_type == PeriodType.ANNUAL and quarter:
			self.add_error("quarter", "An annual objective does not need a quarter.")
		if baseline is not None and target is not None and target > baseline:
			self.add_error("target_tco2e", "The target cannot exceed the baseline for a reduction objective.")
		return cleaned

	def clean_baseline_tco2e(self):
		value = self.cleaned_data["baseline_tco2e"]
		if value is not None and value < Decimal("0"):
			raise forms.ValidationError("The baseline cannot be negative.")
		return value

	def clean_target_tco2e(self):
		value = self.cleaned_data["target_tco2e"]
		if value is not None and value < Decimal("0"):
			raise forms.ValidationError("The target cannot be negative.")
		return value
