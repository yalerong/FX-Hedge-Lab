"""对冲会计底稿：指定关系、有效性、OCI / 损益拆分。"""
from __future__ import annotations

import unittest
from datetime import date

import hedge_accounting
import web_app

TODAY = date(2026, 10, 26)
CONFIG = {
    "base_currency": "CNY",
    "interest_rates": {"CNY": 0.019, "USD": 0.043},
}


def hedge(amount=700_000, action="sell_foreign", locked=7.18,
          due="2026-06-30", trade="2026-05-01", **extra):
    row = {
        "id": f"h-{amount}-{locked}",
        "trade_date": trade,
        "due_date": due,
        "currency": "USD",
        "amount": amount,
        "action": action,
        "locked_rate": locked,
    }
    row.update(extra)
    return row


def group(gross=900_000, period="2026-06", category="order_contract", hedges=None):
    return {
        "period": period,
        "currency": "USD",
        "gross": gross,
        "category": category,
        "hedges": hedges or [],
    }


class EffectivenessTest(unittest.TestCase):
    def test_settled_cash_flow_hedge_is_fully_effective_and_reclassified(self):
        result = hedge_accounting.build(
            [group(hedges=[hedge()])],
            {"USD": 7.0},
            CONFIG,
            TODAY,
            settled_rates={("2026-06", "USD"): 7.05},
        )
        row = result["rows"][0]
        self.assertTrue(row["settled"])
        self.assertEqual(row["rate_basis"], "settlement")
        self.assertEqual(row["spot_rate"], 7.05)
        self.assertAlmostEqual(row["effectiveness"], 1.0, places=6)
        self.assertEqual(row["delta_fv_cny"], 91_000)
        self.assertEqual(row["effective_cny"], 91_000)
        self.assertEqual(row["ineffective_cny"], 0)
        self.assertEqual(row["oci_cny"], 0)
        self.assertEqual(row["reclassified_cny"], 91_000)
        self.assertEqual(row["pl_cny"], 91_000)

    def test_open_cash_flow_hedge_uses_the_forward_curve(self):
        result = hedge_accounting.build(
            [group(gross=1_200_000, period="2027-04",
                   hedges=[hedge(500_000, locked=7.05, due="2027-04-30", trade="2026-10-20")])],
            {"USD": 7.18},
            CONFIG,
            TODAY,
        )
        row = result["rows"][0]
        self.assertEqual(row["item_basis"], "forward")
        self.assertAlmostEqual(row["effectiveness"], 1.0, places=4)
        self.assertLess(row["oci_cny"], 0)
        self.assertEqual(row["pl_cny"], 0)
        self.assertGreater(row["delta_item_cny"], 0)

    def test_fair_value_hedge_uses_spot_and_hits_profit_and_loss(self):
        result = hedge_accounting.build(
            [group(gross=1_000_000, period="2027-01", category="balance_sheet",
                   hedges=[hedge(1_000_000, locked=7.0, due="2027-01-31",
                                trade="2026-10-20", spot_at_trade=7.2)])],
            {"USD": 7.1},
            CONFIG,
            TODAY,
        )
        row = result["rows"][0]
        self.assertEqual(row["hedge_type"], "fair_value")
        self.assertEqual(row["item_basis"], "spot")
        self.assertEqual(row["oci_cny"], 0)
        self.assertEqual(row["pl_cny"], row["delta_fv_cny"])

    def test_under_hedge_only_tests_the_designated_part(self):
        result = hedge_accounting.build(
            [group(gross=1_000_000, hedges=[
                hedge(300_000, trade="2026-05-01"),
                hedge(200_000, locked=7.1, trade="2026-05-02"),
            ])],
            {"USD": 7.2},
            CONFIG,
            TODAY,
        )
        self.assertEqual([row["allocated"] for row in result["rows"]], [300_000, 200_000])

    def test_second_hedge_beyond_the_exposure_is_not_designated(self):
        result = hedge_accounting.build(
            [group(gross=500_000, hedges=[
                hedge(400_000, trade="2026-05-01"),
                hedge(300_000, locked=7.1, trade="2026-05-02"),
            ])],
            {"USD": 7.2},
            CONFIG,
            TODAY,
        )
        second = result["rows"][1]
        self.assertEqual(second["allocated"], 100_000)
        self.assertTrue(any("未指定" in note for note in second["warnings"]))

    def test_no_exposure_means_no_designation(self):
        result = hedge_accounting.build(
            [group(gross=0, hedges=[hedge()])],
            {"USD": 7.2},
            CONFIG,
            TODAY,
        )
        row = result["rows"][0]
        self.assertEqual(row["allocated"], 0)
        self.assertIsNone(row["effectiveness"])
        self.assertTrue(row["warnings"])

    def test_missing_spot_at_trade_is_flagged(self):
        result = hedge_accounting.build(
            [group(gross=1_000_000, hedges=[hedge(500_000)])],
            {"USD": 7.2},
            CONFIG,
            TODAY,
        )
        self.assertTrue(any("缺交易日即期" in note for note in result["rows"][0]["warnings"]))

    def test_settlement_rate_works_without_a_market_rate(self):
        result = hedge_accounting.build(
            [group(hedges=[hedge()])],
            {},
            CONFIG,
            TODAY,
            settled_rates={("2026-06", "USD"): 7.05},
        )
        self.assertEqual(len(result["rows"]), 1)
        self.assertEqual(result["rows"][0]["spot_rate"], 7.05)

    def test_unknown_type_falls_back_with_a_warning(self):
        result = hedge_accounting.build(
            [group(hedges=[hedge(hedge_type="bogus")])],
            {"USD": 7.2},
            CONFIG,
            TODAY,
        )
        row = result["rows"][0]
        self.assertEqual(row["hedge_type"], "cash_flow")
        self.assertTrue(any("未知套期类型" in note for note in row["warnings"]))

    def test_default_type_follows_the_category(self):
        self.assertEqual(hedge_accounting.default_type("balance_sheet"), "fair_value")
        self.assertEqual(hedge_accounting.default_type("order_contract"), "cash_flow")
        self.assertEqual(hedge_accounting.default_type("cash_flow"), "cash_flow")

    def test_dashboard_exposes_the_workpaper(self):
        data = web_app.build_dashboard(
            web_app.sample_state(),
            {"source": "t", "status": "t", "fetched_at": "x",
             "pair_rates": {"USD": 7.18, "EUR": 7.72}},
            forecast_doc={},
        )
        accounting = data["hedge_accounting"]
        self.assertIn("totals", accounting)
        self.assertEqual(len(accounting["rows"]), 1)
        self.assertEqual(accounting["totals"]["reclassified_cny"], -15_000)


if __name__ == "__main__":
    unittest.main()
