"""Stored evaluation snapshots for CarbonPilot's machine-learning models.

Two real scikit-learn models run inside CarbonPilot — a supervised
``RandomForestClassifier`` (objective achievement prediction) and an
unsupervised ``IsolationForest`` (carbon anomaly detection). Neither model
reports a score until it has actually been fitted, so this app stores what each
run did: the data it saw, the hyper-parameters it used, and — for the
supervised model — its evaluation metrics.

The point of these rows is honesty. A model that never trained (not enough
resolved history) records ``insufficient_data=True`` instead of inventing a
metric, and every metric is stored against the exact training set it was
computed from. Rows are append-only snapshots: a new run adds a row, it never
rewrites history.
"""

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.enterprise.models import Company


class ModelKind(models.TextChoices):
	"""The CarbonPilot models whose runs are recorded here."""

	OBJECTIVE_PREDICTION = "OBJECTIVE_PREDICTION", "Objective prediction (RandomForest)"
	ANOMALY_DETECTION = "ANOMALY_DETECTION", "Anomaly detection (IsolationForest)"


class CompanyScopedQuerySet(models.QuerySet):
	"""Shared base queryset: every tenant-owned row is read per company."""

	def for_company(self, company):
		return self.filter(company=company)


class ModelMetrics(models.Model):
	"""One stored snapshot of what a model run saw and how it scored.

	Supervised fields (``accuracy`` … ``f1``, ``training_size``,
	``class_balance``) are only meaningful for ``OBJECTIVE_PREDICTION`` and stay
	``NULL``/empty for the unsupervised anomaly model. Run fields
	(``activities_analyzed`` … ``anomalies_found``) are likewise only used by
	``ANOMALY_DETECTION``. Both kinds share ``params`` (the hyper-parameters that
	were actually used) and ``insufficient_data``/``note`` to record an honest
	"could not train" outcome.
	"""

	class Meta:
		ordering = ("-created_at", "-id")
		indexes = [
			models.Index(fields=("company", "model_kind", "-created_at")),
		]
		verbose_name = "model metrics"
		verbose_name_plural = "model metrics"

	company = models.ForeignKey(
		Company, on_delete=models.CASCADE, related_name="model_metrics"
	)
	model_kind = models.CharField(max_length=32, choices=ModelKind.choices)
	model_name = models.CharField(max_length=64, blank=True, default="")
	model_version = models.CharField(max_length=32, blank=True, default="")

	# --- Supervised evaluation (RandomForest) ------------------------------- #
	# Metrics are computed on the resolved objectives the model was fitted on
	# and stored as percentages (0–100), like the prediction probability.
	accuracy = models.DecimalField(
		max_digits=7, decimal_places=4, null=True, blank=True
	)
	precision = models.DecimalField(
		max_digits=7, decimal_places=4, null=True, blank=True
	)
	recall = models.DecimalField(
		max_digits=7, decimal_places=4, null=True, blank=True
	)
	f1 = models.DecimalField(max_digits=7, decimal_places=4, null=True, blank=True)
	training_size = models.PositiveIntegerField(
		default=0, help_text="Resolved objectives used to fit the model."
	)
	class_balance = models.JSONField(
		default=dict,
		blank=True,
		help_text='Label counts, e.g. {"achieved": 5, "missed": 4}.',
	)

	# --- Run counts (IsolationForest) --------------------------------------- #
	activities_analyzed = models.PositiveIntegerField(default=0)
	groups_analyzed = models.PositiveIntegerField(default=0)
	groups_skipped = models.PositiveIntegerField(default=0)
	anomalies_found = models.PositiveIntegerField(default=0)

	# --- Shared ------------------------------------------------------------- #
	params = models.JSONField(
		default=dict,
		blank=True,
		help_text="Hyper-parameters actually used for this run.",
	)
	insufficient_data = models.BooleanField(
		default=False,
		help_text="True when there was not enough data to train a model.",
	)
	note = models.TextField(blank=True, default="")
	created_at = models.DateTimeField(auto_now_add=True)

	objects = CompanyScopedQuerySet.as_manager()

	def __str__(self):
		return f"{self.get_model_kind_display()} · company {self.company_id}"

	@classmethod
	def latest(cls, company, model_kind):
		"""Most recent snapshot for one company and model kind, or ``None``."""
		if company is None:
			return None
		return (
			cls.objects.for_company(company).filter(model_kind=model_kind).first()
		)


