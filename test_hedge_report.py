"""套期关系文档导出。"""
from __future__ import annotations

import unittest
from datetime import date

import hedge_report
import web_app

ASOF = date(2026, 10, 26)
RATES = {"source": "test", "status": "test", "fetched_at": "2026-10-26T00:00:00Z",
         "pair_rates": {"USD": 7.18, "EUR": 7.72}}


def dashboard(config=None):
    state = web_app.sample_state()
    if config:
        state["config"] = dict(state["config"], **config)
    return web_app.build_dashboard(state, RATES, forecast_doc={}, today=ASOF)


class ReportTest(unittest.TestCase):
    def render(self, **config):
        merged = dict(web_app.DEFAULT_CONFIG, **config)
        return hedge_report.render_html(dashboard(config), merged, ASOF, generated_at="2026-10-26 10:00")

    def test_report_has_all_sections(self):
        html = self.render()
        for heading in ("套期关系指定", "有效性测试", "OCI 滚动", "无效部分与损益影响",
                        "假设、限制与警示", "签署"):
            self.assertIn(heading, html)
        self.assertIn("套期关系与有效性测试文档", html)

    def test_user_text_is_escaped(self):
        html = self.render(entity_name="<script>alert(1)</script>", prepared_by="张三 & 李四")
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertIn("张三 &amp; 李四", html)

    def test_oci_rollforward_nets_settled_hedges(self):
        # 样例里那笔已结算：确认和重分类同额，期末应等于期初
        html = self.render(oci_opening_cny=1000)
        self.assertIn("期初余额（配置录入）", html)
        self.assertIn("1,000.00", html)
        self.assertIn("15,000.00", html)

    def test_standard_is_rendered(self):
        html = self.render(hedge_standard="IFRS9")
        self.assertIn("IFRS 9", html)

    def test_empty_book_still_renders(self):
        state = web_app.empty_state(setup_complete=True)
        data = web_app.build_dashboard(state, RATES, forecast_doc={}, today=ASOF)
        html = hedge_report.render_html(data, dict(web_app.DEFAULT_CONFIG), ASOF)
        self.assertIn("套期关系与有效性测试文档", html)
        self.assertIn("无", html)


if __name__ == "__main__":
    unittest.main()
