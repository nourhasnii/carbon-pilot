from django import forms

from apps.investments.models import ProjectStatus

from .models import PortfolioRun, PortfolioRunObjective


class PortfolioRunForm(forms.ModelForm):
	class Meta:
		model = PortfolioRun
		fields = ("budget", "objective", "included_statuses")
		widgets = {
			"budget": forms.NumberInput(attrs={
				"class": "field-input",
				"min": "0.01",
				"step": "0.01",
				"required": "required",
				"placeholder": "Ex. 250000",
			}),
			"objective": forms.Select(attrs={"class": "field-input"}),
			"included_statuses": forms.CheckboxSelectMultiple(),
		}

	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)
		self.fields["included_statuses"] = forms.MultipleChoiceField(
			choices=[
				(ProjectStatus.PROPOSED, ProjectStatus.PROPOSED.label),
				(ProjectStatus.APPROVED, ProjectStatus.APPROVED.label),
				(ProjectStatus.IN_PROGRESS, ProjectStatus.IN_PROGRESS.label),
				(ProjectStatus.COMPLETED, ProjectStatus.COMPLETED.label),
			],
			widget=forms.CheckboxSelectMultiple,
			required=True,
			label="Statuts inclus",
		)
		for name, field in self.fields.items():
			field.help_text = "Champ obligatoire." if field.required else ""
			field.widget.attrs["aria-describedby"] = f"{name}-help {name}-error"
			field.error_messages["required"] = "Ce champ est obligatoire."

	def clean_budget(self):
		budget = self.cleaned_data.get("budget")
		if budget is None or budget <= 0:
			raise forms.ValidationError("Le budget doit être strictement supérieur à zéro.")
		return budget

	def clean_included_statuses(self):
		statuses = self.cleaned_data.get("included_statuses")
		if not statuses:
			raise forms.ValidationError("Sélectionnez au moins un statut de projet.")
		return statuses

	def clean_objective(self):
		objective = self.cleaned_data.get("objective")
		if objective not in PortfolioRunObjective.values:
			raise forms.ValidationError("L'objectif sélectionné est invalide.")
		return objective
