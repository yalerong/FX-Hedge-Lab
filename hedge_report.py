"""套期关系文档：指定表、有效性测试、OCI 滚动、签署栏。

输出一份自包含的 HTML（内联 CSS、无脚本、无外部资源），浏览器里
`Ctrl+P` 就能打印成 PDF 或存档。为什么不是 docx：核心代码零第三方依赖，
引 python-docx 会破坏那条约束；HTML 在审计场景里也够用（可归档、可打印、
可加签章扫描件）。

**这是底稿，不是审计意见**：科目表、税、减值、套期关系审批流程都不在
工具里。文档里会把这些限制写清楚，免得被当成一份"合规证明"。
"""
from __future__ import annotations

from datetime import date, datetime
from html import escape

STANDARD_NAMES = {
    "CAS24": "CAS 24（企业会计准则第 24 号——套期会计）",
    "IFRS9": "IFRS 9（金融工具）",
    "ASC815": "ASC 815（衍生工具与套期）",
}

TYPE_NAMES = {
    "cash_flow": "现金流量套期",
    "fair_value": "公允价值套期",
    "net_investment": "境外经营净投资套期",
}


def _num(value) -> str:
    try:
        return f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return "—"


def _pct(value) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value) * 100:.2f}%"
    except (TypeError, ValueError):
        return "—"


def _conclusion(ratio, ineffective) -> str:
    if ratio is None:
        return "无法计算（缺交易日即期或被套项目变动为 0）"
    absolute = abs(float(ratio))
    if 0.8 <= absolute <= 1.25:
        return "在 80%~125% 参考带内"
    return "超出 80%~125% 参考带，需复核并补充说明"


