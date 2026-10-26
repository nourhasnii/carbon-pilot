"""ESG / carbon report assembly and PDF generation.

The report is built **only** from the reporting company's own data, and only
from ``APPROVED``, non-archived carbon activities for every official emission
figure. Anomalies, objectives/ predictions, recommendations, risk assessment
and data quality are reused from the modules that already produce them — no
figure is invented and no other tenant's rows can ever be read, because every
queryset starts from the given ``company``.

Two steps, matching the ``ESGReport`` lifecycle:

* :func:`build_report_data` computes the metrics snapshot and the executive
  summary (used to create a ``DRAFT`` report the user can preview);
* :func:`generate_pdf` renders that snapshot to a PDF with ReportLab and
  attaches it to the report (moving it to ``GENERATED``).
"""

from __future__ import annotations

import io
import uuid

from django.core.files.base import ContentFile
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
	Paragraph,
	SimpleDocTemplate,
	Spacer,
	Table,
	TableStyle,
)

from apps.carbon.models import ActivityStatus, AnomalyLog, CarbonActivity
from apps.carbon.services import analytics, anomaly_detection, data_quality
from apps.objectives.models import Objective, ObjectiveStatus, RiskLevel
from apps.objectives.services import objective_prediction
from apps.recommendations.models import (
	OPEN_STATUSES,
	RecommendationLog,
	RecommendationPriority,
)
from apps.risk.models import RiskAssessment

from ..models import ESGReport, ReportStatus

# How many departments / categories are highlighted in the report.
TOP_N = 5


# --------------------------------------------------------------------------- #
# Data assembly
# --------------------------------------------------------------------------- #
def _to_float(value):
	if value is None:
		return 0.0
	return round(float(value), 2)


def _approved_activities(company, period_year):
	"""Approved, non-archived activities of the company for the reporting year."""
	return CarbonActivity.objects.filter(
		company=company,
		status=ActivityStatus.APPROVED,
		is_archived=False,
		period_start__year=period_year,
	)


def _emissions(company, period_year):
	approved = _approved_activities(company, period_year)
	scopes = analytics.scope_breakdown(approved)
	departments = analytics.department_breakdown(approved)
	categories = analytics.category_breakdown(approved)
	return {
		"approved_activities": approved.count(),
		"total_tonnes": _to_float(analytics.total_tonnes(approved)),
		"scopes": [
			{
				"key": row["key"],
				"label": row["label"],
				"tonnes": _to_float(row["tonnes"]),
				"percent": row["percent"],
			}
			for row in scopes
		],
		"top_departments": [
			{
				"label": row["label"],
				"tonnes": _to_float(row["tonnes"]),
				"percent": row["percent"],
				"activities": row["activities"],
			}
			for row in departments[:TOP_N]
		],
		"top_categories": [
			{
				"label": row["label"],
				"tonnes": _to_float(row["tonnes"]),
				"percent": row["percent"],
				"activities": row["activities"],
			}
			for row in categories[:TOP_N]
		],
	}


def _quality(company, period_year):
	activities = list(_approved_activities(company, period_year).select_related("emission_factor"))
	scored = len(activities)
	missing = sum(1 for activity in activities if not activity.evidence)
	coverage = round((scored - missing) / scored * 100, 1) if scored else 0.0
	return {
		"average": data_quality.average_score(activities),
		"scored": scored,
		"missing_evidence": missing,
		"evidence_coverage": coverage,
	}


