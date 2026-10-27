"""VaR 回测：Kupiec 失败率、Christoffersen 独立性、Basel 分区。"""
from __future__ import annotations

import math
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import backtest
import history
import web_app

TODAY = date(2026, 10, 26)


class ChiSquareTest(unittest.TestCase):
    def test_one_degree_of_freedom_matches_the_table(self):
        # χ²(1) 的 95% 分位数是 3.8415
        self.assertAlmostEqual(backtest.chi2_sf(3.8415, 1), 0.05, places=3)

    def test_two_degrees_of_freedom_matches_the_table(self):
        # χ²(2) 的 95% 分位数是 5.9915
        self.assertAlmostEqual(backtest.chi2_sf(5.9915, 2), 0.05, places=3)

    def test_zero_statistic_has_full_probability(self):
        self.assertEqual(backtest.chi2_sf(0.0, 1), 1.0)
        self.assertEqual(backtest.chi2_sf(-1.0, 2), 1.0)


class KupiecTest(unittest.TestCase):
    def test_exactly_expected_failures_cannot_be_rejected(self):
        # 400 天里 4 次突破 = 1%，正好等于 99% VaR 的期望失败率
        result = backtest.kupiec(400, 4, 0.99)
        self.assertAlmostEqual(result["stat"], 0.0, places=10)
        self.assertAlmostEqual(result["p"], 1.0, places=10)

    def test_zero_exceptions_still_has_a_finite_statistic(self):
        # x=0 时 ln(0) 会炸，取连续极限 LR = -2n·ln(1-p)
        result = backtest.kupiec(250, 0, 0.99)
        self.assertAlmostEqual(result["stat"], -2 * 250 * math.log(0.99), places=10)
        self.assertLess(result["p"], 0.05)

    def test_too_many_exceptions_is_rejected(self):
        result = backtest.kupiec(250, 20, 0.99)
        self.assertLess(result["p"], 0.001)

    def test_all_exceptions_still_finite(self):
        result = backtest.kupiec(250, 250, 0.99)
        self.assertIsNotNone(result["stat"])
        self.assertTrue(math.isfinite(result["stat"]))


class ChristoffersenTest(unittest.TestCase):
    def test_no_exception_at_all_is_not_testable(self):
        result = backtest.christoffersen([False] * 50)
        self.assertIsNone(result["stat"])

    def test_clustered_exceptions_are_rejected(self):
        # 突破全扎堆在最后 10 天：昨天突破过，今天大概率还突破
        hits = [False] * 40 + [True] * 10
        result = backtest.christoffersen(hits)
        self.assertIsNotNone(result["stat"])
        self.assertLess(result["p"], 0.01)

    def test_independent_exceptions_are_not_rejected(self):
        # 每 20 天一次突破，间隔固定 = 相互独立
        hits = [index % 20 == 0 for index in range(400)]
        result = backtest.christoffersen(hits)
        self.assertGreater(result["p"], 0.1)


class BaselZoneTest(unittest.TestCase):
    def test_zone_edges(self):
        self.assertEqual(backtest.basel_zone(250, 0), "green")
        self.assertEqual(backtest.basel_zone(250, 4), "green")
        self.assertEqual(backtest.basel_zone(250, 5), "yellow")
        self.assertEqual(backtest.basel_zone(250, 9), "yellow")
        self.assertEqual(backtest.basel_zone(250, 10), "red")

    def test_zone_is_only_defined_for_250_days(self):
        self.assertIsNone(backtest.basel_zone(300, 3))


