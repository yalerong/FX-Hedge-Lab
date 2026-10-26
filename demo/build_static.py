"""把工作台烘成一个可点的静态演示站。

工作台本身是 Python 服务，Cloudflare Pages 只托管静态文件，所以这里：

1. 用一份**合成**的演示数据跑一遍 `build_dashboard`，把结果烘成 JSON；
2. 原样复制真实的 `web/index.html` / `app.js` / `styles.css`——
   演示站跑的就是生产那套前端代码，不是另写一份，否则两边会漂；
3. 在 app.js 之前插一段垫片，把 `fetch("/api/...")` 接管掉：
   读接口返回烘好的 JSON，写接口一律回 403 并说明这是只读演示。

用法：

    python demo/build_static.py <输出目录>
"""
from __future__ import annotations

import json
import re
import shutil
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import web_app  # noqa: E402
import plans  # noqa: E402


def guard_output_dir(out: Path) -> None:
    """构建前会 rmtree 输出目录，所以先确认它不是个能删坏东西的路径。

    `python demo/build_static.py .` 会把 out 解析成仓库根目录，
    直接删掉整个 checkout；传 `demo` 会删掉这个脚本自己和演示页源文件。
    这类参数看着都挺自然，不能靠使用者小心。
    """
    protected = {ROOT, ROOT / "demo", ROOT / "web", ROOT / "data", ROOT / "docs",
                 ROOT / "events", ROOT / "forecast", ROOT / ".git"}
    if out in protected:
        raise SystemExit(f"拒绝：{out} 是源目录，构建会先删空它")
    if out == Path(out.anchor):
        raise SystemExit(f"拒绝：{out} 是盘符/根目录")
    if ROOT == out or ROOT.is_relative_to(out):
        raise SystemExit(f"拒绝：{out} 包含着仓库本身，删它会删掉源码")
    if out.exists() and any(
        (out / name).exists() for name in ("web_app.py", "build_static.py", ".git")
    ):
        raise SystemExit(f"拒绝：{out} 看起来是源目录（里面有源文件），不是构建产物目录")


def month_end(d: date) -> str:
    first_next = date(d.year + (d.month == 12), (d.month % 12) + 1, 1)
    return (first_next - timedelta(days=1)).isoformat()


def demo_state(today: date) -> dict:
    """演示数据。全部合成，刻意排出几种值得看的情形：

    - 一个未来期间：能看到远期贴水，中性情景因此不为 0
    - 一个已结算期间且录了实际发生额：能看到价差/量差分解与超额套保
    - 一个只锁了一半的敞口：能看到覆盖率与待锁建议
    - 一个没有本地行情序列的币种（EUR）：能看到「无月均基准」的诚实兜底
    """
    far = month_end(date(today.year + (today.month > 6), ((today.month + 6 - 1) % 12) + 1, 1))
    near = month_end(date(today.year, today.month, 1) + timedelta(days=62))
    settled = month_end(date(today.year, today.month, 1) - timedelta(days=1))

    return {
        "metadata": {
            "setup_complete": True,
            "data_mode": "sample",
            "created_at": f"{today.isoformat()}T00:00:00Z",
            "updated_at": f"{today.isoformat()}T00:00:00Z",
        },
        "config": dict(
            web_app.DEFAULT_CONFIG,
            default_hedge_ratio=0.8,
            # 这份数据同时有收和付，只能用 comprehensive。
            # 用 export 的话 action_for 会给净付的欧元敞口也返回 sell_foreign，
            # 于是卡片文案写着"买入外币"、按钮填进去的却是卖出。
            enterprise_type="comprehensive",
            interest_rates={"CNY": 0.019, "USD": 0.043, "EUR": 0.025},
        ),
        "monthly_average_rates": {f"{settled[:7]}:USD": 7.12},
        "exposures": [
            {"id": "d1", "due_date": far, "currency": "USD", "amount": 1200000,
             "direction": "receipt", "category": "order_contract", "probability": 1,
             "booked": False, "description": "出口订单预计收款"},
            {"id": "d2", "due_date": far, "currency": "EUR", "amount": 350000,
             "direction": "payment", "category": "cash_flow", "probability": 0.8,
             "booked": False, "description": "进口采购预计付款"},
            {"id": "d3", "due_date": near, "currency": "USD", "amount": 600000,
             "direction": "receipt", "category": "balance_sheet", "probability": 1,
             "booked": True, "description": "已开票应收"},
            {"id": "d4", "due_date": settled, "currency": "USD", "amount": 900000,
             "direction": "receipt", "category": "order_contract", "probability": 1,
             "booked": False, "description": "上月出口订单"},
        ],
        "hedges": [
            {"id": "h1", "trade_date": settled[:8] + "05", "due_date": far,
             "currency": "USD", "amount": 500000, "action": "sell_foreign",
             "locked_rate": 7.05, "description": "远期结汇锁定部分美元收款"},
            {"id": "h2", "trade_date": settled[:8] + "06", "due_date": settled,
             "currency": "USD", "amount": 700000, "action": "sell_foreign",
             "locked_rate": 7.18, "description": "上月远期结汇"},
        ],
        "settlements": [
            {"id": "s1", "due_date": settled, "currency": "USD",
             "actual_rate": 7.05, "actual_amount": 620000,
             "description": "到期结算，客户只提了一部分货"},
        ],
    }