def _objectives(company, period_year):
	queryset = (
		Objective.objects.filter(company=company, year=period_year)
		.exclude(status=ObjectiveStatus.ARCHIVED)
		.select_related("department")
	)
	items = []
	achieved = missed = at_risk = 0
	for objective in queryset:
		if objective.status == ObjectiveStatus.ACHIEVED:
			achieved += 1
		if objective.status == ObjectiveStatus.MISSED:
			missed += 1
		run = objective_prediction.latest_prediction(objective)
		prediction = None
		if run is not None:
			prediction = {
				"probability": _to_float(run.probability_achievement),
				"risk_level": run.risk_level,
				"risk_label": run.get_risk_level_display(),
				"outcome": run.get_expected_outcome_display(),
			}
			if run.risk_level in (RiskLevel.HIGH, RiskLevel.MEDIUM):
				at_risk += 1
		items.append({
			"name": objective.name,
			"scope": objective.scope_label,
			"period": objective.period_label,
			"status": objective.status,
			"status_label": objective.get_status_display(),
			"progress_percent": objective.progress_percent,
			"current_tonnes": _to_float(objective.current_tco2e),
			"target_tonnes": _to_float(objective.target_tco2e),
			"prediction": prediction,
		})
	return {
		"total": len(items),
		"achieved": achieved,
		"missed": missed,
		"at_risk": at_risk,
		"items": items,
	}


def _anomalies(company, period_year):
	counts = anomaly_detection.anomaly_counts(
		AnomalyLog.objects.filter(company=company, period_start__year=period_year)
	)
	return {key: int(value or 0) for key, value in counts.items()}


def _recommendations(company):
	open_items = RecommendationLog.objects.filter(
		company=company, status__in=OPEN_STATUSES
	)
	return {
		"open": open_items.count(),
		"high": open_items.filter(priority=RecommendationPriority.HIGH).count(),
	}


def _risk(company, period_year):
	"""Latest risk assessment for the year, falling back to the newest one."""
	assessment = (
		RiskAssessment.objects.filter(company=company, period_year=period_year)
		.latest_first()
		.first()
	)
	if assessment is None:
		assessment = (
			RiskAssessment.objects.filter(company=company, period_year__isnull=True)
			.latest_first()
			.first()
		)
	if assessment is None:
		assessment = RiskAssessment.objects.filter(company=company).latest_first().first()
	if assessment is None:
		return None
	return {
		"score": _to_float(assessment.score),
		"level": assessment.level,
		"level_label": assessment.get_level_display(),
		"summary": assessment.summary,
		"assessed_on": assessment.created_at.strftime("%Y-%m-%d"),
		"drivers": list(assessment.drivers),
	}


def _build_summary(company_name, period_year, emissions, quality, objectives, anomalies, risk):
	parts = [
		f"{company_name} reported {emissions['total_tonnes']:.2f} tCO2e of approved "
		f"emissions in {period_year} across {emissions['approved_activities']} "
		f"approved activit{'y' if emissions['approved_activities'] == 1 else 'ies'}."
	]
	if not emissions["approved_activities"]:
		parts.append(
			"No approved activity data is available for this year, so the figures "
			"below are incomplete."
		)
	parts.append(
		f"Average data quality is {quality['average']}% with evidence attached to "
		f"{quality['evidence_coverage']}% of approved activities."
	)
	parts.append(
		f"{objectives['total']} reduction objectiv"
		f"{'e' if objectives['total'] == 1 else 'es'} tracked "
		f"({objectives['achieved']} achieved, {objectives['at_risk']} predicted at risk)."
	)
	parts.append(
		f"{anomalies['total']} anomal{'y' if anomalies['total'] == 1 else 'ies'} "
		f"detected ({anomalies['high']} high severity)."
	)
	if risk is not None:
		parts.append(
			f"The latest rule-based risk score is {risk['score']:.2f}/100 "
			f"({risk['level_label']})."
		)
	return " ".join(parts)


