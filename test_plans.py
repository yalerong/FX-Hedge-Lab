"""方案快照、价量分解、类目推导、净额口径的测试。"""
from __future__ import annotations

import copy
import unittest
from datetime import date

import benchmarks
import plans
import variance
import web_app


RATES = {
    "source": "test",
    "status": "test",
    "fetched_at": "2026-05-12T00:00:00Z",
    "pair_rates": {"USD": 7.2, "EUR": 7.8},
}


def dashboard(state, **kw):
    return web_app.build_dashboard(state, RATES, forecast_doc={}, **kw)


class PlanSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.state = copy.deepcopy(web_app.DEMO_STATE)

    def test_freeze_keeps_the_numbers_that_explain_the_advice(self):
        plan = plans.freeze(dashboard(self.state), "首版", "2026-05-12T00:00:00Z")
        self.assertEqual(plan["label"], "首版")
        self.assertEqual(len(plan["rows"]), 2)
        row = next(r for r in plan["rows"] if r["currency"] == "USD")
        for key in ("target_hedge_ratio", "forecast_multiplier", "effective_hedge_ratio",
                    "recommended_amount", "trade_rate", "forward_basis"):
            self.assertIn(key, row)
        # 冻的是决策参数，不是整个 config
        self.assertIn("default_hedge_ratio", plan["config"])
        self.assertNotIn("rate_api_url", plan["config"])
        self.assertEqual(plan["rate_snapshot"]["pair_rates"]["USD"], 7.2)

    def test_changing_a_decision_parameter_marks_the_plan_stale(self):
        plan = plans.freeze(dashboard(self.state), None, "2026-05-12T00:00:00Z")
        config = dict(web_app.DEFAULT_CONFIG, default_hedge_ratio=0.5)

        result = plans.drift(plan, config, RATES["pair_rates"])
        self.assertTrue(result["stale"])
        self.assertEqual(result["decision_changed"]["default_hedge_ratio"],
                         {"from": 0.8, "to": 0.5})
        self.assertEqual(result["scenario_changed"], {})

    def test_scenario_only_change_does_not_make_the_advice_stale(self):
        # 改情景涨跌幅不影响建议金额，只影响损益模拟，不该报"方案过期"
        plan = plans.freeze(dashboard(self.state), None, "2026-05-12T00:00:00Z")
        config = dict(web_app.DEFAULT_CONFIG, optimistic_shift_pct=0.05)

        result = plans.drift(plan, config, RATES["pair_rates"])
        self.assertFalse(result["stale"])
        self.assertIn("optimistic_shift_pct", result["scenario_changed"])

    def test_rate_moves_are_reported_but_do_not_make_it_stale(self):
        plan = plans.freeze(dashboard(self.state), None, "2026-05-12T00:00:00Z")
        result = plans.drift(plan, dict(web_app.DEFAULT_CONFIG), {"USD": 7.5, "EUR": 7.8})
        self.assertFalse(result["stale"])
        self.assertIn("USD", result["rate_moved"])
        self.assertNotIn("EUR", result["rate_moved"])
        self.assertAlmostEqual(result["rate_moved"]["USD"]["move"], 0.041667, places=5)

    def test_tiny_rate_move_is_noise(self):
        plan = plans.freeze(dashboard(self.state), None, "2026-05-12T00:00:00Z")
        result = plans.drift(plan, dict(web_app.DEFAULT_CONFIG), {"USD": 7.2036, "EUR": 7.8})
        self.assertEqual(result["rate_moved"], {})

    def test_signal_change_alone_makes_the_plan_stale(self):
        """配置一个字没动，但预测信号变了，折扣就变了，建议金额跟着变。"""
        signals = {"USD": {"tier": "support", "direction": "up", "mape": 0.018,
                           "n_test": 30, "direction_accuracy": 0.62}}
        # 清掉已锁：support 档 + 方向有利会把比例打到 0.5×，
        # 留着样例里那 50 万锁汇的话建议金额会归零、方案里就没有 USD 这行了
        self.state["hedges"] = []
        data = web_app.build_dashboard(
            self.state, RATES, forecast_doc={"signals": signals}
        )
        plan = plans.freeze(data, None, "2026-05-12T00:00:00Z")
        usd = next(r for r in plan["rows"] if r["currency"] == "USD")
        self.assertEqual(usd["forecast_signal"]["tier"], "support")

        # 同一份配置，信号降档
        worse = {"USD": dict(signals["USD"], tier="reject")}
        result = plans.drift(plan, data["config"], RATES["pair_rates"], worse)
        self.assertTrue(result["stale"])
        self.assertEqual(result["signal_changed"]["USD"]["to"]["tier"], "reject")
        self.assertEqual(result["decision_changed"], {})

    def test_unchanged_signal_is_not_drift(self):
        signals = {"USD": {"tier": "support", "direction": "up", "mape": 0.018,
                           "n_test": 30, "direction_accuracy": 0.62}}
        self.state["hedges"] = []
        data = web_app.build_dashboard(
            self.state, RATES, forecast_doc={"signals": signals}
        )
        plan = plans.freeze(data, None, "2026-05-12T00:00:00Z")
        result = plans.drift(plan, data["config"], RATES["pair_rates"], signals)
        self.assertEqual(result["signal_changed"], {})
        self.assertFalse(result["stale"])

    def test_identical_current_recommendations_are_not_drift(self):
        row = {
            "period": "2026-11", "currency": "USD", "action": "sell_foreign",
            "business_exposure": 1000, "covered_exposure": 0, "net_exposure": 1000,
            "target_hedge_ratio": 0.8, "forecast_multiplier": 0.5,
            "effective_hedge_ratio": 0.4, "recommended_amount": 400,
        }
        plan = plans.freeze(
            {"suggestions": [row], "config": {}, "rates": {}},
            None, "2026-05-12T00:00:00Z",
        )

        result = plans.drift(plan, {}, {}, current_suggestions=[dict(row)])

        self.assertEqual(result["recommendation_changed"], {})
        self.assertFalse(result["stale"])

    def test_recomputed_recommendation_change_makes_the_plan_stale(self):
        row = {
            "period": "2026-11", "currency": "USD", "action": "sell_foreign",
            "business_exposure": 1000, "covered_exposure": 0, "net_exposure": 1000,
            "target_hedge_ratio": 0.8, "forecast_multiplier": 0.5,
            "effective_hedge_ratio": 0.4, "recommended_amount": 400,
        }
        plan = plans.freeze(
            {"suggestions": [row], "config": {}, "rates": {}},
            None, "2026-05-12T00:00:00Z",
        )
        current = [dict(row, forecast_multiplier=1.0, effective_hedge_ratio=0.8,
                        recommended_amount=800)]

        result = plans.drift(plan, {}, {}, current_suggestions=current)

        self.assertTrue(result["stale"])
        self.assertEqual(result["decision_changed"], {})
        self.assertEqual(result["signal_changed"], {})
        self.assertEqual(
            result["recommendation_changed"]["2026-11|USD"]["forecast_multiplier"],
            {"from": 0.5, "to": 1.0},
        )
        self.assertEqual(
            result["recommendation_changed"]["2026-11|USD"]["recommended_amount"],
            {"from": 400, "to": 800},
        )

    def test_recommendation_removed_or_added_makes_the_plan_stale(self):
        usd = {
            "period": "2026-11", "currency": "USD", "action": "sell_foreign",
            "business_exposure": 1000, "covered_exposure": 0, "net_exposure": 1000,
            "target_hedge_ratio": 0.8, "forecast_multiplier": 0.5,
            "effective_hedge_ratio": 0.4, "recommended_amount": 400,
        }
        eur = dict(usd, currency="EUR", action="buy_foreign")
        plan = plans.freeze(
            {"suggestions": [usd], "config": {}, "rates": {}},
            None, "2026-05-12T00:00:00Z",
        )

        result = plans.drift(plan, {}, {}, current_suggestions=[eur])

        self.assertTrue(result["stale"])
        self.assertEqual(
            result["recommendation_changed"]["2026-11|USD"]["status"],
            {"from": "present", "to": "missing"},
        )
        self.assertEqual(
            result["recommendation_changed"]["2026-11|EUR"]["status"],
            {"from": "missing", "to": "present"},
        )

    def test_spot_crossing_forecast_endpoint_is_recommendation_drift(self):
        state = web_app.empty_state()
        state["exposures"] = [{
            "id": "e1", "due_date": "2026-11-30", "currency": "USD",
            "amount": 1000, "direction": "receipt", "category": "cash_flow",
            "probability": 1,
        }]
        signal = {
            "tier": "support", "direction": "up", "mape": 0.018,
            "n_test": 30, "direction_accuracy": 0.62, "current": 7.2,
            "forecast": [{"month": "2026-11", "rate": 7.35}],
        }
        forecast_doc = {"generated_at": "2026-05-12T00:00:00Z", "signals": {"USD": signal}}
        frozen_data = web_app.build_dashboard(
            state, RATES, forecast_doc=forecast_doc, today=date(2026, 5, 12),
        )
        plan = plans.freeze(frozen_data, None, "2026-05-12T00:00:00Z")
        self.assertEqual(plan["rows"][0]["forecast_multiplier"], 0.5)

        current_rates = dict(RATES, pair_rates={"USD": 7.4, "EUR": 7.8})
        current_data = web_app.build_dashboard(
            state, current_rates, forecast_doc=forecast_doc, today=date(2026, 5, 12),
        )
        result = plans.drift(
            plan, frozen_data["config"], current_rates["pair_rates"], {"USD": signal},
            current_suggestions=current_data["suggestions"],
        )

        self.assertEqual(result["signal_changed"], {})
        self.assertTrue(result["stale"])
        self.assertEqual(
            result["recommendation_changed"]["2026-11|USD"]["forecast_multiplier"],
            {"from": 0.5, "to": 1.0},
        )

    def test_signal_horizon_change_alone_makes_the_plan_stale(self):
        """档位方向都没变，只是预测区间挪了——折扣照样会从 0.5 跳回 1.0。

        自从加了"覆盖不到该期间就不给折扣"这道闸门，区间本身就是决策输入，
        指纹里不带它的话，建议金额变了而页面说"方案与当前一致"。
        """
        base = {"tier": "support", "direction": "up", "mape": 0.018,
                "n_test": 30, "direction_accuracy": 0.62,
                "forecast": [{"month": "2026-10", "rate": 7.3},
                             {"month": "2026-11", "rate": 7.35}]}
        plan = plans.freeze(
            {"suggestions": [{
                "period": "2026-11", "currency": "USD", "action": "sell_foreign",
                "business_exposure": 1000, "covered_exposure": 0, "net_exposure": 1000,
                "target_hedge_ratio": 0.8, "forecast_multiplier": 0.5,
                "effective_hedge_ratio": 0.4, "recommended_amount": 400,
                "forecast_signal": base,
            }], "config": {}, "rates": {}},
            None, "2026-05-12T00:00:00Z",
        )
        self.assertEqual(plan["rows"][0]["forecast_signal"]["horizon"],
                         {"from": "2026-10", "to": "2026-11"})

        # 同样的档位方向，区间整体后移
        moved = dict(base, forecast=[{"month": "2027-05", "rate": 7.3},
                                     {"month": "2027-06", "rate": 7.35}])
        result = plans.drift(plan, {}, {}, {"USD": moved})
        self.assertTrue(result["stale"], "区间挪了就该报过期")
        self.assertEqual(result["signal_changed"]["USD"]["to"]["horizon"],
                         {"from": "2027-05", "to": "2027-06"})

    def test_old_fingerprints_are_compared_on_the_keys_they_have(self):
        """指纹字段是会加的，老快照不该因为多出一个新键就永久过期。"""
        plan = plans.freeze(dashboard(self.state), None, "2026-05-12T00:00:00Z")
        raw = {"tier": "support", "direction": "up", "mape": 0.018,
               "n_test": 30, "direction_accuracy": 0.62,
               "forecast": [{"month": "2026-11", "rate": 7.3}]}
        signals = {row["currency"]: raw for row in plan["rows"]}
        for row in plan["rows"]:
            # 模拟加 horizon 之前冻的快照：只有老的那几个键
            row["forecast_signal"] = {key: raw[key] for key in plans.SIGNAL_KEYS}
        result = plans.drift(plan, dict(web_app.DEFAULT_CONFIG), RATES["pair_rates"], signals)
        self.assertEqual(result["signal_changed"], {})
        self.assertFalse(result["stale"])

    def test_enterprise_type_is_not_decision_drift(self):
        """方向改由净敞口决定之后，企业类型只剩提示作用，不该判方案过期。"""
        self.assertNotIn("enterprise_type", plans.DECISION_KEYS)
        plan = plans.freeze(dashboard(self.state), None, "2026-05-12T00:00:00Z")
        config = dict(web_app.DEFAULT_CONFIG, enterprise_type="import")
        result = plans.drift(plan, config, RATES["pair_rates"])
        self.assertFalse(result["stale"])

    def test_unused_strategy_type_is_not_decision_drift(self):
        """strategy_type 全仓没有任何计算读它，改它不该误报"建议已变"。"""
        self.assertNotIn("strategy_type", plans.DECISION_KEYS)
        plan = plans.freeze(dashboard(self.state), None, "2026-05-12T00:00:00Z")
        config = dict(web_app.DEFAULT_CONFIG, strategy_type="aggressive")
        result = plans.drift(plan, config, RATES["pair_rates"])
        self.assertFalse(result["stale"])

    def test_plans_frozen_before_the_field_existed_are_not_stale(self):
        """加 forecast_signal 之前冻的老方案不能被永久误报过期。"""
        plan = plans.freeze(dashboard(self.state), None, "2026-05-12T00:00:00Z")
        for row in plan["rows"]:
            row.pop("forecast_signal", None)
        signals = {"USD": {"tier": "support", "direction": "up", "mape": 0.018,
                           "n_test": 30, "direction_accuracy": 0.62}}
        result = plans.drift(plan, dict(web_app.DEFAULT_CONFIG), RATES["pair_rates"], signals)
        self.assertEqual(result["signal_changed"], {})
        self.assertFalse(result["stale"])

    def test_no_plan_yet(self):
        self.assertEqual(plans.drift(None, {}, {}), {"has_plan": False})

    def test_dashboard_exposes_latest_plan_drift(self):
        self.state["plans"] = [plans.freeze(dashboard(self.state), None, "2026-05-12T00:00:00Z")]
        self.state["config"] = dict(web_app.DEFAULT_CONFIG, default_hedge_ratio=0.4)
        data = dashboard(self.state)
        self.assertTrue(data["plan_drift"]["stale"])
        self.assertEqual(len(data["plans"]), 1)


