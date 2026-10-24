try:
	from .celery import app as celery_app
except ImportError:  # pragma: no cover - Celery is optional in local setup
	celery_app = None

__all__ = ("celery_app",)