from datetime import datetime, timezone as tz
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from apps.enterprise.models import Company, Department, UserProfile
from core.roles import ROLE_AUDITOR, ROLE_EMPLOYEE, ROLE_SUSTAINABILITY_MANAGER
from apps.energy.models import EnergyAsset, EnergyReading


def make_user(name, company, role=None, with_profile=True):
    u = User.objects.create_user(name, password="pw-test-123")
    if role:
        u.groups.add(Group.objects.get(name=role))
    if with_profile:
        UserProfile.objects.create(user=u, company=company)
    return u


class BaseViewTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("setup_roles", verbosity=0)
        cls.a, cls.b = Company.objects.create(nom="A"), Company.objects.create(nom="B")
        cls.rse_a = make_user("rse_a", cls.a, ROLE_SUSTAINABILITY_MANAGER)
        cls.rse_b = make_user("rse_b", cls.b, ROLE_SUSTAINABILITY_MANAGER)
        cls.auditor_a = make_user("aud_a", cls.a, ROLE_AUDITOR)
        cls.employee_a = make_user("emp_a", cls.a, ROLE_EMPLOYEE)
        cls.asset_a = EnergyAsset.objects.create(company=cls.a, name="HVAC A", code="HV1")
        cls.asset_b = EnergyAsset.objects.create(company=cls.b, name="HVAC B", code="HV1")
        cls.reading_b = EnergyReading.objects.create(
            asset=cls.asset_b, timestamp=datetime(2026, 1, 1, tzinfo=tz.utc), value=Decimal("10"))


class AuthAndRoleTests(BaseViewTest):
    def test_anonymous_is_redirected_to_login(self):
        for name in ("energy:dashboard", "energy:asset_list", "energy:reading_list", "energy:import"):
            r = self.client.get(reverse(name))
            self.assertEqual(r.status_code, 302, name)
            self.assertIn("/accounts/login/", r["Location"])

    def test_user_without_company_gets_403(self):
        make_user("orphan", self.a, ROLE_SUSTAINABILITY_MANAGER, with_profile=False)
        self.client.login(username="orphan", password="pw-test-123")
        self.assertEqual(self.client.get(reverse("energy:asset_list")).status_code, 403)

    def test_auditor_is_read_only_even_by_direct_url(self):
        self.client.login(username="aud_a", password="pw-test-123")
        self.assertEqual(self.client.get(reverse("energy:asset_list")).status_code, 200)
        self.assertEqual(self.client.get(reverse("energy:asset_create")).status_code, 403)
        self.assertEqual(self.client.post(reverse("energy:asset_create"), {"name": "x", "code": "x"}).status_code, 403)
        self.assertEqual(self.client.post(reverse("energy:asset_delete", args=[self.asset_a.pk])).status_code, 403)
        self.assertTrue(EnergyAsset.objects.filter(pk=self.asset_a.pk).exists())
        self.assertEqual(self.client.get(reverse("energy:import")).status_code, 403)

    def test_employee_cannot_see_imports(self):
        self.client.login(username="emp_a", password="pw-test-123")
        self.assertEqual(self.client.get(reverse("energy:import_list")).status_code, 403)
        self.assertEqual(self.client.get(reverse("energy:reading_list")).status_code, 200)

    def test_read_only_pages_hide_write_buttons(self):
        self.client.login(username="aud_a", password="pw-test-123")
        html = self.client.get(reverse("energy:asset_list")).content.decode()
        self.assertNotIn("Ajouter un asset", html)


