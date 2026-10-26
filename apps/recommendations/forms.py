"""Filter forms for the AI recommendations workspace."""

from django import forms

from apps.enterprise.models import Department

from .models import (
	RecommendationPriority,
	RecommendationSource,
	RecommendationStatus,
)


class RecommendationFilterForm(forms.Form):
	"""Shared filter bar for the recommendation list.

	Mirrors the existing inline filters (priority, status, source, department);
	the department choices are injected per company so a tenant can never widen
	its own scope through the filter bar.
	"""

	priority = forms.ChoiceField(
		required=False,
		label="Priority",
		choices=[("", "All priorities"), *RecommendationPriority.choices],
		widget=forms.Select(attrs={"class": "field-input"}),
	)
	status = forms.ChoiceField(
		required=False,
		label="Status",
		choices=[("", "Open (new + in progress)"), *RecommendationStatus.choices],
		widget=forms.Select(attrs={"class": "field-input"}),
	)
	source = forms.ChoiceField(
		required=False,
		label="Source",
		choices=[("", "All sources"), *RecommendationSource.choices],
		widget=forms.Select(attrs={"class": "field-input"}),
	)
	department = forms.ModelChoiceField(
		queryset=Department.objects.none(),
		required=False,
		label="Department",
		widget=forms.Select(attrs={"class": "field-input"}),
	)

	def __init__(self, *args, company=None, **kwargs):
		super().__init__(*args, **kwargs)
		self.fields["department"].queryset = (
			Department.objects.filter(company=company).order_by("name")
			if company is not None
			else Department.objects.none()
		)
		self.fields["department"].empty_label = "All departments"
