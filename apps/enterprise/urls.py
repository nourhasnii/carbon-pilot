from django.urls import path

from . import views


app_name = "enterprise"

urlpatterns = [
	path("", views.company_dashboard, name="company_dashboard"),
	path("profile/", views.company_profile, name="company_profile"),
	path("intelligence/", views.company_intelligence, name="company_intelligence"),
]