import math
import random
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.utils import timezone

from apps.energy.models import EnergyReading


def make_daily_readings(asset, days, skip=(), seed=1, unit="kWh", end_offset=1, per_day=1):
    """Crée `days` jours de mesures (se terminant `end_offset` jours avant aujourd'hui).
    Motif hebdomadaire + bruit : une vraie structure à apprendre. `skip` = indices de jours absents."""
    rng = random.Random(seed)
    today = timezone.localdate()
    rows = []
    for i in range(days):
        d = today - timedelta(days=end_offset + days - 1 - i)
        if i in skip:
            continue
        weekly = 1.0 if d.weekday() < 5 else 0.6
        total = (200 + 20 * math.sin(i / 20)) * weekly * (1 + rng.gauss(0, 0.03))
        for h in range(per_day):
            ts = timezone.make_aware(datetime.combine(d, time(h if per_day > 1 else 12, 0)))
            rows.append(EnergyReading(asset=asset, timestamp=ts, value=Decimal(f"{total / per_day:.3f}"), unit=unit))
    EnergyReading.objects.bulk_create(rows)
    return rows
