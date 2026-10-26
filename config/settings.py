"""
Django settings for CarbonPilot project.
"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / '.env', override=False)


# Production deployments must provide DJANGO_SECRET_KEY and turn debug off.
DEBUG = os.environ.get('DJANGO_DEBUG', 'True').lower() == 'true'
SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY') or (
    'django-insecure-carbonpilot-local-development-only' if DEBUG else ''
)
ALLOWED_HOSTS = [
    host.strip()
    for host in os.environ.get('DJANGO_ALLOWED_HOSTS', 'localhost,127.0.0.1').split(',')
    if host.strip()
]


# Application definition

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',

    # Third party
    'rest_framework',
    'drf_spectacular',

    # CarbonPilot apps
    'apps.accounts.apps.AccountsConfig',
    'apps.enterprise.apps.EnterpriseConfig',
    'apps.carbon.apps.CarbonConfig',
    'apps.objectives.apps.ObjectivesConfig',
    'apps.engagement.apps.EngagementConfig',
    'apps.analytics.apps.AnalyticsConfig',
    'apps.recommendations.apps.RecommendationsConfig',
    'apps.risk.apps.RiskConfig',
    'apps.reports.apps.ReportsConfig',
    'apps.suppliers.apps.SuppliersConfig',
    'apps.assistant.apps.AssistantConfig',
    'apps.governance.apps.GovernanceConfig',
    'apps.audits.apps.AuditsConfig',
    'apps.esg.apps.EsgConfig',
    'apps.energy.apps.EnergyConfig',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'apps.accounts.middleware.PasswordChangeRequiredMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR, BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'apps.accounts.context_processors.notifications',
                'apps.energy.context_processors.energy_sidebar',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'


# Database
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
}


# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]


# Internationalization
LANGUAGE_CODE = 'fr-fr'
TIME_ZONE = 'Africa/Tunis'
USE_I18N = True
USE_TZ = True

LOGIN_URL = '/login/'
LOGIN_REDIRECT_URL = '/dashboard/'
LOGOUT_REDIRECT_URL = '/login/'
AUTH_USER_MODEL = 'accounts.User'
AUTHENTICATION_BACKENDS = ['apps.accounts.backends.CompanyAccessModelBackend']

SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SAMESITE = 'Lax'
CSRF_COOKIE_SECURE = not DEBUG
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = 'DENY'

SUPER_ADMIN_EMAIL = os.environ.get('SUPER_ADMIN_EMAIL', '')
SUPER_ADMIN_PASSWORD = os.environ.get('SUPER_ADMIN_PASSWORD', '')
BREVO_API_KEY = os.environ.get('BREVO_API_KEY', '')
DEFAULT_FROM_EMAIL = os.environ.get('DEFAULT_FROM_EMAIL', '')
EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'
APP_BASE_URL = os.environ.get('APP_BASE_URL', 'http://127.0.0.1:8000').rstrip('/')

# Local AI (Ollama). CarbonPilot's recommendation engine talks to a model that
# runs locally on the operator's machine — tenant data is never sent to a cloud
# AI service. Overridable through the environment; no credentials are involved.
# 127.0.0.1 (not "localhost") avoids the Windows IPv6 ::1 vs IPv4 mismatch.
OLLAMA_URL = os.environ.get('OLLAMA_URL', 'http://127.0.0.1:11434/api/generate')
OLLAMA_MODEL = os.environ.get('OLLAMA_MODEL', 'qwen2.5:7b')
# Local CPU generation of a full recommendation set can take minutes.
OLLAMA_TIMEOUT = float(os.environ.get('OLLAMA_TIMEOUT', '900'))
OLLAMA_NUM_CTX = int(os.environ.get('OLLAMA_NUM_CTX', '4096'))
OLLAMA_NUM_PREDICT = int(os.environ.get('OLLAMA_NUM_PREDICT', '1024'))
OLLAMA_KEEP_ALIVE = os.environ.get('OLLAMA_KEEP_ALIVE', '10m')

# Background AI jobs (Celery + Redis)
# -----------------------------------
# One long Qwen job — AI recommendation generation — runs in a Celery worker so
# the browser is never held open for minutes. Redis is both the broker and the
# result backend locally. Everything here is overridable through the
# environment; no credentials are involved (Redis runs on the operator's own
# machine, like Ollama).
REDIS_URL = os.environ.get('CELERY_REDIS_URL', 'redis://127.0.0.1:6379/0')
CELERY_BROKER_URL = os.environ.get('CELERY_BROKER_URL', REDIS_URL)
CELERY_RESULT_BACKEND = os.environ.get('CELERY_RESULT_BACKEND', REDIS_URL)
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_RESULT_SERIALIZER = 'json'
CELERY_TIMEZONE = TIME_ZONE
# Let the UI distinguish "queued" from "a worker picked it up".
CELERY_TASK_TRACK_STARTED = True
# Reconnect instead of crashing when the worker starts before Redis.
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
# A run cannot outlive the Ollama timeout by much; the hard limit kills a
# genuinely stuck task. Note: on Windows with `--pool=solo` Celery cannot
# enforce time limits, which is why the job's own staleness check below is the
# mechanism the UI relies on.
CELERY_TASK_SOFT_TIME_LIMIT = int(OLLAMA_TIMEOUT) + 120
CELERY_TASK_TIME_LIMIT = int(OLLAMA_TIMEOUT) + 300

# A job still Pending/Running after this long is reported to the user as timed
# out (worker died, task lost, machine slept…). Mirrors the hard time limit.
AI_JOB_TIMEOUT_SECONDS = float(
    os.environ.get('AI_JOB_TIMEOUT_SECONDS', str(int(OLLAMA_TIMEOUT) + 300))
)

# A job that is still Pending after this long was never picked up — Redis is
# reachable but no worker is consuming the queue. Reported as a clear
# "worker unavailable" failure instead of leaving the UI spinning forever.
AI_JOB_QUEUE_GRACE_SECONDS = float(
    os.environ.get('AI_JOB_QUEUE_GRACE_SECONDS', '90')
)

# The test suite must pass without a running Redis or worker: tasks execute
# inline. Set CELERY_TASK_ALWAYS_EAGER=true to get the same behaviour manually.
TESTING = 'test' in sys.argv
CELERY_TASK_ALWAYS_EAGER = TESTING or (
    os.environ.get('CELERY_TASK_ALWAYS_EAGER', '').lower() == 'true'
)
CELERY_TASK_EAGER_PROPAGATES = True

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {
        'brevo_console': {'class': 'logging.StreamHandler'},
        'ollama_console': {'class': 'logging.StreamHandler'},
    },
    'loggers': {
        'apps.accounts.services.email_service': {
            'handlers': ['brevo_console'],
            'level': 'INFO' if DEBUG else 'WARNING',
            'propagate': False,
        },
        # Recommendation engine / Ollama client: errors must be visible in the
        # runserver console so failures are never silently swallowed.
        'apps.recommendations': {
            'handlers': ['ollama_console'],
            'level': 'INFO' if DEBUG else 'WARNING',
            'propagate': False,
        },
    },
}

# Static files
STATIC_URL = 'static/'
STATICFILES_DIRS = [BASE_DIR / 'static']

# Media files (pour les PDF des rapports ESG)
MEDIA_URL = 'media/'
MEDIA_ROOT = BASE_DIR / 'media'

# Default primary key field type
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'


# Django REST Framework
REST_FRAMEWORK = {
    'DEFAULT_SCHEMA_CLASS': 'drf_spectacular.openapi.AutoSchema',
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': 20,
}

SPECTACULAR_SETTINGS = {
    'TITLE': 'CarbonPilot API',
    'DESCRIPTION': 'API de pilotage carbone et reporting ESG',
    'VERSION': '1.0.0',
}