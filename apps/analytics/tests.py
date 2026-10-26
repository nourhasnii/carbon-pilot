"""Tests for the background AI job model and its staleness safety net.

These cover the parts of the async job that do not need Celery or Redis: the
status lifecycle, tenant scoping, and the timeouts that turn a dead worker into
a clear ``FAILED`` state for the user.
"""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import User
from apps.enterprise.models import Company

from .models import AiJob, JobStatus, JobType


class AiJobTestCase(TestCase):
	password = "SecureTestPassword2026!"

	def setUp(self):
		self.owner = User.objects.create_user(
			email="aijob-owner@carbonpilot.test",
			password=self.password,
			role=User.Role.COMPANY_OWNER,
			status=User.Status.ACTIVE,
		)
		self.company = Company.objects.create(owner=self.owner, name="AI Jobs Ltd")

		self.other_owner = User.objects.create_user(
			email="aijob-other@carbonpilot.test",
			password=self.password,
			role=User.Role.COMPANY_OWNER,
			status=User.Status.ACTIVE,
		)
		self.other_company = Company.objects.create(
			owner=self.other_owner, name="Other AI Tenant"
		)

	def _job(self, **kwargs):
		defaults = {
			"company": self.company,
			"created_by": self.owner,
			"job_type": JobType.RECOMMENDATION_GENERATION,
		}
		defaults.update(kwargs)
		return AiJob.objects.create(**defaults)

	def _age(self, job, seconds):
		"""Back-date ``created_at`` (it is ``auto_now_add``)."""
		AiJob.objects.filter(pk=job.pk).update(
			created_at=timezone.now() - timedelta(seconds=seconds)
		)
		job.refresh_from_db()

	def test_mark_running_only_moves_a_pending_job(self):
		job = self._job()
		self.assertTrue(job.mark_running())
		self.assertEqual(job.status, JobStatus.RUNNING)
		self.assertIsNotNone(job.started_at)

		# Already running: a duplicate delivery must not restart it.
		self.assertFalse(job.mark_running())

	def test_mark_done_makes_the_job_terminal(self):
		job = self._job()
		job.mark_running()
		job.mark_done("2 recommendations created.", {"created": 2})

		self.assertEqual(job.status, JobStatus.DONE)
		self.assertTrue(job.is_terminal)
		self.assertFalse(job.is_active)
		self.assertIsNotNone(job.finished_at)
		self.assertEqual(job.result, {"created": 2})

	def test_mark_failed_records_a_technical_error_code(self):
		job = self._job()
		job.mark_running()
		job.mark_failed("The AI job stopped unexpectedly.", "task_error")

		self.assertEqual(job.status, JobStatus.FAILED)
		self.assertEqual(job.error_code, "task_error")
		self.assertTrue(job.is_terminal)

	def test_refresh_stale_leaves_a_fresh_job_alone(self):
		job = self._job()
		self.assertFalse(job.refresh_stale())
		job.refresh_from_db()
		self.assertEqual(job.status, JobStatus.PENDING)

	def test_refresh_stale_fails_a_pending_job_no_worker_picked_up(self):
		job = self._job()
		self._age(job, 600)

		self.assertTrue(job.refresh_stale(pending_grace_seconds=90))
		job.refresh_from_db()
		self.assertEqual(job.status, JobStatus.FAILED)
		self.assertEqual(job.error_code, "worker_unavailable")

	def test_refresh_stale_fails_a_running_job_that_outlived_the_timeout(self):
		job = self._job(status=JobStatus.RUNNING)
		self._age(job, 100_000)

		self.assertTrue(job.refresh_stale(timeout_seconds=60))
		job.refresh_from_db()
		self.assertEqual(job.status, JobStatus.FAILED)
		self.assertEqual(job.error_code, "job_timeout")

	def test_refresh_stale_ignores_a_finished_job(self):
		job = self._job()
		job.mark_running()
		job.mark_done("done")
		self._age(job, 100_000)

		self.assertFalse(job.refresh_stale())
		job.refresh_from_db()
		self.assertEqual(job.status, JobStatus.DONE)

	def test_latest_active_is_tenant_scoped(self):
		mine = self._job()
		self._job(company=self.other_company, created_by=self.other_owner)

		self.assertEqual(
			AiJob.objects.latest_active(self.company, JobType.RECOMMENDATION_GENERATION),
			mine,
		)

	def test_latest_active_returns_none_for_a_finished_job(self):
		job = self._job()
		job.mark_running()
		job.mark_done("done")

		self.assertIsNone(
			AiJob.objects.latest_active(self.company, JobType.RECOMMENDATION_GENERATION)
		)

	def test_latest_active_drops_a_job_whose_worker_died(self):
		job = self._job(status=JobStatus.RUNNING)
		self._age(job, 100_000)

		self.assertIsNone(
			AiJob.objects.latest_active(
				self.company,
				JobType.RECOMMENDATION_GENERATION,
				timeout_seconds=60,
			)
		)
		job.refresh_from_db()
		self.assertEqual(job.status, JobStatus.FAILED)

	def test_latest_active_returns_none_without_a_company(self):
		self.assertIsNone(AiJob.objects.latest_active(None))
