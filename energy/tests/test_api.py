from datetime import timedelta

from django.contrib.auth.models import Group, User
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from core.models import Company, UserProfile
from core.roles import ROLE_AUDITOR, ROLE_DEPT_MANAGER, ROLE_EMPLOYEE, ROLE_SUSTAINABILITY_MANAGER
from energy.models import EnergyAnomaly, EnergyAsset, EnergyForecast, EnergyReading, OptimizationScenario
from energy.tests.helpers import make_daily_readings


def make_user(name, company, role):
    u = User.objects.create_user(name, password="pw-test-123")
    u.groups.add(Group.objects.get(name=role))
    UserProfile.objects.create(user=u, company=company)
    return u


class ApiBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("setup_roles", verbosity=0)
        cls.company = Company.objects.create(nom="A")
        cls.other = Company.objects.create(nom="B")
        cls.admin = make_user("admin_a", cls.company, ROLE_SUSTAINABILITY_MANAGER)
        cls.auditor = make_user("aud_a", cls.company, ROLE_AUDITOR)
        cls.employee = make_user("emp_a", cls.company, ROLE_EMPLOYEE)
        cls.admin_b = make_user("admin_b", cls.other, ROLE_SUSTAINABILITY_MANAGER)
        cls.asset = EnergyAsset.objects.create(company=cls.company, name="HVAC", code="HV1")
        cls.asset_b = EnergyAsset.objects.create(company=cls.other, name="Autre", code="X1")

    def setUp(self):
        cache.clear()

    def client_for(self, user):
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION="Token " + Token.objects.get_or_create(user=user)[0].key)
        return c


class AuthTests(ApiBase):
    def test_token_obtained_with_good_credentials(self):
        r = APIClient().post(reverse("api:token"), {"username": "admin_a", "password": "pw-test-123"}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["token"], Token.objects.get(user=self.admin).key)

    def test_bad_credentials_401_and_missing_fields_400(self):
        c = APIClient()
        self.assertEqual(c.post(reverse("api:token"), {"username": "admin_a", "password": "x"}, format="json").status_code, 401)
        self.assertEqual(c.post(reverse("api:token"), {"username": "admin_a"}, format="json").status_code, 400)

    def test_token_endpoint_locks_after_five_failures(self):
        c = APIClient()
        for _ in range(5):
            c.post(reverse("api:token"), {"username": "admin_a", "password": "bad"}, format="json")
        r = c.post(reverse("api:token"), {"username": "admin_a", "password": "pw-test-123"}, format="json")
        self.assertEqual(r.status_code, 429)

    def test_endpoints_refuse_anonymous(self):
        for name in ("asset-list", "reading-list", "forecast-list", "scenario-list", "anomaly-list"):
            self.assertIn(APIClient().get(reverse(f"api:{name}")).status_code, (401, 403), name)

    def test_invalid_token_refused(self):
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION="Token nope")
        self.assertEqual(c.get(reverse("api:asset-list")).status_code, 401)

    def test_user_without_company_is_refused(self):
        u = User.objects.create_user("lonely", password="x")
        u.groups.add(Group.objects.get(name=ROLE_SUSTAINABILITY_MANAGER))
        self.assertEqual(self.client_for(u).get(reverse("api:asset-list")).status_code, 403)

    def test_docs_and_schema_served(self):
        c = APIClient()
        self.assertEqual(c.get(reverse("api:docs")).status_code, 200)
        r = c.get(reverse("api:schema"), {"format": "json"})
        self.assertEqual(r.status_code, 200)
        paths = r.json()["paths"]
        for p in ("/api/assets/", "/api/forecasts/", "/api/anomalies/detect/", "/api/scenarios/", "/api/auth/token/"):
            self.assertIn(p, paths)


