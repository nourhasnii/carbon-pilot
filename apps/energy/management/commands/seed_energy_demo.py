"""Donnees energie DEMO pour une entreprise de CarbonPilot (assets + mesures quotidiennes).

Usage : python manage.py seed_energy_demo [--company GreenTech] [--days 365] [--seed 42]
Relancable sans doublons : les mesures DEMO des assets DEMO sont recreees a chaque execution.
"""
import math
import random
from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.energy.models import EnergyAsset, EnergyReading
from apps.enterprise.models import Company

# (code, nom, type d'asset, type d'energie, conso de base en kWh/jour)
ASSETS = [
    ("HVAC-01", "HVAC principal (DEMO)", "hvac", "electricity", 420.0),
    ("MTR-01", "Compteur général (DEMO)", "meter", "electricity", 950.0),
    ("BLR-01", "Chaudière (DEMO)", "boiler", "gas", 310.0),
]


class Command(BaseCommand):
    help = "Charge des données énergie DEMO (assets + mesures quotidiennes) pour une entreprise."

    def add_arguments(self, parser):
        parser.add_argument("--company", default="GreenTech", help="Partie du nom de l'entreprise cible.")
        parser.add_argument("--days", type=int, default=365)
        parser.add_argument("--seed", type=int, default=42)

    @transaction.atomic
    def handle(self, *args, **opts):
        company = Company.objects.get(name__icontains=opts["company"])
        rng = random.Random(opts["seed"])
        today = timezone.localtime().replace(hour=12, minute=0, second=0, microsecond=0)
        days = opts["days"]
        total = 0

        for code, name, atype, etype, base in ASSETS:
            asset, _ = EnergyAsset.objects.update_or_create(
                company=company,
                code=code,
                defaults=dict(name=name, asset_type=atype, energy_type=etype, is_active=True, is_demo=True),
            )
            EnergyReading.objects.filter(asset=asset, is_demo=True).delete()

            rows = []
            for i in range(days, 0, -1):
                ts = today - timedelta(days=i)
                doy = ts.timetuple().tm_yday
                if atype == "boiler":
                    season = 1 + 0.6 * math.cos(2 * math.pi * (doy - 15) / 365)   # pic en hiver
                else:
                    season = 1 + 0.25 * math.cos(2 * math.pi * (doy - 200) / 365)  # pic en ete (climatisation)
                weekly = 0.65 if ts.weekday() >= 5 else 1.0                        # week-end plus calme
                value = base * season * weekly * rng.gauss(1, 0.06)
                if rng.random() < 0.015:
                    value *= rng.uniform(1.8, 2.5)  # quelques pics anormaux pour la detection d'anomalies
                rows.append(EnergyReading(
                    asset=asset,
                    timestamp=ts,
                    value=Decimal(str(round(max(value, 0.0), 2))),
                    unit="kWh",
                    is_demo=True,
                ))
            EnergyReading.objects.bulk_create(rows)
            total += len(rows)
            self.stdout.write(f"{name} : {len(rows)} mesures")

        self.stdout.write(self.style.SUCCESS(f"OK - {total} mesures DEMO créées pour {company}"))