def demo_forecast(today: date) -> dict:
    """跟着构建日期滚动的合成信号。

    不能用签入的 data/forecast_signals.json：那份是固定月份的，
    而演示敞口的期间是从 today 往后推的，两者会错开——
    拿一段已经结束的预测去影响另一个到期日的建议，演示站上会出现
    自相矛盾的东西。（信号覆盖不到该期间时应该退回 1.0×，
    这个闸门缺口本身另行处理。）
    """
    months = []
    year, month = today.year, today.month
    rate = 7.18
    for _ in range(8):
        month += 1
        if month > 12:
            year, month = year + 1, 1
        rate += 0.012
        months.append({"month": f"{year}-{month:02d}", "rate": round(rate, 4), "dir": "up"})
    return {
        "generated_at": f"{today.isoformat()}T00:00:00Z",
        "horizon_months": len(months),
        "base_currency": "CNY",
        "signals": {
            "USD": {
                "pair": "USDCNY", "current": 7.18,
                "current_month": today.strftime("%Y-%m"),
                "forecast": months, "direction": "up",
                # 预测幅度约 1.3%，刻意压在 MAPE 之下——
                # 演示站默认停在"预测有利但被信噪比闸门挡掉"这一格
                "mape": 0.018, "direction_accuracy": 0.62, "n_test": 30, "tier": "support",
            },
            "EUR": {
                "pair": "EURCNY", "current": 7.72,
                "current_month": today.strftime("%Y-%m"),
                "forecast": [dict(m, rate=round(7.72 + i * 0.02, 4))
                             for i, m in enumerate(months)],
                "direction": "up", "mape": 0.063, "direction_accuracy": 0.42,
                "n_test": 30, "tier": "reject",
            },
        },
    }


DEMO_RATES = {
    "source": "演示用固定汇率（非实时）",
    "source_url": "",
    "status": "demo",
    "fetched_at": None,
    "pair_rates": {"USD": 7.18, "EUR": 7.72, "JPY": 0.049, "HKD": 0.915,
                   "GBP": 9.05, "AUD": 4.72, "SGD": 5.31},
}

