from django.apps import AppConfig


class AuditsConfig(AppConfig):
	default_auto_field = "django.db.models.BigAutoField"
	name = "apps.audits"
	verbose_name = "Internal audit"
