"""P0 修复的回归测试。

每一条都对应一个会算错钱的具体行为，不是"覆盖率打卡"：

1. 同向持仓被当成覆盖 → 少锁；
2. 预测拿序列最后一个月去套任意到期月 → 少锁；
3. 情景表不含存量远期 → 损益被低估；
4. 跨月交割的对冲归错期间 → 重复锁汇；
5. 接口失败时用硬编码汇率兜底 → 拿假价格做真决策；
6. `/web/../` 路径穿越 → 源码和工作区数据可被读走。

外加两个数据安全洞：导入 `{}` 清空工作区、`actual_amount` 的 NaN 污染状态。
"""
from __future__ import annotations

import unittest
from datetime import date
from unittest import mock
from urllib.error import URLError

import web_app

RATES = {
    "source": "test",
    "status": "test",
    "fetched_at": "2026-10-26T00:00:00Z",
    "pair_rates": {"USD": 7.2, "EUR": 7.8},
}
TODAY = date(2026, 10, 26)


def exposure(amount, direction="receipt", period="2027-03", currency="USD"):
    return {
        "id": "e1", "due_date": f"{period}-28", "currency": currency,
        "amount": amount, "direction": direction,
        "category": "order_contract", "probability": 1,
    }


def hedge(amount, action="sell_foreign", due="2027-03-28", currency="USD", rate=7.0, **extra):
    row = {
        "id": "h1", "trade_date": "2026-10-20", "due_date": due, "currency": currency,
        "amount": amount, "action": action, "locked_rate": rate,
    }
    row.update(extra)
    return row


def dashboard(exposures, hedges, settlements=(), config=None, forecast=None):
    state = {
        "config": dict(web_app.DEFAULT_CONFIG, **(config or {})),
        "exposures": list(exposures),
        "hedges": list(hedges),
        "settlements": list(settlements),
    }
    return web_app.build_dashboard(state, RATES, forecast_doc=forecast or {}, today=TODAY)


def signal(rows, current=7.2, mape=0.01, tier="support", current_month="2026-10"):
    return {
        "tier": tier,
        "direction": "up",
        "mape": mape,
        "current": current,
        "current_month": current_month,
        "forecast": [{"month": month, "rate": rate} for month, rate in rows],
    }


class SignedCoverageTest(unittest.TestCase):
    def test_same_direction_position_is_not_coverage(self):
        """100 万收汇配 20 万买入是加仓，不能从建议里扣掉。"""
        data = dashboard([exposure(1_000_000)], [hedge(200_000, action="buy_foreign")])
        suggestion = data["suggestions"][0]
        self.assertEqual(suggestion["covered_exposure"], 0)
        self.assertEqual(suggestion["recommended_amount"], 800_000)
        # 驾驶舱口径一直是 0，现在建议金额和它一致了
        self.assertEqual(data["portfolio"]["locked_cny"], 0)

    def test_opposite_direction_hedge_is_still_subtracted(self):
        data = dashboard([exposure(1_000_000)], [hedge(400_000)])
        self.assertEqual(data["suggestions"][0]["recommended_amount"], 400_000)

    def test_over_hedge_caps_coverage_at_the_exposure(self):
        data = dashboard([exposure(1_000_000)], [hedge(1_500_000)])
        self.assertEqual(data["suggestions"], [])
        self.assertEqual(web_app.covered_notional(1_000_000, -1_500_000), 1_000_000)
        self.assertEqual(web_app.covered_notional(1_000_000, 200_000), 0)


