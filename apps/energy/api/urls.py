from django.urls import path
from drf_spectacular.views import SpectacularAPIView, SpectacularRedocView, SpectacularSwaggerView
from rest_framework.routers import DefaultRouter

from .auth import ObtainToken
from .views import AnomalyViewSet, AssetViewSet, ForecastViewSet, ReadingViewSet, ScenarioViewSet

router = DefaultRouter()
router.register("assets", AssetViewSet, basename="asset")
router.register("readings", ReadingViewSet, basename="reading")
router.register("forecasts", ForecastViewSet, basename="forecast")
router.register("scenarios", ScenarioViewSet, basename="scenario")
router.register("anomalies", AnomalyViewSet, basename="anomaly")

app_name = "api"
urlpatterns = [
    path("auth/token/", ObtainToken.as_view(), name="token"),
    path("schema/", SpectacularAPIView.as_view(), name="schema"),
    path("docs/", SpectacularSwaggerView.as_view(url_name="api:schema"), name="docs"),
    path("redoc/", SpectacularRedocView.as_view(url_name="api:schema"), name="redoc"),
    *router.urls,
]
