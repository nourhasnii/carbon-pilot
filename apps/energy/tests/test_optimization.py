from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from core.models import Company, UserProfile
from core.roles import ROLE_AUDITOR, ROLE_DEPT_MANAGER, ROLE_EMPLOYEE, ROLE_SUSTAINABILITY_MANAGER
from energy.models import EnergyAsset, EnergyForecast, ForecastPoint, OptimizationScenario
from energy.services.optimization import ScenarioError, compute_scenario
from energy.tests.helpers import make_daily_readings


def user(name, company, role):
    u = User.objects.create_user(name, password="pw-test-123")
    u.groups.add(Group.objects.get(name=role))
    UserProfile.objects.create(user=u, company=company)
    return u


def scenario(asset, **kw):
    data = dict(asset=asset, name="HVAC -10 %", action="hvac", horizon_days=7, reduction_percent=Decimal("10"),
                affected_share_percent=Decimal("100"), price_per_kwh=Decimal("0.5"), emission_factor=Decimal("0.4"))
    data.update(kw)
    return OptimizationScenario.objects.create(**data)


class ComputeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(nom="A")
        cls.asset = EnergyAsset.objects.create(company=cls.company, name="HVAC", code="H1")
        make_daily_readings(cls.asset, 60)

    def test_history_baseline_and_exact_arithmetic(self):
        s = compute_scenario(scenario(self.asset))
        self.assertEqual(s.baseline_source, "history")
        self.assertEqual(len(s.daily), 7)
        self.assertAlmostEqual(s.saved_kwh, s.baseline_kwh * 0.10, delta=0.05)
        self.assertAlmostEqual(s.optimized_kwh, s.baseline_kwh - s.saved_kwh, delta=0.02)
        self.assertAlmostEqual(s.cost_saved, s.saved_kwh * 0.5, delta=0.05)
        self.assertAlmostEqual(s.co2_saved_kg, s.saved_kwh * 0.4, delta=0.05)

    def test_affected_share_scales_saving(self):
        full = compute_scenario(scenario(self.asset))
        part = compute_scenario(scenario(self.asset, affected_share_percent=Decimal("40")))
        self.assertAlmostEqual(part.saved_kwh, full.saved_kwh * 0.4, delta=0.1)

    def test_forecast_is_used_when_available(self):
        f = EnergyForecast.objects.create(asset=self.asset, horizon_days=7, status=EnergyForecast.Status.SUCCESS)
        from datetime import date, timedelta
        for i in range(7):
            ForecastPoint.objects.create(forecast=f, date=date.today() + timedelta(days=i), predicted_kwh=100)
        s = compute_scenario(scenario(self.asset))
        self.assertEqual(s.baseline_source, "forecast")
        self.assertEqual(s.baseline_kwh, 700)
        self.assertEqual(s.saved_kwh, 70)

    def test_past_forecast_is_ignored(self):
        from datetime import date, timedelta
        f = EnergyForecast.objects.create(asset=self.asset, horizon_days=7, status=EnergyForecast.Status.SUCCESS)
        for i in range(7):
            ForecastPoint.objects.create(forecast=f, date=date.today() - timedelta(days=30 - i), predicted_kwh=100)
        self.assertEqual(compute_scenario(scenario(self.asset)).baseline_source, "history")

    def test_forecast_of_other_horizon_is_ignored(self):
        EnergyForecast.objects.create(asset=self.asset, horizon_days=30, status=EnergyForecast.Status.SUCCESS)
        self.assertEqual(compute_scenario(scenario(self.asset)).baseline_source, "history")

    def test_payback(self):
        s = compute_scenario(scenario(self.asset, investment_cost=Decimal("1000")))
        yearly = s.cost_saved / 7 * 365
        self.assertAlmostEqual(s.payback_months, 1000 / yearly * 12, delta=0.1)
        self.assertIsNone(compute_scenario(scenario(self.asset)).payback_months)

    def test_not_enough_data_refuses_instead_of_inventing(self):
        empty = EnergyAsset.objects.create(company=self.company, name="Vide", code="V1")
        with self.assertRaises(ScenarioError):
            compute_scenario(scenario(empty))
        short = EnergyAsset.objects.create(company=self.company, name="Court", code="C1")
        make_daily_readings(short, 5)
        with self.assertRaisesMessage(ScenarioError, "Données insuffisantes"):
            compute_scenario(scenario(short))


class ScenarioViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("setup_roles", verbosity=0)
        cls.a, cls.b = Company.objects.create(nom="A"), Company.objects.create(nom="B")
        user("rse_a", cls.a, ROLE_SUSTAINABILITY_MANAGER)
        user("rse_b", cls.b, ROLE_SUSTAINABILITY_MANAGER)
        user("mgr_a", cls.a, ROLE_DEPT_MANAGER)
        user("aud_a", cls.a, ROLE_AUDITOR)
        user("emp_a", cls.a, ROLE_EMPLOYEE)
        cls.asset_a = EnergyAsset.objects.create(company=cls.a, name="HVAC A", code="HV1")
        cls.asset_b = EnergyAsset.objects.create(company=cls.b, name="HVAC B", code="HV1")
        make_daily_readings(cls.asset_a, 60)

    def login(self, name):
        self.client.login(username=name, password="pw-test-123")

    def payload(self, **kw):
        d = {"name": "HVAC -10 %", "asset": self.asset_a.pk, "action": "hvac", "horizon_days": 7,
             "reduction_percent": "10", "affected_share_percent": "40", "price_per_kwh": "0.35",
             "emission_factor": "0.5", "investment_cost": "", "notes": ""}
        d.update(kw)
        return d

    def test_create_computes_and_shows_detail(self):
        self.login("rse_a")
        r = self.client.post(reverse("energy:scenario_create"), self.payload(), follow=True)
        s = OptimizationScenario.objects.get()
        self.assertRedirects(r, reverse("energy:scenario_detail", args=[s.pk]))
        self.assertEqual(s.created_by.username, "rse_a")
        self.assertTrue(s.has_result)
        self.assertContains(r, "Énergie évitée")
        self.assertContains(r, "à confirmer")
        self.assertContains(r, 'id="sc-data"')
        self.assertContains(r, 'role="img"')

    def test_validation_rejects_bad_values(self):
        self.login("rse_a")
        for bad in ({"reduction_percent": "0"}, {"reduction_percent": "95"}, {"affected_share_percent": "0"},
                    {"affected_share_percent": "150"}, {"price_per_kwh": "0"}, {"emission_factor": "-1"},
                    {"investment_cost": "-5"}, {"horizon_days": "5"}):
            r = self.client.post(reverse("energy:scenario_create"), self.payload(**bad))
            self.assertEqual(r.status_code, 200, bad)
        self.assertEqual(OptimizationScenario.objects.count(), 0)

    def test_insufficient_data_is_saved_with_clear_message(self):
        empty = EnergyAsset.objects.create(company=self.a, name="Vide", code="V1")
        self.login("rse_a")
        r = self.client.post(reverse("energy:scenario_create"), self.payload(asset=empty.pk), follow=True)
        s = OptimizationScenario.objects.get()
        self.assertFalse(s.has_result)
        self.assertContains(r, "Calcul impossible")
        self.assertContains(self.client.get(reverse("energy:scenario_list")), "Calcul impossible")

    def test_cannot_use_other_company_asset(self):
        self.login("rse_a")
        r = self.client.post(reverse("energy:scenario_create"), self.payload(asset=self.asset_b.pk))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(OptimizationScenario.objects.count(), 0)

    def test_tenant_isolation(self):
        s = compute_scenario(scenario(self.asset_a))
        self.login("rse_b")
        for name in ("scenario_detail", "scenario_update", "scenario_delete", "scenario_export"):
            self.assertEqual(self.client.get(reverse(f"energy:{name}", args=[s.pk])).status_code, 404, name)
        self.assertNotContains(self.client.get(reverse("energy:scenario_list")), "HVAC A")

    def test_roles(self):
        s = compute_scenario(scenario(self.asset_a))
        self.login("aud_a")
        self.assertEqual(self.client.get(reverse("energy:scenario_detail", args=[s.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("energy:scenario_create")).status_code, 403)
        self.assertEqual(self.client.post(reverse("energy:scenario_delete", args=[s.pk])).status_code, 403)
        self.client.logout(); self.login("emp_a")
        self.assertEqual(self.client.get(reverse("energy:scenario_list")).status_code, 403)
        self.client.logout(); self.login("mgr_a")
        self.assertEqual(self.client.get(reverse("energy:scenario_create")).status_code, 200)
        self.assertEqual(self.client.post(reverse("energy:scenario_delete", args=[s.pk])).status_code, 403)
        self.assertTrue(OptimizationScenario.objects.filter(pk=s.pk).exists())

    def test_update_recomputes_and_delete(self):
        s = compute_scenario(scenario(self.asset_a))
        before = s.saved_kwh
        self.login("rse_a")
        self.client.post(reverse("energy:scenario_update", args=[s.pk]), self.payload(reduction_percent="20", affected_share_percent="100"))
        s.refresh_from_db()
        self.assertAlmostEqual(s.saved_kwh, before * 2, delta=0.1)
        self.client.post(reverse("energy:scenario_delete", args=[s.pk]))
        self.assertFalse(OptimizationScenario.objects.exists())

    def test_export_csv_neutralises_formulas(self):
        s = compute_scenario(scenario(self.asset_a, name="=HYPERLINK(\"x\")"))
        self.login("rse_a")
        r = self.client.get(reverse("energy:scenario_export", args=[s.pk]))
        body = r.content.decode("utf-8-sig")
        self.assertEqual(r["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn("'=HYPERLINK", body)
        self.assertEqual(len(body.strip().splitlines()), 2 + 1 + 1 + 7)

    def test_anonymous_redirected_and_list_ok(self):
        self.assertEqual(self.client.get(reverse("energy:scenario_list")).status_code, 302)
        self.login("rse_a")
        self.assertEqual(self.client.get(reverse("energy:scenario_list")).status_code, 200)
