from django.urls import path

from . import views

app_name = "carbon"

urlpatterns = [
	path("", views.carbon_dashboard, name="dashboard"),
	path("activities/", views.activity_list, name="activity_list"),
	path("activities/new/", views.activity_wizard, name="activity_wizard"),
	path("activities/<int:pk>/", views.activity_detail, name="activity_detail"),
	path("activities/<int:pk>/edit/", views.activity_edit, name="activity_edit"),
	path("activities/<int:pk>/action/", views.activity_action, name="activity_action"),
	path("activities/<int:pk>/delete/", views.activity_delete, name="activity_delete"),
	path("activities/<int:pk>/archive/", views.activity_archive, name="activity_archive"),
	path("activities/<int:pk>/restore/", views.activity_restore, name="activity_restore"),
	path("activities/<int:pk>/evidence/", views.activity_evidence, name="activity_evidence"),
	path("review/", views.review_queue, name="review_queue"),
	path("footprint/", views.carbon_footprint, name="footprint"),
	path("anomalies/", views.anomaly_list, name="anomaly_list"),
	path("anomalies/run/", views.anomaly_run, name="anomaly_run"),
	path("anomalies/<int:pk>/", views.anomaly_detail, name="anomaly_detail"),
	path("anomalies/<int:pk>/status/", views.anomaly_status, name="anomaly_status"),
	path("emission-factors/", views.factor_library, name="factor_library"),
	path("emission-factors/new/", views.factor_create, name="factor_create"),
	path("emission-factors/<int:pk>/", views.factor_detail, name="factor_detail"),
	path("emission-factors/<int:pk>/edit/", views.factor_edit, name="factor_edit"),
	path("emission-factors/<int:pk>/status/", views.factor_toggle_status, name="factor_toggle_status"),
]
