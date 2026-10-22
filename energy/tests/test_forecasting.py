import math
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from core.models import Company
from energy.models import EnergyAsset, EnergyForecast, ForecastPoint
from energy.services import forecasting as fc
from energy.tests.helpers import make_daily_readings


class ForecastBase(TestCase):
    def setUp(self):
        self.company = Company.objects.create(nom="A")
        self.asset = EnergyAsset.objects.create(company=self.company, name="HVAC", code="HV1")

    def run_fc(self, horizon=7):
        f = EnergyForecast.objects.create(asset=self.asset, horizon_days=horizon)
        return fc.run_forecast(f)


class InsufficientDataTests(ForecastBase):
    def test_no_data(self):
        f = self.run_fc()
        self.assertEqual(f.status, EnergyForecast.Status.INSUFFICIENT)
        self.assertEqual(f.points.count(), 0)

    def test_too_short_history_is_refused_not_invented(self):
        make_daily_readings(self.asset, 30)
        f = self.run_fc(7)
        self.assertEqual(f.status, EnergyForecast.Status.INSUFFICIENT)
        self.assertIn("insuffisantes", f.message)
        self.assertEqual(f.points.count(), 0)
        self.assertIsNone(f.rmse)

    def test_horizon_30_needs_more_history_than_horizon_7(self):
        make_daily_readings(self.asset, 90)
        self.assertEqual(self.run_fc(7).status, EnergyForecast.Status.SUCCESS)
        self.assertEqual(self.run_fc(30).status, EnergyForecast.Status.INSUFFICIENT)

    def test_unsupported_units_only_is_insufficient(self):
        make_daily_readings(self.asset, 100, unit="m3")
        self.assertEqual(self.run_fc().status, EnergyForecast.Status.INSUFFICIENT)


class ForecastResultTests(ForecastBase):
    def setUp(self):
        super().setUp()
        make_daily_readings(self.asset, 200)

    def test_success_produces_horizon_points_starting_after_last_day(self):
        f = self.run_fc(7)
        self.assertEqual(f.status, EnergyForecast.Status.SUCCESS)
        pts = list(f.points.all())
        self.assertEqual(len(pts), 7)
        last_day = timezone.localdate() - timedelta(days=1)
        self.assertEqual(pts[0].date, last_day + timedelta(days=1))
        self.assertTrue(all(p.predicted_kwh >= 0 for p in pts))

    def test_metrics_traceability(self):
        f = self.run_fc(7)
        self.assertTrue(math.isfinite(f.mae) and math.isfinite(f.rmse))
        self.assertGreaterEqual(f.rmse, f.mae)
        self.assertIn(f.model_name, fc.MODEL_LABELS)
        self.assertEqual(set(f.metrics["candidates"]), set(fc.MODEL_LABELS))
        self.assertIsNotNone(f.duration_seconds)
        self.assertIsNotNone(f.training_start)
        self.assertEqual(f.n_training_days, 200)

    def test_best_model_has_lowest_rmse(self):
        f = self.run_fc(7)
        rmses = {k: v["rmse"] for k, v in f.metrics["candidates"].items()}
        self.assertEqual(f.model_name, min(rmses, key=rmses.get))

    def test_forecast_learns_weekly_pattern(self):
        """Weekends ~40 % plus bas dans les données : la prévision doit le refléter."""
        f = self.run_fc(7)
        pts = list(f.points.all())
        weekend = [p.predicted_kwh for p in pts if p.date.weekday() >= 5]
        weekday = [p.predicted_kwh for p in pts if p.date.weekday() < 5]
        self.assertTrue(weekend and weekday)  # 7 jours consécutifs contiennent toujours les deux
        self.assertLess(sum(weekend) / len(weekend), sum(weekday) / len(weekday))

    def test_deterministic_for_same_data(self):
        a, b = self.run_fc(7), self.run_fc(7)
        self.assertEqual([round(p.predicted_kwh, 6) for p in a.points.all()], [round(p.predicted_kwh, 6) for p in b.points.all()])

    def test_rerun_replaces_points(self):
        f = self.run_fc(7)
        fc.run_forecast(f)
        self.assertEqual(f.points.count(), 7)