class VarianceTest(unittest.TestCase):
    def test_price_and_volume_add_up_to_the_total(self):
        row = variance.decompose(
            planned_notional=1000, actual_notional=800,
            realized_avg_rate=7.15, benchmark_rate=7.10,
            hedged_notional=500, gross_signed=1000,
        )
        # 量差 = (800-1000) × 7.10 = -1420
        self.assertAlmostEqual(row["volume_variance_cny"], -1420.0, places=6)
        # 价差 = 800 × (7.15-7.10) = 40
        self.assertAlmostEqual(row["price_variance_cny"], 40.0, places=6)
        self.assertAlmostEqual(row["total_variance_cny"], -1380.0, places=6)
        self.assertAlmostEqual(
            row["total_variance_cny"],
            row["volume_variance_cny"] + row["price_variance_cny"], places=9,
        )

    def test_shrinking_exposure_below_the_hedge_is_over_hedging(self):
        # 计划 1000 锁了 800，实际只发生 500：有 300 是裸多头，远期照样得交割
        row = variance.decompose(
            planned_notional=1000, actual_notional=500,
            realized_avg_rate=7.15, benchmark_rate=7.10,
            hedged_notional=800, gross_signed=1000,
        )
        self.assertTrue(row["over_hedged"])
        self.assertAlmostEqual(row["over_hedged_notional"], 300.0, places=6)
        self.assertAlmostEqual(row["volume_gap_pct"], -0.5, places=6)

    def test_payment_side_flips_the_sign(self):
        row = variance.decompose(
            planned_notional=1000, actual_notional=800,
            realized_avg_rate=7.15, benchmark_rate=7.10,
            hedged_notional=0, gross_signed=-1000,
        )
        # 净付方少付了，量差对你有利
        self.assertAlmostEqual(row["volume_variance_cny"], 1420.0, places=6)
        self.assertAlmostEqual(row["price_variance_cny"], -40.0, places=6)

    def test_no_actual_amount_means_no_decomposition(self):
        self.assertIsNone(variance.decompose(1000, None, 7.15, 7.10, 0, 1000))

    def test_zero_actual_amount_is_a_real_number_not_missing(self):
        # 订单彻底黄了：实际发生额 0，不能被当成"没填"
        row = variance.decompose(1000, 0, 7.15, 7.10, 600, 1000)
        self.assertIsNotNone(row)
        self.assertAlmostEqual(row["volume_variance_cny"], -7100.0, places=6)
        self.assertAlmostEqual(row["price_variance_cny"], 0.0, places=6)
        self.assertAlmostEqual(row["over_hedged_notional"], 600.0, places=6)

    def test_backtest_carries_the_decomposition(self):
        state = copy.deepcopy(web_app.DEMO_STATE)
        state["settlements"] = [{
            "id": "s1", "due_date": "2026-06-30", "currency": "USD",
            "actual_rate": 7.21, "actual_amount": 900000,
        }]
        state["monthly_average_rates"] = {"2026-06:USD": 7.15}
        row = next(r for r in dashboard(state)["backtest"] if r["currency"] == "USD")
        var = row["benchmark"]["variance"]
        self.assertEqual(var["planned_notional"], 1200000)
        self.assertEqual(var["actual_notional"], 900000)
        self.assertLess(var["volume_variance_cny"], 0)


