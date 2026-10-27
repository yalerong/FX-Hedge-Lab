"""报价层：买卖价、来源、时点、可成交价。"""
from __future__ import annotations

import copy
import unittest
from datetime import date, datetime, timedelta, timezone

import quotes
import web_app

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)
ASOF = "2026-10-26T08:00:00Z"


class SpotQuoteTest(unittest.TestCase):
    def config(self, **overrides):
        base = dict(web_app.DEFAULT_CONFIG)
        base.update(overrides)
        return base

    def test_default_spread_is_applied_around_mid(self):
        quote = quotes.spot_quote(self.config(), "USD", 7.18, ASOF)
        self.assertAlmostEqual(quote.bid, 7.18 * 0.9995, places=8)
        self.assertAlmostEqual(quote.ask, 7.18 * 1.0005, places=8)
        self.assertAlmostEqual(quote.spread_bp, 10.0, places=6)
        self.assertEqual(quote.spread_source, "default")
        self.assertEqual(quote.source, "api")

    def test_executable_side_depends_on_action(self):
        quote = quotes.spot_quote(self.config(), "USD", 7.18, ASOF)
        self.assertEqual(quote.executable("sell_foreign"), quote.bid)
        self.assertEqual(quote.executable("buy_foreign"), quote.ask)

    def test_currency_spread_override(self):
        quote = quotes.spot_quote(self.config(spreads_bp={"USD": 20}), "USD", 7.18, ASOF)
        self.assertAlmostEqual(quote.spread_bp, 20.0, places=6)
        self.assertEqual(quote.spread_source, "config")

    def test_basis_pips_shift_the_mid(self):
        quote = quotes.spot_quote(self.config(basis_pips={"USD": 5}), "USD", 7.18, ASOF)
        self.assertAlmostEqual(quote.mid, 7.1805, places=6)
        self.assertEqual(quote.basis_pips, 5.0)

    def test_manual_two_way_quote_wins(self):
        config = self.config(spot_overrides={"USD": {"bid": 7.15, "ask": 7.21}})
        quote = quotes.spot_quote(config, "USD", 7.18, ASOF)
        self.assertEqual((quote.bid, quote.ask), (7.15, 7.21))
        self.assertEqual(quote.source, "manual")
        self.assertEqual(quote.spread_source, "override")

    def test_manual_mid_plus_spread(self):
        config = self.config(spot_overrides={"USD": {"mid": 7.2, "spread_bp": 20}})
        quote = quotes.spot_quote(config, "USD", 7.18, ASOF)
        self.assertAlmostEqual(quote.mid, 7.2, places=6)
        self.assertAlmostEqual(quote.spread_bp, 20.0, places=6)


class ForwardQuoteTest(unittest.TestCase):
    def test_forward_override_by_period_and_currency(self):
        config = dict(web_app.DEFAULT_CONFIG, quote_overrides={
            "2027-03:USD": {"bid": 7.05, "ask": 7.09},
        })
        quote = quotes.forward_quote(config, "USD", "2027-03", 7.08, ASOF)
        self.assertEqual((quote.bid, quote.ask), (7.05, 7.09))
        self.assertEqual(quote.source, "manual")

    def test_forward_override_nested_by_period(self):
        config = dict(web_app.DEFAULT_CONFIG, quote_overrides={
            "2027-03": {"USD": {"mid": 7.08, "spread_bp": 30}},
        })
        quote = quotes.forward_quote(config, "USD", "2027-03", 7.07, ASOF)
        self.assertAlmostEqual(quote.mid, 7.08, places=6)
        self.assertAlmostEqual(quote.spread_bp, 30.0, places=6)

    def test_forward_defaults_to_cip_basis(self):
        quote = quotes.forward_quote(dict(web_app.DEFAULT_CONFIG), "USD", "2027-03", 7.08, ASOF, "cip")
        self.assertEqual(quote.source, "cip")
        self.assertLess(quote.bid, quote.ask)
        self.assertEqual(quote.instrument, "forward")
        self.assertEqual(quote.tenor, "2027-03")


class StalenessTest(unittest.TestCase):
    def test_fresh_quote(self):
        stale, age, reason = quotes.staleness(
            (NOW - timedelta(hours=2)).isoformat(), {"quote_max_age_hours": 24}, NOW
        )
        self.assertFalse(stale)
        self.assertAlmostEqual(age, 2.0, places=3)
        self.assertIsNone(reason)

    def test_old_quote_is_stale(self):
        stale, age, reason = quotes.staleness(
            (NOW - timedelta(hours=30)).isoformat(), {"quote_max_age_hours": 24}, NOW
        )
        self.assertTrue(stale)
        self.assertIn("上限", reason)

    def test_missing_timestamp_is_stale(self):
        stale, age, reason = quotes.staleness(None, {}, NOW)
        self.assertTrue(stale)
        self.assertIsNone(age)
        self.assertIn("时点", reason)

    def test_zero_limit_disables_the_gate(self):
        stale, _, _ = quotes.staleness(
            (NOW - timedelta(days=10)).isoformat(), {"quote_max_age_hours": 0}, NOW
        )
        self.assertFalse(stale)