class DataQualityTests(ForecastBase):
    def test_missing_days_are_not_invented(self):
        make_daily_readings(self.asset, 150, skip={40, 41, 42})
        series = fc.build_daily_series(self.asset)
        self.assertEqual(int(series.isna().sum()), 3)

    def test_today_partial_day_is_excluded(self):
        make_daily_readings(self.asset, 70, end_offset=0)  # inclut aujourd'hui
        series = fc.build_daily_series(self.asset)
        self.assertLess(max(series.index), timezone.localdate())

    def test_incomplete_days_are_dropped_for_subdaily_data(self):
        from energy.models import EnergyReading
        make_daily_readings(self.asset, 70, per_day=4)
        # supprime 3 mesures sur 4 pour un jour -> jour incomplet
        day = timezone.localdate() - timedelta(days=10)
        victims = EnergyReading.objects.filter(asset=self.asset, timestamp__date=day).order_by("timestamp")[1:]
        EnergyReading.objects.filter(pk__in=[v.pk for v in victims]).delete()
        series = fc.build_daily_series(self.asset)
        self.assertTrue(series.isna().sum() >= 1)

    def test_mwh_is_converted_to_kwh(self):
        make_daily_readings(self.asset, 70, unit="MWh")
        series = fc.build_daily_series(self.asset)
        self.assertGreater(series.dropna().mean(), 100_000)  # ~200 MWh -> ~200 000 kWh

    def test_stale_data_gives_explicit_warning(self):
        """Dernières mesures il y a 14 jours : la prévision part du dernier jour de données, avec avertissement."""
        make_daily_readings(self.asset, 150, end_offset=15)
        f = self.run_fc(7)
        self.assertEqual(f.status, EnergyForecast.Status.SUCCESS)
        self.assertIn("déjà écoulée", f.message)
        last_valid = timezone.localdate() - timedelta(days=15)
        self.assertEqual(f.points.first().date, last_valid + timedelta(days=1))

    def test_recent_data_has_no_warning(self):
        make_daily_readings(self.asset, 150)
        self.assertEqual(self.run_fc(7).message, "")

    def test_gap_right_before_forecast_origin_refuses(self):
        """Trou dans les 14 derniers jours valides : pas de lag disponible -> pas de prévision inventée."""
        make_daily_readings(self.asset, 150, skip={145, 146, 147})
        f = self.run_fc(7)
        self.assertEqual(f.status, EnergyForecast.Status.INSUFFICIENT)
        self.assertIn("manquantes", f.message)
        self.assertEqual(f.points.count(), 0)


class ActualsComparisonTests(ForecastBase):
    def test_update_actuals_fills_in_when_reality_arrives(self):
        make_daily_readings(self.asset, 120)
        f = self.run_fc(7)
        self.assertIsNone(fc.update_actuals(f))  # pas encore de réel
        # La réalité arrive pour les 3 premiers jours prévus (décalage de fenêtre : on simule en créant ces jours)
        from datetime import datetime, time
        from decimal import Decimal
        from energy.models import EnergyReading
        # on déplace les points dans le passé pour que ces jours soient « complets »
        base = timezone.localdate() - timedelta(days=5)
        for i, p in enumerate(f.points.all()):
            ForecastPoint.objects.filter(pk=p.pk).update(date=base + timedelta(days=i))
        EnergyReading.objects.filter(asset=self.asset, timestamp__date__gte=base).delete()
        for i in range(3):
            ts = timezone.make_aware(datetime.combine(base + timedelta(days=i), time(12, 0)))
            EnergyReading.objects.create(asset=self.asset, timestamp=ts, value=Decimal("210"), unit="kWh")
        m = fc.update_actuals(f)
        self.assertEqual(m["n"], 3)
        self.assertEqual(f.points.exclude(actual_kwh=None).count(), 3)
        self.assertGreaterEqual(m["rmse"], m["mae"])


class UncertaintyAndExplainabilityTests(ForecastBase):
    def setUp(self):
        super().setUp()
        make_daily_readings(self.asset, 150)

    def test_interval_brackets_every_point_and_matches_metrics(self):
        f = self.run_fc()
        iv = f.metrics["interval"]
        self.assertEqual(iv["level"], 80)
        self.assertLessEqual(iv["low"], 0 + 1e-9)    # le bas de la fourchette est sous la prévision
        self.assertGreaterEqual(iv["high"], 0 - 1e-9)
        for p in f.points.all():
            self.assertLessEqual(p.lower_kwh, p.predicted_kwh + 1e-6)
            self.assertGreaterEqual(p.upper_kwh, p.predicted_kwh - 1e-6)
            self.assertGreaterEqual(p.lower_kwh, 0)

    def test_interval_covers_about_eighty_percent_of_test_errors(self):
        f = self.run_fc()
        v = f.metrics["validation"]
        iv = f.metrics["interval"]
        inside = sum(1 for a, b in zip(v["actual"], v["best"]) if b + iv["low"] <= a <= b + iv["high"])
        self.assertGreaterEqual(inside / len(v["actual"]), 0.75)

    def test_validation_series_is_aligned_and_not_in_training_period(self):
        f = self.run_fc()
        v = f.metrics["validation"]
        n = len(v["dates"])
        self.assertTrue(n > 0 and n == len(v["actual"]) == len(v["best"]) == len(v["naive"]))
        self.assertEqual(v["dates"], sorted(v["dates"]))
        self.assertGreaterEqual(v["dates"][0], f.training_start.isoformat())

    def test_importance_sums_to_100_and_is_sorted(self):
        f = self.run_fc()
        imp = f.metrics["importance"]
        self.assertTrue(imp)
        self.assertAlmostEqual(sum(r["share"] for r in imp), 100, delta=0.6)
        self.assertEqual([r["share"] for r in imp], sorted((r["share"] for r in imp), reverse=True))
        self.assertTrue(f.metrics["importance_model"])

    def test_interval_offsets_fallback_when_few_residuals(self):
        iv = fc._interval_offsets([1.0, -1.0, 2.0])
        self.assertEqual(iv["method"], "gaussienne")
        self.assertLess(iv["low"], 0 < iv["high"])
        big = fc._interval_offsets(list(range(-50, 50)))
        self.assertEqual(big["method"], "quantiles")
