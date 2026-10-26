"""组合风险：期限梯度、相关性、参数法 VaR/ES。"""
from __future__ import annotations

import math
import unittest
from datetime import date
from statistics import NormalDist

import risk
import web_app

TODAY = date(2026, 10, 26)
RATES = {
    "source": "test",
    "status": "test",
    "fetched_at": "2026-10-26T00:00:00Z",
    "pair_rates": {"USD": 7.0, "EUR": 7.8},
}


def row(currency="USD", net=1_000_000, rate=7.0, period="2027-01", gross=None):
    return {
        "period": period,
        "currency": currency,
        "business_exposure": net if gross is None else gross,
        "locked_exposure": 0,
        "net_exposure": net,
        "current_rate": rate,
        "rate_available": rate is not None,
    }


class TenorTest(unittest.TestCase):
    def test_buckets_follow_month_distance(self):
        cases = {
            "2026-09": "overdue",
            "2026-10": "0-1m",
            "2026-11": "0-1m",
            "2026-12": "1-3m",
            "2027-01": "1-3m",
            "2027-02": "3-6m",
            "2027-04": "3-6m",
            "2027-05": "6-12m",
            "2027-10": "6-12m",
            "2027-11": ">12m",
        }
        for period, expected in cases.items():
            with self.subTest(period=period):
                self.assertEqual(risk.tenor_bucket(period, TODAY), expected)

    def test_ladder_sums_by_bucket_and_applies_signed_coverage(self):
        rows = [
            row(period="2026-11", net=1_000_000, gross=1_000_000),
            row(period="2026-11", net=-200_000, gross=-200_000),
            row(currency="EUR", period="2027-03", net=500_000, rate=7.8, gross=500_000),
        ]
        rows[0]["locked_exposure"] = -600_000
        ladder = risk.build_ladder(rows, TODAY)["rows"]
        near = next(item for item in ladder if item["bucket"] == "0-1m")
        # 业务敞口 = 100 万 + 20 万（取绝对值），已锁只算方向相反的 60 万
        self.assertEqual(near["gross_cny"], 1_200_000 * 7.0)
        self.assertEqual(near["locked_cny"], 600_000 * 7.0)
        self.assertAlmostEqual(near["coverage"], 0.5, places=6)
        mid = next(item for item in ladder if item["bucket"] == "3-6m")
        self.assertEqual(mid["periods"], 1)


class VarTest(unittest.TestCase):
    def build(self, rows, **overrides):
        config = dict(
            web_app.DEFAULT_CONFIG,
            annual_vol={"USD": 0.05, "EUR": 0.10},
            correlations={},
        )
        config.update(overrides)
        return risk.build(rows, config, TODAY)

    def test_single_currency_matches_the_formula(self):
        result = self.build([row()])
        sigma = 7_000_000 * 0.05
        scale = math.sqrt(1 / 252)
        z_value = NormalDist().inv_cdf(0.99)
        self.assertAlmostEqual(result["sigma_cny"], sigma, places=2)
        self.assertAlmostEqual(result["var_cny"], z_value * sigma * scale, places=2)
        self.assertGreater(result["es_cny"], result["var_cny"])
        self.assertEqual(result["hedge_benefit_cny"], 0)

    def test_correlation_changes_the_result(self):
        rows = [row(), row(currency="EUR", net=-500_000, rate=7.8)]
        same = self.build(rows, correlations={"USD:EUR": 1.0})
        zero = self.build(rows, correlations={"USD:EUR": 0.0})
        opposite = self.build(rows, correlations={"EUR:USD": -1.0})
        # 收美元、付欧元：相关系数越低，组合波动越大（分散效果越差）
        self.assertLess(same["sigma_cny"], zero["sigma_cny"])
        self.assertLess(zero["sigma_cny"], opposite["sigma_cny"])
        self.assertEqual(same["correlations"]["missing_pairs"], 0)
        self.assertEqual(zero["correlations"]["missing_pairs"], 0)

    def test_missing_correlation_falls_back_to_one_and_says_so(self):
        result = self.build([row(), row(currency="EUR", net=-500_000, rate=7.8)])
        self.assertEqual(result["correlations"]["missing_pairs"], 1)
        self.assertEqual(result["correlations"]["matrix"][0][1], 1.0)
        self.assertTrue(any("相关系数" in note for note in result["notes"]))

    def test_missing_vol_uses_default_and_warns(self):
        result = self.build([row()], annual_vol={})
        self.assertEqual(result["positions"][0]["vol"], risk.FALLBACK_VOL)
        self.assertTrue(any("未配置年化波动率" in note for note in result["notes"]))

    def test_limit_utilization(self):
        base = self.build([row()])
        result = self.build([row()], var_limit_cny=base["var_cny"] * 2)
        self.assertAlmostEqual(result["limit_utilization"], 0.5, places=4)
        self.assertFalse(result["over_limit"])

    def test_missing_rate_is_excluded_and_noted(self):
        result = self.build([row(), row(currency="CHF", net=100_000, rate=None)])
        self.assertEqual([item["currency"] for item in result["positions"]], ["USD"])
        self.assertTrue(any("CHF" in note for note in result["notes"]))

    def test_dashboard_exposes_risk_block(self):
        data = web_app.build_dashboard(web_app.sample_state(), RATES, forecast_doc={})
        self.assertIn("var_cny", data["risk"])
        self.assertIn("ladder", data["risk"])


class ConfigValidationTest(unittest.TestCase):
    def test_correlations_must_be_symmetric(self):
        with self.assertRaises(ValueError):
            web_app.validate_config({
                "correlations": {"USD:EUR": 0.4, "EUR:USD": 0.6},
            })

    def test_correlation_range_and_format(self):
        for payload in (
            {"correlations": {"USD:EUR": 1.5}},
            {"correlations": {"USDEUR": 0.3}},
            {"correlations": {"USD:USD": 0.3}},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    web_app.validate_config(payload)

    def test_annual_vol_range(self):
        for value in (-0.1, 0, 4):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    web_app.validate_config({"annual_vol": {"USD": value}})

    def test_var_parameters(self):
        with self.assertRaises(ValueError):
            web_app.validate_config({"var_confidence": 1.2})
        with self.assertRaises(ValueError):
            web_app.validate_config({"var_horizon_days": 0})
        with self.assertRaises(ValueError):
            web_app.validate_config({"var_horizon_days": 300})
        self.assertEqual(web_app.validate_config({"var_horizon_days": 10})["var_horizon_days"], 10)


if __name__ == "__main__":
    unittest.main()
