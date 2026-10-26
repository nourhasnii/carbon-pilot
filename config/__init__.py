"""CarbonPilot project package.

The Celery application is imported here so that ``@shared_task`` definitions are
bound to it as soon as Django starts (the documented Celery/Django pattern).
"""

from .celery import app as celery_app

__all__ = ("celery_app",)
