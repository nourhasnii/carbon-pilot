"""Celery task for the one long-running AI job: recommendation generation.

Only AI recommendation generation is asynchronous — it is a local Qwen
inference call that can take minutes. Every other AI feature (and every
scikit-learn job) keeps running synchronously inside the request, because it is
fast and gains nothing from a worker.

The task never raises out to the broker: any failure is recorded on the
:class:`~apps.analytics.models.AiJob` row as a clear, non-technical ``FAILED``
state so the UI always reaches a terminal status. The real technical detail
stays in the worker log.
"""

import logging

from celery import shared_task

from apps.analytics.models import AI_JOB_TASK_ERROR_MESSAGE, AiJob

from .services import recommendations

logger = logging.getLogger(__name__)


def _result_summary(result):
	"""A small, JSON-safe summary of a :class:`GenerationResult`.

	``result.created`` holds ``RecommendationLog`` instances, which cannot be
	serialised into the job's ``JSONField`` — so only counts and ids are kept.
	"""
	created = list(getattr(result, "created", []) or [])
	return {
		"method": getattr(result, "method", "") or "",
		"created": len(created),
		"created_ids": [getattr(item, "pk", None) for item in created],
		"rejected": int(getattr(result, "rejected", 0) or 0),
		"skipped": int(getattr(result, "skipped", 0) or 0),
		"ai_unavailable": bool(getattr(result, "ai_unavailable", False)),
	}


@shared_task(name="recommendations.run_recommendation_generation")
def run_recommendation_generation(job_id):
	"""Run one recommendation generation and record the outcome on its job."""
	job = (
		AiJob.objects.select_related("company", "created_by")
		.filter(pk=job_id)
		.first()
	)
	if job is None:
		# The row was deleted (company removed) before the worker got to it.
		logger.warning("[AI JOB] job_id=%s not found; nothing to run", job_id)
		return

	# A duplicate delivery (or a job already failed by the staleness check) must
	# not re-run: only a Pending job may start.
	if not job.mark_running():
		logger.info(
			"[AI JOB] job_id=%s skipped (status=%s is not pending)",
			job_id,
			job.status,
		)
		return

	company = job.company
	user = job.created_by

	# ``run_generation`` expects a usable user object; a job whose author was
	# deleted keeps the company scope, so fall back to the company owner.
	if user is None or not getattr(user, "is_authenticated", False):
		user = company.owner

	try:
		result = recommendations.run_generation(user, company)
	except Exception:  # noqa: BLE001 — the worker must never crash on a job.
		logger.exception("[AI JOB] job_id=%s raised", job_id)
		job.mark_failed(AI_JOB_TASK_ERROR_MESSAGE, "task_error")
		return

	if not result.ok:
		# A domain-level refusal (e.g. no company) — report it as-is.
		job.mark_failed(result.message or AI_JOB_TASK_ERROR_MESSAGE, "run_refused")
		return

	# Success. When ``ai_unavailable`` is set the engine fell back to
	# deterministic rules; the flag is carried in the stored summary so the UI
	# can be honest about the provenance of the result.
	job.mark_done(result.message, _result_summary(result))
