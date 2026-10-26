from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm
from django.core.exceptions import ValidationError

from .models import User


class UserLoginForm(AuthenticationForm):
    error_messages = {
        **AuthenticationForm.error_messages,
        "pending": "Your account is awaiting approval from the administrator.",
        "rejected": "Your account request has been rejected.",
        "inactive": "Your account is currently inactive.",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["username"].label = "Work email"
        self.fields["username"].widget = forms.EmailInput(
            attrs={
                "id": "login-email",
                "class": "auth-input",
                "autocomplete": "username",
                "placeholder": "name@company.com",
            }
        )
        self.fields["password"].widget.attrs.update(
            {"id": "login-password", "class": "auth-input password-input"}
        )

    def clean(self):
        email = self.cleaned_data.get("username")
        password = self.cleaned_data.get("password")
        if email and password:
            user = get_user_model().objects.filter(email__iexact=email).first()
            if user and user.check_password(password) and user.status != User.Status.ACTIVE:
                message_key = {
                    User.Status.PENDING: "pending",
                    User.Status.REJECTED: "rejected",
                    User.Status.INACTIVE: "inactive",
                }.get(user.status)
                if message_key:
                    raise ValidationError(self.error_messages[message_key], code=message_key)
        return super().clean()


class CreateUserForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ("first_name", "last_name", "email", "role", "company", "department")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["role"].choices = [
            choice for choice in User.Role.choices if choice[0] != User.Role.SUPER_ADMIN
        ]
        self.fields["company"].required = True

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        if User.objects.filter(email__iexact=email).exclude(pk=self.instance.pk).exists():
            raise ValidationError("An account with this email already exists.")
        return email

    def clean(self):
        cleaned_data = super().clean()
        if (
            cleaned_data.get("role") == User.Role.DEPARTMENT_MANAGER
            and not cleaned_data.get("department")
        ):
            self.add_error("department", "Enter the department this user manages.")
        return cleaned_data

    def save(self, commit=True):
        user = super().save(commit=False)
        user.status = User.Status.PENDING
        user.set_unusable_password()
        user.company_membership = self._matching_company(user.company)
        if commit:
            user.save()
        return user

    @staticmethod
    def _matching_company(company_name):
        from apps.enterprise.models import Company

        companies = Company.objects.filter(name__iexact=company_name)
        return companies.first() if companies.count() == 1 else None


class SignupForm(forms.ModelForm):
    terms = forms.BooleanField(label="I agree to the Terms of Service and Privacy Policy.")

    class Meta:
        model = User
        fields = ("first_name", "last_name", "email", "company", "department", "role")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["role"].choices = [
            choice for choice in User.Role.choices if choice[0] != User.Role.SUPER_ADMIN
        ]
        self.fields["company"].required = True
        for name in ("first_name", "last_name", "email", "company", "department"):
            self.fields[name].widget.attrs["class"] = "auth-input"
        self.fields["first_name"].widget.attrs["id"] = "first-name"
        self.fields["last_name"].widget.attrs["id"] = "last-name"
        self.fields["email"].widget.attrs.update({"id": "signup-email", "type": "email"})
        self.fields["company"].widget.attrs["id"] = "company-name"
        self.fields["department"].widget.attrs["id"] = "department-name"
        self.fields["terms"].widget.attrs["class"] = "terms-checkbox"
        self.fields["role"].initial = User.Role.ESG_REPORTING_MANAGER

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
            self.add_error("department", "Enter the department you manage.")
        return cleaned_data

    def save(self, commit=True):
        user = super().save(commit=False)
        user.status = User.Status.PENDING
        user.set_unusable_password()
        user.company_membership = CreateUserForm._matching_company(user.company)
        if commit:
            user.save()
        return user


class UserFilterForm(forms.Form):
    """Filter bar for the platform user list (Super Admin).

    All fields are optional. The department choices are derived from the
    values already present on the queryset the list view is showing, so the
    dropdown never invents a department that no account belongs to.
    """

    q = forms.CharField(
        required=False,
        label="Search",
        widget=forms.TextInput(
            attrs={"class": "field-input", "placeholder": "Name or email…"}
        ),
    )
    role = forms.ChoiceField(
        required=False,
        label="Role",
        choices=[("", "All roles"), *User.Role.choices],
        widget=forms.Select(attrs={"class": "field-input"}),
    )
    status = forms.ChoiceField(
        required=False,
        label="Status",
        choices=[("", "All statuses"), *User.Status.choices],
        widget=forms.Select(attrs={"class": "field-input"}),
    )
    department = forms.ChoiceField(
        required=False,
        label="Department",
        choices=[("", "All departments")],
        widget=forms.Select(attrs={"class": "field-input"}),
    )

    def __init__(self, *args, queryset=None, **kwargs):
        super().__init__(*args, **kwargs)
        departments = (
            queryset.exclude(department="")
            .order_by("department")
            .values_list("department", flat=True)
            .distinct()
            if queryset is not None
            else []
        )
        self.fields["department"].choices = [
            ("", "All departments"),
            *((value, value) for value in departments),
        ]