class VarianceNominalTest(unittest.TestCase):
    def test_probability_haircut_is_not_reported_as_volume_variance(self):
        """概率 60% 的订单全额兑现，不该被报成"多发生 40%"的量差收益。

        套保规模按期望值（金额 × 概率）定是对的，但用户填的实际发生额是
        真实结算金额。两者直接相减等于把概率折扣算成了业绩。
        """
        state = copy.deepcopy(web_app.DEMO_STATE)
        state["exposures"] = [{
            "id": "e1", "due_date": "2026-06-30", "currency": "USD",
            "amount": 1000000, "direction": "receipt", "category": "order_contract",
            "probability": 0.6,
        }]
        state["hedges"] = []
        state["settlements"] = [{
            "id": "s1", "due_date": "2026-06-30", "currency": "USD",
            "actual_rate": 7.2, "actual_amount": 1000000,
        }]
        state["monthly_average_rates"] = {"2026-06:USD": 7.2}
        row = next(r for r in dashboard(state)["backtest"] if r["currency"] == "USD")
        var = row["benchmark"]["variance"]

        # 计划敞口按名义金额 100 万，不是 60 万
        self.assertEqual(var["planned_notional"], 1000000)
        self.assertEqual(var["volume_gap"], 0)
        self.assertEqual(var["volume_variance_cny"], 0)


