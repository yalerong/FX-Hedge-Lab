import json
import math
import unittest
from datetime import date

import web_app
from forecast import pipeline, trend_gate

try:  # trend_gate 的 EMA/ATR 计算依赖 pandas，核心逻辑不依赖
    import pandas  # noqa: F401
    HAS_PANDAS = True
except ImportError:
    HAS_PANDAS = False


def bt(**overrides):
    base = {
        "mape": 0.02,
        "direction_accuracy": 0.65,
        "n_test": 40,
        "direction_hits": 26,  # 26/40, binomial p ~ 0.04
        "interval_coverage": 0.8,
    }
    base.update(overrides)
    return base


class ClassifyTierTest(unittest.TestCase):
    def test_missing_metrics_reject(self):
        tier, reasons = pipeline.classify_tier({"mape": None, "direction_accuracy": 0.6})
        self.assertEqual(tier, "reject")
        self.assertTrue(reasons)

    def test_good_metrics_support(self):
        tier, reasons = pipeline.classify_tier(bt())
        self.assertEqual(tier, "support")
        self.assertEqual(reasons, [])

    def test_small_sample_demotes_support(self):
        tier, reasons = pipeline.classify_tier(bt(n_test=12, direction_hits=9))
        self.assertEqual(tier, "caution")
        self.assertIn("样本不足", reasons[0])

    def test_insignificant_direction_demotes_support(self):
        # 22/40 = 55% 达到阈值但 p ~ 0.32，统计上与抛硬币无异
        tier, reasons = pipeline.classify_tier(bt(direction_accuracy=0.55, direction_hits=22))
        self.assertEqual(tier, "caution")
        self.assertIn("显著", reasons[0])

    def test_low_coverage_demotes_support(self):
        tier, reasons = pipeline.classify_tier(bt(interval_coverage=0.5))
        self.assertEqual(tier, "caution")
        self.assertIn("区间覆盖率", reasons[0])

    def test_very_low_coverage_rejects(self):
        # 直接进 caution 档（MAPE 偏高），覆盖率 23% 应把它杀到 reject
        tier, reasons = pipeline.classify_tier(
            bt(mape=0.04, direction_accuracy=0.52, interval_coverage=0.23)
        )
        self.assertEqual(tier, "reject")
        self.assertIn("失真", reasons[-1])

    def test_missing_coverage_caps_at_caution(self):
        tier, reasons = pipeline.classify_tier(bt(interval_coverage=None))
        self.assertEqual(tier, "caution")

    def test_binom_p_sanity(self):
        self.assertAlmostEqual(pipeline.binom_p_one_sided(0, 40), 1.0)
        self.assertLess(pipeline.binom_p_one_sided(40, 40), 1e-9)
        # 命中一半时 p 略高于 0.5
        self.assertGreater(pipeline.binom_p_one_sided(20, 40), 0.5)


class SignalHorizonTest(unittest.TestCase):
    """信号覆盖不到该期间就不该给折扣——和其余四道闸门一样，只降不升。"""

    def signal(self, months):
        return {
            "tier": "support", "direction": "up", "mape": 0.005, "current": 7.0,
            "generated_at": "2026-05-10T00:00:00Z",
            "forecast": [{"month": m, "rate": 7.0 + 0.05 * (i + 1)} for i, m in enumerate(months)],
        }

    def test_period_inside_the_horizon_gets_the_discount(self):
        mult, reason = web_app.forecast_multiplier(
            self.signal(["2026-10", "2026-11", "2026-12"]), 700000, "2026-11", today=date(2026, 5, 20)
        )
        self.assertEqual(mult, 0.5)
        self.assertIn("有利", reason)

    def test_period_past_the_horizon_gets_no_discount(self):
        # 一份到 2026-12 结束的预测，对 2027-03 到期的敞口没有发言权
        mult, reason = web_app.forecast_multiplier(
            self.signal(["2026-10", "2026-11", "2026-12"]), 700000, "2027-03", today=date(2026, 5, 20)
        )
        self.assertEqual(mult, 1.0)
        self.assertIn("覆盖不到", reason)
        self.assertIn("2027-03", reason)

    def test_period_before_the_horizon_gets_no_discount(self):
        mult, _ = web_app.forecast_multiplier(
            self.signal(["2026-10", "2026-11"]), 700000, "2026-08", today=date(2026, 5, 20)
        )
        self.assertEqual(mult, 1.0)

    def test_missing_period_keeps_old_behaviour_but_missing_timestamp_does_not_discount(self):
        # 没传期间、或者信号本来就没有逐月预测时，不因为这条闸门改变结果
        self.assertEqual(
            web_app.forecast_multiplier(self.signal(["2026-11"]), 700000, today=date(2026, 5, 20))[0],
            0.5,
        )
        bare = {"tier": "support", "direction": "up", "mape": 0.005}
        self.assertEqual(web_app.forecast_multiplier(bare, 700000, "2030-01", today=date(2026, 5, 20))[0], 1.0)

    def test_horizon_gate_only_lowers_never_raises(self):
        # 信号不利时本来就是 1.0×，加了这条闸门也不能变成折扣
        signal = dict(self.signal(["2026-11"]), direction="down")
        self.assertEqual(web_app.forecast_multiplier(signal, 700000, "2030-01", today=date(2026, 5, 20))[0], 1.0)


