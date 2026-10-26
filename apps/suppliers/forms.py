"""Supplier forms.

Filter choices (country, category) are derived from the caller's own suppliers
so they never expose another tenant's data.
"""

from django import forms

from .models import (
	ESGStatus,
	RiskLevel,
	Supplier,
	SupplierDocument,
	SupplierDocumentType,
)

TEXT_INPUT = {"class": "field-input"}
SELECT_INPUT = {"class": "field-input"}


class SupplierForm(forms.ModelForm):
	"""Create / edit a supplier. ``esg_status`` is derived, never typed in."""

	class Meta:
		model = Supplier
		fields = (
			"name", "legal_name", "country", "city", "category",
			"contact_name", "contact_email", "contact_phone", "website",
			"risk_level", "notes", "is_active",
		)
		widgets = {
			"name": forms.TextInput(attrs={**TEXT_INPUT, "placeholder": "e.g. Nordwind Logistics"}),
			"legal_name": forms.TextInput(attrs=TEXT_INPUT),
			"country": forms.TextInput(attrs={**TEXT_INPUT, "list": "country-options"}),
			"city": forms.TextInput(attrs=TEXT_INPUT),
			"category": forms.TextInput(attrs={**TEXT_INPUT, "list": "category-options", "placeholder": "e.g. Logistics"}),
			"contact_name": forms.TextInput(attrs=TEXT_INPUT),
			"contact_email": forms.EmailInput(attrs=TEXT_INPUT),
			"contact_phone": forms.TextInput(attrs=TEXT_INPUT),
			"website": forms.URLInput(attrs={**TEXT_INPUT, "placeholder": "https://example.com"}),
			"risk_level": forms.Select(attrs=SELECT_INPUT),
			"notes": forms.Textarea(attrs={**TEXT_INPUT, "rows": 4}),
			"is_active": forms.CheckboxInput(),
		}

	def __init__(self, *args, company=None, **kwargs):
		super().__init__(*args, **kwargs)
		self.company = company
		self.fields["risk_level"].required = False
		self.fields["risk_level"].choices = [
			("", "Not rated"),
			*RiskLevel.choices,
		]
		for field in ("name", "country", "category"):
			self.fields[field].required = True
			self.fields[field].widget.attrs["required"] = "required"

	def clean(self):
		cleaned = super().clean()
		for field in ("name", "country", "category"):
			value = cleaned.get(field)
			if value is not None and not value.strip():
				self.add_error(field, "This field cannot be empty.")
		# Uniqueness is enforced per company so two tenants may both have a
		# supplier with the same name.
		name = (cleaned.get("name") or "").strip()
		if name and self.company is not None:
			matches = Supplier.objects.filter(company=self.company, name__iexact=name)
			if self.instance.pk:
				matches = matches.exclude(pk=self.instance.pk)
			if matches.exists():
				self.add_error("name", "A supplier with this name already exists.")
		return cleaned


class SupplierDocumentForm(forms.ModelForm):
	class Meta:
		model = SupplierDocument
		fields = ("title", "document_type", "file")
		widgets = {
			"title": forms.TextInput(attrs={**TEXT_INPUT, "placeholder": "e.g. ISO 14001 certificate 2026"}),
			"document_type": forms.Select(attrs=SELECT_INPUT),
			# File input styling is handled by the template.
			"file": forms.ClearableFileInput(attrs={"class": "field-input"}),
		}

	def clean_file(self):
		upload = self.cleaned_data.get("file")
		if upload and upload.size == 0:
			raise forms.ValidationError("The uploaded file is empty.")
		return upload


class SupplierFilterForm(forms.Form):
	"""Filter bar for the supplier list."""

	q = forms.CharField(
		required=False,
		label="Search",
		widget=forms.TextInput(attrs={**TEXT_INPUT, "placeholder": "Search name, contact, email…"}),
	)
	esg_status = forms.ChoiceField(
		required=False,
		label="ESG status",
		choices=[("", "All ESG statuses"), *ESGStatus.choices],
		widget=forms.Select(attrs=SELECT_INPUT),
	)
	risk_level = forms.ChoiceField(
		required=False,
		label="Risk",
		choices=[("", "All risk levels"), *RiskLevel.choices],
		widget=forms.Select(attrs=SELECT_INPUT),
	)
	country = forms.ChoiceField(
		required=False, label="Country", widget=forms.Select(attrs=SELECT_INPUT)
	)
	category = forms.ChoiceField(
		required=False, label="Category", widget=forms.Select(attrs=SELECT_INPUT)
	)
	active = forms.ChoiceField(
		required=False,
		label="Active",
		choices=[
			("", "All suppliers"),
			("active", "Active only"),
			("inactive", "Inactive only"),
		],
		widget=forms.Select(attrs=SELECT_INPUT),
	)

	def __init__(self, *args, company=None, **kwargs):
		super().__init__(*args, **kwargs)
		countries = []
		categories = []
		if company is not None:
			countries = (
				Supplier.objects.filter(company=company)
				.exclude(country="")
				.values_list("country", flat=True)
				.distinct()
				.order_by("country")
			)
			categories = (
				Supplier.objects.filter(company=company)
				.exclude(category="")
				.values_list("category", flat=True)
				.distinct()
				.order_by("category")
			)
		self.fields["country"].choices = [("", "All countries"), *[(c, c) for c in countries]]
		self.fields["category"].choices = [("", "All categories"), *[(c, c) for c in categories]]
