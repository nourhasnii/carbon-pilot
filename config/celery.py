"""Celery application for CarbonPilot's long-running AI jobs.

Only genuinely long jobs run here. The recommendation generation job asks a
local Qwen model over Ollama and can take minutes of CPU time, which is far too
long to hold a web request open. The scikit-learn jobs (anomaly detection,
objective prediction) finish in well under a second and stay synchronous.

The project follows Celery's documented Django integration: this module creates
the app, reads its settings from the ``CELERY_`` namespace in
``config.settings``, and ``config/__init__.py`` imports it so the app is always
loaded before tasks are used.
"""

import os

from celery import Celery

# The worker runs with the same settings module as the web process.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("carbonpilot")

# All Celery configuration lives in Django settings, prefixed with CELERY_.
app.config_from_object("django.conf:settings", namespace="CELERY")

# Pick up tasks.py modules from every installed app.
app.autodiscover_tasks()