class RealizedOnActualTest(unittest.TestCase):
    def test_realized_average_follows_the_actual_amount(self):
        """实际收到的比计划多时，多出来那部分按到期即期结，均价要跟着变。

        原来一律按计划量算并在计划量上截断：计划 1000、锁 500@6、
        实际收到 2000、即期 8，真实均价是 (500×6 + 1500×8)/2000 = 7.5，
        而旧算法给 7.0，对着基准 7.0 报出"价差为零"。
        """
        row = benchmarks.benchmark_row(
            "2026-06", "USD", gross_signed=1000.0,
            hedges=[{"amount": 500, "locked_rate": 6.0, "action": "sell_foreign",
                     "trade_date": "2026-05-01"}],
            actual_rate=8.0, average_rate=7.0, average_source="test",
            actual_notional=2000.0,
        )
        self.assertAlmostEqual(row["realized_avg_rate"], 7.5, places=10)
        self.assertAlmostEqual(row["notional"], 2000.0, places=6)
        self.assertAlmostEqual(row["hedge_coverage"], 0.25, places=6)

    def test_over_hedging_uses_the_uncapped_total(self):
        """超额要用未截断的对冲总量算，用截断后的值永远算不出超额。"""
        row = benchmarks.benchmark_row(
            "2026-06", "USD", gross_signed=1000.0,
            hedges=[{"amount": 900, "locked_rate": 7.2, "action": "sell_foreign",
                     "trade_date": "2026-05-01"}],
            actual_rate=7.0, average_rate=7.1, average_source="test",
            actual_notional=400.0,
        )
        self.assertAlmostEqual(row["offsetting_total"], 900.0, places=6)
        self.assertAlmostEqual(row["hedged_notional"], 400.0, places=6)

        var = variance.decompose(
            planned_notional=1000, actual_notional=400,
            realized_avg_rate=row["realized_avg_rate"], benchmark_rate=7.1,
            hedged_notional=row["offsetting_total"], gross_signed=1000,
        )
        # 只收到 400 却锁了 900，有 500 是裸空头
        self.assertTrue(var["over_hedged"])
        self.assertAlmostEqual(var["over_hedged_notional"], 500.0, places=6)


