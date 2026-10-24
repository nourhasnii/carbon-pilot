from decimal import Decimal

from django.test import SimpleTestCase

from apps.investments.services.finance import (
	cost_per_tco2e,
	cumulative_tco2e,
	net_cost,
	planned_vs_actual,
	roi,
	simple_payback_years,
)


class FinanceTests(SimpleTestCase):
	def test_financial_values_are_rounded_with_decimal(self):
		self.assertEqual(net_cost(None, Decimal("100.005")), Decimal("100.01"))
		self.assertEqual(cumulative_tco2e(Decimal("2.5"), 4), Decimal("10.00"))
		self.assertEqual(cost_per_tco2e(Decimal("100"), Decimal("2.5"), 4), Decimal("10.00"))
		self.assertEqual(simple_payback_years(Decimal("100"), Decimal("25")), Decimal("4.00"))
		self.assertEqual(roi(Decimal("100"), Decimal("25"), 4), Decimal("0.00"))

	def test_division_by_zero_returns_none(self):
		self.assertIsNone(cumulative_tco2e(Decimal("0"), 4))
		self.assertIsNone(cost_per_tco2e(Decimal("100"), Decimal("0"), 4))
		self.assertIsNone(simple_payback_years(Decimal("100"), Decimal("0")))
		self.assertIsNone(roi(Decimal("0"), Decimal("25"), 4))

	def test_planned_vs_actual(self):
		self.assertEqual(
			planned_vs_actual(Decimal("100"), Decimal("125")),
			{"absolute": Decimal("25.00"), "relative": Decimal("0.25")},
		)
		self.assertIsNone(planned_vs_actual(Decimal("100"), None))
		self.assertIsNone(planned_vs_actual(Decimal("0"), Decimal("25"))["relative"])