def build_report_data(company, *, period_year):
	"""Compute the metrics snapshot and executive summary for one company/year.

	Only the given ``company``'s own data is read. Returns ``(metrics, summary)``.
	"""
	if company is None:
		raise ValueError("A company is required to build an ESG report.")

	emissions = _emissions(company, period_year)
	quality = _quality(company, period_year)
	objectives = _objectives(company, period_year)
	anomalies = _anomalies(company, period_year)
	recommendations = _recommendations(company)
	risk = _risk(company, period_year)
	metrics = {
		"period_year": period_year,
		"company": {"id": company.pk, "name": company.name},
		"emissions": emissions,
		"quality": quality,
		"objectives": objectives,
		"anomalies": anomalies,
		"recommendations": recommendations,
		"risk": risk,
	}
	summary = _build_summary(
		company.name, period_year, emissions, quality, objectives, anomalies, risk
	)
	return metrics, summary


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #
def create_draft(company, *, period_year, actor=None, title=None):
	"""Create (or refresh) the preview report for a company/year.

	Any earlier draft for the same year is replaced so the preview always
	reflects the current data, while previously generated reports are kept as
	history.
	"""
	metrics, summary = build_report_data(company, period_year=period_year)
	ESGReport.objects.filter(
		company=company, period_year=period_year, status=ReportStatus.DRAFT
	).delete()
	return ESGReport.objects.create(
		company=company,
		period_year=period_year,
		title=(title or f"ESG & Carbon Report {period_year}"),
		status=ReportStatus.DRAFT,
		summary=summary,
		metrics_json=metrics,
		created_by=actor if getattr(actor, "is_authenticated", False) else None,
	)


def generate_pdf(report):
	"""Render ``report`` to a PDF and attach it (sets the status to GENERATED)."""
	pdf_bytes = render_pdf(report)
	filename = f"esg-report-{report.period_year}-{uuid.uuid4().hex[:8]}.pdf"
	if report.file:
		report.file.delete(save=False)
	report.file.save(filename, ContentFile(pdf_bytes), save=False)
	report.status = ReportStatus.GENERATED
	report.save(update_fields=("file", "status"))
	return report


# --------------------------------------------------------------------------- #
# PDF rendering (ReportLab platypus)
# --------------------------------------------------------------------------- #
BRAND = colors.HexColor("#147d62")
BRAND_DARK = colors.HexColor("#0f5c48")
INK = colors.HexColor("#1c2b27")
MUTED = colors.HexColor("#60716c")
LINE = colors.HexColor("#e2ece8")
MINT = colors.HexColor("#eaf5f0")

_CONTENT_WIDTH = A4[0] - 36 * mm

DISCLAIMER = (
	"This report was generated by CarbonPilot from the company's own approved "
	"activity data. It is a management and decision-support document: it is "
	"<b>not a legal CSRD certification</b>, it has not been audited or assured "
	"by a third party, and it must not be presented as a formal CSRD / ESRS "
	"compliance statement. Official figures include approved activities only; "
	"draft, submitted and rejected activities are excluded."
)


def _styles():
	base = getSampleStyleSheet()
	return {
		"cover_company": ParagraphStyle(
			"cover_company", parent=base["Title"], fontName="Helvetica-Bold",
			fontSize=20, leading=24, textColor=INK, alignment=0, spaceAfter=2,
		),
		"cover_title": ParagraphStyle(
			"cover_title", parent=base["Title"], fontName="Helvetica-Bold",
			fontSize=13, leading=17, textColor=BRAND_DARK, alignment=0, spaceAfter=4,
		),
		"cover_meta": ParagraphStyle(
			"cover_meta", parent=base["BodyText"], fontName="Helvetica",
			fontSize=10, leading=14, textColor=MUTED, alignment=0,
		),
		"h2": ParagraphStyle(
			"h2", parent=base["Heading2"], fontName="Helvetica-Bold",
			fontSize=13, leading=16, textColor=BRAND, spaceBefore=16, spaceAfter=8,
		),
		"body": ParagraphStyle(
			"body", parent=base["BodyText"], fontName="Helvetica",
			fontSize=10, leading=15, textColor=INK, spaceAfter=6,
		),
		"small": ParagraphStyle(
			"small", parent=base["BodyText"], fontName="Helvetica",
			fontSize=8.5, leading=12, textColor=MUTED,
		),
		"note": ParagraphStyle(
			"note", parent=base["BodyText"], fontName="Helvetica",
			fontSize=9, leading=13, textColor=INK,
		),
		"th": ParagraphStyle(
			"th", parent=base["BodyText"], fontName="Helvetica-Bold",
			fontSize=9, leading=12, textColor=BRAND_DARK,
		),
		"td": ParagraphStyle(
			"td", parent=base["BodyText"], fontName="Helvetica",
			fontSize=9, leading=12, textColor=INK,
		),
	}


