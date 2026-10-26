from django.urls import path

from . import views

app_name = "governance"

urlpatterns = [
	path("", views.policy_list, name="policy_list"),
	path("new/", views.policy_create, name="policy_create"),
	path("ai-analysis/", views.policy_ai_analysis, name="policy_ai_analysis"),
	path("<int:pk>/", views.policy_detail, name="policy_detail"),
	path("<int:pk>/edit/", views.policy_edit, name="policy_edit"),
	path("<int:pk>/submit/", views.policy_submit_review, name="policy_submit_review"),
	path("<int:pk>/decision/", views.policy_decide, name="policy_decide"),
	path("<int:pk>/versions/new/", views.policy_new_version, name="policy_new_version"),
	path("<int:pk>/archive/", views.policy_archive, name="policy_archive"),
	path("<int:pk>/record-review/", views.policy_record_review, name="policy_record_review"),
]
