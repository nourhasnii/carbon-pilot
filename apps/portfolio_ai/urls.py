from django.urls import path

from . import views

app_name = "portfolio_ai"

urlpatterns = [
	path("", views.run_create, name="run_create"),
	path("runs/", views.run_history, name="run_history"),
	path("runs/<int:pk>/", views.run_detail, name="run_detail"),
	path("runs/<int:pk>/status/", views.run_status, name="run_status"),
	path("runs/<int:pk>/summary/", views.run_summary, name="run_summary"),
	path("llm/description/", views.generate_description, name="generate_description"),
]
