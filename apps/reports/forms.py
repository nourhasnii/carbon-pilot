"""GET filter forms for the ESG / carbon report module."""

from django import forms

from .models import ReportStatus


class ReportFilterForm(forms.Form):
    """Filter bar for the company-scoped report history list.

    The reporting-year choices are built from the reports that already exist
    for the caller's own company, so the dropdown never offers a year that no
    report covers. All fields are optional.
    """

    period_year = forms.ChoiceField(
        required=False,
        label="Year",
        choices=[("", "All years")],
        widget=forms.Select(attrs={"class": "field-input"}),
    )
    status = forms.ChoiceField(
        required=False,
        label="Status",
        choices=[("", "All statuses"), *ReportStatus.choices],
        widget=forms.Select(attrs={"class": "field-input"}),
    )

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        years = (
            company.esg_reports.order_by("-period_year")
            .values_list("period_year", flat=True)
            .distinct()
            if company is not None
            else []
        )
        self.fields["period_year"].choices = [
            ("", "All years"),
            *((str(year), str(year)) for year in years),
        ]