class CancelledOrderTest(unittest.TestCase):
    def test_zero_actual_amount_still_produces_a_row(self):
        """订单彻底黄了（实际发生额 0）恰恰最该看到超额套保警告。

        表单占位符就写着"订单黄了就填 0"。用 notional <= 0 去判空的话，
        整个司库口径块会消失——在 80 万远期还得交割的时候把面板清空。
        """
        row = benchmarks.benchmark_row(
            "2026-06", "USD", gross_signed=1000000.0,
            hedges=[{"amount": 800000, "locked_rate": 7.2, "action": "sell_foreign",
                     "trade_date": "2026-05-01"}],
            actual_rate=7.3, average_rate=7.15, average_source="test",
            actual_notional=0.0,
        )
        self.assertIsNotNone(row, "实际发生额为 0 不等于没数据")
        self.assertEqual(row["notional"], 0.0)
        self.assertEqual(row["offsetting_total"], 800000.0)
        # 一分钱没结算，价格类效应归零
        self.assertEqual(row["hedge_effect_cny"], 0.0)
        self.assertEqual(row["timing_effect_cny"], 0.0)

        var = variance.decompose(
            planned_notional=1000000, actual_notional=0,
            realized_avg_rate=row["realized_avg_rate"], benchmark_rate=7.15,
            hedged_notional=row["offsetting_total"], gross_signed=1000000,
        )
        self.assertTrue(var["over_hedged"])
        self.assertEqual(var["over_hedged_notional"], 800000.0)

    def test_flat_book_has_no_treasury_benchmark(self):
        # 收付正好抵平：没有净敞口就没有方向，不能默认按付汇方给符号
        self.assertIsNone(benchmarks.benchmark_row(
            "2026-06", "USD", gross_signed=0.0, hedges=[],
            actual_rate=7.3, average_rate=7.2, average_source="test",
            actual_notional=500000.0,
        ))

    def test_offsetting_total_counts_every_leg(self):
        """累加写在带 break 的循环里的话，溢出之后的锁汇根本不会被算进去。"""
        row = benchmarks.benchmark_row(
            "2026-06", "USD", gross_signed=1000.0,
            hedges=[
                {"amount": 1000, "locked_rate": 7.1, "action": "sell_foreign", "trade_date": "2026-05-01"},
                {"amount": 300, "locked_rate": 7.2, "action": "sell_foreign", "trade_date": "2026-05-02"},
                {"amount": 200, "locked_rate": 7.3, "action": "sell_foreign", "trade_date": "2026-05-03"},
            ],
            actual_rate=7.0, average_rate=7.1, average_source="test",
        )
        self.assertEqual(row["offsetting_total"], 1500.0)
        self.assertEqual(row["hedged_notional"], 1000.0)


