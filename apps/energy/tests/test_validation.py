from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.enterprise.models import Company, UserProfile
from core.roles import ROLE_SUSTAINABILITY_MANAGER
from apps.energy.forms import AssetForm, ForecastForm, ReadingForm, ScenarioForm
from apps.energy.models import EnergyAsset, EnergyForecast, EnergyReading
from apps.energy.services.importer import import_readings
from apps.energy.validation import clean_search, parse_int_param
from apps.energy.tests.helpers import make_daily_readings


class HelperTests(TestCase):
    def test_parse_int_param_rejects_everything_odd(self):
        for bad in (None, "", "abc", "²", "٣", "-1", "+1", "1.5", " 1", "1 ", "0", "99999999999", "1e3", "١٢٣"):
            self.assertIsNone(parse_int_param(bad), repr(bad))
        self.assertEqual(parse_int_param("12"), 12)
        self.assertEqual(parse_int_param("x", default=90), 90)
        self.assertEqual(parse_int_param("5000", default=90, lo=1, hi=3650), 90)

    def test_clean_search_strips_controls_and_caps_length(self):
        self.assertEqual(clean_search("ab\x00c‮d"), "abcd")
        self.assertEqual(len(clean_search("x" * 500)), 100)
        self.assertEqual(clean_search(None), "")


class BaseCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("setup_roles", verbosity=0)
        cls.company = Company.objects.create(nom="A")
        cls.other = Company.objects.create(nom="B")
        u = User.objects.create_user("rse", password="pw-test-123")
        u.groups.add(Group.objects.get(name=ROLE_SUSTAINABILITY_MANAGER))
        UserProfile.objects.create(user=u, company=cls.company)
        cls.asset = EnergyAsset.objects.create(company=cls.company, name="HVAC A", code="HV1")
        cls.gas = EnergyAsset.objects.create(company=cls.company, name="Chaudière", code="GAZ1", energy_type="gas")

    def login(self):
        self.client.login(username="rse", password="pw-test-123")


class AssetFormTests(BaseCase):
    def form(self, **kw):
        data = {"name": "Compteur 1", "code": "CPT-1", "asset_type": "meter", "energy_type": "electricity", "is_active": "on"}
        data.update(kw)
        return AssetForm(data, company=self.company)

    def test_valid(self):
        self.assertTrue(self.form().is_valid())

    def test_code_rules(self):
        for bad in ("CPT 1", "-abc", "a/b", "é1", "x" * 41, "a;b", "<b>"):
            self.assertIn("code", self.form(code=bad).errors, bad)

    def test_code_unique_case_insensitive(self):
        self.assertIn("code", self.form(code="hv1").errors)

    def test_name_rules(self):
        for bad in ("A", "   ", "---", "Ab‮c", "Ab​c", "Ab\x07c", "x" * 121):
            self.assertIn("name", self.form(name=bad).errors, repr(bad))

    def test_department_of_other_company_refused(self):
        from apps.enterprise.models import Department
        d = Department.objects.create(company=self.other, nom="X", code="X") if hasattr(Department, "nom") else None
        if d is None:
            self.skipTest("modèle Department différent")
        self.assertIn("department", self.form(department=d.pk).errors)

    def test_changing_energy_type_with_incompatible_units_refused(self):
        EnergyReading.objects.create(asset=self.gas, timestamp=timezone.now() - timedelta(days=1), value=5, unit="m3")
        f = AssetForm({"name": "Chaudière", "code": "GAZ1", "asset_type": "boiler", "energy_type": "electricity", "is_active": "on"},
                      company=self.company, instance=self.gas)
        self.assertIn("energy_type", f.errors)

    def test_html_attributes_present(self):
        a = self.form().fields["code"].widget.attrs
        self.assertIn("pattern", a)
        self.assertEqual(self.form().fields["name"].widget.attrs["minlength"], "2")


