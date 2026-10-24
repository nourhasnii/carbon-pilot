from django.urls import path

from . import views

app_name = "investments"

urlpatterns = [
	path("projects/", views.project_list, name="project_list"),
	path("projects/new/", views.project_create, name="project_create"),
	path("projects/<int:pk>/", views.project_detail, name="project_detail"),
	path("projects/<int:pk>/edit/", views.project_edit, name="project_edit"),
	path("projects/<int:pk>/delete/confirm/", views.project_delete_confirm, name="project_delete_confirm"),
	path("projects/<int:pk>/delete/", views.project_delete, name="project_delete"),
	path("projects/<int:pk>/investments/new/", views.investment_create, name="investment_create"),
	path("projects/<int:pk>/investments/<int:child_pk>/edit/", views.investment_edit, name="investment_edit"),
	path("projects/<int:pk>/investments/<int:child_pk>/delete/confirm/", views.investment_delete_confirm, name="investment_delete_confirm"),
	path("projects/<int:pk>/investments/<int:child_pk>/delete/", views.investment_delete, name="investment_delete"),
	path("projects/<int:pk>/impacts/new/", views.impact_create, name="impact_create"),
	path("projects/<int:pk>/impacts/new/", views.impact_create, name="impact_assessment_create"),
	path("projects/<int:pk>/impacts/<int:child_pk>/edit/", views.impact_edit, name="impact_edit"),
	path("projects/<int:pk>/impacts/<int:child_pk>/edit/", views.impact_edit, name="impact_assessment_edit"),
	path("projects/<int:pk>/impacts/<int:child_pk>/delete/confirm/", views.impact_delete_confirm, name="impact_delete_confirm"),
	path("projects/<int:pk>/impacts/<int:child_pk>/delete/confirm/", views.impact_delete_confirm, name="impact_assessment_delete_confirm"),
	path("projects/<int:pk>/impacts/<int:child_pk>/delete/", views.impact_delete, name="impact_delete"),
	path("projects/<int:pk>/impacts/<int:child_pk>/delete/", views.impact_delete, name="impact_assessment_delete"),
	path("projects/<int:pk>/scenarios/new/", views.scenario_create, name="scenario_create"),
	path("projects/<int:pk>/scenarios/new/", views.scenario_create, name="project_scenario_create"),
	path("projects/<int:pk>/scenarios/<int:child_pk>/edit/", views.scenario_edit, name="scenario_edit"),
	path("projects/<int:pk>/scenarios/<int:child_pk>/edit/", views.scenario_edit, name="project_scenario_edit"),
	path("projects/<int:pk>/scenarios/<int:child_pk>/delete/confirm/", views.scenario_delete_confirm, name="scenario_delete_confirm"),
	path("projects/<int:pk>/scenarios/<int:child_pk>/delete/confirm/", views.scenario_delete_confirm, name="project_scenario_delete_confirm"),
	path("projects/<int:pk>/scenarios/<int:child_pk>/delete/", views.scenario_delete, name="scenario_delete"),
	path("projects/<int:pk>/scenarios/<int:child_pk>/delete/", views.scenario_delete, name="project_scenario_delete"),
]