class RolesAndTenantTests(ApiBase):
    def test_auditor_reads_but_cannot_write(self):
        c = self.client_for(self.auditor)
        self.assertEqual(c.get(reverse("api:asset-list")).status_code, 200)
        self.assertEqual(c.post(reverse("api:asset-list"), {"name": "Zed", "code": "Z1"}, format="json").status_code, 403)
        self.assertEqual(c.delete(reverse("api:asset-detail", args=[self.asset.pk])).status_code, 403)
        self.assertTrue(EnergyAsset.objects.filter(pk=self.asset.pk).exists())

    def test_employee_has_no_access_to_anomalies_or_scenarios(self):
        c = self.client_for(self.employee)
        self.assertEqual(c.get(reverse("api:anomaly-list")).status_code, 403)
        self.assertEqual(c.get(reverse("api:scenario-list")).status_code, 403)
        self.assertEqual(c.post(reverse("api:forecast-list"), {"asset": self.asset.pk}, format="json").status_code, 403)

    def test_lists_only_show_own_company(self):
        r = self.client_for(self.admin).get(reverse("api:asset-list"))
        self.assertEqual([a["code"] for a in r.json()["results"]], ["HV1"])

    def test_other_company_object_is_404(self):
        c = self.client_for(self.admin)
        self.assertEqual(c.get(reverse("api:asset-detail", args=[self.asset_b.pk])).status_code, 404)
        self.assertEqual(c.delete(reverse("api:asset-detail", args=[self.asset_b.pk])).status_code, 404)
        self.assertTrue(EnergyAsset.objects.filter(pk=self.asset_b.pk).exists())

    def test_company_sent_by_client_is_ignored(self):
        r = self.client_for(self.admin).post(reverse("api:asset-list"),
                                             {"name": "Pirate", "code": "P1", "company": self.other.pk}, format="json")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(EnergyAsset.objects.get(code="P1").company, self.company)

    def test_cannot_use_foreign_asset_in_reading_or_forecast(self):
        c = self.client_for(self.admin)
        r = c.post(reverse("api:reading-list"), {"asset": self.asset_b.pk, "timestamp": "2026-01-01T10:00:00",
                                                  "value": "5", "unit": "kWh"}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(c.post(reverse("api:forecast-list"), {"asset": self.asset_b.pk, "horizon_days": 7},
                                format="json").status_code, 400)


class AssetApiTests(ApiBase):
    def test_crud_cycle(self):
        c = self.client_for(self.admin)
        r = c.post(reverse("api:asset-list"), {"name": "Pompe", "code": "PO-1", "energy_type": "electricity"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        pk = r.json()["id"]
        self.assertEqual(c.patch(reverse("api:asset-detail", args=[pk]), {"name": "Pompe 2"}, format="json").status_code, 200)
        self.assertEqual(EnergyAsset.objects.get(pk=pk).name, "Pompe 2")
        self.assertEqual(c.delete(reverse("api:asset-detail", args=[pk])).status_code, 204)
        self.assertFalse(EnergyAsset.objects.filter(pk=pk).exists())

    def test_input_rules_same_as_web_forms(self):
        c = self.client_for(self.admin)
        url = reverse("api:asset-list")
        r = c.post(url, {"name": "Pompe", "code": "a b"}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("code", r.json())
        dup = c.post(url, {"name": "Autre pompe", "code": "hv1"}, format="json")  # casse différente
        self.assertEqual(dup.status_code, 400)
        self.assertIn("code", dup.json())
        self.assertEqual(c.post(url, {"name": "", "code": "Q1"}, format="json").status_code, 400)
        self.assertEqual(c.post(url, {"name": "A‮B", "code": "Q2"}, format="json").status_code, 400)

    def test_patch_keeps_unsent_fields(self):
        c = self.client_for(self.admin)
        c.patch(reverse("api:asset-detail", args=[self.asset.pk]), {"is_active": False}, format="json")
        self.asset.refresh_from_db()
        self.assertFalse(self.asset.is_active)
        self.assertEqual(self.asset.code, "HV1")


class ReadingApiTests(ApiBase):
    def test_create_list_filter_delete(self):
        c = self.client_for(self.admin)
        ts = (timezone.now() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
        r = c.post(reverse("api:reading-list"), {"asset": self.asset.pk, "timestamp": ts, "value": "12.5", "unit": "kWh"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["asset_code"], "HV1")
        self.assertEqual(c.get(reverse("api:reading-list"), {"asset": self.asset.pk}).json()["count"], 1)
        self.assertEqual(c.get(reverse("api:reading-list"), {"asset": "²"}).status_code, 200)  # chiffre exotique ignoré
        self.assertEqual(c.delete(reverse("api:reading-detail", args=[r.json()["id"]])).status_code, 204)

    def test_invalid_readings_rejected(self):
        c = self.client_for(self.admin)
        url = reverse("api:reading-list")
        base = {"asset": self.asset.pk, "timestamp": "2026-01-01T10:00:00", "value": "5", "unit": "kWh"}
        future = (timezone.now() + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%S")
        for patch in ({"value": "-1"}, {"value": "abc"}, {"timestamp": future}, {"timestamp": "1990-01-01T00:00:00"},
                      {"unit": "m3"}, {"timestamp": "pas une date"}, {"value": "1e12"}):
            r = c.post(url, {**base, **patch}, format="json")
            self.assertEqual(r.status_code, 400, patch)

    def test_duplicate_timestamp_rejected(self):
        c = self.client_for(self.admin)
        body = {"asset": self.asset.pk, "timestamp": "2026-01-01T10:00:00", "value": "5", "unit": "kWh"}
        self.assertEqual(c.post(reverse("api:reading-list"), body, format="json").status_code, 201)
        self.assertEqual(c.post(reverse("api:reading-list"), body, format="json").status_code, 400)


class ForecastApiTests(ApiBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        make_daily_readings(cls.asset, 90)

    def test_post_runs_forecast_and_returns_202(self):
        c = self.client_for(self.admin)
        r = c.post(reverse("api:forecast-list"), {"asset": self.asset.pk, "horizon_days": 7}, format="json")
        self.assertEqual(r.status_code, 202, r.content)
        body = r.json()
        self.assertEqual(body["status"], "success")
        d = c.get(reverse("api:forecast-detail", args=[body["id"]])).json()
        self.assertEqual(len(d["points"]), 7)
        self.assertIsNotNone(d["points"][0]["lower_kwh"])
        self.assertIn("seasonal_naive", d["candidates"])
        self.assertIsNotNone(d["rmse"])

    def test_insufficient_data_is_reported_not_crashed(self):
        a = EnergyAsset.objects.create(company=self.company, name="Vide", code="V0")
        r = self.client_for(self.admin).post(reverse("api:forecast-list"), {"asset": a.pk, "horizon_days": 7}, format="json")
        self.assertEqual(r.status_code, 202)
        self.assertEqual(r.json()["status"], "insufficient_data")

    def test_invalid_horizon_and_pending_duplicate_rejected(self):
        c = self.client_for(self.admin)
        self.assertEqual(c.post(reverse("api:forecast-list"), {"asset": self.asset.pk, "horizon_days": 15}, format="json").status_code, 400)
        EnergyForecast.objects.create(asset=self.asset, horizon_days=7, status="pending")
        self.assertEqual(c.post(reverse("api:forecast-list"), {"asset": self.asset.pk, "horizon_days": 7}, format="json").status_code, 400)

    def test_auditor_can_read_not_create(self):
        c = self.client_for(self.auditor)
        self.assertEqual(c.get(reverse("api:forecast-list")).status_code, 200)
        self.assertEqual(c.post(reverse("api:forecast-list"), {"asset": self.asset.pk, "horizon_days": 7}, format="json").status_code, 403)

    def test_forecast_of_other_company_is_404(self):
        f = EnergyForecast.objects.create(asset=self.asset_b, horizon_days=7)
        self.assertEqual(self.client_for(self.admin).get(reverse("api:forecast-detail", args=[f.pk])).status_code, 404)


class ScenarioApiTests(ApiBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        make_daily_readings(cls.asset, 60)

    BODY = {"name": "HVAC -10 %", "action": "hvac", "horizon_days": 7, "reduction_percent": "10",
            "affected_share_percent": "100", "price_per_kwh": "0.35", "emission_factor": "0.5"}

    def test_create_computes_result(self):
        c = self.client_for(self.admin)
        r = c.post(reverse("api:scenario-list"), {**self.BODY, "asset": self.asset.pk}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        b = r.json()
        self.assertAlmostEqual(b["saved_kwh"], b["baseline_kwh"] * 0.10, delta=0.05)
        self.assertEqual(OptimizationScenario.objects.get().created_by, self.admin)

    def test_bounds_enforced(self):
        c = self.client_for(self.admin)
        for patch in ({"reduction_percent": "95"}, {"reduction_percent": "0"}, {"affected_share_percent": "0"},
                      {"price_per_kwh": "0"}, {"emission_factor": "9"}, {"notes": "x" * 2001}):
            r = c.post(reverse("api:scenario-list"), {**self.BODY, "asset": self.asset.pk, **patch}, format="json")
            self.assertEqual(r.status_code, 400, patch)

    def test_update_recomputes_and_duplicate_name_rejected(self):
        c = self.client_for(self.admin)
        pk = c.post(reverse("api:scenario-list"), {**self.BODY, "asset": self.asset.pk}, format="json").json()["id"]
        r = c.patch(reverse("api:scenario-detail", args=[pk]), {"reduction_percent": "20"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertAlmostEqual(r.json()["saved_kwh"], r.json()["baseline_kwh"] * 0.20, delta=0.05)
        dup = c.post(reverse("api:scenario-list"), {**self.BODY, "asset": self.asset.pk}, format="json")
        self.assertEqual(dup.status_code, 400)

    def test_without_history_scenario_is_saved_with_message(self):
        a = EnergyAsset.objects.create(company=self.company, name="Neuf", code="N1")
        r = self.client_for(self.admin).post(reverse("api:scenario-list"), {**self.BODY, "asset": a.pk}, format="json")
        self.assertEqual(r.status_code, 201)
        self.assertIsNone(r.json()["saved_kwh"])
        self.assertTrue(r.json()["result_message"])


class AnomalyApiTests(ApiBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        make_daily_readings(cls.asset, 90)
        day = timezone.localdate() - timedelta(days=10)
        EnergyReading.objects.filter(asset=cls.asset, timestamp__date=day).update(value=900)

    def test_detect_then_review(self):
        c = self.client_for(self.admin)
        r = c.post(reverse("api:anomaly-detect"), {"asset": self.asset.pk}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertGreaterEqual(r.json()["created"], 1)
        items = c.get(reverse("api:anomaly-list"), {"status": "new", "severity": "high"}).json()["results"]
        self.assertTrue(items)
        pk = items[0]["id"]
        r = c.patch(reverse("api:anomaly-detail", args=[pk]), {"status": "acknowledged", "note": "Panne"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        a = EnergyAnomaly.objects.get(pk=pk)
        self.assertEqual((a.status, a.reviewed_by), ("acknowledged", self.admin))

    def test_review_cannot_change_measured_values(self):
        c = self.client_for(self.admin)
        c.post(reverse("api:anomaly-detect"), {}, format="json")
        a = EnergyAnomaly.objects.first()
        before = a.actual_kwh
        c.patch(reverse("api:anomaly-detail", args=[a.pk]), {"actual_kwh": 1, "status": "dismissed"}, format="json")
        a.refresh_from_db()
        self.assertEqual(a.actual_kwh, before)

    def test_invalid_status_and_long_note_rejected(self):
        c = self.client_for(self.admin)
        c.post(reverse("api:anomaly-detect"), {}, format="json")
        pk = EnergyAnomaly.objects.first().pk
        url = reverse("api:anomaly-detail", args=[pk])
        self.assertEqual(c.patch(url, {"status": "bidon"}, format="json").status_code, 400)
        self.assertEqual(c.patch(url, {"note": "x" * 501}, format="json").status_code, 400)

    def test_auditor_reads_but_cannot_detect_or_review(self):
        self.client_for(self.admin).post(reverse("api:anomaly-detect"), {}, format="json")
        c = self.client_for(self.auditor)
        self.assertEqual(c.get(reverse("api:anomaly-list")).status_code, 200)
        self.assertEqual(c.post(reverse("api:anomaly-detect"), {}, format="json").status_code, 403)
        pk = EnergyAnomaly.objects.first().pk
        self.assertEqual(c.patch(reverse("api:anomaly-detail", args=[pk]), {"status": "dismissed"}, format="json").status_code, 403)

    def test_detect_on_asset_without_data_is_400(self):
        a = EnergyAsset.objects.create(company=self.company, name="Vide", code="V9")
        r = self.client_for(self.admin).post(reverse("api:anomaly-detect"), {"asset": a.pk}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_anomalies_cannot_be_deleted(self):
        c = self.client_for(self.admin)
        c.post(reverse("api:anomaly-detect"), {}, format="json")
        pk = EnergyAnomaly.objects.first().pk
        self.assertIn(c.delete(reverse("api:anomaly-detail", args=[pk])).status_code, (403, 405))
        self.assertTrue(EnergyAnomaly.objects.filter(pk=pk).exists())
