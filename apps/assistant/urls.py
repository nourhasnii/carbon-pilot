from django.urls import path

from . import views

app_name = "assistant"

urlpatterns = [
	path("", views.assistant_page, name="assistant_page"),
	path("ask/", views.assistant_ask, name="assistant_ask"),
	path("clear/", views.assistant_clear, name="assistant_clear"),
]
