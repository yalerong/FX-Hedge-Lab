"""远期价与月均基准的测试。"""
from __future__ import annotations

import unittest
from datetime import date

import benchmarks
import forwards
import web_app


CONFIG = dict(
    web_app.DEFAULT_CONFIG,
    interest_rates={"CNY": 0.02, "USD": 0.05},
    forward_overrides={},
)
TODAY = date(2026, 1, 1)


class ForwardRateTest(unittest.TestCase):
    def test_higher_foreign_rate_gives_a_discount(self):
        # 美元利率高于人民币：远期结汇价低于即期（远期贴水），这是利差决定的，
        # 不是对汇率走势的判断。
        result = forwards.forward_rate(7.2, "USD", "2026-12", CONFIG, today=TODAY)
        self.assertEqual(result["basis"], "cip")
        self.assertLess(result["rate"], 7.2)
        self.assertLess(result["points"], 0)

    def test_matches_covered_interest_parity_formula(self):
        result = forwards.forward_rate(7.2, "USD", "2026-12", CONFIG, today=TODAY)
        days = round(result["tenor_years"] * forwards.DAYS_PER_YEAR)
        # 两条腿各按自己的货币市场基准折年：人民币 ACT/365，美元 ACT/360。
        expected = (
            7.2
            * (1 + 0.02 * forwards.accrual_years(days, "CNY"))
            / (1 + 0.05 * forwards.accrual_years(days, "USD"))
        )
        self.assertAlmostEqual(result["rate"], expected, places=10)
        self.assertEqual(result["day_count"], {"base": "ACT/365", "foreign": "ACT/360"})

    def test_day_count_basis_follows_the_currency(self):
        # 用 365 一把尺子量所有币种，1 年期美元/人民币远期会偏 5~6bp。
        self.assertEqual(forwards.day_count_basis("USD"), "act360")
        self.assertEqual(forwards.day_count_basis("eur"), "act360")
        self.assertEqual(forwards.day_count_basis("CNY"), "act365")
        self.assertEqual(forwards.day_count_basis("GBP"), "act365")
        self.assertAlmostEqual(forwards.accrual_years(360, "USD"), 1.0)
        self.assertAlmostEqual(forwards.accrual_years(360, "CNY"), 360 / 365)

    def test_longer_tenor_means_bigger_points(self):
        near = forwards.forward_rate(7.2, "USD", "2026-03", CONFIG, today=TODAY)
        far = forwards.forward_rate(7.2, "USD", "2026-12", CONFIG, today=TODAY)
        self.assertGreater(abs(far["points"]), abs(near["points"]))

    def test_bank_quote_wins_over_the_formula(self):
        config = dict(CONFIG, forward_overrides={"2026-12:USD": 7.05})
        result = forwards.forward_rate(7.2, "USD", "2026-12", config, today=TODAY)
        self.assertEqual(result["basis"], "quote")
        self.assertEqual(result["rate"], 7.05)

    def test_bare_period_override_must_name_the_currency(self):
        # {"2026-12": 7.05} 是个美元报价，套到欧元上会差 10% 还标着「银行报价」
        config = dict(CONFIG, forward_overrides={"2026-12": 7.05})
        result = forwards.forward_rate(7.8, "EUR", "2026-12", config, today=TODAY)
        self.assertNotEqual(result["basis"], "quote")
        # 写成 {月份: {币种: 报价}} 才生效
        config = dict(CONFIG, forward_overrides={"2026-12": {"EUR": 7.7}})
        result = forwards.forward_rate(7.8, "EUR", "2026-12", config, today=TODAY)
        self.assertEqual(result["basis"], "quote")
        self.assertEqual(result["rate"], 7.7)

    def test_missing_interest_rate_falls_back_to_spot_and_says_so(self):
        config = dict(CONFIG, interest_rates={"CNY": 0.02})
        result = forwards.forward_rate(7.2, "EUR", "2026-12", config, today=TODAY)
        self.assertEqual(result["basis"], "spot")
        self.assertEqual(result["rate"], 7.2)
        self.assertIn("EUR", result["note"])

    def test_past_period_has_no_forward_points(self):
        result = forwards.forward_rate(7.2, "USD", "2025-06", CONFIG, today=TODAY)
        self.assertEqual(result["tenor_years"], 0.0)
        self.assertEqual(result["points"], 0.0)
        self.assertEqual(result["basis"], "spot")


class ScenarioShiftTest(unittest.TestCase):
    def test_default_is_uniform_across_currencies(self):
        self.assertTrue(web_app.scenario_is_uniform(CONFIG, ["USD", "EUR"]))
        self.assertEqual(web_app.shift_for(CONFIG, "optimistic", "USD"),
                         web_app.shift_for(CONFIG, "optimistic", "EUR"))

    def test_per_currency_override_breaks_the_correlation_one_assumption(self):
        config = dict(CONFIG, scenario_shifts={"EUR": {"optimistic": -0.01, "pessimistic": 0.02}})
        self.assertFalse(web_app.scenario_is_uniform(config, ["USD", "EUR"]))
        self.assertEqual(web_app.shift_for(config, "optimistic", "USD"), 0.03)
        self.assertEqual(web_app.shift_for(config, "optimistic", "EUR"), -0.01)
        # 没覆盖的情景仍走全局值
        self.assertEqual(web_app.shift_for(config, "custom", "EUR"), 0.01)


