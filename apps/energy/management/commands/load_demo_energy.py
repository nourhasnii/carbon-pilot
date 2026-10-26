"""Charge un jeu de données DEMO (fictif, explicitement marqué is_demo=True).

Usage : python manage.py load_demo_energy --password <mdp> [--days 365] [--seed 42]
Ne JAMAIS présenter ces données comme réelles (cahier des charges, règle « Données réelles »).
"""
import math
import random
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.enterprise.models import Company, Department, UserProfile
from core.roles import (ROLE_AUDITOR, ROLE_DEPT_MANAGER, ROLE_EMPLOYEE, ROLE_SUSTAINABILITY_MANAGER)
from apps.energy.models import EnergyAsset, EnergyReading

DEMO_USERS = [
    ("demo_rse", ROLE_SUSTAINABILITY_MANAGER),
    ("demo_manager", ROLE_DEPT_MANAGER),
    ("demo_employee", ROLE_EMPLOYEE),
    ("demo_auditor", ROLE_AUDITOR),
]


# Jours (il y a N jours) -> facteur : pic de +70 %, compteur à l'arrêt, chute de 45 %, pic de +55 %.
DEMO_ANOMALIES = {23: 1.7, 61: 0.02, 118: 0.55, 205: 1.55}


class Command(BaseCommand):
    help = "Crée une entreprise DEMO, 4 utilisateurs (un par rôle), 3 assets et un historique fictif."

    def add_arguments(self, parser):
        parser.add_argument("--password", required=True, help="Mot de passe des comptes demo (non stocké dans le code).")
        parser.add_argument("--days", type=int, default=365)
        parser.add_argument("--seed", type=int, default=42)

    def handle(self, *args, **o):
        call_command("setup_roles", verbosity=0)
        rng = random.Random(o["seed"])
        company, _ = Company.objects.get_or_create(nom="DEMO — Entreprise fictive", defaults={"secteur": "DEMO"})
        dept, _ = Department.objects.get_or_create(company=company, code="OPS", defaults={"nom": "Opérations"})

        for username, role in DEMO_USERS:
            user, created = User.objects.get_or_create(username=username)
            user.set_password(o["password"])
            user.save()
            user.groups.set([Group.objects.get(name=role)])
            UserProfile.objects.update_or_create(user=user, defaults={"company": company, "department": dept})

        specs = [
            ("HVAC-01", "HVAC principal (DEMO)", EnergyAsset.AssetType.HVAC, 320, 140, 1),
            ("CPT-GEN", "Compteur général (DEMO)", EnergyAsset.AssetType.METER, 900, 120, 0),
            ("BOIL-01", "Chaudière (DEMO)", EnergyAsset.AssetType.BOILER, 200, 150, -1),
        ]
        today = timezone.localdate()
        total = 0
        for code, name, atype, base, amp, season_sign in specs:
            asset, _ = EnergyAsset.objects.get_or_create(
                company=company, code=code,
                defaults=dict(name=name, asset_type=atype, department=dept, is_demo=True),
            )
            asset.readings.filter(is_demo=True).delete()
            rows = []
            for i in range(o["days"], 0, -1):
                d = today - timedelta(days=i)
                doy = d.timetuple().tm_yday
                season = season_sign * math.cos(2 * math.pi * (doy - 200) / 365)  # pic été si +1
                weekday = 0.75 if d.weekday() >= 5 else 1.0
                v = (base + amp * 0.5 * season) * weekday * (1 + rng.gauss(0, 0.05))
                v *= DEMO_ANOMALIES.get(i, 1.0)  # quelques incidents fictifs pour montrer la détection d'anomalies
                ts = timezone.make_aware(datetime.combine(d, time(12, 0)))
                rows.append(EnergyReading(asset=asset, timestamp=ts, value=Decimal(f"{max(v, 0):.3f}"), unit="kWh", is_demo=True))
            EnergyReading.objects.bulk_create(rows, ignore_conflicts=True)
            total += len(rows)
            if "HVAC" in name.upper():
                self._demo_scenario(asset)
        self.stdout.write(self.style.SUCCESS(f"DEMO chargée : {total} mesures (is_demo=True). Comptes : {', '.join(u for u, _ in DEMO_USERS)}"))


    def _demo_scenario(self, asset):
        """Scénario DEMO : « réduire le HVAC de 10 % » (hypothèses par défaut, à confirmer)."""
        from apps.energy.models import OptimizationScenario
        from apps.energy.services.optimization import ScenarioError, compute_scenario
        OptimizationScenario.objects.filter(asset=asset, is_demo=True).delete()
        sc = OptimizationScenario.objects.create(
            asset=asset, name="Réduire le HVAC de 10 %", action="hvac", horizon_days=30,
            reduction_percent=10, affected_share_percent=100, is_demo=True,
            notes="Scénario DEMO : prix du kWh et facteur d'émission par défaut, à confirmer.")
        try:
            compute_scenario(sc)
        except ScenarioError:
            pass
