from django.urls import path

from . import views as v

app_name = "energy"
urlpatterns = [
    path("", v.DashboardView.as_view(), name="dashboard"),
    path("history/", v.HistoryView.as_view(), name="history"),
    path("assets/", v.AssetList.as_view(), name="asset_list"),
    path("assets/new/", v.AssetCreate.as_view(), name="asset_create"),
    path("assets/<int:pk>/edit/", v.AssetUpdate.as_view(), name="asset_update"),
    path("assets/<int:pk>/delete/", v.AssetDelete.as_view(), name="asset_delete"),
    path("readings/", v.ReadingList.as_view(), name="reading_list"),
    path("readings/export.csv", v.ReadingExportCSV.as_view(), name="reading_export"),
    path("readings/new/", v.ReadingCreate.as_view(), name="reading_create"),
    path("readings/<int:pk>/edit/", v.ReadingUpdate.as_view(), name="reading_update"),
    path("readings/<int:pk>/delete/", v.ReadingDelete.as_view(), name="reading_delete"),
    path("import/", v.ImportView.as_view(), name="import"),
    path("imports/", v.ImportList.as_view(), name="import_list"),
    path("imports/<int:pk>/", v.ImportDetail.as_view(), name="import_detail"),
    path("forecasts/", v.ForecastList.as_view(), name="forecast_list"),
    path("forecasts/new/", v.ForecastCreate.as_view(), name="forecast_create"),
    path("forecasts/<int:pk>/", v.ForecastDetail.as_view(), name="forecast_detail"),
    path("forecasts/<int:pk>/export.csv", v.ForecastExportCSV.as_view(), name="forecast_export"),
    path("forecasts/<int:pk>/status/", v.ForecastStatus.as_view(), name="forecast_status"),
    path("anomalies/", v.AnomalyList.as_view(), name="anomaly_list"),
    path("anomalies/detect/", v.AnomalyDetect.as_view(), name="anomaly_detect"),
    path("anomalies/export.csv", v.AnomalyExportCSV.as_view(), name="anomaly_export"),
    path("anomalies/<int:pk>/", v.AnomalyDetail.as_view(), name="anomaly_detail"),
    path("scenarios/", v.ScenarioList.as_view(), name="scenario_list"),
    path("scenarios/new/", v.ScenarioCreate.as_view(), name="scenario_create"),
    path("scenarios/<int:pk>/", v.ScenarioDetail.as_view(), name="scenario_detail"),
    path("scenarios/<int:pk>/edit/", v.ScenarioUpdate.as_view(), name="scenario_update"),
    path("scenarios/<int:pk>/delete/", v.ScenarioDelete.as_view(), name="scenario_delete"),
    path("scenarios/<int:pk>/export.csv", v.ScenarioExportCSV.as_view(), name="scenario_export"),
]