class ForecastPeriodTest(unittest.TestCase):
    def test_uses_the_maturity_month_not_the_last_month(self):
        """11 月到期、11 月预测不动、12 月才涨 —— 不能按 12 月打折。"""
        data = dashboard(
            [exposure(1_000_000, period="2026-11")],
            [],
            forecast={"generated_at": web_app.now_iso(),
                      "signals": {"USD": signal([("2026-11", 7.2), ("2026-12", 7.9)])}},
        )
        suggestion = data["suggestions"][0]
        self.assertEqual(suggestion["forecast_multiplier"], 1.0)
        self.assertIn("持平", suggestion["forecast_reason"])
        self.assertEqual(suggestion["recommended_amount"], 800_000)

    def test_near_month_move_beyond_noise_discounts(self):
        data = dashboard(
            [exposure(1_000_000, period="2026-11")],
            [],
            forecast={"generated_at": web_app.now_iso(),
                      "signals": {"USD": signal([("2026-11", 7.416), ("2026-12", 7.5)])}},
        )
        self.assertEqual(data["suggestions"][0]["forecast_multiplier"], 0.5)
        self.assertEqual(data["suggestions"][0]["recommended_amount"], 400_000)

    def test_far_month_move_is_compared_with_scaled_error(self):
        """6 个月 +2% 的变动，要对着 1 个月误差 ×√6 比，不能对着 1 个月比。"""
        rows = [("2026-11", 7.2), ("2026-12", 7.2), ("2027-01", 7.2),
                ("2027-02", 7.2), ("2027-03", 7.2), ("2027-04", 7.344)]
        data = dashboard(
            [exposure(1_000_000, period="2027-04")],
            [],
            forecast={"generated_at": web_app.now_iso(), "signals": {"USD": signal(rows)}},
        )
        suggestion = data["suggestions"][0]
        self.assertEqual(suggestion["forecast_multiplier"], 1.0)
        self.assertIn("6 个月模型误差", suggestion["forecast_reason"])

    def test_stale_signal_is_ignored_and_surfaced(self):
        stale = {"generated_at": "2026-01-01T00:00:00Z",
                 "signals": {"USD": signal([("2026-11", 7.9)])}}
        data = dashboard(
            [exposure(1_000_000, period="2026-11")], [], forecast=stale
        )
        self.assertTrue(data["forecast_stale"])
        self.assertIn("有效期", data["forecast_stale_reason"])
        self.assertEqual(data["suggestions"][0]["forecast_multiplier"], 1.0)
        self.assertEqual(data["suggestions"][0]["recommended_amount"], 800_000)

    def test_signal_without_timestamp_is_treated_as_stale(self):
        data = dashboard(
            [exposure(1_000_000, period="2026-11")], [],
            forecast={"signals": {"USD": signal([("2026-11", 7.9)])}},
        )
        self.assertTrue(data["forecast_stale"])
        self.assertIn("生成时间", data["forecast_stale_reason"])
        self.assertEqual(data["suggestions"][0]["recommended_amount"], 800_000)

    def test_no_signal_is_not_stale(self):
        data = dashboard([exposure(1_000_000)], [], forecast={})
        self.assertFalse(data["forecast_stale"])
        self.assertIsNone(data["forecast_stale_reason"])


class ScenarioMarksExistingHedgesTest(unittest.TestCase):
    def test_existing_hedge_is_marked_at_scenario_price(self):
        # 目标 50%，500k 已锁 → 没有新增建议，情景只反映存量
        data = dashboard(
            [exposure(1_000_000)],
            [hedge(500_000, rate=7.0)],
            config={"default_hedge_ratio": 0.5},
        )
        totals = data["scenario_totals"]
        # 中性：业务敞口不动，存量远期按 7.2 重估
        self.assertEqual(totals["neutral"]["total_projected_gain_loss"], -100_000)
        # 悲观 -3%：业务 -216,000 + 远期 +8,000
        self.assertEqual(totals["pessimistic"]["total_projected_gain_loss"], -208_000)

    def test_fully_hedged_book_is_scenario_invariant(self):
        """全部锁掉之后，任何汇率下合计都等于锁定的远期点。"""
        data = dashboard(
            [exposure(1_000_000)],
            [hedge(1_000_000, rate=7.0)],
            config={"default_hedge_ratio": 1.0},
        )
        for name, row in data["scenario_totals"].items():
            self.assertEqual(row["total_projected_gain_loss"], -200_000, name)

    def test_scenario_row_reports_the_hedge_book(self):
        data = dashboard(
            [exposure(1_000_000)],
            [hedge(500_000), hedge(200_000, rate=7.1)],
        )
        row = data["scenario_rows"][0]
        self.assertEqual(row["hedge_count"], 2)
        self.assertEqual(row["existing_hedge_notional"], 700_000)