# --------------------------------------------------------------------------- #
# Background AI jobs
# --------------------------------------------------------------------------- #
class JobType(models.TextChoices):
	"""Long-running AI work that runs in a Celery worker."""

	RECOMMENDATION_GENERATION = (
		"RECOMMENDATION_GENERATION",
		"AI recommendation generation",
	)


class JobStatus(models.TextChoices):
	"""Job lifecycle shown to the user: Pending → Running → Done/Failed."""

	PENDING = "PENDING", "Pending"
	RUNNING = "RUNNING", "Running"
	DONE = "DONE", "Done"
	FAILED = "FAILED", "Failed"


ACTIVE_JOB_STATUSES = (JobStatus.PENDING, JobStatus.RUNNING)

# User-facing messages. They never contain stack traces or technical detail;
# the technical cause stays in the worker log and in ``error_code``.
AI_JOB_TIMEOUT_MESSAGE = (
	"This AI job did not finish in time and was stopped, so no result is shown. "
	"Nothing was changed — please try again."
)
AI_JOB_WORKER_UNAVAILABLE_MESSAGE = (
	"This AI job was queued but no background worker picked it up, so it was "
	"stopped and no result is shown. Nothing was changed — ask an administrator "
	"to check that the Celery worker is running, then try again."
)
AI_JOB_QUEUE_UNAVAILABLE_MESSAGE = (
	"The AI job could not be queued because the background job service is "
	"unavailable, so nothing was started. Nothing was changed — ask an "
	"administrator to check that Redis and the Celery worker are running, then "
	"try again."
)
AI_JOB_START_FAILED_MESSAGE = (
	"The AI job could not be started. Nothing was changed — please try again."
)
AI_JOB_TASK_ERROR_MESSAGE = (
	"The AI job stopped unexpectedly, so no result is shown. Nothing was "
	"changed — please try again."
)


class AiJobManager(models.Manager.from_queryset(CompanyScopedQuerySet)):
	"""Manager for :class:`AiJob`, tenant-scoped like every other model."""

	def latest_active(self, company, job_type=None, *, timeout_seconds=None):
		"""Newest job still Pending/Running, or ``None``.

		A job that has been idle longer than the timeout is first marked
		``FAILED``; it is then no longer "active news" and ``None`` is returned
		so a stale entry never sticks to the page.
		"""
		if company is None:
			return None
		queryset = self.for_company(company).filter(status__in=ACTIVE_JOB_STATUSES)
		if job_type:
			queryset = queryset.filter(job_type=job_type)
		job = queryset.first()
		if job is not None and job.refresh_stale(timeout_seconds=timeout_seconds):
			return None
		return job


