"""历史波动率与历史模拟 VaR。"""
from __future__ import annotations

import unittest

import history
import risk
import web_app
from datetime import date

TODAY = date(2026, 10, 26)


class SeriesTest(unittest.TestCase):
    def test_usd_daily_series_is_available(self):
        rows = history.series_for("USD")
        self.assertGreater(len(rows), 250)
        self.assertTrue(all(value > 0 for _, value in rows))
        self.assertEqual(rows, sorted(rows))

    def test_jpy_is_derived_from_usdjpy_and_usdcny(self):
        rows = history.series_for("JPY")
        self.assertGreater(len(rows), 250)
        # 1 JPY 约 0.04~0.06 CNY
        self.assertTrue(0.03 < rows[-1][1] < 0.08)

    def test_unknown_currency_has_no_series(self):
        self.assertEqual(history.series_for("EUR"), [])

    def test_annual_vol(self):
        returns = [0.001, -0.001] * 100
        vol = history.annual_vol(returns)
        self.assertIsNotNone(vol)
        self.assertGreater(vol, 0)
        self.assertLess(vol, 1)


class HistoricalSimTest(unittest.TestCase):
    def config(self, **overrides):
        base = dict(web_app.DEFAULT_CONFIG)
        base.update(overrides)
        return base

    def test_usd_book_is_available(self):
        result = history.historical_sim({"USD": 7_000_000}, self.config(), 0.99, 1)
        self.assertTrue(result["available"])
        self.assertGreater(result["var_cny"], 0)
        self.assertGreaterEqual(result["observations"], 250)
        self.assertEqual(result["currencies"], ["USD"])
        self.assertIn("USD", result["vols"])

    def test_missing_currency_blocks_the_whole_simulation(self):
        result = history.historical_sim({"USD": 7_000_000, "EUR": -3_000_000}, self.config(), 0.99, 1)
        self.assertFalse(result["available"])
        self.assertIn("EUR", result["missing"])
        self.assertIn("EUR", result["reason"])

    def test_window_is_respected(self):
        result = history.historical_sim(
            {"USD": 7_000_000}, self.config(historical_window_days=100), 0.99, 1
        )
        self.assertEqual(result["days"], 100)
        self.assertEqual(result["observations"], 100)

    def test_horizon_reduces_observations(self):
        one_day = history.historical_sim({"USD": 7_000_000}, self.config(), 0.99, 1)
        five_day = history.historical_sim({"USD": 7_000_000}, self.config(), 0.99, 5)
        self.assertEqual(five_day["observations"], one_day["observations"] - 4)

    def test_empty_book(self):
        result = history.historical_sim({}, self.config(), 0.99, 1)
        self.assertFalse(result["available"])


class RiskIntegrationTest(unittest.TestCase):
    def test_dashboard_exposes_historical_block(self):
        state = {
            "config": dict(web_app.DEFAULT_CONFIG),
            "exposures": [{
                "id": "e", "due_date": "2027-03-31", "currency": "USD", "amount": 1_000_000,
                "direction": "receipt", "category": "order_contract", "probability": 1,
            }],
            "hedges": [],
            "settlements": [],
        }
        rates = {"source": "t", "status": "t", "fetched_at": "2026-10-26T00:00:00Z",
                 "pair_rates": {"USD": 7.18}}
        data = web_app.build_dashboard(state, rates, forecast_doc={}, today=TODAY)
        historical = data["risk"]["historical"]
        self.assertTrue(historical["available"])
        self.assertGreater(historical["var_cny"], 0)
        self.assertIsNotNone(data["risk"]["positions"][0]["historical_vol"])

    def test_parametric_and_historical_are_both_reported(self):
        rows = [{
            "period": "2027-01", "currency": "USD", "business_exposure": 1_000_000,
            "locked_exposure": 0, "net_exposure": 1_000_000,
            "current_rate": 7.0, "rate_available": True,
        }]
        result = risk.build(rows, dict(web_app.DEFAULT_CONFIG), TODAY)
        self.assertIn("var_cny", result)
        self.assertIn("historical", result)
        self.assertTrue(any("历史模拟" in note for note in result["notes"]))


class ConfigTest(unittest.TestCase):
    def test_window_bounds(self):
        for value in (10, 3000):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    web_app.validate_config({"historical_window_days": value})
        self.assertEqual(
            web_app.validate_config({"historical_window_days": 300})["historical_window_days"], 300
        )

    def test_hedge_standard(self):
        with self.assertRaises(ValueError):
            web_app.validate_config({"hedge_standard": "GAAP"})
        self.assertEqual(web_app.validate_config({"hedge_standard": "IFRS9"})["hedge_standard"], "IFRS9")

    def test_entity_name_is_trimmed_and_bounded(self):
        config = web_app.validate_config({"entity_name": "  ABC  "})
        self.assertEqual(config["entity_name"], "ABC")
        with self.assertRaises(ValueError):
            web_app.validate_config({"entity_name": 123})


if __name__ == "__main__":
    unittest.main()