class TenantIsolationViewTests(BaseViewTest):
    def setUp(self):
        self.client.login(username="rse_a", password="pw-test-123")

    def test_cannot_open_other_company_asset_or_reading(self):
        self.assertEqual(self.client.get(reverse("energy:asset_update", args=[self.asset_b.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("energy:asset_delete", args=[self.asset_b.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("energy:reading_update", args=[self.reading_b.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("energy:reading_delete", args=[self.reading_b.pk])).status_code, 404)
        self.assertTrue(EnergyAsset.objects.filter(pk=self.asset_b.pk).exists())

    def test_lists_never_leak_other_company_data(self):
        r = self.client.get(reverse("energy:asset_list"))
        self.assertContains(r, "HVAC A")
        self.assertNotContains(r, "HVAC B")
        r = self.client.get(reverse("energy:reading_list") + f"?asset={self.asset_b.pk}")
        self.assertEqual(len(r.context["object_list"]), 0)

    def test_company_id_from_browser_is_ignored(self):
        self.client.post(reverse("energy:asset_create"), {
            "name": "Nouveau", "code": "N1", "asset_type": "meter", "energy_type": "electricity",
            "is_active": "on", "company": self.b.pk})
        self.assertEqual(EnergyAsset.objects.get(code="N1").company, self.a)

    def test_reading_form_rejects_asset_of_other_company(self):
        r = self.client.post(reverse("energy:reading_create"), {
            "asset": self.asset_b.pk, "timestamp": "2026-02-01T10:00", "value": "5", "unit": "kWh"})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(EnergyReading.objects.filter(value=Decimal("5")).exists())

    def test_history_only_counts_own_company(self):
        EnergyReading.objects.create(asset=self.asset_a, timestamp=datetime.now(tz.utc), value=Decimal("7"))
        r = self.client.get(reverse("energy:history") + "?days=3650")
        self.assertEqual(r.context["stats"]["total"], Decimal("7"))


class CrudTests(BaseViewTest):
    def setUp(self):
        self.client.login(username="rse_a", password="pw-test-123")

    def test_asset_crud_cycle(self):
        r = self.client.post(reverse("energy:asset_create"), {
            "name": "Chaudière", "code": "CH1", "asset_type": "boiler", "energy_type": "gas", "is_active": "on"})
        self.assertRedirects(r, reverse("energy:asset_list"))
        asset = EnergyAsset.objects.get(code="CH1")
        self.client.post(reverse("energy:asset_update", args=[asset.pk]), {
            "name": "Chaudière 2", "code": "CH1", "asset_type": "boiler", "energy_type": "gas"})
        asset.refresh_from_db()
        self.assertEqual(asset.name, "Chaudière 2")
        self.assertFalse(asset.is_active)
        self.client.post(reverse("energy:asset_delete", args=[asset.pk]))
        self.assertFalse(EnergyAsset.objects.filter(pk=asset.pk).exists())

    def test_duplicate_asset_code_shows_error(self):
        r = self.client.post(reverse("energy:asset_create"), {
            "name": "Dup", "code": "HV1", "asset_type": "meter", "energy_type": "electricity"})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "existe déjà")

    def test_reading_crud_and_validation(self):
        url = reverse("energy:reading_create")
        bad = self.client.post(url, {"asset": self.asset_a.pk, "timestamp": "2026-02-01T10:00", "value": "-3", "unit": "kWh"})
        self.assertEqual(bad.status_code, 200)
        ok = self.client.post(url, {"asset": self.asset_a.pk, "timestamp": "2026-02-01T10:00", "value": "12.5", "unit": "kWh"})
        self.assertRedirects(ok, reverse("energy:reading_list"))
        dup = self.client.post(url, {"asset": self.asset_a.pk, "timestamp": "2026-02-01T10:00", "value": "9", "unit": "kWh"})
        self.assertContains(dup, "existe déjà")
        reading = EnergyReading.objects.get(asset=self.asset_a)
        self.client.post(reverse("energy:reading_delete", args=[reading.pk]))
        self.assertEqual(EnergyReading.objects.filter(asset=self.asset_a).count(), 0)

    def test_import_view_reports_errors(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        f = SimpleUploadedFile("m.csv", b"asset_code,timestamp,value,unit\nHV1,2026-01-01 10:00,5,kWh\nZZZ,2026-01-01 11:00,5,kWh\n")
        r = self.client.post(reverse("energy:import"), {"file": f}, follow=True)
        self.assertContains(r, "Lignes rejetées")
        self.assertContains(r, "Asset inconnu")
        self.assertEqual(EnergyReading.objects.filter(asset=self.asset_a).count(), 1)

    def test_import_wrong_extension_is_refused(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        r = self.client.post(reverse("energy:import"), {"file": SimpleUploadedFile("m.txt", b"x")})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Format non supporté")


class ErrorPageTests(TestCase):
    def test_custom_404(self):
        r = self.client.get("/nimporte/quoi/")
        self.assertEqual(r.status_code, 404)
        self.assertContains(r, "Page introuvable", status_code=404)


class DemoCommandTests(TestCase):
    def test_demo_data_is_flagged(self):
        call_command("load_demo_energy", password="pw-demo-123", days=30, verbosity=0)
        self.assertTrue(EnergyReading.objects.exists())
        self.assertFalse(EnergyReading.objects.filter(is_demo=False).exists())
        self.assertFalse(EnergyAsset.objects.filter(is_demo=False).exists())


class ChartPayloadTests(BaseViewTest):
    """Régression : les données des graphiques doivent être un OBJET JSON, pas une chaîne JSON encodée deux fois."""

    def payload(self, html, element_id):
        import json, re
        m = re.search(r'<script id="%s" type="application/json">(.*?)</script>' % element_id, html, re.S)
        self.assertIsNotNone(m, f"{element_id} introuvable")
        return json.loads(m.group(1))

    def test_dashboard_chart_data_is_an_object(self):
        EnergyReading.objects.create(asset=self.asset_a, timestamp=datetime.now(tz.utc), value=Decimal("7"))
        self.client.login(username="rse_a", password="pw-test-123")
        data = self.payload(self.client.get(reverse("energy:dashboard") + "?days=3650").content.decode(), "chart-data")
        self.assertIsInstance(data, dict)
        self.assertEqual(len(data["labels"]), len(data["values"]))
        self.assertEqual(data["values"], [7.0])

    def test_forecast_chart_data_is_an_object(self):
        from apps.energy.models import EnergyForecast
        from apps.energy.tasks import run_forecast_task
        from apps.energy.tests.helpers import make_daily_readings
        make_daily_readings(self.asset_a, 100)
        f = EnergyForecast.objects.create(asset=self.asset_a, horizon_days=7)
        run_forecast_task(f.pk)
        self.client.login(username="rse_a", password="pw-test-123")
        data = self.payload(self.client.get(reverse("energy:forecast_detail", args=[f.pk])).content.decode(), "fc-data")
        self.assertIsInstance(data, dict)
        self.assertEqual(len(data["forecast"]), 7)
        self.assertGreater(len(data["history"]), 0)


class ExportTests(BaseViewTest):
    def setUp(self):
        self.client.login(username="rse_a", password="pw-test-123")

    def test_reading_export_is_csv_scoped_to_company(self):
        EnergyReading.objects.create(asset=self.asset_a, timestamp=datetime(2026, 1, 5, 10, 0, tzinfo=tz.utc), value=Decimal("12.5"))
        r = self.client.get(reverse("energy:reading_export"))
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/csv", r["Content-Type"])
        self.assertIn("attachment", r["Content-Disposition"])
        body = r.content.decode("utf-8-sig")
        self.assertIn("HV1", body)
        self.assertIn("12.500", body)
        self.assertNotIn("HVAC B", body)          # asset de l'autre entreprise
        self.assertEqual(body.count("\n"), 2)     # en-tête + 1 ligne

    def test_reading_export_respects_asset_filter(self):
        a2 = EnergyAsset.objects.create(company=self.a, name="Autre", code="AUT")
        EnergyReading.objects.create(asset=self.asset_a, timestamp=datetime(2026, 1, 5, tzinfo=tz.utc), value=Decimal("1"))
        EnergyReading.objects.create(asset=a2, timestamp=datetime(2026, 1, 5, tzinfo=tz.utc), value=Decimal("2"))
        body = self.client.get(reverse("energy:reading_export") + f"?asset={a2.pk}").content.decode("utf-8-sig")
        self.assertIn("AUT", body)
        self.assertNotIn("HV1", body)

    def test_csv_formula_injection_is_neutralised(self):
        evil = EnergyAsset.objects.create(company=self.a, name='=HYPERLINK("http://x","y")', code="@cmd")
        EnergyReading.objects.create(asset=evil, timestamp=datetime(2026, 1, 5, tzinfo=tz.utc), value=Decimal("1"))
        body = self.client.get(reverse("energy:reading_export")).content.decode("utf-8-sig")
        self.assertIn("'=HYPERLINK", body)
        self.assertIn("'@cmd", body)
        self.assertNotIn(",=HYPERLINK", body)

    def test_anonymous_and_unprivileged_cannot_export(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse("energy:reading_export")).status_code, 302)
        user = User.objects.create_user("nogroup", password="pw-test-123")
        UserProfile.objects.create(user=user, company=self.a)
        self.client.login(username="nogroup", password="pw-test-123")
        self.assertEqual(self.client.get(reverse("energy:reading_export")).status_code, 403)

    def test_forecast_export_and_isolation(self):
        from apps.energy.models import EnergyForecast
        from apps.energy.tasks import run_forecast_task
        from apps.energy.tests.helpers import make_daily_readings
        make_daily_readings(self.asset_a, 100)
        f = EnergyForecast.objects.create(asset=self.asset_a, horizon_days=7)
        run_forecast_task(f.pk)
        r = self.client.get(reverse("energy:forecast_export", args=[f.pk]))
        self.assertEqual(r.status_code, 200)
        lines = r.content.decode("utf-8-sig").strip().splitlines()
        self.assertEqual(lines[0], "date,prevu_kwh,bas_80pct_kwh,haut_80pct_kwh,reel_kwh,ecart_kwh,modele,donnee_demo")
        self.assertEqual(len(lines), 8)  # en-tête + 7 jours
        self.client.logout()
        self.client.login(username="rse_b", password="pw-test-123")
        self.assertEqual(self.client.get(reverse("energy:forecast_export", args=[f.pk])).status_code, 404)


class UiFeatureTests(BaseViewTest):
    def setUp(self):
        self.client.login(username="rse_a", password="pw-test-123")

    def test_theme_switch_and_script_present(self):
        html = self.client.get(reverse("energy:dashboard")).content.decode()
        self.assertIn('role="switch"', html)
        self.assertIn("data-theme-toggle", html)
        self.assertIn("cp-theme", html)          # script anti-flash dans le <head>
        self.assertIn("js/ui.js", html)

    def test_quick_ranges_keep_filters_and_mark_current(self):
        html = self.client.get(reverse("energy:history") + f"?days=30&period=week&asset={self.asset_a.pk}").content.decode()
        self.assertIn("Périodes rapides", html)
        self.assertIn(f"days=7&amp;period=week&amp;asset={self.asset_a.pk}", html)
        self.assertIn('aria-current="true"', html)

    def test_breadcrumbs_on_form_pages(self):
        html = self.client.get(reverse("energy:asset_create")).content.decode()
        self.assertIn("Fil d'Ariane", html)

    def test_import_field_is_marked_for_dropzone(self):
        self.assertIn("data-dropzone", self.client.get(reverse("energy:import")).content.decode())

    def test_login_page_has_theme_switch(self):
        self.client.logout()
        html = self.client.get(reverse("login")).content.decode()
        self.assertIn("data-theme-toggle", html)
        self.assertIn("form-control", html)


class HeadingStructureTests(BaseViewTest):
    """Accessibilité : un seul h1 par page, premier titre du document = h1, pas de saut de niveau."""

    PAGES = ["energy:dashboard", "energy:history", "energy:asset_list", "energy:reading_list", "energy:import",
             "energy:import_list", "energy:forecast_list", "energy:forecast_create", "energy:asset_create", "energy:reading_create"]

    def test_heading_hierarchy_on_every_page(self):
        import re
        self.client.login(username="rse_a", password="pw-test-123")
        for name in self.PAGES:
            html = self.client.get(reverse(name)).content.decode()
            levels = [int(m) for m in re.findall(r"<h([1-6])[\s>]", html)]
            self.assertEqual(levels.count(1), 1, f"{name}: {levels.count(1)} h1")
            self.assertEqual(levels[0], 1, f"{name}: le premier titre est h{levels[0]}")
            for prev, cur in zip(levels, levels[1:]):
                self.assertLessEqual(cur, prev + 1, f"{name}: saut de h{prev} à h{cur}")

    def test_pages_declare_language_and_landmarks(self):
        self.client.login(username="rse_a", password="pw-test-123")
        html = self.client.get(reverse("energy:dashboard")).content.decode()
        for needle in ('<html lang="fr">', "<main ", "<nav ", "<header", "<footer", 'class="skip-link"'):
            self.assertIn(needle, html)

    def test_every_form_field_has_a_label(self):
        import re
        self.client.login(username="rse_a", password="pw-test-123")
        for name in ("energy:asset_create", "energy:reading_create", "energy:forecast_create", "energy:import"):
            html = self.client.get(reverse(name)).content.decode()
            ids = re.findall(r'<(?:input|select|textarea)[^>]*\sid="([^"]+)"[^>]*>', html)
            inputs = [i for i in re.findall(r'<input[^>]*>', html) if 'type="hidden"' not in i]
            for tag in inputs:
                m = re.search(r'\sid="([^"]+)"', tag)
                if m:
                    self.assertIn(f'for="{m.group(1)}"', html, f"{name}: champ {m.group(1)} sans label")
