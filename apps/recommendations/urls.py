from django.urls import path

from . import views

app_name = "recommendations"

urlpatterns = [
	path("", views.recommendation_list, name="recommendation_list"),
	path("generate/", views.recommendation_generate, name="recommendation_generate"),
	path("ai-job/status/", views.ai_job_status, name="ai_job_status"),
	path(
		"<int:pk>/start-action/",
		views.recommendation_start_action,
		name="recommendation_start_action",
	),
	path(
		"<int:pk>/status/<str:status>/",
		views.recommendation_set_status,
		name="recommendation_set_status",
	),
	path("<int:pk>/", views.recommendation_detail, name="recommendation_detail"),
]
