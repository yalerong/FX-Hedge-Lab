from __future__ import annotations

import argparse
import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import pstdev
from typing import Any


BASE_CURRENCY = "CNY"


@dataclass(frozen=True)
class Exposure:
    period: str
    currency: str
    amount: float
    purpose: str
    risk_type: str
    source: str
    source_id: str


def signed_amount(direction: str, amount: float) -> float:
    if direction in {"asset", "receipt", "export", "inflow", "buy_foreign"}:
        return amount
    if direction in {"liability", "payment", "import", "outflow", "sell_foreign"}:
        return -amount
    raise ValueError(f"unknown direction: {direction}")


def rate_key(currency: str) -> str:
    if currency == BASE_CURRENCY:
        return BASE_CURRENCY
    return f"{currency}/{BASE_CURRENCY}"


def load_case(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def identify_and_measure(data: dict[str, Any]) -> list[Exposure]:
    exposures: list[Exposure] = []

    for row in data.get("historical_exposures", []):
        exposures.append(
            Exposure(
                period=row["period"],
                currency=row["currency"],
                amount=float(row["amount"]),
                purpose=row.get("purpose", "balance_sheet"),
                risk_type=row.get("risk_type", "translation"),
                source="historical_exposure",
                source_id=row["id"],
            )
        )

    for row in data.get("opening_exposures", []):
        exposures.append(
            Exposure(
                period=row["period"],
                currency=row["currency"],
                amount=float(row["amount"]),
                purpose=row.get("purpose", "balance_sheet"),
                risk_type=row.get("risk_type", "translation"),
                source="opening_exposure",
                source_id=row["id"],
            )
        )

    changes: dict[tuple[str, str, str], float] = defaultdict(float)
    for row in data.get("forecast_changes", []):
        key = (row["period"], row["currency"], row.get("purpose", "balance_sheet"))
        changes[key] += signed_amount(row["direction"], float(row["amount"]))

    for key, amount in changes.items():
        period, currency, purpose = key
        exposures.append(
            Exposure(
                period=period,
                currency=currency,
                amount=amount,
                purpose=purpose,
                risk_type="transaction" if purpose != "balance_sheet" else "translation",
                source="forecast_change",
                source_id=f"{period}:{currency}:{purpose}",
            )
        )

    for row in data.get("balance_sheet_items", []):
        exposures.append(
            Exposure(
                period=row["period"],
                currency=row["currency"],
                amount=signed_amount(row["side"], float(row["amount"])),
                purpose="balance_sheet",
                risk_type="translation",
                source="balance_sheet",
                source_id=row["id"],
            )
        )

    for row in data.get("business_docs", []):
        probability = float(row.get("probability", 1.0))
        bad_debt_rate = float(row.get("bad_debt_rate", 0.0))
        expected_amount = float(row["amount"]) * probability * (1.0 - bad_debt_rate)
        exposures.append(
            Exposure(
                period=row["settlement_period"],
                currency=row["currency"],
                amount=signed_amount(row["direction"], expected_amount),
                purpose=row.get("purpose", "order_contract"),
                risk_type="transaction",
                source=row.get("doc_type", "business_doc"),
                source_id=row["id"],
            )
        )

    for row in data.get("industry_factors", []):
        score = float(row["value"]) * float(row.get("weight", 1.0))
        exposures.append(
            Exposure(
                period=row["period"],
                currency=row.get("currency", BASE_CURRENCY),
                amount=score,
                purpose="economic_indicator_score",
                risk_type="economic",
                source="industry_factor",
                source_id=row["id"],
            )
        )

    return exposures


def trade_exposures(data: dict[str, Any]) -> list[Exposure]:
    exposures: list[Exposure] = []
    for row in data.get("executed_trades", []):
        period = row["delivery_period"]
        if row["sell_currency"] != BASE_CURRENCY:
            exposures.append(
                Exposure(
                    period=period,
                    currency=row["sell_currency"],
                    amount=-float(row["sell_amount"]),
                    purpose=row.get("purpose", "hedge_trade"),
                    risk_type="hedge",
                    source="executed_trade",
                    source_id=row["id"],
                )
            )
        if row["buy_currency"] != BASE_CURRENCY:
            exposures.append(
                Exposure(
                    period=period,
                    currency=row["buy_currency"],
                    amount=float(row["buy_amount"]),
                    purpose=row.get("purpose", "hedge_trade"),
                    risk_type="hedge",
                    source="executed_trade",
                    source_id=row["id"],
                )
            )
    return exposures


def aggregate_exposures(
    exposures: list[Exposure],
    include_hedges: bool = False,
    materiality: float = 0.0,
) -> dict[tuple[str, str], float]:
    totals: dict[tuple[str, str], float] = defaultdict(float)
    for exposure in exposures:
        if exposure.risk_type == "economic":
            continue
        if exposure.risk_type == "hedge" and not include_hedges:
            continue
        totals[(exposure.period, exposure.currency)] += exposure.amount
    return {key: value for key, value in totals.items() if abs(value) >= materiality}


def pnl_by_scenario(
    totals: dict[tuple[str, str], float],
    data: dict[str, Any],
    scenario_name: str,
) -> dict[tuple[str, str], float]:
    base_rates = data["rates"]["base"]
    scenario_rates = data["rates"]["scenarios"][scenario_name]
    pnl: dict[tuple[str, str], float] = {}
    for key, amount in totals.items():
        _, currency = key
        if currency == BASE_CURRENCY:
            pnl[key] = 0.0
            continue
        pair = rate_key(currency)
        pnl[key] = amount * (float(scenario_rates[pair]) - float(base_rates[pair]))
    return pnl


def forward_rate(data: dict[str, Any], period: str, currency: str) -> float:
    pair = rate_key(currency)
    forwards = data.get("forward_rates", {})
    return float(forwards.get(period, {}).get(pair, data["rates"]["base"][pair]))


def simulate_hedge_strategy(
    unhedged_totals: dict[tuple[str, str], float],
    data: dict[str, Any],
    hedge_ratios: list[float] | None = None,
) -> list[dict[str, Any]]:
    hedge_ratios = hedge_ratios or [0.0, 0.25, 0.5, 0.75, 1.0]
    scenario_names = list(data["rates"]["scenarios"])
    rows: list[dict[str, Any]] = []

    for ratio in hedge_ratios:
        scenario_pnl: dict[str, float] = {}
        for scenario_name in scenario_names:
            total_pnl = 0.0
            scenario_rates = data["rates"]["scenarios"][scenario_name]
            for (period, currency), exposure_amount in unhedged_totals.items():
                if currency == BASE_CURRENCY:
                    continue
                pair = rate_key(currency)
                spot_at_settlement = float(scenario_rates[pair])
                base_rate = float(data["rates"]["base"][pair])
                fwd = forward_rate(data, period, currency)
                hedge_position = -exposure_amount * ratio
                exposure_pnl = exposure_amount * (spot_at_settlement - base_rate)
                hedge_pnl = hedge_position * (spot_at_settlement - fwd)
                total_pnl += exposure_pnl + hedge_pnl
            scenario_pnl[scenario_name] = round(total_pnl, 2)
        rows.append(
            {
                "hedge_ratio": ratio,
                "scenario_pnl_cny": scenario_pnl,
                "worst_case_cny": min(scenario_pnl.values()) if scenario_pnl else 0.0,
                "pnl_stddev_cny": round(pstdev(scenario_pnl.values()), 2) if len(scenario_pnl) > 1 else 0.0,
            }
        )

    return sorted(rows, key=lambda row: (-row["worst_case_cny"], row["pnl_stddev_cny"]))


def validate_case(data: dict[str, Any], all_totals: dict[tuple[str, str], float]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    threshold = float(data.get("validation", {}).get("pnl_tolerance_cny", 1.0))
    base_rates = data["rates"]["base"]
    actual_rates = data.get("rates", {}).get("actual", {})

    for (period, currency), amount in all_totals.items():
        if currency == BASE_CURRENCY:
            continue
        pair = rate_key(currency)
        if pair not in base_rates:
            issues.append({"level": "error", "message": f"missing base rate for {pair}"})
        if pair not in actual_rates:
            issues.append({"level": "warning", "message": f"missing actual rate for {pair}"})

    for trade in data.get("executed_trades", []):
        if trade["sell_currency"] == BASE_CURRENCY or trade["buy_currency"] == BASE_CURRENCY:
            foreign_amount = float(trade["sell_amount"] if trade["sell_currency"] != BASE_CURRENCY else trade["buy_amount"])
            cny_amount = float(trade["buy_amount"] if trade["buy_currency"] == BASE_CURRENCY else trade["sell_amount"])
            implied_rate = cny_amount / foreign_amount
            stated_rate = float(trade["rate"])
            if abs(implied_rate - stated_rate) > 0.0001:
                issues.append(
                    {
                        "level": "error",
                        "message": f"trade {trade['id']} rate mismatch",
                        "implied_rate": round(implied_rate, 6),
                        "stated_rate": stated_rate,
                    }
                )

    reported = {
        (row["period"], row["currency"]): float(row["reported_pnl_cny"])
        for row in data.get("accounting_checks", [])
    }
    for key, reported_pnl in reported.items():
        period, currency = key
        pair = rate_key(currency)
        if pair not in actual_rates or pair not in base_rates:
            continue
        expected_pnl = all_totals.get(key, 0.0) * (float(actual_rates[pair]) - float(base_rates[pair]))
        diff = expected_pnl - reported_pnl
        if abs(diff) > threshold:
            issues.append(
                {
                    "level": "error",
                    "message": f"reported pnl differs for {period} {currency}",
                    "expected_pnl_cny": round(expected_pnl, 2),
                    "reported_pnl_cny": round(reported_pnl, 2),
                    "difference_cny": round(diff, 2),
                }
            )

    return issues


def build_report(data: dict[str, Any]) -> dict[str, Any]:
    raw_exposures = identify_and_measure(data)
    hedge_offsets = trade_exposures(data)
    unhedged_totals = aggregate_exposures(raw_exposures, materiality=float(data.get("materiality_foreign", 0.0)))
    hedged_totals = aggregate_exposures(raw_exposures + hedge_offsets, include_hedges=True)

    scenario_reports = {
        name: {f"{period}:{currency}": round(value, 2) for (period, currency), value in pnl_by_scenario(unhedged_totals, data, name).items()}
        for name in data["rates"]["scenarios"]
    }
    strategy_rank = simulate_hedge_strategy(unhedged_totals, data)
    issues = validate_case(data, hedged_totals)

    return {
        "logic": [
            "collect source data",
            "identify risk type from data_type/source",
            "measure exposure by model",
            "aggregate by period and currency",
            "simulate pnl under rate scenarios",
            "rank hedge ratios by worst-case pnl and volatility",
            "validate trades, rates, and accounting pnl",
        ],
        "unhedged_exposure": {f"{period}:{currency}": round(value, 2) for (period, currency), value in unhedged_totals.items()},
        "exposure_after_executed_trades": {f"{period}:{currency}": round(value, 2) for (period, currency), value in hedged_totals.items()},
        "unhedged_scenario_pnl_cny": scenario_reports,
        "recommended_strategy": strategy_rank[0] if strategy_rank else None,
        "strategy_rank": strategy_rank,
        "validation_issues": issues,
    }


def explain_report(report: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("小白版理解")
    lines.append("")
    lines.append("这个模型只回答四个问题：")
    lines.append("1. 公司未来会多收还是多付外币？")
    lines.append("2. 如果汇率涨跌，这些外币会让公司多赚还是多亏人民币？")
    lines.append("3. 已经做过的远期/结汇交易能抵掉多少风险？")
    lines.append("4. 哪个套保比例能让最坏情况不那么难看？")
    lines.append("")

    lines.append("第一步：找敞口，也就是还没有锁死汇率的外币金额。")
    for key, amount in report.get("unhedged_exposure", {}).items():
        period, currency = key.split(":", 1)
        direction = "未来净收款/资产更多" if amount > 0 else "未来净付款/负债更多"
        lines.append(f"- {period} {currency}: {amount:,.2f}，意思是{direction}。")
    lines.append("")

    lines.append("第二步：看汇率变化会带来什么影响。")
    lines.append("公式很简单：汇兑影响 = 外币敞口 x (未来汇率 - 当前基准汇率)。")
    for scenario, rows in report.get("unhedged_scenario_pnl_cny", {}).items():
        total = sum(float(value) for value in rows.values())
        lines.append(f"- 情景 {scenario}: 未套保合计影响约 {total:,.2f} CNY。")
    lines.append("")

    lines.append("第三步：把已执行交易算进去。")
    lines.append("如果公司已经买了远期、做了结汇或购汇，就相当于提前锁定一部分外币，剩下的才是真正还暴露在汇率波动里的部分。")
    for key, amount in report.get("exposure_after_executed_trades", {}).items():
        period, currency = key.split(":", 1)
        lines.append(f"- {period} {currency}: 执行交易后剩余 {amount:,.2f}。")
    lines.append("")

    recommended = report.get("recommended_strategy") or {}
    lines.append("第四步：比较套保比例。")
    if recommended:
        ratio = float(recommended.get("hedge_ratio", 0.0))
        lines.append(
            f"当前样例推荐套保 {ratio:.0%}，因为它的最坏情景损益是 "
            f"{float(recommended.get('worst_case_cny', 0.0)):,.2f} CNY，"
            f"情景波动是 {float(recommended.get('pnl_stddev_cny', 0.0)):,.2f} CNY。"
        )
    else:
        lines.append("当前没有可比较的套保策略。")
    lines.append("")

    lines.append("第五步：自动校验。")
    issues = report.get("validation_issues", [])
    if not issues:
        lines.append("没有发现交易汇率、行情或财务损益口径上的明显问题。")
    else:
        lines.append(f"发现 {len(issues)} 个问题，优先看 error：")
        for issue in issues:
            lines.append(f"- {issue.get('level', '')}: {issue.get('message', '')}")
    lines.append("")

    lines.append("对应酷滴页面的五块产品逻辑：")
    lines.append("- 风险识别：找出外币敞口在哪里。")
    lines.append("- 风险测量：把汇率变化换算成人民币损益。")
    lines.append("- 风险管理：比较不同套保比例和最坏情景。")
    lines.append("- 交易管理：把已经执行的交易纳入剩余敞口。")
    lines.append("- 风险回溯：用实际汇率和财务损益检查差异。")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="FX risk, hedge, and validation simulator.")
    parser.add_argument("case_file", type=Path)
    parser.add_argument("--pretty", action="store_true")
    parser.add_argument("--explain", action="store_true", help="print a beginner-friendly explanation instead of JSON")
    args = parser.parse_args()

    report = build_report(load_case(args.case_file))
    if args.explain:
        print(explain_report(report))
        return

    indent = 2 if args.pretty else None
    print(json.dumps(report, ensure_ascii=False, indent=indent))


if __name__ == "__main__":
    main()