SHIM = """<script>
// ==== 静态演示垫片 ====
// 下面这段是**只有演示站有**的东西：把 fetch 接管掉，读接口返回预先烘好的
// dashboard JSON，写接口一律拒绝。前端代码本身与生产版一字不差。
(function () {
  var BAKED = __STATE__;
  function reply(body, status) {
    return Promise.resolve(new Response(JSON.stringify(body), {
      status: status || 200,
      headers: { "Content-Type": "application/json; charset=utf-8" },
    }));
  }
  function textReply(text, status, contentType) {
    return Promise.resolve(new Response(text, {
      status: status || 200,
      headers: { "Content-Type": contentType || "text/plain; charset=utf-8" },
    }));
  }
  function csvCell(value) {
    var text = value == null ? "" : String(value);
    if (/^[=+@-]/.test(text)) text = "'" + text;
    return /[",\\r\\n]/.test(text) ? '"' + text.replace(/"/g, '""') + '"' : text;
  }
  function demoCsv(path) {
    var collection = new URL(path, window.location.href).searchParams.get("collection") || "exposures";
    var specs = {
      exposures: { fields: ["id", "created_at", "due_date", "currency", "amount", "direction", "probability", "booked", "category", "description"], rows: BAKED.exposures },
      hedges: { fields: ["id", "created_at", "trade_date", "due_date", "currency", "action", "amount", "locked_rate", "designated_period", "hedge_type", "spot_at_trade", "description"], rows: BAKED.hedges },
      settlements: { fields: ["id", "created_at", "due_date", "currency", "actual_rate", "actual_amount", "description"], rows: BAKED.settlements },
    };
    var spec = specs[collection];
    if (!spec) return null;
    return [spec.fields.join(",")].concat((spec.rows || []).map(function (row) {
      return spec.fields.map(function (field) { return csvCell(row[field]); }).join(",");
    })).join("\\r\\n") + "\\r\\n";
  }
  var realFetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    var path = String((input && input.url) || input || "");
    if (path.indexOf("/api/") === -1) return realFetch(input, init);
    var method = ((init && init.method) || "GET").toUpperCase();
    if (method === "GET" && path.indexOf("/api/state") !== -1) return reply(BAKED);
    if (method === "GET" && (path.indexOf("/api/export") !== -1 || path.indexOf("/api/workspace/export") !== -1)) return reply({
      exported_at: BAKED.workspace && BAKED.workspace.metadata && BAKED.workspace.metadata.updated_at,
      state: BAKED,
      demo: true,
    });
    if (method === "GET" && path.indexOf("/api/csv/export") !== -1) {
      var csv = demoCsv(path);
      return csv === null
        ? reply({ ok: false, error: "不支持的明细类型。" }, 400)
        : textReply(csv, 200, "text/csv; charset=utf-8");
    }
    if (method === "GET" && path.indexOf("/api/xlsx/export") !== -1) {
      return reply({
        ok: false,
        error: "这是只读的静态演示站，不能导出 Excel。真实工具在本地运行（见页脚仓库链接）。",
      }, 403);
    }
    if (path.indexOf("/api/csv/import") !== -1) {
      return reply({
        ok: false,
        error: "这是只读的静态演示站，不能导入 CSV。真实工具在本地运行（见页脚仓库链接）。",
      }, 403);
    }
    if (path.indexOf("/api/xlsx/import") !== -1) {
      return reply({
        ok: false,
        error: "这是只读的静态演示站，不能导入 Excel。真实工具在本地运行（见页脚仓库链接）。",
      }, 403);
    }
    if (method === "GET" && path.indexOf("/api/backups") !== -1) return reply({
      backups: [],
      data_file: "static demo",
    });
    return reply({
      ok: false,
      error: "这是只读的静态演示站，改不了数据。真实工具在本地运行（见页脚仓库链接）。",
    }, 403);
  };
})();
</script>
"""

BANNER = """<div class="demo-banner">
  <strong>只读演示</strong>
  <span>数据全部合成，不含任何真实企业的头寸或对手方；汇率为演示用固定值。
  按钮和表单可以点，但不会真的改数据。</span>
  <span class="demo-asof">数据截至 __ASOF__</span>
  <a href="/method">看方法与研究结论 →</a>
  <a href="https://github.com/yalerong/FX-Hedge-Lab">源码</a>
</div>
"""

# Cloudflare Pages 读构建产物里的 `_headers`，给静态响应加头。这里的取值是被
# 演示站自身的形态卡死的，不是抄来的模板：
#
# - `script-src`/`style-src` 必须留 `'unsafe-inline'`。垫片是内联 <script>，
#   /method 与 404 页是内联 <style>，工作台里还有一处 `style=` 属性。
#   换成 hash 的话每次构建都要重算，而这个脚本本来就是靠字符串锚点改 HTML 的——
#   锚点一漂，hash 对不上就是整页白屏，代价远大于收益。
# - 真正在挡事的是另外几条：`frame-ancestors 'none'` 挡别人把演示站嵌进自己页面里
#   做点击劫持；`connect-src 'self'` 让页面即便被注入也发不出外部请求；
#   `object-src`/`base-uri` 断掉两条老注入路径。
# - `form-action` 用 `'self'` 而不是 `'none'`：四个表单都靠 app.js 的
#   preventDefault 拦着，万一 app.js 没加载出来会退化成原生提交，
#   `'self'` 让它原地跳转（无害），`'none'` 只会多一条看不懂的报错。
# - `X-Robots-Tag` 是给 robots.txt 补一道：robots.txt 只管爬虫抓不抓，
#   这个头把 noindex 标到每个响应上，非 HTML 资源也覆盖得到。
CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data:",
    "font-src 'self'",
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'self'",
    "frame-ancestors 'none'",
])

HEADERS = f"""/*
  Content-Security-Policy: {CSP}
  X-Content-Type-Options: nosniff
  X-Frame-Options: DENY
  Referrer-Policy: no-referrer
  Permissions-Policy: accelerometer=(), camera=(), geolocation=(), gyroscope=(), microphone=(), payment=(), usb=()
  Cross-Origin-Opener-Policy: same-origin
  X-Robots-Tag: noindex, nofollow, noarchive
"""


