from io import StringIO

from django.core.management import call_command
from django.test import SimpleTestCase

from apps.energy.services.evaluation import evaluate, make_series


class AnomalyEvaluationTests(SimpleTestCase):
    def test_series_generation_is_deterministic_and_has_known_truth(self):
        a, truth_a = make_series(3)
        b, truth_b = make_series(3)
        self.assertEqual(a, b)
        self.assertEqual(truth_a, truth_b)
        self.assertEqual(len(truth_a), 6)

    def test_detector_quality_on_known_anomalies(self):
        """Seuils volontairement en dessous des valeurs mesurées (F1 ≈ 98 % à 3 % de bruit)."""
        r = evaluate(seeds=range(10), noise=0.03)
        self.assertGreaterEqual(r["recall"], 0.95)
        self.assertGreaterEqual(r["precision"], 0.90)
        self.assertGreaterEqual(r["f1"], 0.93)
        self.assertEqual(r["tp"] + r["fn"], 60)  # 10 séries x 6 anomalies injectées

    def test_quality_degrades_with_heavy_noise_but_stays_reasonable(self):
        calm = evaluate(seeds=range(10), noise=0.03)
        noisy = evaluate(seeds=range(10), noise=0.10)
        self.assertLessEqual(noisy["recall"], calm["recall"])
        self.assertGreaterEqual(noisy["f1"], 0.80)

    def test_command_prints_confusion_matrix(self):
        out = StringIO()
        call_command("evaluate_anomalies", "--series", "3", stdout=out)
        text = out.getvalue()
        self.assertIn("Précision", text)
        self.assertIn("Vraiment anormal", text)
