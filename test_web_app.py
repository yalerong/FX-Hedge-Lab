import unittest

import web_app


class WebAppLogicTest(unittest.TestCase):
    def setUp(self):
        self.rates = {
            "source": "test",
            "status": "test",
            "fetched_at": "2026-05-12T00:00:00Z",
            "pair_rates": {"USD": 7.2, "EUR": 7.8},
        }

    def test_dashboard_builds_suggestions_and_backtest(self):
        dashboard = web_app.build_dashboard(web_app.DEMO_STATE, self.rates)

        self.assertEqual(len(dashboard["exposures"]), 2)
        self.assertEqual(len(dashboard["hedges"]), 1)
        self.assertEqual(len(dashboard["suggestions"]), 2)
        self.assertGreater(len(dashboard["plain_language"]), 2)

        usd = next(row for row in dashboard["net_exposures"] if row["currency"] == "USD")
        self.assertEqual(usd["business_exposure"], 1200000)
        self.assertEqual(usd["locked_exposure"], -500000)
        self.assertEqual(usd["net_exposure"], 700000)
        self.assertEqual(usd["target_hedge_ratio"], 0.8)

        usd_suggestion = next(row for row in dashboard["suggestions"] if row["currency"] == "USD")
        self.assertEqual(usd_suggestion["recommended_amount"], 460000)
        self.assertIn("neutral", usd_suggestion["scenario_projection"])
        self.assertIn("fair_value_change_gain_loss", usd_suggestion["scenario_projection"]["optimistic"])

        backtest_usd = next(row for row in dashboard["backtest"] if row["currency"] == "USD")
        self.assertEqual(backtest_usd["hedge_effect_cny"], -15000)

    def test_pair_rates_from_open_endpoint_payload(self):
        payload = {"rates": {"USD": 1, "CNY": 7.2, "EUR": 0.9}}
        rates = web_app.pair_rates_from_payload(payload, ["USD", "EUR"])

        self.assertEqual(rates["USD"], 7.2)
        self.assertEqual(rates["EUR"], 8.0)


if __name__ == "__main__":
    unittest.main()
