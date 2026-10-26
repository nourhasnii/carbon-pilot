from django.urls import path

from . import views

app_name = "risk"

urlpatterns = [
	path("", views.risk_assessment_view, name="risk_assessment"),
	path("run/", views.risk_assessment_run, name="risk_assessment_run"),
]
