"""静态演示站构建的冒烟测试。

`demo/build_static.py` 靠几个字符串锚点往 `web/index.html` 里插垫片。
锚点一变（比如改了 app.js 的版本号写法），垫片就**静默**不注入——
本地跑构建照样成功，部署上去才发现页面在打真实的 /api 然后白屏。
这里把这几件事钉死。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BUILD = ROOT / "demo" / "build_static.py"
BAKED_RE = r"var BAKED = (\{.*?\});\n"

# 构建脚本的提示和 SystemExit 文案都是中文。子进程默认按控制台编码写出去
# （Windows 上是 cp936），这边再按 utf-8 解就会失败，subprocess 把 stdout/stderr
# 留成 None——测试于是死在 `proc.stdout + proc.stderr` 上，而不是死在它要测的事情上。
# 钉死子进程的 IO 编码，让两边一致。
UTF8_ENV = {**os.environ, "PYTHONIOENCODING": "utf-8"}


class StaticBuildTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.out = Path(cls._tmp.name) / "site"
        proc = subprocess.run(
            [sys.executable, str(BUILD), str(cls.out)],
            capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT),
            env=UTF8_ENV,
        )
        assert proc.returncode == 0, proc.stderr
        cls.html = (cls.out / "index.html").read_text(encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_every_file_the_page_needs_is_there(self):
        for name in ("index.html", "method.html", "robots.txt", "404.html",
                     "_headers", "web/app.js", "web/styles.css"):
            self.assertTrue((self.out / name).exists(), f"缺 {name}")

    def test_the_shim_actually_got_injected_before_app_js(self):
        self.assertIn("静态演示垫片", self.html, "垫片没注入——锚点大概改了")
        shim_at = self.html.index("静态演示垫片")
        app_at = self.html.index('src="/web/app.js')
        self.assertLess(shim_at, app_at, "垫片必须排在 app.js 之前，否则接管不到 fetch")

    def test_baked_state_is_valid_json_with_the_keys_the_page_reads(self):
        match = re.search(BAKED_RE, self.html, re.S)
        self.assertIsNotNone(match, "找不到烘进去的 dashboard")
        baked = json.loads(match.group(1))
        for key in ("net_exposures", "suggestions", "portfolio", "scenario_totals",
                    "scenario_rows", "backtest", "audit", "plans", "plan_drift", "config"):
            self.assertIn(key, baked, f"烘进去的数据缺 {key}")
        self.assertTrue(baked["suggestions"], "演示数据要有待锁汇建议，否则页面空一块")

    def test_baked_demo_workspace_is_marked_initialized(self):
        match = re.search(BAKED_RE, self.html, re.S)
        baked = json.loads(match.group(1))

        self.assertEqual(baked["workspace"]["data_mode"], "sample")
        self.assertTrue(baked["workspace"]["setup_complete"])

    def test_static_csv_export_uses_displayed_rows(self):
        self.assertIn("BAKED.exposures", self.html)
        self.assertNotIn("static-demo,,,,", self.html)

    def test_frontend_resets_stale_edit_and_undo_state(self):
        source_js = (self.out / "web" / "app.js").read_text(encoding="utf-8")

        self.assertIn("form.reset();", source_js)
        self.assertIn("clearUndoState();", source_js)
        self.assertIn("lastDeleted = null;", source_js)
        # 换工作区后同时清掉编辑态，否则陈旧表单会覆盖新工作区里同 ID 的记录
        self.assertIn("resetActiveEdits();", source_js)
        self.assertEqual(source_js.count("afterWorkspaceReplaced();"), 6)

    def test_frontend_escapes_stored_plan_fields_and_disables_past_due_actions(self):
        source_js = (self.out / "web" / "app.js").read_text(encoding="utf-8")

        self.assertIn("${escapeHtml(row.trade_rate)}", source_js)
        self.assertNotIn("${row.trade_rate}", source_js)
        self.assertIn('notes.map(escapeHtml).join("；")', source_js)
        self.assertNotIn('${notes.join("；")}', source_js)
        self.assertIn('<button type="button" disabled>按建议填入锁汇单</button>', source_js)
        self.assertIn("if (!item.past_due)", source_js)

    def test_productized_workflow_exposes_user_actions_without_new_dependencies(self):
        source_html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        source_js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        source_css = (ROOT / "web" / "styles.css").read_text(encoding="utf-8")

        self.assertLess(source_html.index('id="todo"'), source_html.index('id="cockpit"'))
        self.assertIn("primary-file-action", source_html)
        self.assertEqual(source_html.count('id="importXlsxFile"'), 1)
        self.assertIn('id="advancedConfig"', source_html)
        self.assertLess(source_html.index('id="dataManagement"'), source_html.index('id="resetDemoBtn"'))
        self.assertIn("TRADINGVIEW_CNY_CURRENCIES", source_js)
        self.assertIn("www.tradingview.com/symbols/", source_js)
        self.assertIn('rel="noopener noreferrer"', source_js)
        self.assertIn("行情为参考价，真正可执行价格以银行远期报价为准", source_js)
        self.assertIn("window.print()", source_js)
        self.assertIn("data-print-plan", source_css)

    def test_config_form_exposes_provisional_confirmation_flags(self):
        self.assertIn('name="confirmed_parameters"', self.html)

    def test_demo_data_shows_the_things_worth_showing(self):
        match = re.search(BAKED_RE, self.html, re.S)
        baked = json.loads(match.group(1))

        # 有未来期间，远期贴水才看得见、中性情景才不为 0
        bases = {row.get("forward_basis") for row in baked["suggestions"]}
        self.assertIn("cip", bases, "演示数据要有未来期间，否则远期价全退回即期")
        self.assertNotEqual(baked["scenario_totals"]["neutral"]["total_projected_gain_loss"], 0)

        # 有已结算且录了实际发生额的期间，价量分解才出得来
        with_variance = [
            row for row in baked["backtest"]
            if (row.get("benchmark") or {}).get("variance")
        ]
        self.assertTrue(with_variance, "演示数据要有一条录了实际发生额的结算")

        # 冻过方案且改过参数，方案存档与漂移提示才是活的
        self.assertTrue(baked["plans"])
        self.assertTrue(baked["plan_drift"]["stale"], "演示站要能看到参数漂移的样子")

    def test_page_is_marked_read_only_and_not_indexed(self):
        # 断言 "只读演示" 是不够的：改写后的 <title> 里也有这四个字，
        # 横幅整个丢掉这条也照样绿。要断言横幅特有的标记。
        self.assertIn('class="demo-banner"', self.html, "只读横幅没插进去")
        self.assertIn("数据全部合成", self.html)
        self.assertIn('name="robots"', self.html)
        self.assertIn("Disallow", (self.out / "robots.txt").read_text(encoding="utf-8"))

    def test_banner_says_when_the_data_was_baked(self):
        # 页面把构建那天的日期烘死在里面（到期日、已过期标记都是相对它算的）。
        # 不写明截至日期的话，放久了访客看到的是一份过期决策而不自知。
        import datetime
        self.assertIn("数据截至", self.html)
        self.assertIn(datetime.date.today().isoformat(), self.html)

    def test_forecast_signals_track_the_build_date(self):
        # 不能用签入的那份固定月份信号：演示期间是从构建日往后推的，
        # 两者错开就会拿一段已经结束的预测去影响别的到期日
        match = re.search(BAKED_RE, self.html, re.S)
        baked = json.loads(match.group(1))
        signals = (baked.get("forecast") or {}).get("signals") or {}
        self.assertTrue(signals, "演示站要有信号，否则闸门那块看不到")
        import datetime
        today = datetime.date.today()
        for currency, signal in signals.items():
            with self.subTest(currency=currency):
                months = [row["month"] for row in signal.get("forecast", [])]
                self.assertTrue(months)
                self.assertGreaterEqual(max(months), today.strftime("%Y-%m"),
                                        "信号的预测区间不能整段落在过去")

    def test_refuses_to_wipe_source_directories(self):
        """构建前会 rmtree 输出目录，所以危险参数必须在删之前就被拒。

        `python demo/build_static.py .` 会解析成仓库根、`demo` 会解析成
        脚本自己所在的目录——这两个参数都挺自然，不能靠使用者小心。
        """
        for arg in (".", "demo", "web", "data"):
            with self.subTest(arg=arg):
                proc = subprocess.run(
                    [sys.executable, str(BUILD), arg],
                    capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT),
                    env=UTF8_ENV,
                )
                self.assertNotEqual(proc.returncode, 0, f"传 {arg} 居然构建成功了")
                self.assertIn("拒绝", proc.stdout + proc.stderr)
        # 源文件一个都不能少
        for name in ("demo/build_static.py", "demo/index.html", "web/index.html", "web_app.py"):
            self.assertTrue((ROOT / name).exists(), f"{name} 被删了")

    def test_security_headers_are_emitted_for_every_path(self):
        text = (self.out / "_headers").read_text(encoding="utf-8")
        first_line = text.split("\n", 1)[0]
        self.assertEqual(first_line, "/*",
                         "规则行必须是 /*，否则这些头只盖到某个子路径上")
        for header in ("Content-Security-Policy", "X-Content-Type-Options",
                       "X-Frame-Options", "Referrer-Policy", "X-Robots-Tag"):
            self.assertIn(f"  {header}: ", text, f"缺 {header}")
        # 这条是这份 _headers 里唯一挡得住实际攻击的：别人把演示站嵌进自己页面
        # 做点击劫持。写错成 'self' 之类的话文件照样在、测试照样绿。
        self.assertIn("frame-ancestors 'none'", text)

    def test_csp_stays_compatible_with_what_the_page_actually_loads(self):
        """CSP 是 `'self'` 单源的，页面一旦引到外部资源就会在生产上被静默挡掉。

        本地 `python web_app.py` 不发这些头，所以这类回归在本地跑是看不出来的：
        加个 CDN 字体或统计脚本，本地一切正常，部署上去才发现被 CSP 拦了。
        这里在构建产物上扫一遍外部来源。
        """
        offenders = []
        for path in sorted(self.out.rglob("*")):
            if not path.is_file() or path.suffix not in (".html", ".css", ".js"):
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for match in re.finditer(
                r"""(?:src|href)\s*=\s*["'](https?:)?//[^"']+|@import[^;]*//|url\(\s*["']?(https?:)?//""",
                text,
            ):
                snippet = match.group(0)
                # 页脚指向 GitHub 仓库的超链接是导航，不受 CSP 取源限制
                if snippet.startswith("href") and "github.com" in snippet:
                    continue
                offenders.append(f"{path.name}: {snippet[:80]}")
        self.assertEqual(offenders, [],
                         "CSP 只放行同源，这些外部引用会在演示站上被挡掉")

    def test_no_external_attribution_leaks_into_the_bundle(self):
        for path in self.out.rglob("*"):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for word in ("酷滴", "114463", "fx_patent_reconstruction"):
                self.assertNotIn(word, text, f"{path.name} 里有不该外发的表述：{word}")

    def test_live_page_exposes_workspace_management_and_advanced_controls(self):
        source_html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        source_js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        source_css = (ROOT / "web" / "styles.css").read_text(encoding="utf-8")

        for marker in (
            'id="workspaceBadge"', 'id="setupPanel"', 'id="dataManagement"',
            'id="exportWorkspaceBtn"', 'id="importWorkspaceFile"',
            'id="csvCollectionSelect"', 'id="exportCsvBtn"', 'id="importCsvFile"',
            'id="exportXlsxBtn"', 'id="importXlsxFile"',
            'id="restoreLatestBackupBtn"', 'id="undoDeleteBtn"', 'id="clearBusinessBtn"',
            'name="supported_currencies"',
            'name="interest_rates"', 'name="forward_overrides"',
            'name="month_currency_hedge_ratios"', 'name="scenario_shifts"',
            'name="monthly_average_rates"',
        ):
            self.assertIn(marker, source_html)
        self.assertIn('"Content-Type": "application/json"', source_js)
        self.assertIn('"PUT"', source_js)
        self.assertIn("parseConfigJsonFields", source_js)
        self.assertIn("localToday", source_js)
        self.assertIn("默认套保比例（%）", source_html)
        self.assertIn("CONFIG_PERCENT_FIELDS", source_js)
        self.assertEqual(source_js.count("Number(data[key]) / 100"), 1,
                         "配置百分比只能换算一次，否则 80% 会被保存成 0.8%")
        self.assertIn("Number(config[key]) * 100", source_js)
        self.assertIn("badge.title = badge.textContent", source_js)
        self.assertIn(".topbar .workspace-badge", source_css)

    def test_detail_tables_have_closed_sections_for_editable_rows(self):
        source_html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        for tbody_id in ("exposureRows", "hedgeRows", "settlementRows"):
            with self.subTest(tbody_id=tbody_id):
                tbody_at = source_html.index(f'id="{tbody_id}"')
                table_close_at = source_html.index("</table>", tbody_at)
                section_close_at = source_html.index("</section>", table_close_at)
                next_panel_at = source_html.find('<section class="panel"', tbody_at + 1)
                self.assertTrue(next_panel_at == -1 or section_close_at < next_panel_at)

    def test_static_demo_write_failures_are_user_readable_for_new_routes(self):
        self.assertIn("/api/backups", self.html)
        self.assertIn("/api/export", self.html)
        self.assertIn("/api/csv/export", self.html)
        self.assertIn("/api/csv/import", self.html)
        self.assertIn("/api/xlsx/export", self.html)
        self.assertIn("/api/xlsx/import", self.html)
        self.assertIn("只读", self.html)


if __name__ == "__main__":
    unittest.main()
