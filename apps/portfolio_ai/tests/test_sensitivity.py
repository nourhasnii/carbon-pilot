from decimal import Decimal

from django.test import SimpleTestCase

from apps.portfolio_ai.services.optimizer import ProjectItem
from apps.portfolio_ai.services.sensitivity import run_sensitivity


class SensitivityTests(SimpleTestCase):
	def items(self):
		return [
			ProjectItem(1, "A", Decimal("100"), Decimal("100"), Decimal("10"), Decimal("20"), Decimal("5")),
			ProjectItem(2, "B", Decimal("200"), Decimal("180"), Decimal("20"), Decimal("40"), Decimal("5")),
			ProjectItem(3, "C", Decimal("300"), Decimal("500"), Decimal("30"), Decimal("60"), Decimal("5")),
		]

	def test_factor_one_matches_base_and_score_is_bounded(self):
		result = run_sensitivity(self.items(), Decimal("300"), "MAX_TCO2E")
		self.assertGreaterEqual(result["stability_score"], Decimal("0"))
		self.assertLessEqual(result["stability_score"], Decimal("1"))
		base_cases = [
			case for case in result["cases"]
			if case["factor"] == Decimal("1.0")
		]
		self.assertTrue(all(case["selected_ids"] == result["base"]["selected_ids"] for case in base_cases))

	def test_energy_price_is_not_applicable_for_tco2e(self):
		result = run_sensitivity(self.items(), Decimal("300"), "MAX_TCO2E")
		energy_cases = [case for case in result["cases"] if case["parameter"] == "energy_price"]
		self.assertTrue(all(case["applicable"] is False for case in energy_cases))

	def test_a_cost_factor_can_change_selection(self):
		result = run_sensitivity(self.items(), Decimal("300"), "MAX_ROI")
		cost_cases = [case for case in result["cases"] if case["parameter"] == "cost_factor"]
		self.assertTrue(any(case["selected_ids"] != result["base"]["selected_ids"] for case in cost_cases))