class NominalNettingTest(unittest.TestCase):
    def test_planned_notional_nets_receipts_against_payments(self):
        """计划侧必须和 gross_signed 同口径净额，否则凭空造出量差。

        六月收 100 万、付 40 万，净敞口 60 万。计划侧若按毛额 140 万记，
        实际结算 60 万就会被报成"缩水 80 万"——576 万人民币的假损失，
        而这个模块的存在意义正是避免误判缩水。
        """
        state = copy.deepcopy(web_app.DEMO_STATE)
        state["exposures"] = [
            {"id": "e1", "due_date": "2026-06-30", "currency": "USD", "amount": 1000000,
             "direction": "receipt", "category": "cash_flow", "probability": 1},
            {"id": "e2", "due_date": "2026-06-15", "currency": "USD", "amount": 400000,
             "direction": "payment", "category": "cash_flow", "probability": 1},
        ]
        state["hedges"] = []
        state["settlements"] = [{
            "id": "s1", "due_date": "2026-06-30", "currency": "USD",
            "actual_rate": 7.2, "actual_amount": 600000,
        }]
        state["monthly_average_rates"] = {"2026-06:USD": 7.2}
        row = next(r for r in dashboard(state)["backtest"] if r["currency"] == "USD")
        var = row["benchmark"]["variance"]

        self.assertEqual(var["planned_notional"], 600000)
        self.assertEqual(var["volume_gap"], 0)
        self.assertEqual(var["volume_variance_cny"], 0)


