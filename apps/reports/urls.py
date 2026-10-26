from django.urls import path

from . import views

app_name = "reports"

urlpatterns = [
	path("", views.report_list, name="report_list"),
	path("create/", views.report_create, name="report_create"),
	path("<int:pk>/", views.report_detail, name="report_detail"),
	path("<int:pk>/generate/", views.report_generate, name="report_generate"),
	path("<int:pk>/download/", views.report_download, name="report_download"),
]
