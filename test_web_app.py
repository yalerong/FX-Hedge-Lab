import copy
import unittest
from datetime import date

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
        # Pass an empty forecast doc so the test does not depend on whether
        # data/forecast_signals.json happens to exist on disk.
        dashboard = web_app.build_dashboard(web_app.DEMO_STATE, self.rates, forecast_doc={})

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

    def test_sample_state_keeps_future_actions_and_settled_history(self):
        today = date(2026, 9, 23)
        state = web_app.sample_state(today=today)
        dashboard = web_app.build_dashboard(state, self.rates, forecast_doc={}, today=today)

        self.assertTrue(any(row["due_date"] > today.isoformat() for row in state["exposures"]))
        self.assertTrue(dashboard["suggestions"])
        self.assertTrue(all(not row["past_due"] for row in dashboard["suggestions"]))
        self.assertTrue(any(row["settled"] for row in dashboard["backtest"]))

    def test_fallback_rates_show_exposure_but_block_advice(self):
        dashboard = web_app.build_dashboard(
            web_app.DEMO_STATE,
            {
                "source": "built-in fallback rates",
                "status": "fallback",
                "fetched_at": "2026-05-12T00:00:00Z",
                "pair_rates": {"USD": 7.2, "EUR": 7.8},
            },
            forecast_doc={},
        )

        self.assertFalse(dashboard["rates_actionable"])
        self.assertEqual(dashboard["suggestions"], [])
        self.assertTrue(dashboard["net_exposures"])
        self.assertTrue(dashboard["rate_trial_reasons"])

    def test_cached_after_refresh_error_is_trial_only(self):
        dashboard = web_app.build_dashboard(
            web_app.DEMO_STATE,
            {
                "source": "ExchangeRate-API open endpoint",
                "status": "cached_after_refresh_error",
                "last_error": "timeout",
                "fetched_at": "2026-05-12T00:00:00Z",
                "pair_rates": {"USD": 7.2, "EUR": 7.8},
            },
            forecast_doc={},
        )

        self.assertFalse(dashboard["rates_actionable"])
        self.assertTrue(dashboard["suggestions"])
        self.assertTrue(all(row["trial"] for row in dashboard["suggestions"]))
        self.assertTrue(any("timeout" in reason for row in dashboard["suggestions"] for reason in row["trial_reasons"]))

    def test_scenario_rows_cover_exposures_without_recommendation(self):
        # 已锁量超过目标覆盖量时不会再产生建议，但剩余敞口的浮动损益必须照样出现。
        state = copy.deepcopy(web_app.DEMO_STATE)
        state["hedges"][0]["amount"] = 1000000
        dashboard = web_app.build_dashboard(state, self.rates, forecast_doc={})

        usd_suggestion = [row for row in dashboard["suggestions"] if row["currency"] == "USD"]
        self.assertEqual(usd_suggestion, [])

        usd_scenario = next(row for row in dashboard["scenario_rows"] if row["currency"] == "USD")
        self.assertFalse(usd_scenario["has_recommendation"])
        self.assertEqual(usd_scenario["recommended_amount"], 0)
        self.assertEqual(usd_scenario["net_exposure"], 200000)
        # 敞口还在，乐观/悲观两端就不能都是 0
        self.assertNotEqual(usd_scenario["projection"]["optimistic"]["total_projected_gain_loss"], 0)
        self.assertIn("2026-06:USD", dashboard["scenario_summary"])

    def test_scenario_rows_match_suggestion_projection(self):
        dashboard = web_app.build_dashboard(web_app.DEMO_STATE, self.rates, forecast_doc={})
        usd_suggestion = next(row for row in dashboard["suggestions"] if row["currency"] == "USD")
        usd_scenario = next(row for row in dashboard["scenario_rows"] if row["currency"] == "USD")

        self.assertTrue(usd_scenario["has_recommendation"])
        self.assertEqual(usd_scenario["projection"], usd_suggestion["scenario_projection"])

    def test_over_risk_limit_is_a_flag_not_a_decision(self):
        state = copy.deepcopy(web_app.DEMO_STATE)
        state["config"] = dict(web_app.DEFAULT_CONFIG, risk_limit_cny=1000000)
        dashboard = web_app.build_dashboard(state, self.rates, forecast_doc={})

        usd = next(row for row in dashboard["net_exposures"] if row["currency"] == "USD")
        self.assertTrue(usd["over_risk_limit"])

        # 阈值只是提示：抬高阈值不改变任何建议金额
        state["config"] = dict(web_app.DEFAULT_CONFIG, risk_limit_cny=10 ** 9)
        relaxed = web_app.build_dashboard(state, self.rates, forecast_doc={})
        relaxed_usd = next(row for row in relaxed["net_exposures"] if row["currency"] == "USD")
        self.assertFalse(relaxed_usd["over_risk_limit"])
        self.assertEqual(
            [row["recommended_amount"] for row in dashboard["suggestions"]],
            [row["recommended_amount"] for row in relaxed["suggestions"]],
        )

    def test_backtest_marks_rows_without_settlement_rate(self):
        dashboard = web_app.build_dashboard(web_app.DEMO_STATE, self.rates, forecast_doc={})

        usd = next(row for row in dashboard["backtest"] if row["currency"] == "USD")
        self.assertTrue(usd["settled"])
        self.assertEqual(usd["rate_basis"], "settlement")
        self.assertEqual(usd["actual_rate"], 7.21)

        eur = next(row for row in dashboard["backtest"] if row["currency"] == "EUR")
        self.assertFalse(eur["settled"])
        self.assertEqual(eur["rate_basis"], "market_estimate")
        self.assertIn("尚未录入到期实际汇率", eur["plain_text"])
        self.assertTrue(
            any("还没录入到期实际汇率" in line for line in dashboard["plain_language"])
        )

    def test_settled_backtest_survives_missing_market_rate(self):
        state = copy.deepcopy(web_app.DEMO_STATE)
        state["config"] = dict(web_app.DEFAULT_CONFIG, supported_currencies=["USD", "CHF"])
        state["exposures"] = [{
            "id": "chf-exp",
            "due_date": "2026-06-30",
            "currency": "CHF",
            "amount": 100000,
            "direction": "receipt",
            "category": "cash_flow",
            "probability": 1,
        }]
        state["hedges"] = [{
            "id": "chf-hedge",
            "trade_date": "2026-05-01",
            "due_date": "2026-06-30",
            "currency": "CHF",
            "amount": 50000,
            "action": "sell_foreign",
            "locked_rate": 8.1,
        }]
        state["settlements"] = [{
            "id": "chf-settle",
            "due_date": "2026-06-30",
            "currency": "CHF",
            "actual_rate": 8.2,
        }]

        dashboard = web_app.build_dashboard(
            state,
            {"source": "test", "status": "test", "fetched_at": "x", "pair_rates": {"USD": 7.2}},
            forecast_doc={},
        )

        chf = next(row for row in dashboard["backtest"] if row["currency"] == "CHF")
        self.assertTrue(chf["settled"])
        self.assertEqual(chf["actual_rate"], 8.2)
        self.assertIsNone(chf["reference_rate"])

    def test_scenario_totals_sum_every_currency_and_period(self):
        dashboard = web_app.build_dashboard(web_app.DEMO_STATE, self.rates, forecast_doc={})
        totals = dashboard["scenario_totals"]
        rows = dashboard["scenario_rows"]

        self.assertEqual(set(totals), {"neutral", "optimistic", "pessimistic", "custom"})
        # DEMO_STATE 的期间都在过去，远期退回即期，所以中性场景恰好是 0。
        # 这条以前写成无条件断言，其实是靠"跑测试的那天已经过了 2026-06"才成立的，
        # 而且和 README 里"中性不再恒等于 0"自相矛盾。见下面那条未来期间的测试。
        self.assertEqual(totals["neutral"]["total_projected_gain_loss"], 0)

        for name in totals:
            expected_exposure = sum(
                row["projection"][name]["unrealized_exchange_gain_loss"] for row in rows
            )
            expected_hedge = sum(
                row["projection"][name][row["accounting_bucket"]] for row in rows
            )
            self.assertAlmostEqual(
                totals[name]["unrealized_exchange_gain_loss"], round(expected_exposure, 2), places=2
            )
            self.assertAlmostEqual(totals[name]["hedge_pnl"], round(expected_hedge, 2), places=2)
            self.assertAlmostEqual(
                totals[name]["total_projected_gain_loss"],
                round(expected_exposure + expected_hedge, 2),
                places=2,
            )
            self.assertEqual(
                sum(totals[name]["by_bucket"].values()), totals[name]["hedge_pnl"]
            )

        # 乐观与悲观对称，合计应互为相反数
        self.assertAlmostEqual(
            totals["optimistic"]["total_projected_gain_loss"],
            -totals["pessimistic"]["total_projected_gain_loss"],
            places=2,
        )

    def test_portfolio_totals_do_not_net_across_currencies(self):
        dashboard = web_app.build_dashboard(web_app.DEMO_STATE, self.rates, forecast_doc={})
        portfolio = dashboard["portfolio"]

        # USD 净收 + EUR 净付：取绝对值相加，不能互相抵消
        usd_gross = 1200000 * self.rates["pair_rates"]["USD"]
        eur_gross = 350000 * self.rates["pair_rates"]["EUR"]
        self.assertAlmostEqual(portfolio["gross_exposure_cny"], usd_gross + eur_gross, places=2)
        self.assertGreater(portfolio["gross_exposure_cny"], usd_gross)

        self.assertAlmostEqual(
            portfolio["locked_cny"], 500000 * self.rates["pair_rates"]["USD"], places=2
        )
        self.assertAlmostEqual(
            portfolio["locked_ratio"], portfolio["locked_cny"] / portfolio["gross_exposure_cny"], places=4
        )
        self.assertEqual(portfolio["currency_count"], 2)
        self.assertEqual(portfolio["pending_count"], len(dashboard["suggestions"]))
        self.assertEqual(portfolio["rate_missing"], [])

        by_currency = {row["currency"]: row for row in portfolio["by_currency"]}
        self.assertAlmostEqual(
            by_currency["USD"]["recommended_cny"], 460000 * self.rates["pair_rates"]["USD"], places=2
        )
        self.assertEqual(by_currency["EUR"]["locked_cny"], 0)

    def test_portfolio_skips_currencies_without_a_rate(self):
        state = copy.deepcopy(web_app.DEMO_STATE)
        state["exposures"].append(
            {
                "id": "demo-exp-3",
                "due_date": "2026-07-31",
                "currency": "ZZZ",
                "amount": 999,
                "direction": "receipt",
                "category": "cash_flow",
                "probability": 1,
            }
        )
        dashboard = web_app.build_dashboard(state, self.rates, forecast_doc={})
        portfolio = dashboard["portfolio"]

        self.assertIn("ZZZ", portfolio["rate_missing"])
        zzz = next(row for row in portfolio["by_currency"] if row["currency"] == "ZZZ")
        self.assertEqual(zzz["gross_cny"], 0)

    def test_missing_rate_blocks_recommendations_and_trial_pricing(self):
        state = copy.deepcopy(web_app.DEMO_STATE)
        state["exposures"] = [{
            "id": "e-missing", "due_date": "2027-03-31", "currency": "ZZZ",
            "amount": 1000000, "direction": "receipt", "category": "order_contract",
            "probability": 1,
        }]
        state["hedges"] = []
        state["settlements"] = []

        dashboard = web_app.build_dashboard(state, self.rates, forecast_doc={})
        zzz = dashboard["net_exposures"][0]

        self.assertFalse(zzz["rate_available"])
        self.assertIsNone(zzz["current_rate"])
        self.assertIsNone(zzz["cny_risk"])
        self.assertEqual(dashboard["suggestions"], [])
        self.assertEqual(dashboard["scenario_rows"], [])
        self.assertIn("ZZZ", dashboard["portfolio"]["rate_missing"])

    def test_recommendations_are_marked_trial_until_pricing_inputs_are_confirmed(self):
        state = copy.deepcopy(web_app.DEMO_STATE)
        state["config"] = dict(web_app.DEFAULT_CONFIG)
        dashboard = web_app.build_dashboard(state, self.rates, forecast_doc={})

        self.assertTrue(dashboard["suggestions"])
        self.assertTrue(all(row["trial"] for row in dashboard["suggestions"]))
        self.assertTrue(any("执行前请核对当前即期汇率" in reason for row in dashboard["suggestions"]
                            for reason in row["trial_reasons"]))

    def test_neutral_scenario_is_not_zero_once_there_are_forward_points(self):
        """远期贴水本身就是成本，中性场景不该恒等于 0。

        以前整条远期路径在 build_dashboard 这一层没有测试覆盖——因为样例数据
        的期间全在过去，永远走即期兜底。这里显式给一个未来期间和一个固定的今天。
        """
        state = copy.deepcopy(web_app.DEMO_STATE)
        state["config"] = dict(
            web_app.DEFAULT_CONFIG,
            interest_rates={"CNY": 0.02, "USD": 0.05, "EUR": 0.025},
        )
        state["exposures"] = [{
            "id": "e-future", "due_date": "2027-06-30", "currency": "USD",
            "amount": 1000000, "direction": "receipt", "category": "order_contract",
            "probability": 1,
        }]
        state["hedges"] = []
        state["settlements"] = []

        dashboard = web_app.build_dashboard(
            state, self.rates, forecast_doc={}, today=date(2027, 1, 1)
        )
        sug = dashboard["suggestions"][0]
        self.assertEqual(sug["forward_basis"], "cip")
        # 美元利率高于人民币，出口商锁远期结汇要贴水
        self.assertLess(sug["forward_rate"], sug["spot_rate"])
        self.assertLess(sug["forward_points"], 0)

        neutral = dashboard["scenario_totals"]["neutral"]["total_projected_gain_loss"]
        self.assertNotEqual(neutral, 0)
        self.assertLess(neutral, 0, "锁远期结汇的贴水是成本，中性场景应该是负的")

        # 同一份数据，如果到期日已过就退回即期，中性场景回到 0
        past = web_app.build_dashboard(
            state, self.rates, forecast_doc={}, today=date(2028, 1, 1)
        )
        self.assertEqual(past["suggestions"][0]["forward_basis"], "spot")
        self.assertEqual(past["scenario_totals"]["neutral"]["total_projected_gain_loss"], 0)

    def test_same_direction_position_raises_risk_instead_of_being_dropped(self):
        """方向相同的"锁汇"是加仓：既不算覆盖，也不能从剩余风险里抹掉。

        两次都踩过坑：第一版直接取 abs(locked) 当覆盖（覆盖率虚高）；
        第二版为了保住「已锁 + 剩余 = 业务敞口」这个等式，把同向持仓整个
        丢掉——100 收 + 50 买入的净敞口是 150，驾驶舱却报 100，低估风险。
        等式只在 0 ≤ 覆盖 ≤ 敞口 时成立，不是不变量。
        """
        state = copy.deepcopy(web_app.DEMO_STATE)
        state["exposures"] = [{
            "id": "e1", "due_date": "2026-06-30", "currency": "USD",
            "amount": 1200000, "direction": "receipt", "category": "order_contract",
            "probability": 1,
        }]
        # 收汇敞口却买入外币：方向反了
        state["hedges"] = [{
            "id": "h1", "trade_date": "2026-05-12", "due_date": "2026-06-30",
            "currency": "USD", "amount": 500000, "action": "buy_foreign", "locked_rate": 7.18,
        }]
        portfolio = web_app.build_dashboard(state, self.rates, forecast_doc={})["portfolio"]

        rate = self.rates["pair_rates"]["USD"]
        self.assertEqual(portfolio["locked_cny"], 0, "反向的持仓不算覆盖")
        self.assertEqual(portfolio["locked_ratio"], 0)
        # 净敞口 = 120 万收 + 50 万买入 = 170 万，比业务敞口还大
        self.assertAlmostEqual(portfolio["net_exposure_cny"], 1700000 * rate, places=2)
        self.assertGreater(portfolio["net_exposure_cny"], portfolio["gross_exposure_cny"])
        self.assertAlmostEqual(portfolio["added_risk_cny"], 500000 * rate, places=2)

    def test_over_hedging_leaves_a_naked_opposite_position(self):
        """锁过头 = 反向裸头寸，剩余风险不是 0。"""
        state = copy.deepcopy(web_app.DEMO_STATE)
        state["exposures"] = [{
            "id": "e1", "due_date": "2026-06-30", "currency": "USD",
            "amount": 1000000, "direction": "receipt", "category": "order_contract",
            "probability": 1,
        }]
        state["hedges"] = [{
            "id": "h1", "trade_date": "2026-05-12", "due_date": "2026-06-30",
            "currency": "USD", "amount": 1500000, "action": "sell_foreign", "locked_rate": 7.18,
        }]
        portfolio = web_app.build_dashboard(state, self.rates, forecast_doc={})["portfolio"]
        rate = self.rates["pair_rates"]["USD"]

        # 覆盖最多算到敞口本身
        self.assertAlmostEqual(portfolio["locked_cny"], 1000000 * rate, places=2)
        # 多卖的 50 万是净空头，仍然是风险
        self.assertAlmostEqual(portfolio["net_exposure_cny"], 500000 * rate, places=2)
        self.assertAlmostEqual(portfolio["added_risk_cny"], 500000 * rate, places=2)

    def test_totals_are_additive_only_when_hedges_are_proper(self):
        # 正常套保（方向相反、不超额）下等式才成立
        portfolio = web_app.build_dashboard(
            web_app.DEMO_STATE, self.rates, forecast_doc={}
        )["portfolio"]
        self.assertAlmostEqual(
            portfolio["locked_cny"] + portfolio["net_exposure_cny"],
            portfolio["gross_exposure_cny"], places=2,
        )
        self.assertEqual(portfolio["added_risk_cny"], 0)

    def test_zero_probability_is_rejected_not_silently_promoted(self):
        """概率 0 以前被 `or 1` 悄悄换成 1，等于把已取消的订单按全额记进敞口。"""
        for value in (0, 0.0, "0", -0.5, 1.5, "abc"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    web_app.validate_exposure({
                        "due_date": "2026-09-30", "currency": "USD", "amount": 100,
                        "direction": "receipt", "probability": value,
                    })

        # 不填或留空仍然默认 1
        for value in (None, ""):
            row = {"due_date": "2026-09-30", "currency": "USD", "amount": 100,
                   "direction": "receipt", "probability": value}
            web_app.validate_exposure(row)
            self.assertEqual(row["probability"], 1.0)

    def test_unsettled_benchmark_is_marked_provisional(self):
        dashboard = web_app.build_dashboard(web_app.DEMO_STATE, self.rates, forecast_doc={})
        for row in dashboard["backtest"]:
            bench = row.get("benchmark")
            if bench:
                self.assertEqual(bench["settled"], row["settled"])

    def test_pair_rates_from_open_endpoint_payload(self):
        payload = {"rates": {"USD": 1, "CNY": 7.2, "EUR": 0.9}}
        rates = web_app.pair_rates_from_payload(payload, ["USD", "EUR"])

        self.assertEqual(rates["USD"], 7.2)
        self.assertEqual(rates["EUR"], 8.0)


if __name__ == "__main__":
    unittest.main()