class BacktestRunTest(unittest.TestCase):
    def config(self, **overrides):
        base = dict(web_app.DEFAULT_CONFIG)
        base.update(overrides)
        return base

    def test_usd_book_gets_three_models(self):
        result = backtest.run({"USD": 7_000_000}, self.config(), 0.99, 1, TODAY)
        self.assertTrue(result["available"])
        self.assertEqual(result["test_days"], 250)
        self.assertEqual([model["key"] for model in result["models"]], ["historical", "parametric", "ewma"])
        self.assertEqual(len(result["series"]["loss"]), 250)
        self.assertEqual(len(result["series"]["var"]["historical"]), 250)

    def test_exception_count_matches_the_series(self):
        result = backtest.run({"USD": 7_000_000}, self.config(), 0.99, 1, TODAY)
        for model in result["models"]:
            hits = result["series"]["exceptions"][model["key"]]
            # EWMA 前几个观测没有估计值，会被裁掉，所以不能直接用 250
            self.assertEqual(model["exceptions"], len(hits))
            self.assertGreaterEqual(model["exceptions"], 0)
            self.assertLessEqual(model["exceptions"], result["test_days"])

    def test_book_without_history_is_unavailable(self):
        result = backtest.run({"EUR": 1_000_000}, self.config(), 0.99, 1, TODAY)
        self.assertFalse(result["available"])
        self.assertIn("EUR", result["reason"])

    def test_missing_currency_marks_the_result_partial(self):
        result = backtest.run({"USD": 7_000_000, "EUR": -2_000_000}, self.config(), 0.99, 1, TODAY)
        self.assertTrue(result["available"])
        self.assertTrue(result["partial"])
        self.assertEqual(result["missing"], ["EUR"])
        self.assertAlmostEqual(result["coverage"], 7 / 9, places=4)
        self.assertTrue(any("覆盖" in note for note in result["notes"]))

    def test_shorter_test_window_drops_the_basel_zone(self):
        result = backtest.run({"USD": 7_000_000}, self.config(backtest_days=120), 0.99, 1, TODAY)
        self.assertTrue(result["available"])
        self.assertEqual(result["test_days"], 120)
        for model in result["models"]:
            self.assertIsNone(model["zone"])
        self.assertTrue(any("250" in note for note in result["notes"]))


def _write_series(directory: Path, currency: str, returns: list[float]) -> None:
    price = 7.0
    lines = ["date,value", f"2020-01-01,{price:.6f}"]
    day = date(2020, 1, 1)
    for value in returns:
        day += timedelta(days=1)
        price *= math.exp(value)
        lines.append(f"{day.isoformat()},{price:.6f}")
    (directory / f"{currency}CNY_daily.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _fake_returns(count: int, seed: int = 12345) -> list[float]:
    state = seed
    values = []
    for _ in range(count):
        state = (1103515245 * state + 12345) % (2 ** 31)
        values.append((state / 2 ** 31 - 0.5) * 0.004)
    return values


class LookAheadTest(unittest.TestCase):
    """回测最容易犯的错：拿全样本估参数，再回头给历史打分。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self._restore)
        self.original_dir = history.HISTORY_DIR
        history.HISTORY_DIR = Path(self.tmp.name)
        history._CACHE.clear()

    def _restore(self):
        history.HISTORY_DIR = self.original_dir
        history._CACHE.clear()

    def test_changing_the_last_days_does_not_move_earlier_var(self):
        returns = _fake_returns(620)
        _write_series(Path(self.tmp.name), "USD", returns)
        before = backtest.run({"USD": 1_000_000}, dict(web_app.DEFAULT_CONFIG), 0.99, 1)

        shocked = returns[:-3] + [0.05, -0.05, 0.08]
        history._CACHE.clear()
        _write_series(Path(self.tmp.name), "USD", shocked)
        after = backtest.run({"USD": 1_000_000}, dict(web_app.DEFAULT_CONFIG), 0.99, 1)

        # 最后 3 天的参数/样本会变，之前的日子一点都不该动
        for key in ("historical", "parametric"):
            self.assertEqual(
                before["series"]["var"][key][:-3],
                after["series"]["var"][key][:-3],
                f"{key} 的 VaR 受到了未来数据的影响",
            )
        self.assertNotEqual(before["series"]["var"]["historical"][-1], after["series"]["var"]["historical"][-1])


if __name__ == "__main__":
    unittest.main()