class MonthlyBenchmarkTest(unittest.TestCase):
    def test_manual_rate_wins(self):
        state = {"monthly_average_rates": {"2026-06:USD": 7.11}}
        rate, source = benchmarks.monthly_average(state, "2026-06", "USD")
        self.assertEqual(rate, 7.11)
        self.assertIn("财务", source)

    def test_falls_back_to_local_market_series_for_usd(self):
        rate, source = benchmarks.monthly_average({}, "2025-06", "USD")
        self.assertIsNotNone(rate, "本地行情文件里应该有 2025-06 的美元数据")
        self.assertGreater(rate, 5)
        self.assertLess(rate, 9)
        self.assertIsNotNone(source)

    def test_unknown_currency_returns_nothing_instead_of_guessing(self):
        rate, source = benchmarks.monthly_average({}, "2025-06", "EUR")
        self.assertIsNone(rate)
        self.assertIsNone(source)

    def test_fully_hedged_receipt_locks_in_the_gap_to_the_benchmark(self):
        # 全额锁在 7.30，月均 7.10，到期即期 7.00：
        # 结汇均价就是 7.30，比月均好 0.20/美元
        row = benchmarks.benchmark_row(
            "2026-06", "USD", gross_signed=1000.0,
            hedges=[{"amount": 1000, "locked_rate": 7.30, "action": "sell_foreign"}],
            actual_rate=7.00, average_rate=7.10, average_source="test",
        )
        self.assertAlmostEqual(row["realized_avg_rate"], 7.30, places=10)
        self.assertAlmostEqual(row["vs_benchmark_cny"], 200.0, places=6)
        self.assertTrue(row["beats_benchmark"])
        self.assertEqual(row["hedge_coverage"], 1.0)

    def test_attribution_splits_into_hedge_and_timing(self):
        # 半额锁 7.30，另一半按到期 7.00 结：均价 7.15
        row = benchmarks.benchmark_row(
            "2026-06", "USD", gross_signed=1000.0,
            hedges=[{"amount": 500, "locked_rate": 7.30, "action": "sell_foreign"}],
            actual_rate=7.00, average_rate=7.10, average_source="test",
        )
        self.assertAlmostEqual(row["realized_avg_rate"], 7.15, places=10)
        # 套保效应 = 均价 - 到期即期 = +0.15/美元
        self.assertAlmostEqual(row["hedge_effect_cny"], 150.0, places=6)
        # 择时效应 = 到期即期 - 月均 = -0.10/美元（市场自己走的，跟锁不锁无关）
        self.assertAlmostEqual(row["timing_effect_cny"], -100.0, places=6)
        self.assertAlmostEqual(row["vs_benchmark_cny"], 50.0, places=6)

    def test_payment_side_flips_the_sign(self):
        # 净付：结汇均价越低越好，所以同样的价格关系要给出相反的符号
        row = benchmarks.benchmark_row(
            "2026-06", "EUR", gross_signed=-1000.0,
            hedges=[{"amount": 1000, "locked_rate": 7.30, "action": "buy_foreign"}],
            actual_rate=7.00, average_rate=7.10, average_source="test",
        )
        self.assertAlmostEqual(row["vs_benchmark_cny"], -200.0, places=6)
        self.assertFalse(row["beats_benchmark"])

    def test_over_hedging_does_not_inflate_the_average(self):
        # 锁了 2000 但敞口只有 1000：多锁的那部分是另一回事，不算进这笔业务的均价
        row = benchmarks.benchmark_row(
            "2026-06", "USD", gross_signed=1000.0,
            hedges=[{"amount": 2000, "locked_rate": 7.30, "action": "sell_foreign"}],
            actual_rate=7.00, average_rate=7.10, average_source="test",
        )
        self.assertEqual(row["hedged_notional"], 1000.0)
        self.assertAlmostEqual(row["realized_avg_rate"], 7.30, places=10)

    def test_wrong_direction_hedge_is_not_coverage(self):
        # 收汇敞口配 buy_foreign 不是套保，是把敞口做得更大。
        # 以前只取 abs(amount)，这笔会被算成 100% 覆盖，
        # 于是同一张卡上交易员口径和司库口径给出相反的符号。
        row = benchmarks.benchmark_row(
            "2026-06", "USD", gross_signed=1000.0,
            hedges=[{"amount": 1000, "locked_rate": 7.30, "action": "buy_foreign"}],
            actual_rate=7.00, average_rate=7.10, average_source="test",
        )
        self.assertEqual(row["hedged_notional"], 0.0)
        self.assertEqual(row["hedge_coverage"], 0.0)
        self.assertAlmostEqual(row["realized_avg_rate"], 7.00, places=10)
        self.assertAlmostEqual(row["hedge_effect_cny"], 0.0, places=6)

    def test_missing_action_is_not_counted_as_coverage(self):
        # 方向不明就不能声称覆盖了敞口
        row = benchmarks.benchmark_row(
            "2026-06", "USD", gross_signed=1000.0,
            hedges=[{"amount": 1000, "locked_rate": 7.30}],
            actual_rate=7.00, average_rate=7.10, average_source="test",
        )
        self.assertEqual(row["hedged_notional"], 0.0)

    def test_bare_period_key_must_name_the_currency(self):
        # {"2026-06": 7.11} 是个美元价，不能套到日元头上
        state = {"monthly_average_rates": {"2026-06": 7.11}}
        self.assertEqual(benchmarks.monthly_average(state, "2026-06", "JPY"), (None, None))
        # 写成 {月份: {币种: 价}} 才生效
        state = {"monthly_average_rates": {"2026-06": {"JPY": 0.049}}}
        rate, _ = benchmarks.monthly_average(state, "2026-06", "JPY")
        self.assertEqual(rate, 0.049)

    def test_no_benchmark_means_no_row(self):
        self.assertIsNone(benchmarks.benchmark_row(
            "2026-06", "USD", 1000.0, [], 7.0, None, None,
        ))


if __name__ == "__main__":
    unittest.main()