class AiJob(models.Model):
	"""One long-running AI job, tied to a company and the user who started it.

	The row is the single source of truth for the UI: the Celery worker updates
	``status``/``message``/``result``, and the page polls this row. That keeps
	tenant isolation intact — the row carries the company, so a status request
	can be scoped exactly like any other object in the platform.
	"""

	class Meta:
		ordering = ("-created_at", "-id")
		indexes = [
			models.Index(fields=("company", "job_type", "-created_at")),
		]
		verbose_name = "AI job"
		verbose_name_plural = "AI jobs"

	company = models.ForeignKey(
		Company, on_delete=models.CASCADE, related_name="ai_jobs"
	)
	created_by = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		on_delete=models.SET_NULL,
		null=True,
		blank=True,
		related_name="ai_jobs",
	)
	job_type = models.CharField(max_length=32, choices=JobType.choices)
	status = models.CharField(
		max_length=16, choices=JobStatus.choices, default=JobStatus.PENDING
	)
	celery_task_id = models.CharField(max_length=255, blank=True, default="")
	message = models.TextField(blank=True, default="")
	error_code = models.CharField(max_length=64, blank=True, default="")
	result = models.JSONField(
		default=dict, blank=True, help_text="Small JSON summary of the outcome."
	)
	created_at = models.DateTimeField(auto_now_add=True)
	started_at = models.DateTimeField(null=True, blank=True)
	finished_at = models.DateTimeField(null=True, blank=True)

	objects = AiJobManager()

	def __str__(self):
		return f"{self.get_job_type_display()} #{self.pk} · {self.status}"

	@property
	def is_terminal(self):
		return self.status in (JobStatus.DONE, JobStatus.FAILED)

	@property
	def is_active(self):
		return self.status in ACTIVE_JOB_STATUSES

	def mark_running(self):
		"""Move Pending → Running. Never resurrects a finished job."""
		if self.status != JobStatus.PENDING:
			return False
		self.status = JobStatus.RUNNING
		self.started_at = timezone.now()
		self.save(update_fields=("status", "started_at"))
		return True

	def mark_done(self, message, result=None):
		self.status = JobStatus.DONE
		self.message = message or ""
		self.result = result or {}
		self.error_code = ""
		self.finished_at = timezone.now()
		self.save(
			update_fields=("status", "message", "result", "error_code", "finished_at")
		)

	def mark_failed(self, message, error_code=""):
		self.status = JobStatus.FAILED
		self.message = message or ""
		self.error_code = error_code or ""
		self.finished_at = timezone.now()
		self.save(update_fields=("status", "message", "error_code", "finished_at"))

	def refresh_stale(self, *, timeout_seconds=None, pending_grace_seconds=None, now=None):
		"""Fail a job that hung longer than a run can possibly take.

		This is the timeout safety net: it does not depend on the worker still
		being alive (a dead worker writes nothing), so a job whose worker died
		always ends up in a clear ``FAILED`` state for the user.

		Two distinct situations are reported honestly:

		* still ``PENDING`` past the (short) queue grace → no worker consumed
		  the queue at all ("worker unavailable");
		* ``RUNNING``/``PENDING`` past the full run timeout → the worker died or
		  the task was lost mid-run ("timed out").
		"""
		if not self.is_active:
			return False
		now = now or timezone.now()
		elapsed = (now - self.created_at).total_seconds()

		if self.status == JobStatus.PENDING:
			grace = (
				pending_grace_seconds
				if pending_grace_seconds is not None
				else settings.AI_JOB_QUEUE_GRACE_SECONDS
			)
			if grace and grace > 0 and elapsed > grace:
				self.mark_failed(
					AI_JOB_WORKER_UNAVAILABLE_MESSAGE, "worker_unavailable"
				)
				return True
			return False

		timeout = (
			timeout_seconds
			if timeout_seconds is not None
			else settings.AI_JOB_TIMEOUT_SECONDS
		)
		if not timeout or timeout <= 0:
			return False
		if elapsed <= timeout:
			return False
		self.mark_failed(AI_JOB_TIMEOUT_MESSAGE, "job_timeout")
		return True

	def as_status_payload(self):
		"""Small JSON payload for the polling endpoint."""
		return {
			"id": self.pk,
			"job_type": self.job_type,
			"status": self.status,
			"status_label": self.get_status_display(),
			"message": self.message,
			"error_code": self.error_code,
			"is_active": self.is_active,
			"is_terminal": self.is_terminal,
			"created_at": self.created_at.isoformat() if self.created_at else None,
			"started_at": self.started_at.isoformat() if self.started_at else None,
			"finished_at": self.finished_at.isoformat() if self.finished_at else None,
			"result": self.result or {},
		}