BANNER_CSS = """
.demo-banner {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px 18px;
  padding: 12px 24px;
  background: var(--accent-soft);
  border-bottom: 1px solid var(--accent-line);
  font-size: 13px;
  color: var(--ink, var(--text));
}
.demo-banner strong { color: var(--accent-dark); }
.demo-banner span { flex: 1 1 420px; color: var(--muted); }
.demo-banner a { color: var(--accent-dark); font-weight: 600; }
.demo-banner .demo-asof { flex: 0 0 auto; color: var(--muted); font-family: var(--mono, monospace); }
/* 需要真实服务端的动作（例如导出套期关系文档）在只读演示站上不显示 */
[data-demo-hide] { display: none !important; }
"""


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "demo" / "_static").resolve()
    today = date.today()

    state = demo_state(today)
    forecast = demo_forecast(today)
    # 演示站的报价是固定值，但时点必须跟着构建日走：时点为空会被
    # 报价层判成"过期"，于是每张建议都挂着试算标签，看起来像故障。
    demo_rates = dict(DEMO_RATES, fetched_at=f"{today.isoformat()}T00:00:00Z", status="demo")
    dashboard = web_app.build_dashboard(
        state, demo_rates, forecast_doc=forecast, today=today
    )
    # 冻一份方案进去，这样「方案存档」和参数漂移在演示站上也是活的
    plan = dict(plans.freeze(dashboard, "上月冻结的方案", f"{today.isoformat()}T00:00:00Z"),
                id="demo-plan-1")
    state["plans"] = [plan]
    state["config"] = dict(state["config"], default_hedge_ratio=0.7)  # 制造一次参数漂移
    dashboard = web_app.build_dashboard(
        state, demo_rates, forecast_doc=forecast, today=today
    )
    dashboard["audit"] = [
        {"at": f"{today.isoformat()}T02:10:00Z", "action": "update", "collection": "config",
         "id": None, "before": None, "after": {"default_hedge_ratio": {"from": 0.8, "to": 0.7}}},
        {"at": f"{today.isoformat()}T02:05:00Z", "action": "freeze", "collection": "plans",
         "id": "demo-plan-1", "before": None,
         "after": {"label": plan["label"], "rows": len(plan["rows"])}},
        {"at": f"{today.isoformat()}T02:00:00Z", "action": "create", "collection": "exposures",
         "id": "d3", "before": None, "after": state["exposures"][2]},
    ]

    guard_output_dir(out)
    if out.exists():
        shutil.rmtree(out)
    (out / "web").mkdir(parents=True)

    shutil.copy(ROOT / "web" / "app.js", out / "web" / "app.js")
    styles = (ROOT / "web" / "styles.css").read_text(encoding="utf-8") + BANNER_CSS
    (out / "web" / "styles.css").write_text(styles, encoding="utf-8")

    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    shim = SHIM.replace("__STATE__", json.dumps(dashboard, ensure_ascii=False))
    banner = BANNER.replace("__ASOF__", today.isoformat())
    html, banner_count = re.subn(r"<body>", "<body>\n" + banner.replace("\\", "\\\\"), html, count=1)
    if banner_count != 1:
        # 只读横幅是这个站唯一说明"数据是合成的"的地方，
        # 静默丢掉比构建失败严重得多
        raise SystemExit("没能插入只读横幅：<body> 锚点变了")
    if '<script src="/web/app.js' not in html:
        raise SystemExit("没能插入垫片：app.js 的引用锚点变了")
    html = html.replace('<script src="/web/app.js', shim + '    <script src="/web/app.js', 1)
    html = html.replace(
        "<title>外汇风险与锁汇工作台</title>",
        "<title>外汇风险与锁汇工作台 · 只读演示</title>"
        '\n    <meta name="robots" content="noindex,nofollow,noarchive" />',
        1,
    )
    (out / "index.html").write_text(html, encoding="utf-8")

    # 原来那个讲方法的展示页留在 /method.html
    shutil.copy(ROOT / "demo" / "index.html", out / "method.html")
    for name in ("robots.txt", "404.html"):
        shutil.copy(ROOT / "demo" / name, out / name)

    # Pages 自己消费掉 `_headers`，它不会被当成一个可访问的文件发出去
    (out / "_headers").write_text(HEADERS, encoding="utf-8")

    size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    print(f"输出目录: {out}")
    print(f"文件数: {sum(1 for f in out.rglob('*') if f.is_file())}，合计 {size / 1024:.0f} KB")
    print(f"烘入 dashboard: {len(dashboard['suggestions'])} 条建议 / "
          f"{len(dashboard['net_exposures'])} 行净敞口 / {len(dashboard['backtest'])} 行回测")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
