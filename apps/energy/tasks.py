from celery import shared_task

from .models import EnergyForecast
from .services.forecasting import run_forecast as _run


@shared_task
def run_forecast_task(forecast_id: int):
    forecast = EnergyForecast.objects.select_related("asset").get(pk=forecast_id)
    _run(forecast)
    return forecast.status
