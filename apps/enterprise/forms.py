from django import forms
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.accounts.models import User

from .models import Company, Department


class CompanyForm(forms.ModelForm):
	reports_esg = forms.TypedChoiceField(
		label="Does your company already report ESG?",
		choices=(("yes", "Yes"), ("no", "No")),
		coerce=lambda value: value == "yes",
		widget=forms.RadioSelect(attrs={"class": "form-check-input"}),
	)

	class Meta:
		model = Company
		fields = (
			"name", "legal_name", "siret", "sector", "size",
			"country", "city", "address", "website", "contact_email",
			"reports_esg", "sustainability_goal", "target_year",
		)
		widgets = {
			"name": forms.TextInput(attrs={"autocomplete": "organization"}),
			"legal_name": forms.TextInput(attrs={"autocomplete": "organization"}),
			"address": forms.TextInput(attrs={"autocomplete": "street-address"}),
			"country": forms.TextInput(attrs={"autocomplete": "country-name"}),
			"city": forms.TextInput(attrs={"autocomplete": "address-level2"}),
			"target_year": forms.NumberInput(attrs={"max": "2100"}),
		}

	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)
		if self.instance.pk:
			self.fields["reports_esg"].initial = "yes" if self.instance.reports_esg else "no"
		for name, field in self.fields.items():
			field.required = True
			if name == "reports_esg":
				continue
			widget = field.widget
			widget.attrs["class"] = "form-select" if isinstance(widget, forms.Select) else "form-control"
			widget.attrs["required"] = "required"
		self.fields["target_year"].widget.attrs["min"] = str(timezone.now().year)

	def clean_target_year(self):
		target_year = self.cleaned_data["target_year"]
		if target_year < timezone.now().year:
			raise ValidationError("Enter the current year or a future target year.")
		return target_year


class DepartmentForm(forms.ModelForm):
	class Meta:
		model = Department
		fields = ("name", "code", "description")
		widgets = {
			"name": forms.TextInput(attrs={"class": "form-control", "required": "required"}),
			"code": forms.TextInput(attrs={"class": "form-control"}),
			"description": forms.Textarea(attrs={"class": "form-control", "rows": 4}),
		}


class CompanyInviteForm(forms.Form):
	email = forms.EmailField(widget=forms.EmailInput(attrs={"class": "form-control", "autocomplete": "email"}))
	role = forms.ChoiceField(widget=forms.Select(attrs={"class": "form-select"}))
	department = forms.ChoiceField(required=False, widget=forms.Select(attrs={"class": "form-select"}))

	def __init__(self, company, *args, **kwargs):
		super().__init__(*args, **kwargs)
		self.company = company
		self.fields["role"].choices = [
			(role, label)
			for role, label in User.Role.choices
			if role not in (User.Role.SUPER_ADMIN, User.Role.COMPANY_OWNER)
		]
		department_names = Department.objects.filter(company=company).values_list("name", flat=True)
		self.fields["department"].choices = [("", "No department")] + [
			(name, name) for name in department_names
		]

	def clean_email(self):
		email = self.cleaned_data["email"].strip().lower()
		if User.objects.filter(email__iexact=email).exists():
			raise ValidationError("An account with this email already exists.")
		return email

	def clean(self):
		cleaned_data = super().clean()
		if (
			cleaned_data.get("role") == User.Role.DEPARTMENT_MANAGER
			and not cleaned_data.get("department")
		):
			self.add_error("department", "Select the department this user manages.")
		return cleaned_data

	def save(self):
		user = User(
			email=self.cleaned_data["email"],
			role=self.cleaned_data["role"],
			company=self.company.name,
			company_membership=self.company,
			department=self.cleaned_data["department"],
			status=User.Status.INVITED,
		)
		user.set_unusable_password()
		user.save()
		return user