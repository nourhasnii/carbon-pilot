"""Forms for the ESG intelligence hub.

Every queryset used by a form is scoped to the caller's company, so a doctored
request body can never attach another tenant's catalog entry, department or
user to an indicator.
"""

from django import forms
from django.utils import timezone

from apps.enterprise.models import Department

from .models import ESGPillar, Indicator, IndicatorCatalogEntry, IndicatorStatus, IndicatorValue
from .services.access import catalog_entries, company_members

# A measurement may describe any of the last twenty reporting years, or the
# year currently being closed. Anything outside that window is a typo.
REPORTING_YEAR_MIN_OFFSET = -20
REPORTING_YEAR_MAX_OFFSET = 1


class IndicatorForm(forms.ModelForm):
	"""Create/edit the company's tracking of one catalog indicator."""

	class Meta:
		model = Indicator
		fields = ("catalog_entry", "owner", "department", "notes")
		widgets = {
			"catalog_entry": forms.Select(attrs={"class": "field-input"}),
			"owner": forms.Select(attrs={"class": "field-input"}),
			"department": forms.Select(attrs={"class": "field-input"}),
			"notes": forms.Textarea(
				attrs={
					"class": "field-input",
					"rows": 3,
					"placeholder": "How this metric is collected and who owns it.",
				}
			),
		}

	def __init__(self, *args, company=None, **kwargs):
		super().__init__(*args, **kwargs)
		self.company = company
		self.fields["catalog_entry"].queryset = (
			catalog_entries(company) if company is not None else IndicatorCatalogEntry.objects.none()
		)
		self.fields["catalog_entry"].empty_label = "— Select a catalog indicator —"
		self.fields["catalog_entry"].error_messages["required"] = (
			"Select the catalog indicator this row tracks."
		)
		self.fields["department"].queryset = (
			Department.objects.filter(company=company).order_by("name")
			if company is not None
			else Department.objects.none()
		)
		self.fields["department"].required = False
		self.fields["department"].empty_label = "— Company-wide —"
		self.fields["owner"].queryset = company_members(company)
		self.fields["owner"].required = False
		self.fields["owner"].empty_label = "— No owner yet —"
		if self.instance.pk:
			# The tracked metric is immutable; changing it would break history.
			self.fields["catalog_entry"].disabled = True

	def clean(self):
		cleaned = super().clean()
		# The cross-company guards for the department, owner and catalog entry
		# live in ``Indicator.clean()``, which Django runs right after this form
		# and reports per field. Only the "one row per catalog indicator" rule
		# needs a friendlier message than the generic uniqueness error.
		entry = cleaned.get("catalog_entry")
		if self.company is not None and entry is not None and not self.instance.pk:
			if Indicator.objects.filter(company=self.company, catalog_entry=entry).exists():
				self.add_error(
					"catalog_entry",
					"Your company already tracks this indicator — open it instead of creating a duplicate.",
				)
		# The explicit message above replaces the generic unique-constraint one.
		self._validate_unique = False
		return cleaned


class IndicatorValueForm(forms.ModelForm):
	"""Record one reporting period's measurement.

	The value is optional on purpose: leaving it blank records a **missing**
	measurement, which is not the same as a genuine measured zero.
	"""

	REPORT_CHOICES = (
		(IndicatorStatus.DRAFT, "Save as draft"),
		(IndicatorStatus.REPORTED, "Report as ESG data"),
	)

	status = forms.ChoiceField(
		choices=REPORT_CHOICES,
		initial=IndicatorStatus.DRAFT,
		widget=forms.RadioSelect(attrs={"class": "cp-choice-input"}),
		help_text="Only reported and verified measurements count towards completeness.",
	)

	class Meta:
		model = IndicatorValue
		fields = ("period_year", "value", "unit", "evidence_reference", "note")
		widgets = {
			"period_year": forms.NumberInput(
				attrs={"class": "field-input", "min": 1990, "max": 2100, "step": 1}
			),
			"value": forms.NumberInput(
				attrs={"class": "field-input", "step": "any", "placeholder": "Leave blank if unknown"}
			),
			"unit": forms.TextInput(attrs={"class": "field-input"}),
			"evidence_reference": forms.TextInput(
				attrs={
					"class": "field-input",
					"placeholder": "e.g. invoice ref, meter report, HR export",
				}
			),
			"note": forms.Textarea(
				attrs={"class": "field-input", "rows": 3, "placeholder": "Optional note for reviewers."}
			),
		}

	def __init__(self, *args, indicator=None, **kwargs):
		super().__init__(*args, **kwargs)
		self.indicator = indicator
		self.fields["value"].required = False
		self.fields["unit"].required = False
		self.fields["evidence_reference"].required = False
		self.fields["note"].required = False
		if indicator is not None and not self.fields["unit"].initial:
			self.fields["unit"].initial = indicator.unit
		if self.instance.pk and self.instance.status in (
			IndicatorStatus.DRAFT,
			IndicatorStatus.REPORTED,
		):
			self.fields["status"].initial = self.instance.status

		current_year = timezone.now().year
		self.min_year = current_year + REPORTING_YEAR_MIN_OFFSET
		self.max_year = current_year + REPORTING_YEAR_MAX_OFFSET
		year_field = self.fields["period_year"]
		year_field.error_messages["required"] = "Enter the reporting year."
		year_field.error_messages["invalid"] = "Enter the reporting year as a whole number."
		year_field.widget.attrs["min"] = str(self.min_year)
		year_field.widget.attrs["max"] = str(self.max_year)
		# Leaving the measurement blank is allowed: it records "not reported",
		# which stays distinct from a genuine measured zero.
		self.fields["value"].error_messages["invalid"] = "Enter a valid number, or leave it blank."

	def clean_period_year(self):
		year = self.cleaned_data.get("period_year")
		if not year:
			raise forms.ValidationError("A reporting year is required.")
		if not self.min_year <= year <= self.max_year:
			raise forms.ValidationError(
				f"Reporting year must be between {self.min_year} and {self.max_year}."
			)
		return year

	def clean(self):
		cleaned = super().clean()
		if cleaned.get("status") == IndicatorStatus.REPORTED and cleaned.get("value") is None:
			self.add_error(
				"value",
				"A reported measurement needs a value. Save it as a draft if the data is missing.",
			)
		if self.instance.pk and self.instance.value is not None:
			# A stored measurement may not be silently blanked out into "missing".
			if cleaned.get("value") is None:
				self.add_error(
					"value",
					"Keep the existing value, or delete the period instead of clearing it.",
				)
		return cleaned


