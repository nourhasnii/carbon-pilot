from django import forms

from apps.enterprise.models import Department

from .models import (
	ApprovalDecision,
	Policy,
	PolicyCategory,
	PolicyOwner,
	PolicyStatus,
	PolicyVersion,
	ResponsibilityRole,
)
from .services import company_for_user, company_members

# Minimum length of a policy description, enforced server-side.
DESCRIPTION_MIN_LENGTH = 10


class PolicyFilterForm(forms.Form):
	"""Filter bar for the governance policy list."""

	status = forms.ChoiceField(
		required=False,
		label="Status",
		choices=[("", "All statuses"), *PolicyStatus.choices],
		widget=forms.Select(attrs={"class": "field-input"}),
	)
	category = forms.ChoiceField(
		required=False,
		label="Category",
		choices=[("", "All categories"), *PolicyCategory.choices],
		widget=forms.Select(attrs={"class": "field-input"}),
	)
	due = forms.ChoiceField(
		required=False,
		label="Review",
		choices=[("", "Any review state"), ("due", "Due for review")],
		widget=forms.Select(attrs={"class": "field-input"}),
	)
	department = forms.ModelChoiceField(
		queryset=Department.objects.none(),
		required=False,
		empty_label="All departments",
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


class PolicyForm(forms.ModelForm):
	"""Create/edit a policy's descriptive metadata (status is workflow-driven)."""

	class Meta:
		model = Policy
		fields = ("title", "code", "category", "description", "next_review_date")
		widgets = {
			"title": forms.TextInput(
				attrs={"class": "field-input", "placeholder": "e.g. Supplier Code of Conduct"}
			),
			"code": forms.TextInput(
				attrs={"class": "field-input", "placeholder": "e.g. GOV-001 (optional)"}
			),
			"category": forms.Select(attrs={"class": "field-input"}),
			"description": forms.Textarea(
				attrs={"class": "field-input", "rows": 3, "placeholder": "Purpose and scope of the policy"}
			),
			"next_review_date": forms.DateInput(
				attrs={"class": "field-input", "type": "date"}
			),
		}

	def __init__(self, *args, company=None, **kwargs):
		super().__init__(*args, **kwargs)
		self.company = company
		self.fields["next_review_date"].required = False
		self.fields["code"].required = False

		self.fields["title"].error_messages["required"] = "Give the policy a title."
		self.fields["category"].error_messages["required"] = "Pick a policy category."
		self.fields["next_review_date"].error_messages["invalid"] = (
			"Enter a valid date (for example 2026-01-31)."
		)

		# The database column is optional, but a policy without a description is
		# not reviewable — so the form requires a meaningful one. The length is
		# enforced in ``clean_description`` because a ``min_length`` set after
		# the field is built would not register its validator.
		description = self.fields["description"]
		description.required = True
		description.error_messages["required"] = "Describe what this policy covers."
		description.widget.attrs["minlength"] = str(DESCRIPTION_MIN_LENGTH)

	def clean_description(self):
		description = (self.cleaned_data.get("description") or "").strip()
		if description and len(description) < DESCRIPTION_MIN_LENGTH:
			raise forms.ValidationError(
				f"Description must be at least {DESCRIPTION_MIN_LENGTH} characters."
			)
		return description

	def clean_code(self):
		code = (self.cleaned_data.get("code") or "").strip().upper()
		if code and self.company is not None:
			queryset = Policy.objects.filter(company=self.company, code=code)
			if self.instance.pk:
				queryset = queryset.exclude(pk=self.instance.pk)
			if queryset.exists():
				raise forms.ValidationError("Another policy of your company already uses this code.")
		return code

	def clean(self):
		cleaned = super().clean()
		# ``clean_code`` already reports a duplicate code with a friendly,
		# field-level message, so the generic uniqueness error is suppressed.
		self._validate_unique = False
		return cleaned


class PolicyVersionForm(forms.ModelForm):
	"""Author the text of one policy version."""

	class Meta:
		model = PolicyVersion
		fields = ("summary", "content", "change_notes", "effective_from")
		widgets = {
			"summary": forms.TextInput(
				attrs={"class": "field-input", "placeholder": "One-line abstract of this version"}
			),
			"content": forms.Textarea(
				attrs={"class": "field-input", "rows": 6, "placeholder": "Full policy text"}
			),
			"change_notes": forms.Textarea(
				attrs={"class": "field-input", "rows": 2, "placeholder": "What changed since the previous version"}
			),
			"effective_from": forms.DateInput(attrs={"class": "field-input", "type": "date"}),
		}

	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)
		for name in ("summary", "content", "change_notes", "effective_from"):
			self.fields[name].required = False


class PolicyOwnerForm(forms.ModelForm):
	"""One row of the policy responsibility matrix."""

	class Meta:
		model = PolicyOwner
		fields = ("responsibility_role", "department", "user")
		widgets = {
			"responsibility_role": forms.Select(attrs={"class": "field-input"}),
			"department": forms.Select(attrs={"class": "field-input"}),
			"user": forms.Select(attrs={"class": "field-input"}),
		}

	def __init__(self, *args, company=None, **kwargs):
		super().__init__(*args, **kwargs)
		self.company = company
		self.fields["responsibility_role"].choices = ResponsibilityRole.choices
		self.fields["department"].queryset = (
			Department.objects.filter(company=company).order_by("name")
			if company is not None
			else Department.objects.none()
		)
		self.fields["department"].required = False
		self.fields["department"].empty_label = "— Company-wide —"
		self.fields["user"].queryset = company_members(company)
		self.fields["user"].required = False
		self.fields["user"].empty_label = "— Department only —"

	def clean(self):
		cleaned = super().clean()
		# Unchanged extra rows are never saved; don't flag them as invalid.
		if not self.has_changed():
			return cleaned
		department = cleaned.get("department")
		user = cleaned.get("user")
		if not department and not user:
			raise forms.ValidationError(
				"Pick a department, a user, or both for this responsibility row."
			)
		# Never accept a department or user from another tenant, whatever the
		# request body claims.
		if self.company is not None:
			if department is not None and department.company_id != self.company.pk:
				raise forms.ValidationError("The department must belong to your company.")
			if user is not None:
				user_company = company_for_user(user)
				if user_company is not None and user_company.pk != self.company.pk:
					raise forms.ValidationError("The user must belong to your company.")
		return cleaned


PolicyOwnerFormSet = forms.inlineformset_factory(
	Policy,
	PolicyOwner,
	form=PolicyOwnerForm,
	extra=1,
	can_delete=True,
)


class PolicyDecisionForm(forms.Form):
	"""Approve or reject the current version."""

	decision = forms.ChoiceField(
		widget=forms.RadioSelect,
		choices=(
			(ApprovalDecision.APPROVED, "Approve"),
			(ApprovalDecision.REJECTED, "Reject"),
		),
	)
	comment = forms.CharField(
		required=False,
		widget=forms.Textarea(
			attrs={"class": "field-input", "rows": 2, "placeholder": "Optional comment recorded with your decision"}
		),
	)