class HedgePeriodTest(unittest.TestCase):
    def test_designated_period_wins(self):
        self.assertEqual(
            web_app.hedge_period(
                {"due_date": "2027-04-02", "currency": "USD", "designated_period": "2027-03"},
                {("2027-03", "USD")}, 5,
            ),
            "2027-03",
        )

    def test_t_plus_two_falls_back_to_the_exposure_month(self):
        self.assertEqual(
            web_app.hedge_period(
                {"due_date": "2027-04-02", "currency": "USD"}, {("2027-03", "USD")}, 5
            ),
            "2027-03",
        )

    def test_ambiguous_month_is_not_guessed(self):
        # 4 月本身也有敞口，就不能把 4/2 的交割硬算回 3 月
        self.assertEqual(
            web_app.hedge_period(
                {"due_date": "2027-04-02", "currency": "USD"},
                {("2027-03", "USD"), ("2027-04", "USD")}, 5,
            ),
            "2027-04",
        )

    def test_grace_can_be_disabled(self):
        self.assertEqual(
            web_app.hedge_period(
                {"due_date": "2027-04-02", "currency": "USD"}, {("2027-03", "USD")}, 0
            ),
            "2027-04",
        )

    def test_dashboard_does_not_double_recommend_across_the_month_boundary(self):
        data = dashboard(
            [exposure(1_000_000)],
            [hedge(500_000, due="2027-04-02")],
        )
        periods = [row["period"] for row in data["net_exposures"]]
        self.assertEqual(periods, ["2027-03"])
        self.assertEqual(data["suggestions"][0]["recommended_amount"], 300_000)

    def test_real_april_exposure_keeps_the_hedge_in_april(self):
        data = dashboard(
            [exposure(1_000_000), exposure(200_000, period="2027-04")],
            [hedge(500_000, due="2027-04-02")],
        )
        by_period = {row["period"]: row for row in data["net_exposures"]}
        self.assertEqual(by_period["2027-03"]["locked_exposure"], 0)
        self.assertEqual(by_period["2027-04"]["locked_exposure"], -500_000)


class NoFallbackRateTest(unittest.TestCase):
    def test_failed_fetch_returns_no_rates_at_all(self):
        with mock.patch.object(web_app, "read_json", return_value={}), \
                mock.patch.object(web_app, "write_json"), \
                mock.patch.object(web_app, "urlopen", side_effect=URLError("down")):
            cache = web_app.load_rates({})
        self.assertEqual(cache["pair_rates"], {})
        self.assertEqual(cache["status"], "unavailable")
        self.assertNotIn("fallback", cache["source"])

    def test_missing_rates_block_advice_instead_of_faking_it(self):
        data = web_app.build_dashboard(
            web_app.sample_state(),
            {"source": "无可用报价", "status": "unavailable", "pair_rates": {}},
            forecast_doc={},
        )
        self.assertEqual(data["suggestions"], [])
        self.assertEqual(data["scenario_rows"], [])
        self.assertIn("USD", data["portfolio"]["rate_missing"])


class StaticPathTest(unittest.TestCase):
    def test_traversal_is_rejected(self):
        for path in ("/web/../web_app.py", "/web/../data/fx_workspace.json",
                     "/web/../../secret"):
            with self.subTest(path=path):
                self.assertIsNone(web_app.resolve_web_path(path))

    def test_normal_asset_is_served(self):
        self.assertEqual(
            web_app.resolve_web_path("/web/styles.css"),
            (web_app.WEB_ROOT / "styles.css").resolve(),
        )


class DataSafetyTest(unittest.TestCase):
    def test_empty_import_is_rejected(self):
        for payload in ({}, {"workspace": {}}, {"state": {"metadata": []}}):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    web_app.imported_workspace_from_body(payload)

    def test_full_export_imports(self):
        state = web_app.imported_workspace_from_body({"state": web_app.sample_state()})
        self.assertEqual(len(state["exposures"]), 2)

    def test_nan_actual_amount_is_rejected(self):
        for value in ("nan", "inf", "-inf"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    web_app.validate_settlement({
                        "due_date": "2027-03-31", "currency": "USD",
                        "actual_rate": 7.1, "actual_amount": value,
                    })

    def test_zero_actual_amount_is_still_allowed(self):
        row = {"due_date": "2027-03-31", "currency": "USD",
               "actual_rate": 7.1, "actual_amount": 0}
        web_app.validate_settlement(row)
        self.assertEqual(row["actual_amount"], 0.0)


if __name__ == "__main__":
    unittest.main()
