from django.urls import path

from . import views

app_name = "esg"

urlpatterns = [
	path("", views.esg_dashboard, name="dashboard"),
	# ``catalog/`` rather than ``indicators/``: the legacy accounts app already
	# registers ``esg/indicators/`` before this include, so that path would be
	# shadowed by its placeholder view.
	path("catalog/", views.indicator_list, name="indicator_list"),
	path("indicators/new/", views.indicator_create, name="indicator_create"),
	path("indicators/<int:pk>/", views.indicator_detail, name="indicator_detail"),
	path("indicators/<int:pk>/edit/", views.indicator_edit, name="indicator_edit"),
	path(
		"indicators/<int:pk>/active/",
		views.indicator_set_active,
		name="indicator_set_active",
	),
	path(
		"indicators/<int:pk>/values/new/",
		views.value_create,
		name="value_create",
	),
	path("values/<int:pk>/edit/", views.value_edit, name="value_edit"),
	path("values/<int:pk>/report/", views.value_report, name="value_report"),
	path("values/<int:pk>/verify/", views.value_verify, name="value_verify"),
	path("values/<int:pk>/return/", views.value_return, name="value_return"),
	path("assessments/run/", views.assessment_run, name="assessment_run"),
	path(
		"assessments/<int:pk>/",
		views.assessment_detail,
		name="assessment_detail",
	),
	path("coach/", views.coach, name="coach"),
	path("coach/run/", views.coach_run, name="coach_run"),
	path(
		"coach/recommendation/",
		views.coach_confirm_recommendation,
		name="coach_confirm_recommendation",
	),
]
