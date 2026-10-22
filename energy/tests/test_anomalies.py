from datetime import date, timedelta

from django.contrib.auth.models import Group, User
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import Company, UserProfile
from core.roles import ROLE_AUDITOR, ROLE_DEPT_MANAGER, ROLE_EMPLOYEE, ROLE_SUSTAINABILITY_MANAGER
from energy.models import EnergyAnomaly, EnergyAsset, EnergyReading
from energy.services.anomalies import AnomalyError, analyse_series, detect_for_asset
from energy.tests.helpers import make_daily_readings


def weekly_series(n=120, spikes=None, seed=0):
    """Série hebdomadaire régulière (semaine 100, week-end 60) avec un peu de bruit déterministe."""
    start = date(2026, 1, 5)  # un lundi
    vals = {}
    for i in range(n):
        d = start + timedelta(days=i)
        base = 100.0 if d.weekday() < 5 else 60.0
        vals[d] = base + ((i * 7919 + seed) % 7 - 3) * 0.5  # bruit de ±1,5 kWh
    for i, f in (spikes or {}).items():
        vals[start + timedelta(days=i)] *= f
    return vals, start


class AnalyseSeriesTests(TestCase):
    def test_clean_series_has_no_alert(self):
        vals, _ = weekly_series()
        self.assertEqual(analyse_series(vals), [])

    def test_spike_drop_and_zero_are_found_with_right_kind(self):
        vals, start = weekly_series(spikes={60: 1.8, 80: 0.5, 100: 0.01})
        found = {f["date"]: f for f in analyse_series(vals)}
        self.assertEqual(found[start + timedelta(days=60)]["kind"], EnergyAnomaly.Kind.SPIKE)
        self.assertEqual(found[start + timedelta(days=80)]["kind"], EnergyAnomaly.Kind.DROP)
        self.assertEqual(found[start + timedelta(days=100)]["kind"], EnergyAnomaly.Kind.ZERO)
        self.assertEqual(len(found), 3)  # ni plus ni moins : pas de fausses alertes

    def test_weekend_is_not_flagged_as_drop(self):
        """Un week-end bas est normal : on compare un samedi aux samedis, pas aux jours ouvrés."""
        vals, start = weekly_series(n=150)
        self.assertTrue(any(d.weekday() >= 5 for d in vals))
        self.assertEqual(analyse_series(vals), [])

    def test_small_deviation_below_five_percent_ignored(self):
        vals, start = weekly_series(spikes={70: 1.03})
        self.assertEqual(analyse_series(vals), [])

    def test_explanation_is_readable_and_numbers_match(self):
        vals, start = weekly_series(spikes={60: 1.8})
        f = [x for x in analyse_series(vals) if x["date"] == start + timedelta(days=60)][0]
        self.assertIn("au-dessus", f["explanation"])
        self.assertIn("mesurés", f["explanation"])
        self.assertAlmostEqual(f["actual"] / f["expected"], 1.8, delta=0.05)
        self.assertEqual(f["severity"], EnergyAnomaly.Severity.HIGH)

    def test_not_enough_history_refused(self):
        vals, _ = weekly_series(n=10)
        with self.assertRaises(AnomalyError):
            analyse_series(vals)

    def test_perfectly_flat_series_does_not_crash(self):
        start = date(2026, 1, 5)
        vals = {start + timedelta(days=i): 100.0 for i in range(60)}
        vals[start + timedelta(days=50)] = 300.0
        found = analyse_series(vals)
        self.assertEqual([f["date"] for f in found], [start + timedelta(days=50)])


class AnomalyViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("setup_roles", verbosity=0)
        cls.a, cls.b = Company.objects.create(nom="A"), Company.objects.create(nom="B")
        for name, comp, role in (("rse_a", cls.a, ROLE_SUSTAINABILITY_MANAGER), ("rse_b", cls.b, ROLE_SUSTAINABILITY_MANAGER),
                                 ("mgr_a", cls.a, ROLE_DEPT_MANAGER), ("aud_a", cls.a, ROLE_AUDITOR), ("emp_a", cls.a, ROLE_EMPLOYEE)):
            u = User.objects.create_user(name, password="pw-test-123")
            u.groups.add(Group.objects.get(name=role))
            UserProfile.objects.create(user=u, company=comp)
        cls.asset = EnergyAsset.objects.create(company=cls.a, name="HVAC A", code="HV1")
        cls.asset_b = EnergyAsset.objects.create(company=cls.b, name="HVAC B", code="HV1")
        make_daily_readings(cls.asset, 90)
        # un pic franc sur un jour ouvré récent
        r = EnergyReading.objects.filter(asset=cls.asset).order_by("-timestamp")[20]
        r.value = r.value * 3
        r.save()
        cls.spike_day = timezone.localtime(r.timestamp).date()

    def login(self, n="rse_a"):
        self.client.logout()
        self.client.login(username=n, password="pw-test-123")

    def test_detect_finds_spike_and_is_idempotent_keeping_review(self):
        res = detect_for_asset(self.asset)
        self.assertGreaterEqual(res["created"], 1)
        an = EnergyAnomaly.objects.get(asset=self.asset, date=self.spike_day)
        self.assertEqual(an.kind, "spike")
        an.status, an.note = "acknowledged", "Fuite confirmée"
        an.save()
        res2 = detect_for_asset(self.asset)
        self.assertEqual(res2["created"], 0)
        an.refresh_from_db()
        self.assertEqual((an.status, an.note), ("acknowledged", "Fuite confirmée"))
        self.assertEqual(EnergyAnomaly.objects.filter(asset=self.asset, date=self.spike_day).count(), 1)

    def test_detect_insufficient_data_message(self):
        short = EnergyAsset.objects.create(company=self.a, name="Court", code="C1")
        make_daily_readings(short, 10)
        with self.assertRaisesMessage(AnomalyError, "insuffisantes"):
            detect_for_asset(short)

    def test_detect_view_and_list(self):
        self.login()
        r = self.client.post(reverse("energy:anomaly_detect"), {"asset": self.asset.pk}, follow=True)
        self.assertContains(r, "Analyse terminée")
        self.assertContains(self.client.get(reverse("energy:anomaly_list")), "HVAC A")
        self.assertEqual(self.client.get(reverse("energy:anomaly_detect")).status_code, 405)

    def test_detect_with_other_company_asset_refused(self):
        self.login()
        r = self.client.post(reverse("energy:anomaly_detect"), {"asset": self.asset_b.pk}, follow=True)
        self.assertContains(r, "Asset invalide")
        self.assertEqual(EnergyAnomaly.objects.count(), 0)

    def test_review_flow_and_validation(self):
        detect_for_asset(self.asset)
        an = EnergyAnomaly.objects.get(asset=self.asset, date=self.spike_day)
        self.login()
        url = reverse("energy:anomaly_detail", args=[an.pk])
        self.assertContains(self.client.get(url), 'id="an-data"')
        bad = self.client.post(url, {"status": "hacked", "note": "x"})
        self.assertEqual(bad.status_code, 200)
        bad2 = self.client.post(url, {"status": "dismissed", "note": "x" * 501})
        self.assertEqual(bad2.status_code, 200)
        bad3 = self.client.post(url, {"status": "dismissed", "note": "a\x00b"})
        self.assertEqual(bad3.status_code, 200)
        an.refresh_from_db()
        self.assertEqual(an.status, "new")
        ok = self.client.post(url, {"status": "dismissed", "note": "Maintenance planifiée"})
        self.assertEqual(ok.status_code, 302)
        an.refresh_from_db()
        self.assertEqual((an.status, an.reviewed_by.username), ("dismissed", "rse_a"))

    def test_roles(self):
        detect_for_asset(self.asset)
        an = EnergyAnomaly.objects.first()
        self.login("aud_a")
        self.assertEqual(self.client.get(reverse("energy:anomaly_list")).status_code, 200)
        self.assertEqual(self.client.get(reverse("energy:anomaly_detail", args=[an.pk])).status_code, 200)
        self.assertEqual(self.client.post(reverse("energy:anomaly_detail", args=[an.pk]), {"status": "dismissed"}).status_code, 403)
        self.assertEqual(self.client.post(reverse("energy:anomaly_detect"), {}).status_code, 403)
        self.login("emp_a")
        self.assertEqual(self.client.get(reverse("energy:anomaly_list")).status_code, 403)
        self.login("mgr_a")
        self.assertEqual(self.client.post(reverse("energy:anomaly_detect"), {"asset": self.asset.pk}).status_code, 302)

    def test_tenant_isolation(self):
        detect_for_asset(self.asset)
        an = EnergyAnomaly.objects.first()
        self.login("rse_b")
        self.assertEqual(self.client.get(reverse("energy:anomaly_detail", args=[an.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("energy:anomaly_detail", args=[an.pk]), {"status": "dismissed"}).status_code, 404)
        self.assertNotContains(self.client.get(reverse("energy:anomaly_list")), "HVAC A")
        self.assertNotContains(self.client.get(reverse("energy:anomaly_export")), "HV1,")

    def test_filters_and_weird_params(self):
        detect_for_asset(self.asset)
        self.login()
        r = self.client.get(reverse("energy:anomaly_list"), {"status": "acknowledged"})
        self.assertEqual(len(r.context["object_list"]), 0)
        for w in ("²", "x", "%00", "999999999999", "<script>"):
            self.assertEqual(self.client.get(reverse("energy:anomaly_list"), {"asset": w, "status": w, "severity": w, "page": w}).status_code in (200, 404), True)

    def test_dashboard_shows_new_anomalies_and_export_csv(self):
        detect_for_asset(self.asset)
        self.login()
        self.assertContains(self.client.get(reverse("energy:dashboard")), "à examiner")
        r = self.client.get(reverse("energy:anomaly_export"))
        self.assertEqual(r["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn("HV1", r.content.decode("utf-8-sig"))