def _table(headers: list[str], rows: list[list[str]], column_classes: list[str] | None = None) -> str:
    head = "".join(f"<th>{escape(item)}</th>" for item in headers)
    body = []
    for row in rows:
        cells = []
        for index, cell in enumerate(row):
            cls = column_classes[index] if column_classes and index < len(column_classes) else ""
            cells.append(f'<td class="{cls}">{cell}</td>' if cls else f"<td>{cell}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    if not body:
        body.append(f'<tr><td colspan="{len(headers)}" class="empty">无</td></tr>')
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def render_html(
    dashboard: dict, config: dict, asof: date, generated_at: str | None = None
) -> str:
    generated = generated_at or datetime.now().strftime("%Y-%m-%d %H:%M")
    accounting = dashboard.get("hedge_accounting") or {}
    rows = accounting.get("rows") or []
    totals = accounting.get("totals") or {}
    net_rows = {
        (row.get("period"), row.get("currency")): row
        for row in (dashboard.get("net_exposures") or [])
    }
    rates = dashboard.get("rates") or {}
    entity = config.get("entity_name") or "（未填写主体名称）"
    prepared_by = config.get("prepared_by") or "（未填写）"
    standard = config.get("hedge_standard", "CAS24")
    opening = float(config.get("oci_opening_cny", 0) or 0)

    designated = [row for row in rows if float(row.get("allocated") or 0) > 0]
    cash_flow_rows = [row for row in designated if row.get("hedge_type") == "cash_flow"]
    net_investment_rows = [row for row in designated if row.get("hedge_type") == "net_investment"]
    cash_effective = sum(float(row.get("effective_cny") or 0) for row in cash_flow_rows)
    cash_reclassified = sum(float(row.get("reclassified_cny") or 0) for row in cash_flow_rows)
    # 期初 + 本期确认 − 本期重分类。已结算的套期"确认"和"重分类"同额，
    # 净影响为 0；只把重分类减掉会凭空多出一笔余额。
    closing = opening + cash_effective - cash_reclassified

    designation_rows = []
    for index, row in enumerate(designated, start=1):
        item = net_rows.get((row.get("period"), row.get("currency"))) or {}
        designation_rows.append([
            str(index),
            escape(TYPE_NAMES.get(row.get("hedge_type"), row.get("hedge_type") or "-")),
            escape(f"{row.get('currency')}"),
            _num(row.get("notional")),
            _num(row.get("allocated")),
            escape(f"{row.get('period')} {row.get('currency')}"),
            _num(item.get("business_exposure")),
            escape(str(row.get("trade_date") or "-")),
            escape(str(row.get("due_date") or "-")),
            f"{row.get('locked_rate')}",
            "已结算" if row.get("settled") else "未结清",
        ])

    effectiveness_rows = []
    for index, row in enumerate(designated, start=1):
        effectiveness_rows.append([
            str(index),
            escape(f"{row.get('period')} {row.get('currency')}"),
            _num(row.get("mtm_cny")),
            _num(row.get("delta_fv_cny")),
            _num(row.get("delta_item_cny")),
            _pct(row.get("effectiveness")),
            _num(row.get("effective_cny")),
            _num(row.get("ineffective_cny")),
            escape(_conclusion(row.get("effectiveness"), row.get("ineffective_cny"))),
        ])

    pl_rows = [
        ["无效部分（计入当期损益）", _num(totals.get("ineffective_cny"))],
        ["现金流量套期重分类", _num(totals.get("reclassified_cny"))],
        ["公允价值套期有效部分（与被套项目对冲）",
         _num(float(totals.get("pl_cny") or 0) - float(totals.get("ineffective_cny") or 0)
              - float(totals.get("reclassified_cny") or 0))],
        ["损益影响合计", _num(totals.get("pl_cny"))],
    ]

    warnings = list(accounting.get("warnings") or [])[:20]
    warning_html = "".join(f"<li>{escape(str(item))}</li>" for item in warnings) or "<li>无</li>"
    limitations = [
        "本文档为管理会计底稿，不构成审计意见，也不替代总账凭证。",
        f"会计准则口径：{STANDARD_NAMES.get(standard, standard)}；80%~125% 仅为旧 IAS 39 参考带，"
        "CAS 24（2017）与 IFRS 9 不设硬阈值，超出时需补充定性说明。",
        "被套项目变动：现金流量套期用同期远期（假设衍生工具）计量；公允价值/净投资套期用即期重估。",
        "缺交易日即期的套期关系按起息日平价（MtM=0）近似，明细中逐笔标注。",
        "未包含：税、减值、信用估值调整（CVA）、科目表映射与审批流程。",
        f"汇率来源：{rates.get('source') or '-'}；状态：{rates.get('status') or '-'}；"
        f"报价时点：{rates.get('fetched_at') or '-'}。",
    ]
    limitation_html = "".join(f"<li>{escape(item)}</li>" for item in limitations)

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<title>套期关系与有效性测试文档 · {escape(entity)} · {asof.isoformat()}</title>
<style>
  :root {{ color-scheme: light; }}
  body {{ font-family: "Microsoft YaHei", "PingFang SC", Arial, sans-serif; color: #1f1e1d;
         margin: 32px auto; max-width: 1080px; line-height: 1.55; font-size: 13px; }}
  h1 {{ font-size: 22px; margin: 0 0 6px; }}
  h2 {{ font-size: 15px; margin: 26px 0 8px; padding-bottom: 4px; border-bottom: 1px solid #d5d0c4; }}
  .sub {{ color: #6f6d66; margin-bottom: 18px; }}
  table {{ width: 100%; border-collapse: collapse; margin: 6px 0 10px; }}
  th, td {{ border: 1px solid #d5d0c4; padding: 6px 8px; text-align: left; vertical-align: top; }}
  th {{ background: #f5f3ee; font-weight: 600; white-space: nowrap; }}
  td.num, th.num {{ text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }}
  .meta td {{ border: none; padding: 3px 10px 3px 0; }}
  .meta td:first-child {{ color: #6f6d66; width: 120px; }}
  .note {{ color: #6f6d66; font-size: 12px; }}
  ul {{ margin: 4px 0 0 18px; padding: 0; }}
  li {{ margin: 3px 0; }}
  .empty {{ color: #6f6d66; text-align: center; }}
  .sign td {{ height: 46px; }}
  @media print {{
    body {{ margin: 0; max-width: none; font-size: 12px; }}
    h2 {{ page-break-after: avoid; }}
    table {{ page-break-inside: auto; }}
    tr {{ page-break-inside: avoid; }}
  }}
</style>
</head>
<body>
<h1>套期关系与有效性测试文档</h1>
<div class="sub">{escape(entity)} · 报告日 {asof.isoformat()} · 生成时间 {escape(generated.replace("T", " ").replace("Z", ""))}</div>

<table class="meta">
  <tr><td>主体</td><td>{escape(entity)}</td><td>报告日</td><td>{asof.isoformat()}</td></tr>
  <tr><td>会计准则</td><td>{escape(STANDARD_NAMES.get(standard, standard))}</td>
      <td>编制人</td><td>{escape(prepared_by)}</td></tr>
  <tr><td>套期关系数</td><td>{len(designated)} 笔（已指定）</td>
      <td>未结清</td><td>{accounting.get("open_count", 0)} 笔</td></tr>
</table>

<h2>一、套期关系指定</h2>
{_table(
    ["#", "套期类型", "币种", "名义金额", "指定金额", "被套项目", "被套敞口", "交易日", "到期日", "锁定价", "状态"],
    designation_rows,
    ["num", "", "", "num", "num", "", "num", "", "", "num", ""],
)}
<p class="note">指定关系按「期间 × 币种」归集；同一桶内多笔远期按交易日先进先出分配敞口，超出部分不参与有效性测试。</p>

<h2>二、有效性测试</h2>
{_table(
    ["#", "套期关系", "远期 MtM", "套保工具变动", "被套项目变动", "有效性比率", "有效部分", "无效部分", "结论"],
    effectiveness_rows,
    ["num", "", "num", "num", "num", "num", "num", "num", ""],
)}
<p class="note">有效性比率 = −套保工具公允价值变动 ÷ 被套项目公允价值变动。现金流量套期用同期远期计量被套项目，公允价值/净投资套期用即期。</p>

<h2>三、现金流量套期 OCI 滚动</h2>
{_table(
    ["项目", "金额（CNY）"],
    [
        ["期初余额（配置录入）", _num(opening)],
        ["加：本期确认的有效部分", _num(cash_effective)],
        ["减：本期重分类进损益", _num(cash_reclassified)],
        ["期末余额", _num(closing)],
    ],
    ["", "num"],
)}
<p class="note">期初余额来自配置 `oci_opening_cny`；工具不跨期结转，正式报表请与总账核对。境外经营净投资套期有效部分
{_num(sum(float(row.get("oci_cny") or 0) for row in net_investment_rows))} CNY，处置前不结转。</p>

<h2>四、无效部分与损益影响</h2>
{_table(["项目", "金额（CNY）"], pl_rows, ["", "num"])}

<h2>五、假设、限制与警示</h2>
<ul>{limitation_html}</ul>
<p class="note">逐笔警示：</p>
<ul>{warning_html}</ul>

<h2>六、签署</h2>
<table class="sign">
  <tr><td style="width:120px">编制人</td><td>{escape(prepared_by)}</td><td style="width:120px">日期</td><td></td></tr>
  <tr><td>复核人</td><td></td><td>日期</td><td></td></tr>
  <tr><td>审批人</td><td></td><td>日期</td><td></td></tr>
</table>
<p class="note">本文件由 FX Hedge Lab 生成，数据截至 {asof.isoformat()}。打印或另存为 PDF 后归档。</p>
</body>
</html>"""