class SettlementPairingTest(unittest.TestCase):
    def test_rate_and_amount_come_from_the_same_record(self):
        """一个期间录了多条结算记录时，汇率和金额必须取自同一条。"""
        state = copy.deepcopy(web_app.DEMO_STATE)
        state["monthly_average_rates"] = {"2026-06:USD": 7.15}
        state["settlements"] = [
            {"id": "s1", "due_date": "2026-06-30", "currency": "USD",
             "actual_rate": 7.0, "actual_amount": 900000},
            # 后录的这条只填了汇率。旧实现会用这条的汇率 + 上一条的金额。
            {"id": "s2", "due_date": "2026-06-30", "currency": "USD", "actual_rate": 7.3},
        ]
        row = next(r for r in dashboard(state)["backtest"] if r["currency"] == "USD")
        self.assertEqual(row["actual_rate"], 7.3)
        self.assertIsNone(row["benchmark"]["variance"], "最后那条没填金额，就不该有量差")


class ResetKeepsPlansTest(unittest.TestCase):
    def test_demo_state_has_no_plans_key(self):
        # 恢复样例直接写 DEMO_STATE 的话，已冻结的方案会被不可逆地抹掉
        self.assertNotIn("plans", web_app.DEMO_STATE)


class NaturalOffsetScopeTest(unittest.TestCase):
    def test_offset_only_happens_inside_a_period(self):
        """六月净收美元、七月净付美元，是期限错配不是天然对冲。

        六月那天照样全额敞着，跨期间相加把它算成"抵消"会把风险抹平。
        """
        state = copy.deepcopy(web_app.DEMO_STATE)
        state["exposures"] = [
            {"id": "e1", "due_date": "2026-06-30", "currency": "USD", "amount": 1000000,
             "direction": "receipt", "category": "cash_flow", "probability": 1},
            {"id": "e2", "due_date": "2026-07-31", "currency": "USD", "amount": 1000000,
             "direction": "payment", "category": "cash_flow", "probability": 1},
        ]
        state["hedges"] = []
        portfolio = dashboard(state)["portfolio"]
        rate = RATES["pair_rates"]["USD"]

        self.assertAlmostEqual(portfolio["net_exposure_cny"], 2000000 * rate, places=2)
        self.assertAlmostEqual(portfolio["net_after_offset_cny"], 2000000 * rate, places=2)
        self.assertEqual(portfolio["natural_offset_cny"], 0)

    def test_offset_inside_one_period_still_counts(self):
        state = copy.deepcopy(web_app.DEMO_STATE)
        state["exposures"] = [
            {"id": "e1", "due_date": "2026-06-30", "currency": "USD", "amount": 1000000,
             "direction": "receipt", "category": "cash_flow", "probability": 1},
            {"id": "e2", "due_date": "2026-06-15", "currency": "EUR", "amount": 500000,
             "direction": "payment", "category": "cash_flow", "probability": 1},
        ]
        state["hedges"] = []
        portfolio = dashboard(state)["portfolio"]
        # 同一期间内两个币种方向相反，净额小于绝对值合计
        self.assertLess(portfolio["net_after_offset_cny"], portfolio["net_exposure_cny"])
        self.assertGreater(portfolio["natural_offset_cny"], 0)

    def test_natural_offset_is_never_negative(self):
        state = copy.deepcopy(web_app.DEMO_STATE)
        state["exposures"] = [{
            "id": "e1", "due_date": "2026-06-30", "currency": "USD", "amount": 1000000,
            "direction": "receipt", "category": "cash_flow", "probability": 1,
        }]
        state["hedges"] = [{
            "id": "h1", "trade_date": "2026-05-12", "due_date": "2026-06-30",
            "currency": "USD", "amount": 1500000, "action": "sell_foreign", "locked_rate": 7.18,
        }]
        portfolio = dashboard(state)["portfolio"]
        self.assertGreaterEqual(portfolio["natural_offset_cny"], 0)
        self.assertGreaterEqual(portfolio["net_after_offset_cny"], 0)


