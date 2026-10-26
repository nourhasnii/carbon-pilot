"""Queue the one long AI job — recommendation generation — on Celery.

Enqueueing is a quick database write plus a broker hand-off, so the request
returns immediately and the browser is never held open for a minutes-long Qwen
inference. Authorisation happens in the view *before* this module is called;
here we only own the job row and the broker hand-off.

Failure modes are reported honestly and never leak technical detail:

* the broker/Redis is unreachable → the job is recorded as ``FAILED`` and the
  caller gets a clear "background service unavailable" message;
* a job for this company is already Pending/Running → nothing new is created,
  and the existing job is returned so the UI can keep polling it.
"""

import logging
from dataclasses import dataclass

from django.conf import settings

from apps.analytics.models import (
	AI_JOB_QUEUE_UNAVAILABLE_MESSAGE,
	AI_JOB_START_FAILED_MESSAGE,
	AiJob,
	JobStatus,
	JobType,
)

logger = logging.getLogger(__name__)

#: Shown when the user asks for a new run while one is already in flight.
ALREADY_RUNNING_MESSAGE = (
	"An AI recommendation job is already running for your company — this page "
	"will update when it finishes."
)
QUEUED_MESSAGE = (
	"AI recommendation job queued. This page will update as soon as it runs."
)

# Exception types that mean "the broker was not reachable" rather than a bug in
# our own code. Matched by name so we do not need to import kombu/redis here.
_BROKER_ERROR_NAMES = frozenset(
	{
		"OperationalError",
		"RedisError",
		"ConnectionError",
		"ConnectionRefusedError",
		"TimeoutError",
	}
)


def _is_broker_error(exc):
	"""True when *exc* looks like a broker/connection failure (Redis down)."""
	return any(
		klass.__name__ in _BROKER_ERROR_NAMES for klass in type(exc).__mro__
	)


@dataclass
class EnqueueResult:
	ok: bool
	job: AiJob = None
	reused: bool = False
	message: str = ""


def enqueue_recommendation_generation(user, company):
	"""Create the job row and hand it to Celery. Returns immediately."""
	if company is None:
		return EnqueueResult(
			ok=False, message="No company is associated with this account."
		)

	existing = AiJob.objects.latest_active(
		company, JobType.RECOMMENDATION_GENERATION
	)
	if existing is not None:
		return EnqueueResult(
			ok=True, job=existing, reused=True, message=ALREADY_RUNNING_MESSAGE
		)

	job = AiJob.objects.create(
		company=company,
		created_by=user if getattr(user, "is_authenticated", False) else None,
		job_type=JobType.RECOMMENDATION_GENERATION,
		status=JobStatus.PENDING,
	)

	# Imported lazily so that loading the views/services never pulls in the
	# Celery task module (and Celery itself) unless a job is actually queued.
	from ..tasks import run_recommendation_generation

	try:
		async_result = run_recommendation_generation.delay(job.pk)
	except Exception as exc:  # noqa: BLE001 — reported to the user, not raised.
		broker_down = _is_broker_error(exc)
		message = (
			AI_JOB_QUEUE_UNAVAILABLE_MESSAGE
			if broker_down
			else AI_JOB_START_FAILED_MESSAGE
		)
		error_code = "broker_unavailable" if broker_down else "enqueue_failed"
		logger.warning(
			"[AI JOB] enqueue failed job_id=%s error_code=%s error=%r",
			job.pk,
			error_code,
			exc,
		)
		job.mark_failed(message, error_code)
		return EnqueueResult(ok=False, job=job, message=message)

	task_id = (getattr(async_result, "id", "") or "")[:255]
	if task_id:
		job.celery_task_id = task_id
		job.save(update_fields=("celery_task_id",))

	if settings.CELERY_TASK_ALWAYS_EAGER:
		# Eager mode (tests / CELERY_TASK_ALWAYS_EAGER=true) runs the task
		# inline, so it has already finished by now — reload the row so the
		# caller reports the real outcome instead of a stale "queued".
		job.refresh_from_db(
			fields=("status", "message", "error_code", "result", "finished_at")
		)

	return EnqueueResult(ok=True, job=job, message=QUEUED_MESSAGE)
