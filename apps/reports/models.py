"""ESG / carbon report snapshots.

An :class:`ESGReport` is a company's ESG & carbon report for one reporting
year. It is built exclusively from that tenant's own data — approved carbon
activities, objectives and their predictions, anomalies, recommendations and
the latest risk assessment — and can be exported to a PDF.

Every row belongs to exactly one company and is never shared across tenants.
"""

import os
import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.enterprise.models import Company


class ReportStatus(models.TextChoices):
	DRAFT = "DRAFT", "Draft"
	GENERATED = "GENERATED", "Generated"


def report_pdf_upload_to(instance, filename):
	"""Store each report under its own company/year folder."""
	company_id = instance.company_id or "unassigned"
	year = instance.period_year or timezone.localdate().year
	extension = os.path.splitext(filename)[1].lower() or ".pdf"
	return f"esg_reports/company_{company_id}/{year}/{uuid.uuid4().hex}{extension}"


class ESGReportQuerySet(models.QuerySet):
	def for_company(self, company):
		return self.filter(company=company)

	def latest_first(self):
		return self.order_by("-created_at", "-id")


class ESGReport(models.Model):
	"""One ESG & carbon report for a company and a reporting year.

	``metrics_json`` is an immutable snapshot of the figures used to build the
	report, so a generated PDF can always be traced back to the data it was
	built from. ``file`` stays empty until the PDF is generated.
	"""

	class Meta:
		ordering = ("-created_at", "-id")
		indexes = [
			models.Index(fields=("company", "-created_at")),
			models.Index(fields=("company", "period_year")),
		]

	company = models.ForeignKey(
		Company, on_delete=models.CASCADE, related_name="esg_reports"
	)
	period_year = models.PositiveSmallIntegerField(
		help_text="Reporting year the report covers."
	)
	title = models.CharField(max_length=255)
	status = models.CharField(
		max_length=16, choices=ReportStatus.choices, default=ReportStatus.DRAFT
	)
	summary = models.TextField(
		blank=True, help_text="Plain-language executive summary of the reporting year."
	)
	metrics_json = models.JSONField(
		default=dict,
		blank=True,
		help_text="Snapshot of emissions, quality, objectives, anomalies, "
		"recommendations and risk used to build the report.",
	)
	file = models.FileField(
		upload_to=report_pdf_upload_to,
		null=True,
		blank=True,
		help_text="Generated PDF; empty until the report is generated.",
	)
	created_by = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		null=True,
		blank=True,
		on_delete=models.SET_NULL,
		related_name="esg_reports_created",
	)
	created_at = models.DateTimeField(auto_now_add=True)

	objects = ESGReportQuerySet.as_manager()

	def __str__(self):
		return f"{self.title} · {self.get_status_display()} · company {self.company_id}"

	@property
	def is_generated(self):
		return self.status == ReportStatus.GENERATED and bool(self.file)

	@property
	def metrics(self):
		"""The stored metrics snapshot (empty dict when none was recorded)."""
		return dict(self.metrics_json or {})

	@property
	def filename(self):
		return os.path.basename(self.file.name) if self.file else ""
