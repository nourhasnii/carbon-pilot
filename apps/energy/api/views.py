"""API REST (DRF). Chaque requête est limitée à l'entreprise du compte (jamais fournie par le client)."""
from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.energy.mixins import get_company
from apps.energy.models import (
    EnergyAnomaly, EnergyAsset, EnergyForecast, EnergyReading, OptimizationScenario,
)
from apps.energy.services.anomalies import AnomalyError, detect_for_asset
from apps.energy.tasks import run_forecast_task
from apps.energy.validation import parse_int_param

from . import serializers as s
from .permissions import CompanyModelPermission

ASSET_PARAM = OpenApiParameter("asset", int, description="Filtrer sur l'identifiant d'un asset")


class CompanyViewSet(viewsets.GenericViewSet):
    permission_classes = [CompanyModelPermission]
    model_name = ""

    @property
    def company(self):
        return get_company(self.request.user)

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        if getattr(self, "swagger_fake_view", False):
            return ctx
        ctx["company"] = self.company
        return ctx

    def filter_asset(self, qs, field="asset_id"):
        asset = parse_int_param(self.request.query_params.get("asset"))
        return qs.filter(**{field: asset}) if asset else qs


@extend_schema(tags=["Assets"])
@extend_schema_view(
    list=extend_schema(summary="Lister les assets de mon entreprise"),
    create=extend_schema(summary="Créer un asset"),
    retrieve=extend_schema(summary="Détail d'un asset"),
    update=extend_schema(summary="Remplacer un asset"),
    partial_update=extend_schema(summary="Modifier un asset"),
    destroy=extend_schema(summary="Supprimer un asset et ses mesures"),
)
class AssetViewSet(CompanyViewSet, viewsets.ModelViewSet):
    model_name = "energyasset"
    serializer_class = s.AssetSerializer

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return EnergyAsset.objects.none()
        return EnergyAsset.objects.filter(company=self.company).order_by("name")


@extend_schema(tags=["Mesures"])
@extend_schema_view(list=extend_schema(summary="Lister les mesures", parameters=[ASSET_PARAM]),
                    create=extend_schema(summary="Ajouter une mesure (ex. depuis un capteur)"))
class ReadingViewSet(CompanyViewSet, viewsets.ModelViewSet):
    model_name = "energyreading"
    serializer_class = s.ReadingSerializer

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return EnergyReading.objects.none()
        qs = EnergyReading.objects.for_company(self.company).select_related("asset").order_by("-timestamp")
        return self.filter_asset(qs)


@extend_schema(tags=["Prévisions"])
@extend_schema_view(
    list=extend_schema(summary="Lister les prévisions", parameters=[ASSET_PARAM]),
    retrieve=extend_schema(summary="Détail d'une prévision (points, fourchette 80 %, métriques MAE/RMSE)"),
    destroy=extend_schema(summary="Supprimer une prévision"),
)
class ForecastViewSet(CompanyViewSet, mixins.ListModelMixin, mixins.RetrieveModelMixin, mixins.DestroyModelMixin,
                      mixins.CreateModelMixin):
    model_name = "energyforecast"

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return EnergyForecast.objects.none()
        qs = EnergyForecast.objects.for_company(self.company).select_related("asset")
        return self.filter_asset(qs)

    def get_serializer_class(self):
        if self.action == "create":
            return s.ForecastCreateSerializer
        return s.ForecastDetailSerializer if self.action == "retrieve" else s.ForecastSerializer

    @extend_schema(summary="Lancer une prévision (asynchrone)", responses={202: s.ForecastSerializer})
    def create(self, request, *args, **kwargs):
        ser = self.get_serializer(data=request.data)
        ser.is_valid(raise_exception=True)
        forecast = EnergyForecast.objects.create(asset=ser.validated_data["asset"],
                                                 horizon_days=ser.validated_data["horizon_days"],
                                                 requested_by=request.user)
        run_forecast_task.delay(forecast.pk)  # Celery : ne bloque pas la requête (immédiat en mode eager)
        forecast.refresh_from_db()
        return Response(s.ForecastSerializer(forecast).data, status=status.HTTP_202_ACCEPTED)


@extend_schema(tags=["Scénarios"])
@extend_schema_view(
    list=extend_schema(summary="Lister les scénarios d'optimisation", parameters=[ASSET_PARAM]),
    create=extend_schema(summary="Créer et calculer un scénario « et si ? »"),
    update=extend_schema(summary="Remplacer un scénario (recalcul)"),
    partial_update=extend_schema(summary="Modifier un scénario (recalcul)"),
)
class ScenarioViewSet(CompanyViewSet, viewsets.ModelViewSet):
    model_name = "optimizationscenario"
    serializer_class = s.ScenarioSerializer

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return OptimizationScenario.objects.none()
        return self.filter_asset(OptimizationScenario.objects.for_company(self.company).select_related("asset"))


@extend_schema(tags=["Anomalies"])
@extend_schema_view(
    list=extend_schema(summary="Lister les anomalies", parameters=[
        ASSET_PARAM,
        OpenApiParameter("status", str, enum=[c for c, _ in EnergyAnomaly.Status.choices]),
        OpenApiParameter("severity", str, enum=[c for c, _ in EnergyAnomaly.Severity.choices])]),
    retrieve=extend_schema(summary="Détail d'une anomalie"),
    partial_update=extend_schema(summary="Décider : confirmer, écarter, commenter"),
)
class AnomalyViewSet(CompanyViewSet, mixins.ListModelMixin, mixins.RetrieveModelMixin, mixins.UpdateModelMixin):
    model_name = "energyanomaly"
    http_method_names = ["get", "patch", "post", "head", "options"]
    action_permissions = {"detect": "add"}

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return EnergyAnomaly.objects.none()
        qs = self.filter_asset(EnergyAnomaly.objects.for_company(self.company).select_related("asset", "reviewed_by"))
        p = self.request.query_params
        if p.get("status") in {c for c, _ in EnergyAnomaly.Status.choices}:
            qs = qs.filter(status=p["status"])
        if p.get("severity") in {c for c, _ in EnergyAnomaly.Severity.choices}:
            qs = qs.filter(severity=p["severity"])
        return qs

    def get_serializer_class(self):
        return s.AnomalyDetectSerializer if self.action == "detect" else s.AnomalySerializer

    def perform_update(self, serializer):
        serializer.save(reviewed_by=self.request.user, reviewed_at=timezone.now())

    @extend_schema(summary="Lancer la détection d'anomalies", request=s.AnomalyDetectSerializer,
                   responses={200: {"type": "object", "properties": {"analysed_assets": {"type": "integer"},
                              "created": {"type": "integer"}, "skipped": {"type": "integer"},
                              "to_review": {"type": "integer"}}}})
    @action(detail=False, methods=["post"])
    def detect(self, request):
        ser = self.get_serializer(data=request.data)
        ser.is_valid(raise_exception=True)
        asset = ser.validated_data.get("asset")
        assets = [asset] if asset else list(EnergyAsset.objects.filter(company=self.company, is_active=True))
        created = skipped = 0
        for a in assets:
            try:
                created += detect_for_asset(a)["created"]
            except AnomalyError:
                skipped += 1
        if asset and skipped:
            return Response({"asset": ["Données insuffisantes pour analyser cet asset."]}, status=status.HTTP_400_BAD_REQUEST)
        to_review = EnergyAnomaly.objects.for_company(self.company).filter(status=EnergyAnomaly.Status.NEW).count()
        return Response({"analysed_assets": len(assets) - skipped, "created": created, "skipped": skipped,
                         "to_review": to_review})
