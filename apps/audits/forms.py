"""Audit forms.

Every relation (department, policy, owner, lead auditor) is scoped to the
caller's own company, and the cross-tenant relation guards live in the model
``clean()`` methods so a forged request body can never link another tenant's
record.
"""

from django import forms

from apps.enterprise.models import Department
from apps.governance.models import Policy

from .models import (
	Audit,
	AuditEvidence,
	AuditStatus,
	AuditType,
	Finding,
	RemediationPlan,
)
from .services import company_members

TEXT_INPUT = {"class": "field-input"}
SELECT_INPUT = {"class": "field-input"}
DATE_INPUT = {"class": "field-input", "type": "date"}


class AuditForm(forms.ModelForm):
	"""Create / edit an engagement. The status is workflow-driven, never typed."""

	class Meta:
		model = Audit
		fields = (
			"title", "audit_type", "scope_summary", "department",
			"related_policy", "start_date", "end_date", "lead_auditor",
		)
		widgets = {
			"title": forms.TextInput(
				attrs={**TEXT_INPUT, "placeholder": "e.g. Q3 internal control review"}
			),
			"audit_type": forms.Select(attrs=SELECT_INPUT),
			"scope_summary": forms.Textarea(
				attrs={**TEXT_INPUT, "rows": 3, "placeholder": "Scope and objectives of the engagement"}
			),
			"department": forms.Select(attrs=SELECT_INPUT),
			"related_policy": forms.Select(attrs=SELECT_INPUT),
			"start_date": forms.DateInput(attrs=DATE_INPUT),
			"end_date": forms.DateInput(attrs=DATE_INPUT),
			"lead_auditor": forms.Select(attrs=SELECT_INPUT),
		}

	def __init__(self, *args, company=None, **kwargs):
		super().__init__(*args, **kwargs)
		self.company = company
		self.fields["department"].queryset = (
			Department.objects.filter(company=company).order_by("name")
			if company is not None
			else Department.objects.none()
		)
		self.fields["related_policy"].queryset = (
			Policy.objects.filter(company=company).order_by("title")
			if company is not None
			else Policy.objects.none()
		)
		self.fields["lead_auditor"].queryset = company_members(company)
		for name in ("department", "related_policy", "lead_auditor"):
			self.fields[name].required = False
			self.fields[name].empty_label = "— None —"
		self.fields["end_date"].required = False
		self.fields["scope_summary"].required = False

		self.fields["title"].error_messages["required"] = "Give the audit a title."
		self.fields["audit_type"].error_messages["required"] = "Pick an audit type."
		self.fields["start_date"].error_messages["required"] = "Enter the audit start date."
		for name in ("start_date", "end_date"):
			self.fields[name].error_messages["invalid"] = (
				"Enter a valid date (for example 2026-01-31)."
			)

	# Date ordering and the cross-company relation guards live in
	# ``Audit.clean()``: Django runs it right after this form, so the messages
	# land under the right field without being repeated.


class AuditFilterForm(forms.Form):
	"""Filter bar for the audits list."""

	status = forms.ChoiceField(
		required=False, label="Status", widget=forms.Select(attrs=SELECT_INPUT)
	)
	audit_type = forms.ChoiceField(
		required=False, label="Type", widget=forms.Select(attrs=SELECT_INPUT)
	)
	department = forms.ChoiceField(
		required=False, label="Department", widget=forms.Select(attrs=SELECT_INPUT)
	)

	def __init__(self, *args, company=None, **kwargs):
		super().__init__(*args, **kwargs)
		self.fields["status"].choices = [("", "All statuses"), *AuditStatus.choices]
		self.fields["audit_type"].choices = [("", "All types"), *AuditType.choices]
		departments = (
			Department.objects.filter(company=company).order_by("name")
			if company is not None
			else Department.objects.none()
		)
		self.fields["department"].choices = [
			("", "All departments"),
			*[(str(d.pk), d.name) for d in departments],
		]


class FindingForm(forms.ModelForm):
	"""Create / edit a finding. The status is workflow-driven, never typed."""

	class Meta:
		model = Finding
		fields = (
			"title", "description", "severity", "department",
			"related_policy", "owner", "due_date",
		)
		widgets = {
			"title": forms.TextInput(
				attrs={**TEXT_INPUT, "placeholder": "e.g. Missing documented approval for supplier X"}
			),
			"description": forms.Textarea(
				attrs={**TEXT_INPUT, "rows": 4, "placeholder": "What was observed, and why it matters"}
			),
			"severity": forms.Select(attrs=SELECT_INPUT),
			"department": forms.Select(attrs=SELECT_INPUT),
			"related_policy": forms.Select(attrs=SELECT_INPUT),
			"owner": forms.Select(attrs=SELECT_INPUT),
			"due_date": forms.DateInput(attrs=DATE_INPUT),
		}

	def __init__(self, *args, company=None, restrict_department=None, **kwargs):
		super().__init__(*args, **kwargs)
		self.company = company
		departments = (
			Department.objects.filter(company=company).order_by("name")
			if company is not None
			else Department.objects.none()
		)
		if restrict_department is not None:
			# A department manager may only file findings against their own
			# department; the choice is fixed server-side.
			departments = departments.filter(pk=restrict_department.pk)
			self.fields["department"].initial = restrict_department.pk
			self.fields["department"].disabled = True
		self.fields["department"].queryset = departments
		self.fields["related_policy"].queryset = (
			Policy.objects.filter(company=company).order_by("title")
			if company is not None
			else Policy.objects.none()
		)
		self.fields["owner"].queryset = company_members(company)
		for name in ("department", "related_policy", "owner"):
			self.fields[name].required = False
			self.fields[name].empty_label = "— None —"
		self.fields["due_date"].required = False
		self.fields["description"].required = False


class RemediationPlanForm(forms.ModelForm):
	"""Create / edit a corrective action. The status is workflow-driven."""

	class Meta:
		model = RemediationPlan
		fields = ("title", "description", "owner", "due_date")
		widgets = {
			"title": forms.TextInput(
				attrs={**TEXT_INPUT, "placeholder": "e.g. Document the approval and retrain the buyer"}
			),
			"description": forms.Textarea(
				attrs={**TEXT_INPUT, "rows": 3, "placeholder": "What will be done to close the finding"}
			),
			"owner": forms.Select(attrs=SELECT_INPUT),
			"due_date": forms.DateInput(attrs=DATE_INPUT),
		}

	def __init__(self, *args, company=None, **kwargs):
		super().__init__(*args, **kwargs)
		self.company = company
		self.fields["owner"].queryset = company_members(company)
		self.fields["owner"].required = False
		self.fields["owner"].empty_label = "— Unassigned —"
		self.fields["due_date"].required = False
		self.fields["description"].required = False


class AuditEvidenceForm(forms.ModelForm):
	"""Attach a supporting file to an audit or to one of its findings."""

	class Meta:
		model = AuditEvidence
		fields = ("title", "file")
		widgets = {
			"title": forms.TextInput(
				attrs={**TEXT_INPUT, "placeholder": "e.g. Signed approval email"}
			),
			"file": forms.ClearableFileInput(attrs={"class": "field-input"}),
		}

	def clean_file(self):
		upload = self.cleaned_data.get("file")
		if upload and upload.size == 0:
			raise forms.ValidationError("The uploaded file is empty.")
		return upload