class BookTest(unittest.TestCase):
    def test_book_honours_the_injected_clock(self):
        # 同一个 asof / 同一个 max_age，只换 now，结论必须不同。
        # 少了这一步，now 参数就是个摆设，测试会随真实时间漂移。
        config = dict(web_app.DEFAULT_CONFIG, quote_max_age_hours=24)
        fresh = quotes.build_book(
            config, {"USD": 7.18}, {}, ASOF, datetime(2026, 10, 26, 9, 0, tzinfo=timezone.utc)
        )
        stale = quotes.build_book(
            config, {"USD": 7.18}, {}, ASOF, datetime(2026, 10, 28, 9, 0, tzinfo=timezone.utc)
        )
        self.assertFalse(fresh["stale"])
        self.assertTrue(stale["stale"])
        self.assertIn("上限", stale["stale_reason"])

    def test_book_contains_spots_and_forwards(self):
        book = quotes.build_book(
            dict(web_app.DEFAULT_CONFIG),
            {"USD": 7.18},
            {("2027-03", "USD"): (7.05, "cip")},
            ASOF,
            NOW,
        )
        self.assertFalse(book["stale"])
        self.assertEqual(book["spot"][0]["currency"], "USD")
        self.assertEqual(book["forwards"][0]["period"], "2027-03")
        self.assertLess(book["forwards"][0]["bid"], book["forwards"][0]["ask"])
        self.assertGreater(book["forwards"][0]["executable_buy"], book["forwards"][0]["executable_sell"])


class DashboardQuoteTest(unittest.TestCase):
    def test_suggestion_uses_the_executable_side_and_scenario_uses_mid(self):
        state = {
            "config": dict(web_app.DEFAULT_CONFIG, default_hedge_ratio=0.8, quote_max_age_hours=0),
            "exposures": [{
                "id": "e", "due_date": "2027-03-31", "currency": "USD", "amount": 1_000_000,
                "direction": "receipt", "category": "order_contract", "probability": 1,
            }],
            "hedges": [],
            "settlements": [],
        }
        rates = {"source": "t", "status": "t", "fetched_at": ASOF, "pair_rates": {"USD": 7.2}}
        data = web_app.build_dashboard(state, rates, forecast_doc={}, today=date(2026, 10, 26))
        suggestion = data["suggestions"][0]
        self.assertLess(suggestion["bid"], suggestion["mid_rate"])
        self.assertLess(suggestion["mid_rate"], suggestion["ask"])
        self.assertEqual(suggestion["trade_rate"], suggestion["bid"])
        self.assertGreater(suggestion["spread_cost_cny"], 0)
        # 情景按中间价估值：把价差从 10bp 改成 0，情景合计不应该变
        wide = copy.deepcopy(state)
        wide["config"]["default_spread_bp"] = 0
        narrow = web_app.build_dashboard(wide, rates, forecast_doc={}, today=date(2026, 10, 26))
        self.assertEqual(
            data["scenario_totals"]["neutral"]["total_projected_gain_loss"],
            narrow["scenario_totals"]["neutral"]["total_projected_gain_loss"],
        )
        self.assertNotEqual(
            data["suggestions"][0]["trade_rate"], narrow["suggestions"][0]["trade_rate"]
        )

    def test_quotes_block_is_exposed(self):
        rates = {"source": "t", "status": "t", "fetched_at": ASOF, "pair_rates": {"USD": 7.18}}
        data = web_app.build_dashboard(web_app.sample_state(), rates, forecast_doc={})
        self.assertIn("quotes", data)
        self.assertIn("spot", data["quotes"])


class QuoteConfigValidationTest(unittest.TestCase):
    def test_two_way_override_requires_both_sides(self):
        with self.assertRaises(ValueError):
            web_app.validate_config({"spot_overrides": {"USD": {"bid": 7.1}}})

    def test_ask_cannot_be_below_bid(self):
        with self.assertRaises(ValueError):
            web_app.validate_config({"spot_overrides": {"USD": {"bid": 7.2, "ask": 7.1}}})

    def test_unknown_quote_field(self):
        with self.assertRaises(ValueError):
            web_app.validate_config({"spot_overrides": {"USD": {"last": 7.1}}})

    def test_quote_overrides_key_must_be_a_period(self):
        with self.assertRaises(ValueError):
            web_app.validate_config({"quote_overrides": {"2027-13:USD": {"mid": 7.1}}})

    def test_spread_bounds(self):
        with self.assertRaises(ValueError):
            web_app.validate_config({"spreads_bp": {"USD": 2000}})
        with self.assertRaises(ValueError):
            web_app.validate_config({"default_spread_bp": -1})
        with self.assertRaises(ValueError):
            web_app.validate_config({"quote_max_age_hours": -1})


if __name__ == "__main__":
    unittest.main()