class IndicatorFilterForm(forms.Form):
	"""Search/filter controls for the indicator catalog (GET only)."""

	q = forms.CharField(
		required=False,
		label="Search",
		widget=forms.TextInput(
			attrs={"class": "field-input", "placeholder": "Indicator name or code"}
		),
	)
	pillar = forms.ChoiceField(
		required=False,
		label="Pillar",
		choices=[("", "All pillars")] + list(ESGPillar.choices),
		widget=forms.Select(attrs={"class": "field-input"}),
	)
	status = forms.ChoiceField(
		required=False,
		label="Status",
		choices=[
			("", "Any status"),
			*IndicatorStatus.choices,
			("NONE", "No data"),
			("INACTIVE", "Inactive"),
		],
		widget=forms.Select(attrs={"class": "field-input"}),
	)
	department = forms.ChoiceField(
		required=False,
		label="Department",
		choices=[("", "All departments")],
		widget=forms.Select(attrs={"class": "field-input"}),
	)
	year = forms.ChoiceField(
		required=False,
		label="Reporting year",
		choices=[("", "Any year")],
		widget=forms.Select(attrs={"class": "field-input"}),
	)

	def __init__(self, *args, company=None, years=(), **kwargs):
		super().__init__(*args, **kwargs)
		departments = (
			Department.objects.filter(company=company).order_by("name")
			if company is not None
			else Department.objects.none()
		)
		self.fields["department"].choices = [("", "All departments")] + [
			(str(department.pk), department.name) for department in departments
		]
		self.fields["year"].choices = [("", "Any year")] + [
			(str(year), str(year)) for year in years
		]


class RecommendationConfirmForm(forms.Form):
	"""Confirm an AI-recommended indicator before it becomes a draft.

	The user must supply the unit: CarbonPilot never invents a measurement unit
	for a metric coming out of the model.
	"""

	name = forms.CharField(
		max_length=255,
		widget=forms.TextInput(attrs={"class": "field-input"}),
	)
	pillar = forms.ChoiceField(
		choices=ESGPillar.choices,
		widget=forms.Select(attrs={"class": "field-input"}),
	)
	unit = forms.CharField(
		max_length=32,
		widget=forms.TextInput(
			attrs={"class": "field-input", "placeholder": "e.g. tCO2e, MWh, %, hours"}
		),
	)
	make_required = forms.BooleanField(
		required=False,
		label="Add to my required-indicator catalog",
		help_text="Required indicators are scored by the completeness engine.",
	)

	def clean_name(self):
		name = (self.cleaned_data.get("name") or "").strip()
		if len(name) < 3:
			raise forms.ValidationError("Give the indicator a clear name.")
		return name

	def clean_unit(self):
		unit = (self.cleaned_data.get("unit") or "").strip()
		if not unit:
			raise forms.ValidationError("A measurement unit is required.")
		return unit


__all__ = [
	"IndicatorFilterForm",
	"IndicatorForm",
	"IndicatorValueForm",
	"RecommendationConfirmForm",
]