class ActionDirectionTest(unittest.TestCase):
    """操作方向只由净敞口决定，企业类型不能盖掉它。"""

    def test_net_payable_always_buys_even_for_an_exporter(self):
        config = {"enterprise_type": "export"}
        self.assertEqual(web_app.action_for(config, -350000), "buy_foreign")

    def test_net_receivable_always_sells_even_for_an_importer(self):
        config = {"enterprise_type": "import"}
        self.assertEqual(web_app.action_for(config, 700000), "sell_foreign")

    def test_action_agrees_with_the_card_text(self):
        # 以前文案说"买入外币/远期购汇"、action 却是 sell_foreign，
        # 点「按建议填入锁汇单」会填一笔加仓交易
        for enterprise_type in ("export", "import", "comprehensive"):
            for net in (700000, -350000):
                with self.subTest(enterprise_type=enterprise_type, net=net):
                    config = {"enterprise_type": enterprise_type}
                    action = web_app.action_for(config, net)
                    text = web_app.suggestion_text("USD", net, 0.7, 1000, action)
                    expected = "买入外币/远期购汇" if net < 0 else "卖出外币/远期结汇"
                    self.assertIn(expected, text)
                    self.assertEqual(action, "buy_foreign" if net < 0 else "sell_foreign")

    def test_unexpected_direction_is_flagged_not_overridden(self):
        self.assertTrue(web_app.direction_is_unexpected({"enterprise_type": "export"}, -1))
        self.assertTrue(web_app.direction_is_unexpected({"enterprise_type": "import"}, 1))
        self.assertFalse(web_app.direction_is_unexpected({"enterprise_type": "export"}, 1))
        self.assertFalse(web_app.direction_is_unexpected({"enterprise_type": "comprehensive"}, -1))


@unittest.skipUnless(HAS_PANDAS, "trend_gate 需要 pandas；不装依赖的那条 CI 作业跳过这组")
class TrendGateTest(unittest.TestCase):
    def _series(self, drift):
        # 单调漂移 + 小幅锯齿，确保 ATR 非零
        return [100 + drift * i + (0.05 if i % 2 else -0.05) for i in range(120)]

    def test_uptrend(self):
        out = trend_gate.evaluate_series(self._series(+0.3))
        self.assertEqual(out["direction"], "up")
        self.assertEqual(out["alignment"], 6)
        self.assertGreater(out["energy"], 0)

    def test_downtrend(self):
        out = trend_gate.evaluate_series(self._series(-0.3))
        self.assertEqual(out["direction"], "down")
        self.assertEqual(out["alignment"], 6)
        self.assertLess(out["energy"], 0)

    def test_too_short_raises(self):
        with self.assertRaises(RuntimeError):
            trend_gate.evaluate_series([100.0] * 30)

    def test_zero_atr_yields_json_safe_energy(self):
        # 尾段完全不动（钉住汇率/节假日回填）→ ATR=0，energy 必须是 None 而不是 NaN
        closes = self._series(+0.3)[:100] + [200.0] * 20
        out = trend_gate.evaluate_series(closes)
        self.assertIsNone(out["energy"])
        json.dumps(out, allow_nan=False)  # 不应抛错

    def test_constant_series_is_flat(self):
        out = trend_gate.evaluate_series([7.2] * 120)
        self.assertEqual(out["direction"], "flat")
        self.assertEqual(out["alignment"], 0)
        self.assertIsNone(out["energy"])


class ForecastMultiplierTest(unittest.TestCase):
    def _signal(self, tier="support", direction="up", current=7.2, final=7.5, mape=0.02,
                generated_at="2026-05-10T00:00:00Z"):
        return {
            "tier": tier,
            "direction": direction,
            "current": current,
            "mape": mape,
            "generated_at": generated_at,
            "forecast": [{"month": "2026-12", "rate": final}],
        }

    def test_favorable_support_discounts(self):
        # 收外币(net>0)且预测外币升值：幅度 4.2% >> MAPE 2%，允许降到 50%
        mult, reason = web_app.forecast_multiplier(self._signal(), net=1000, today=date(2026, 5, 20))
        self.assertEqual(mult, 0.5)

    def test_favorable_but_within_noise_no_discount(self):
        # 预测幅度 0.69% < MAPE 2%：方向有利也不打折
        mult, reason = web_app.forecast_multiplier(self._signal(final=7.25), net=1000, today=date(2026, 5, 20))
        self.assertEqual(mult, 1.0)
        self.assertIn("未超过模型误差", reason)

    def test_unfavorable_always_full(self):
        mult, _ = web_app.forecast_multiplier(self._signal(direction="down"), net=1000, today=date(2026, 5, 20))
        self.assertEqual(mult, 1.0)

    def test_reject_ignores_direction(self):
        mult, reason = web_app.forecast_multiplier(self._signal(tier="reject"), net=1000, today=date(2026, 5, 20))
        self.assertEqual(mult, 1.0)
        self.assertIn("不达标", reason)

    def test_stale_signal_does_not_discount(self):
        mult, reason = web_app.forecast_multiplier(
            self._signal(generated_at="2026-03-01T00:00:00Z"),
            net=1000,
            today=date(2026, 5, 20),
        )
        self.assertEqual(mult, 1.0)
        self.assertIn("超过 45 天", reason)

    def test_live_spot_past_forecast_endpoint_does_not_discount(self):
        mult, reason = web_app.forecast_multiplier(
            self._signal(final=7.5),
            net=1000,
            live_spot=7.6,
            today=date(2026, 5, 20),
        )
        self.assertEqual(mult, 1.0)
        self.assertIn("不利", reason)

    def test_fresh_signal_uses_live_spot_move(self):
        mult, _ = web_app.forecast_multiplier(
            self._signal(final=7.5, mape=0.02),
            net=1000,
            live_spot=7.2,
            today=date(2026, 5, 20),
        )
        self.assertEqual(mult, 0.5)


if __name__ == "__main__":
    unittest.main()