class CategorySuggestionTest(unittest.TestCase):
    def test_booked_items_are_balance_sheet(self):
        category, reason = web_app.suggest_category({"booked": True, "probability": 1})
        self.assertEqual(category, "balance_sheet")
        self.assertIn("已入账", reason)

    def test_certain_but_unbooked_is_order_contract(self):
        category, _ = web_app.suggest_category({"probability": 1})
        self.assertEqual(category, "order_contract")

    def test_uncertain_is_cash_flow(self):
        category, reason = web_app.suggest_category({"probability": 0.6})
        self.assertEqual(category, "cash_flow")
        self.assertIn("60%", reason)

    def test_backend_annotates_every_saved_exposure(self):
        """后端也要算推荐，绕过浏览器的 API 客户端才拿得到。"""
        row = {"due_date": "2026-09-30", "currency": "USD", "amount": 100,
               "direction": "receipt", "probability": 0.5, "category": "balance_sheet"}
        web_app.validate_exposure(row)
        self.assertEqual(row["suggested_category"], "cash_flow")
        self.assertIn("50%", row["suggestion_reason"])
        self.assertIs(row["booked"], False)

    def test_suggestion_is_always_a_legal_category(self):
        for row in ({}, {"probability": None}, {"probability": "bad"}, {"booked": False}):
            category, _ = web_app.suggest_category(row)
            self.assertTrue(web_app.known_category(category), row)


class NetOffsetTest(unittest.TestCase):
    def test_gross_and_net_are_both_reported(self):
        portfolio = dashboard(web_app.DEMO_STATE)["portfolio"]
        # 净收 USD 70 万 × 7.2 = 504 万；净付 EUR 35 万 × 7.8 = 273 万
        self.assertAlmostEqual(portfolio["net_exposure_cny"], 5040000 + 2730000, places=2)
        # 同向变动假设下两者对冲，净额只剩 231 万
        self.assertAlmostEqual(portfolio["net_after_offset_cny"], 5040000 - 2730000, places=2)
        self.assertGreater(portfolio["natural_offset_cny"], 0)

    def test_same_direction_currencies_do_not_offset(self):
        state = copy.deepcopy(web_app.DEMO_STATE)
        # 两个币种都是净收，没有天然对冲可言
        state["exposures"][1]["direction"] = "receipt"
        portfolio = dashboard(state)["portfolio"]
        self.assertAlmostEqual(
            portfolio["net_after_offset_cny"], portfolio["net_exposure_cny"], places=2
        )
        self.assertAlmostEqual(portfolio["natural_offset_cny"], 0, places=2)


if __name__ == "__main__":
    unittest.main()
