from django.contrib.auth.models import Group, User
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from apps.enterprise.models import Company, UserProfile
from core.roles import ROLE_AUDITOR, ROLE_EMPLOYEE, ROLE_SUSTAINABILITY_MANAGER
from apps.energy.models import EnergyAsset, EnergyForecast
from apps.energy.tasks import run_forecast_task
from apps.energy.tests.helpers import make_daily_readings


def user(name, company, role):
    u = User.objects.create_user(name, password="pw-test-123")
    u.groups.add(Group.objects.get(name=role))
    UserProfile.objects.create(user=u, company=company)
    return u


class ForecastViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("setup_roles", verbosity=0)
        cls.a, cls.b = Company.objects.create(nom="A"), Company.objects.create(nom="B")
        user("rse_a", cls.a, ROLE_SUSTAINABILITY_MANAGER)
        user("rse_b", cls.b, ROLE_SUSTAINABILITY_MANAGER)
        user("aud_a", cls.a, ROLE_AUDITOR)
        user("emp_a", cls.a, ROLE_EMPLOYEE)
        cls.asset_a = EnergyAsset.objects.create(company=cls.a, name="HVAC A", code="HV1")
        cls.asset_b = EnergyAsset.objects.create(company=cls.b, name="HVAC B", code="HV1")
        make_daily_readings(cls.asset_a, 120)

    def login(self, name):
        self.client.login(username=name, password="pw-test-123")

    def test_create_runs_async_task_and_shows_result(self):
        self.login("rse_a")
        r = self.client.post(reverse("energy:forecast_create"), {"asset": self.asset_a.pk, "horizon_days": 7}, follow=True)
        f = EnergyForecast.objects.get()
        self.assertRedirects(r, reverse("energy:forecast_detail", args=[f.pk]))
        self.assertEqual(f.status, EnergyForecast.Status.SUCCESS)
        self.assertEqual(f.requested_by.username, "rse_a")
        self.assertContains(r, "Comparaison des modèles")
        self.assertContains(r, "RandomForestRegressor")

    def test_insufficient_data_page_has_clear_message(self):
        self.login("rse_a")
        asset = EnergyAsset.objects.create(company=self.a, name="Vide", code="V1")
        self.client.post(reverse("energy:forecast_create"), {"asset": asset.pk, "horizon_days": 7})
        f = EnergyForecast.objects.get(asset=asset)
        r = self.client.get(reverse("energy:forecast_detail", args=[f.pk]))
        self.assertContains(r, "Données insuffisantes")

    def test_cannot_forecast_other_company_asset(self):
        self.login("rse_a")
        r = self.client.post(reverse("energy:forecast_create"), {"asset": self.asset_b.pk, "horizon_days": 7})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(EnergyForecast.objects.count(), 0)

    def test_invalid_horizon_rejected(self):
        self.login("rse_a")
        r = self.client.post(reverse("energy:forecast_create"), {"asset": self.asset_a.pk, "horizon_days": 5})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(EnergyForecast.objects.count(), 0)

    def test_other_company_cannot_read_forecast_or_status(self):
        f = EnergyForecast.objects.create(asset=self.asset_a, horizon_days=7)
        run_forecast_task(f.pk)
        self.login("rse_b")
        self.assertEqual(self.client.get(reverse("energy:forecast_detail", args=[f.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("energy:forecast_status", args=[f.pk])).status_code, 404)
        self.assertNotContains(self.client.get(reverse("energy:forecast_list")), "HVAC A")

    def test_status_endpoint_json(self):
        f = EnergyForecast.objects.create(asset=self.asset_a, horizon_days=7)
        self.login("rse_a")
        data = self.client.get(reverse("energy:forecast_status", args=[f.pk])).json()
        self.assertEqual(data["status"], "pending")
        self.assertFalse(data["done"])
        run_forecast_task(f.pk)
        self.assertTrue(self.client.get(reverse("energy:forecast_status", args=[f.pk])).json()["done"])

    def test_pending_page_shows_loading_indicator(self):
        f = EnergyForecast.objects.create(asset=self.asset_a, horizon_days=7)
        self.login("rse_a")
        self.assertContains(self.client.get(reverse("energy:forecast_detail", args=[f.pk])), "Calcul en cours")

    def test_auditor_and_employee_are_read_only(self):
        for name in ("aud_a", "emp_a"):
            self.client.logout()
            self.login(name)
            self.assertEqual(self.client.get(reverse("energy:forecast_list")).status_code, 200, name)
            self.assertEqual(self.client.get(reverse("energy:forecast_create")).status_code, 403, name)
            self.assertEqual(
                self.client.post(reverse("energy:forecast_create"), {"asset": self.asset_a.pk, "horizon_days": 7}).status_code, 403, name)
        self.assertEqual(EnergyForecast.objects.count(), 0)

    def test_anonymous_redirected(self):
        self.assertEqual(self.client.get(reverse("energy:forecast_list")).status_code, 302)

    def test_demo_banner_when_demo_data(self):
        from apps.energy.models import EnergyReading
        EnergyReading.objects.filter(asset=self.asset_a).update(is_demo=True)
        f = EnergyForecast.objects.create(asset=self.asset_a, horizon_days=7)
        run_forecast_task(f.pk)
        self.login("rse_a")
        self.assertContains(self.client.get(reverse("energy:forecast_detail", args=[f.pk])), "DEMO")