def _data_table(header, rows, widths):
	styles = _styles()
	data = [[Paragraph(str(cell), styles["th"]) for cell in header]]
	data += [[Paragraph(str(cell), styles["td"]) for cell in row] for row in rows]
	table = Table(data, colWidths=widths, repeatRows=1)
	table.setStyle(TableStyle([
		("BACKGROUND", (0, 0), (-1, 0), MINT),
		("LINEBELOW", (0, 0), (-1, 0), 0.8, BRAND),
		("LINEBELOW", (0, 1), (-1, -1), 0.4, LINE),
		("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
		("TOPPADDING", (0, 0), (-1, -1), 5),
		("BOTTOMPADDING", (0, 0), (-1, -1), 5),
		("LEFTPADDING", (0, 0), (-1, -1), 6),
		("RIGHTPADDING", (0, 0), (-1, -1), 6),
	]))
	return table


def _kpi_row(items, styles):
	"""A horizontal band of label/value boxes."""
	cells = [
		Paragraph(
			f'<font size="7" color="#60716c">{label.upper()}</font><br/>'
			f'<font size="14" color="#1c2b27"><b>{value}</b></font>',
			styles["note"],
		)
		for label, value in items
	]
	table = Table([cells], colWidths=[_CONTENT_WIDTH / len(cells)] * len(cells))
	table.setStyle(TableStyle([
		("BACKGROUND", (0, 0), (-1, -1), MINT),
		("BOX", (0, 0), (-1, -1), 0.4, LINE),
		("INNERGRID", (0, 0), (-1, -1), 0.4, LINE),
		("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
		("TOPPADDING", (0, 0), (-1, -1), 9),
		("BOTTOMPADDING", (0, 0), (-1, -1), 9),
		("LEFTPADDING", (0, 0), (-1, -1), 10),
	]))
	return table


def _cover(report, metrics, styles):
	company_name = metrics.get("company", {}).get("name") or report.company.name
	header = Table(
		[[Paragraph(company_name, styles["cover_company"])],
		 [Paragraph(report.title, styles["cover_title"])],
		 [Paragraph(
			 f"Reporting period: {report.period_year} &nbsp;|&nbsp; "
			 f"Generated on {report.created_at:%Y-%m-%d} &nbsp;|&nbsp; "
			 f"Status: {report.get_status_display()}", styles["cover_meta"],
		 )]],
		colWidths=[_CONTENT_WIDTH],
	)
	header.setStyle(TableStyle([
		("BACKGROUND", (0, 0), (-1, -1), MINT),
		("LINEBEFORE", (0, 0), (0, -1), 4, BRAND),
		("LEFTPADDING", (0, 0), (-1, -1), 16),
		("RIGHTPADDING", (0, 0), (-1, -1), 16),
		("TOPPADDING", (0, 0), (-1, -1), 6),
		("BOTTOMPADDING", (0, 0), (-1, -1), 6),
		("TOPPADDING", (0, 0), (0, 0), 14),
		("BOTTOMPADDING", (0, -1), (-1, -1), 14),
	]))
	return [header, Spacer(1, 14)]


def _executive_summary(report, metrics, styles):
	emissions = metrics.get("emissions", {})
	quality = metrics.get("quality", {})
	risk = metrics.get("risk")
	level_label = risk["level_label"] if risk else "Not assessed"
	kpis = [
		("Total emissions", f"{emissions.get('total_tonnes', 0):.2f} tCO2e"),
		("Approved activities", emissions.get("approved_activities", 0)),
		("Data quality", f"{quality.get('average', 0)}%"),
		("Risk level", level_label),
	]
	return [
		Paragraph("Executive summary", styles["h2"]),
		_kpi_row(kpis, styles),
		Spacer(1, 10),
		Paragraph(report.summary or "No summary was recorded.", styles["body"]),
	]


def _emissions_section(metrics, styles):
	emissions = metrics.get("emissions", {})
	scopes = emissions.get("scopes", [])
	total = emissions.get("total_tonnes", 0.0)
	flow = [
		Paragraph("Emissions overview", styles["h2"]),
		Paragraph(
			f"Official emissions cover <b>{emissions.get('approved_activities', 0)} "
			f"approved activit{'y' if emissions.get('approved_activities', 0) == 1 else 'ies'}</b> "
			f"for the reporting year, totalling "
			f"<b>{total:.2f} tCO2e</b>.", styles["body"],
		),
		_data_table(
			["Scope", "Emissions (tCO2e)", "Share of total"],
			[[row["label"], f"{row['tonnes']:.2f}", f"{row['percent']}%"] for row in scopes],
			[_CONTENT_WIDTH * 0.5, _CONTENT_WIDTH * 0.28, _CONTENT_WIDTH * 0.22],
		),
	]
	if emissions.get("top_departments"):
		flow.append(Spacer(1, 12))
		flow.append(Paragraph("Top departments", styles["note"]))
		flow.append(Spacer(1, 4))
		flow.append(_data_table(
			["Department", "Emissions (tCO2e)", "Share"],
			[[row["label"], f"{row['tonnes']:.2f}", f"{row['percent']}%"]
			 for row in emissions["top_departments"]],
			[_CONTENT_WIDTH * 0.5, _CONTENT_WIDTH * 0.28, _CONTENT_WIDTH * 0.22],
		))
	if emissions.get("top_categories"):
		flow.append(Spacer(1, 12))
		flow.append(Paragraph("Top categories", styles["note"]))
		flow.append(Spacer(1, 4))
		flow.append(_data_table(
			["Category", "Emissions (tCO2e)", "Share"],
			[[row["label"], f"{row['tonnes']:.2f}", f"{row['percent']}%"]
			 for row in emissions["top_categories"]],
			[_CONTENT_WIDTH * 0.5, _CONTENT_WIDTH * 0.28, _CONTENT_WIDTH * 0.22],
		))
	return flow


def _objectives_section(metrics, styles):
	objectives = metrics.get("objectives", {})
	items = objectives.get("items", [])
	flow = [
		Paragraph("Objectives & progress", styles["h2"]),
		Paragraph(
			f"<b>{objectives.get('total', 0)}</b> reduction objective(s): "
			f"{objectives.get('achieved', 0)} achieved, "
			f"{objectives.get('at_risk', 0)} predicted at risk, "
			f"{objectives.get('missed', 0)} missed.", styles["body"],
		),
	]
	if not items:
		flow.append(Paragraph("No reduction objectives were recorded for this year.", styles["small"]))
		return flow
	rows = []
	for item in items:
		prediction = item.get("prediction")
		prediction_text = (
			f"{prediction['probability']:.1f}% ({prediction['risk_label']})"
			if prediction else "No prediction"
		)
		rows.append([
			item["name"],
			item["scope"],
			f"{item['current_tonnes']:.2f} / {item['target_tonnes']:.2f}",
			f"{item['progress_percent']}%",
			item["status_label"],
			prediction_text,
		])
	flow.append(_data_table(
		["Objective", "Scope", "Current / target (tCO2e)", "Progress", "Status", "Latest prediction"],
		rows,
		[_CONTENT_WIDTH * 0.26, _CONTENT_WIDTH * 0.14, _CONTENT_WIDTH * 0.18,
		 _CONTENT_WIDTH * 0.1, _CONTENT_WIDTH * 0.13, _CONTENT_WIDTH * 0.19],
	))
	return flow


def _anomalies_section(metrics, styles):
	anomalies = metrics.get("anomalies", {})
	recommendations = metrics.get("recommendations", {})
	return [
		Paragraph("Anomalies & recommendations", styles["h2"]),
		_kpi_row(
			[
				("Anomalies detected", anomalies.get("total", 0)),
				("High severity", anomalies.get("high", 0)),
				("Medium severity", anomalies.get("medium", 0)),
				("Open recommendations", recommendations.get("open", 0)),
				("High priority", recommendations.get("high", 0)),
			],
			styles,
		),
		Spacer(1, 8),
		Paragraph(
			"Anomalies are flagged by CarbonPilot's IsolationForest model on the "
			"company's own approved activities; recommendations are corrective "
			"actions proposed from those findings and from objectives.", styles["small"],
		),
	]


def _risk_section(metrics, styles):
	risk = metrics.get("risk")
	flow = [Paragraph("Risk assessment", styles["h2"])]
	if not risk:
		flow.append(Paragraph(
			"No risk assessment has been run for this company yet.", styles["small"],
		))
		return flow
	flow.append(_kpi_row(
		[
			("Risk score", f"{risk['score']:.2f} / 100"),
			("Risk level", risk["level_label"]),
			("Method", "Rule-based"),
			("Assessed on", risk.get("assessed_on", "-")),
		],
		styles,
	))
	if risk.get("summary"):
		flow.append(Spacer(1, 10))
		flow.append(Paragraph(risk["summary"], styles["body"]))
	drivers = risk.get("drivers") or []
	if drivers:
		flow.append(Spacer(1, 6))
		flow.append(Paragraph("Ranked risk drivers", styles["note"]))
		flow.append(Spacer(1, 4))
		flow.append(_data_table(
			["Driver", "Points", "Why"],
			[[d.get("label", "-"), f"{float(d.get('points', 0)):.2f} / {d.get('max_points', '-')}",
			  d.get("detail", "")] for d in drivers],
			[_CONTENT_WIDTH * 0.24, _CONTENT_WIDTH * 0.16, _CONTENT_WIDTH * 0.6],
		))
	return flow


def _quality_section(metrics, styles):
	quality = metrics.get("quality", {})
	band = "High confidence" if quality.get("average", 0) >= 80 else (
		"Medium confidence" if quality.get("average", 0) >= 50 else "Low confidence"
	)
	return [
		Paragraph("Data quality notes", styles["h2"]),
		_kpi_row(
			[
				("Average quality", f"{quality.get('average', 0)}%"),
				("Activities scored", quality.get("scored", 0)),
				("Evidence coverage", f"{quality.get('evidence_coverage', 0)}%"),
				("Missing evidence", quality.get("missing_evidence", 0)),
			],
			styles,
		),
		Spacer(1, 8),
		Paragraph(
			f"Data quality reflects the completeness of each approved activity "
			f"(reporting period, department, verified emission factor, source and "
			f"evidence). The current band is <b>{band}</b>. "
			f"{quality.get('missing_evidence', 0)} approved activit"
			f"{'y' if quality.get('missing_evidence', 0) == 1 else 'ies'} "
			"still lack supporting evidence.", styles["small"],
		),
	]


def _disclaimer(styles):
	return [
		Spacer(1, 18),
		Paragraph("Disclaimer", styles["h2"]),
		Paragraph(DISCLAIMER, styles["small"]),
	]


def render_pdf(report):
	"""Render ``report`` to PDF bytes (A4, ReportLab platypus)."""
	metrics = report.metrics
	styles = _styles()
	buffer = io.BytesIO()
	doc = SimpleDocTemplate(
		buffer,
		pagesize=A4,
		leftMargin=18 * mm,
		rightMargin=18 * mm,
		topMargin=16 * mm,
		bottomMargin=16 * mm,
		title=report.title,
		author="CarbonPilot",
		subject=f"ESG & carbon report {report.period_year}",
	)
	story = []
	story += _cover(report, metrics, styles)
	story.append(Spacer(1, 6))
	story += _executive_summary(report, metrics, styles)
	story += _emissions_section(metrics, styles)
	story += _objectives_section(metrics, styles)
	story += _anomalies_section(metrics, styles)
	story += _risk_section(metrics, styles)
	story += _quality_section(metrics, styles)
	story += _disclaimer(styles)
	doc.build(story)
	return buffer.getvalue()