class ReadingFormTests(BaseCase):
    def form(self, **kw):
        data = {"asset": self.asset.pk, "timestamp": (timezone.localtime() - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M"),
                "value": "12.5", "unit": "kWh"}
        data.update(kw)
        return ReadingForm(data, company=self.company)

    def test_valid(self):
        self.assertTrue(self.form().is_valid(), self.form().errors)

    def test_bad_values(self):
        for bad in ("-1", "abc", "1e400", "nan", "inf", "1000000001", "1.2345", ""):
            self.assertIn("value", self.form(value=bad).errors, bad)

    def test_dates(self):
        future = (timezone.localtime() + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")
        self.assertIn("timestamp", self.form(timestamp=future).errors)
        self.assertIn("timestamp", self.form(timestamp="1999-12-31T10:00").errors)
        self.assertIn("timestamp", self.form(timestamp="pas une date").errors)
        self.assertIn("timestamp", self.form(timestamp="2025-02-30T10:00").errors)

    def test_unit_must_match_energy(self):
        self.assertIn("unit", self.form(unit="m3").errors)      # électricité en m³
        self.assertIn("unit", self.form(unit="Joule").errors)
        f = self.form(asset=self.gas.pk, unit="m3")
        self.assertTrue(f.is_valid(), f.errors)

    def test_other_company_asset_refused(self):
        a = EnergyAsset.objects.create(company=self.other, name="Autre", code="O1")
        self.assertIn("asset", self.form(asset=a.pk).errors)

    def test_duplicate_timestamp_refused(self):
        ts = (timezone.now() - timedelta(days=1)).replace(second=0, microsecond=0)
        EnergyReading.objects.create(asset=self.asset, timestamp=ts, value=1)
        f = self.form(timestamp=timezone.localtime(ts).strftime("%Y-%m-%dT%H:%M"))
        self.assertFalse(f.is_valid())
        self.assertIn("existe déjà", str(f.errors))


class ImportFormTests(BaseCase):
    def post(self, name, content):
        self.login()
        return self.client.post(reverse("energy:import"), {"file": SimpleUploadedFile(name, content)})

    def test_rejects_wrong_extension_empty_fake_xlsx_binary_csv(self):
        self.assertContains(self.post("a.exe", b"x"), "Format non support")
        self.assertContains(self.post("a.csv", b""), "vide")
        self.assertContains(self.post("a.xlsx", b"ceci est du texte"), "vrai classeur")
        self.assertContains(self.post("a.csv", b"asset_code,value\x00\x01"), "CSV texte valide")
        self.assertEqual(EnergyReading.objects.count(), 0)

    def test_row_level_rules(self):
        ts = (timezone.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
        csv = (
            "asset_code,timestamp,value,unit\n"
            f"HV1,{ts},10,kWh\n"                      # ok
            f"HV1,1990-01-01 10:00,10,kWh\n"          # trop ancien
            f"GAZ1,{ts},10,L\n"                       # unité incohérente (gaz en litres)
            f"HV1,{ts},10,m3\n"                       # électricité en m3
            f"INCONNU,{ts},10,kWh\n"                  # asset inconnu
        )
        self.asset.is_active = True
        res = import_readings(SimpleUploadedFile("a.csv", csv.encode()), self.company)
        self.assertEqual(res.imported_rows, 1)
        msgs = " | ".join(e.message for e in res.errors)
        self.assertIn("antérieure à 2000", msgs)
        self.assertIn("pas valable", msgs)
        self.assertIn("Asset inconnu", msgs)

    def test_inactive_asset_refused_and_decimals_rounded(self):
        off = EnergyAsset.objects.create(company=self.company, name="Éteint", code="OFF1", is_active=False)
        ts = (timezone.now() - timedelta(days=2)).strftime("%Y-%m-%d %H:%M")
        csv = f"asset_code,timestamp,value\nOFF1,{ts},5\nHV1,{ts},1.23456\n"
        res = import_readings(SimpleUploadedFile("a.csv", csv.encode()), self.company)
        self.assertEqual(res.imported_rows, 1)
        self.assertIn("inactif", res.errors[0].message)
        self.assertEqual(EnergyReading.objects.get().value, Decimal("1.235"))

    def test_long_cell_does_not_blow_up_error_report(self):
        ts = (timezone.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
        res = import_readings(SimpleUploadedFile("a.csv", (f"asset_code,timestamp,value\n{'Z' * 5000},{ts},1\n").encode()), self.company)
        self.assertLess(len(res.errors[0].message), 100)


class ForecastAndScenarioFormTests(BaseCase):
    def test_duplicate_running_forecast_refused(self):
        EnergyForecast.objects.create(asset=self.asset, horizon_days=7, status="running")
        f = ForecastForm({"asset": self.asset.pk, "horizon_days": 7}, company=self.company)
        self.assertFalse(f.is_valid())
        self.assertIn("déjà en cours", str(f.errors))
        self.assertTrue(ForecastForm({"asset": self.asset.pk, "horizon_days": 30}, company=self.company).is_valid())

    def test_too_many_pending_forecasts_refused(self):
        for i in range(3):
            a = EnergyAsset.objects.create(company=self.company, name=f"Asset {i}", code=f"P{i}")
            EnergyForecast.objects.create(asset=a, horizon_days=7, status="pending")
        self.assertIn("Trop de prévisions", str(ForecastForm({"asset": self.asset.pk, "horizon_days": 7}, company=self.company).errors))

    def scenario(self, **kw):
        data = {"name": "Scénario test", "asset": self.asset.pk, "action": "hvac", "horizon_days": 7, "reduction_percent": "10",
                "affected_share_percent": "50", "price_per_kwh": "0.35", "emission_factor": "0.5", "investment_cost": "", "notes": ""}
        data.update(kw)
        return ScenarioForm(data, company=self.company)

    def test_scenario_bounds(self):
        self.assertTrue(self.scenario().is_valid())
        for field, bad in (("reduction_percent", "80.5"), ("reduction_percent", "0.05"), ("affected_share_percent", "100.1"),
                           ("price_per_kwh", "101"), ("emission_factor", "5.1"), ("investment_cost", "-1"),
                           ("investment_cost", "1000000001"), ("price_per_kwh", "abc"), ("price_per_kwh", "nan"),
                           ("name", "ab"), ("name", "!!!"), ("notes", "x" * 2001), ("notes", "a\x00b"), ("horizon_days", "15")):
            self.assertIn(field, self.scenario(**{field: bad}).errors, (field, bad))

    def test_scenario_notes_keep_newlines(self):
        self.assertTrue(self.scenario(notes="ligne 1\nligne 2").is_valid())

    def test_scenario_duplicate_name_per_asset(self):
        from apps.energy.models import OptimizationScenario
        OptimizationScenario.objects.create(asset=self.asset, name="Doublon", reduction_percent=10)
        self.assertIn("name", self.scenario(name="doublon").errors)


class UrlParameterTests(BaseCase):
    def test_weird_query_params_never_crash(self):
        self.login()
        weird = ["²", "٣", "-1", "99999999999999999999", "1e9", "%00", "'", "<script>", "a" * 3000, "0"]
        for name in ("energy:dashboard", "energy:history", "energy:reading_list", "energy:asset_list", "energy:reading_export",
                     "energy:forecast_list", "energy:scenario_list"):
            for w in weird:
                r = self.client.get(reverse(name), {"asset": w, "days": w, "period": w, "q": w, "page": w})
                self.assertIn(r.status_code, (200, 404), (name, w[:10], r.status_code))

    def test_days_out_of_range_falls_back(self):
        self.login()
        self.assertEqual(self.client.get(reverse("energy:history"), {"days": "999999"}).context["days"], 90)
        self.assertEqual(self.client.get(reverse("energy:history"), {"days": "30"}).context["days"], 30)


class LoginThrottleTests(BaseCase):
    def setUp(self):
        cache.clear()

    def attempt(self, pw, user="rse"):
        return self.client.post(reverse("login"), {"username": user, "password": pw})

    def test_lock_after_five_failures_then_correct_password_still_blocked(self):
        for _ in range(5):
            self.assertContains(self.attempt("faux"), "Saisissez")
        r = self.attempt("pw-test-123")
        self.assertContains(r, "Trop de tentatives")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_success_resets_counter_and_other_user_unaffected(self):
        for _ in range(3):
            self.attempt("faux")
        self.assertEqual(self.attempt("pw-test-123").status_code, 302)
        self.client.logout()
        for _ in range(5):
            self.attempt("faux", user="autre")
        self.assertEqual(self.attempt("pw-test-123").status_code, 302)


class FrenchLabelsTests(BaseCase):
    def test_no_english_labels_in_forms(self):
        forms_ = [AssetForm(company=self.company), ReadingForm(company=self.company),
                  ForecastForm(company=self.company), ScenarioForm(company=self.company)]
        labels = [f.label for form in forms_ for f in form.fields.values()]
        for bad in ("Asset type", "Energy type", "Is active", "Unit", "Value", "Name", "Code"):
            self.assertNotIn(bad, labels)


class StalePendingForecastTests(TestCase):
    """Un worker arrêté ne doit pas bloquer à jamais les nouvelles prévisions."""

    def test_old_pending_forecast_does_not_block(self):
        from datetime import timedelta
        from django.utils import timezone
        from apps.enterprise.models import Company
        from apps.energy.forms import ForecastForm
        from apps.energy.models import EnergyAsset, EnergyForecast
        company = Company.objects.create(nom="Stale")
        asset = EnergyAsset.objects.create(company=company, name="Compteur", code="S1")
        old = EnergyForecast.objects.create(asset=asset, horizon_days=7, status="pending")
        data = {"asset": asset.pk, "horizon_days": 7}
        self.assertFalse(ForecastForm(data, company=company).is_valid())  # récente : bloque
        EnergyForecast.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(minutes=30))
        self.assertTrue(ForecastForm(data, company=company).is_valid())  # ancienne : ne bloque plus
