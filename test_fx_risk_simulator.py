import unittest
from pathlib import Path

from fx_risk_simulator import build_report, explain_report, load_case


class FxRiskSimulatorTest(unittest.TestCase):
    def setUp(self):
        self.data = load_case(Path(__file__).with_name("sample_data.json"))

    def test_builds_exposure_and_strategy(self):
        report = build_report(self.data)
        self.assertAlmostEqual(report["unhedged_exposure"]["2026-06:USD"], 2493000.0)
        self.assertAlmostEqual(report["unhedged_exposure"]["2026-06:EUR"], -350000.0)
        self.assertEqual(report["recommended_strategy"]["hedge_ratio"], 1.0)

    def test_validation_flags_accounting_difference(self):
        report = build_report(self.data)
        messages = [issue["message"] for issue in report["validation_issues"]]
        self.assertIn("reported pnl differs for 2026-06 USD", messages)

    def test_beginner_explanation_mentions_core_logic(self):
        explanation = explain_report(build_report(self.data))
        self.assertIn("第一步：找敞口", explanation)
        self.assertIn("当前样例推荐套保 100%", explanation)
        self.assertIn("风险回溯", explanation)


if __name__ == "__main__":
    unittest.main()
