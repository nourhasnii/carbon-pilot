from django.urls import path

from . import views

app_name = "objectives"

urlpatterns = [
	path("", views.objective_list, name="objective_list"),
	path("new/", views.objective_create, name="objective_create"),
	path("<int:pk>/", views.objective_detail, name="objective_detail"),
	path("<int:pk>/edit/", views.objective_edit, name="objective_edit"),
	path("<int:pk>/refresh/", views.objective_refresh_status, name="objective_refresh_status"),
	path("<int:pk>/predict/", views.objective_run_prediction, name="objective_run_prediction"),
]
