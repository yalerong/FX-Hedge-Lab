"""方案快照。

问题：配置是可变的。改一次目标套保比例，历史建议全部跟着重算，
于是回答不了"**当初**是按什么参数给的这条建议"。回测、归因、复盘
全都建立在一个会变的地基上。

做法：把某一时刻的建议连同当时的配置、汇率、预测信号一起冻成一份
**只读方案**。之后所有回溯都引用方案，而不是引用当前配置。

同样重要的是反向能力：拿当前配置和最近一次方案对比，**告诉用户参数
已经漂了哪几项**。不然快照只是存档，不会影响任何决策。
"""
from __future__ import annotations

# 会影响建议金额的配置项。只比这些——改个汇率接口地址不算参数漂移。
DECISION_KEYS = (
    "default_hedge_ratio",
    "month_currency_hedge_ratios",
    "interest_rates",
    "forward_overrides",
)
# `strategy_type` 和 `enterprise_type` 都曾经在上面这张表里。
# 前者全仓没有任何计算读它；后者自从操作方向改由净敞口决定之后，
# 只剩下"标出反常方向"这一个提示作用，不再影响任何金额。
# 留在表里的话，把出口型改成进口型会把一份数字和操作完全相同的方案
# 判成过期，与这张表"只含影响建议金额的字段"的定义自相矛盾。

# 影响情景损益、但不影响建议金额的项，单独归一类
SCENARIO_KEYS = (
    "optimistic_shift_pct",
    "pessimistic_shift_pct",
    "custom_scenario_shift_pct",
    "scenario_shifts",
)


def freeze(dashboard: dict, label: str | None, now_iso: str) -> dict:
    """把当前建议冻成一份方案。

    只留决策相关的字段：方案是用来回答"当初怎么算的"，不是数据库备份。
    """
    rows = []
    for item in dashboard.get("suggestions", []):
        rows.append(
            {
                "period": item["period"],
                "currency": item["currency"],
                "action": item["action"],
                "business_exposure": item["business_exposure"],
                "covered_exposure": item["covered_exposure"],
                "net_exposure": item["net_exposure"],
                "target_hedge_ratio": item["target_hedge_ratio"],
                "forecast_multiplier": item["forecast_multiplier"],
                "effective_hedge_ratio": item["effective_hedge_ratio"],
                "forecast_reason": item.get("forecast_reason"),
                "recommended_amount": item["recommended_amount"],
                "spot_rate": item.get("spot_rate"),
                "trade_rate": item.get("trade_rate"),
                "forward_basis": item.get("forward_basis"),
                "accounting_bucket": item.get("accounting_bucket"),
                # 折扣是从这个信号算出来的。只存折扣不存信号的话，
                # 事后既说不清当初模型是什么状态，也没法判断信号变没变。
                "forecast_signal": _signal_fingerprint(item.get("forecast_signal")),
            }
        )

    config = dashboard.get("config", {})
    rates = dashboard.get("rates", {})
    return {
        "created_at": now_iso,
        "label": label or f"{now_iso[:10]} 方案",
        "config": {key: config.get(key) for key in DECISION_KEYS + SCENARIO_KEYS},
        "rate_snapshot": {
            "source": rates.get("source"),
            "status": rates.get("status"),
            "fetched_at": rates.get("fetched_at"),
            "pair_rates": dict(rates.get("pair_rates") or {}),
        },
        "rows": rows,
        "total_recommended": {
            row["currency"]: round(
                sum(r["recommended_amount"] for r in rows if r["currency"] == row["currency"]), 2
            )
            for row in rows
        },
    }


# 信号里只有这几项会改变折扣，存指纹而不是整个对象——
# forecast 数组每月都变，拿整段去比会天天报"漂移"。
SIGNAL_KEYS = ("tier", "direction", "mape", "n_test", "direction_accuracy")
RECOMMENDATION_KEYS = (
    "action",
    "forecast_multiplier",
    "effective_hedge_ratio",
    "recommended_amount",
)


def _signal_fingerprint(signal: dict | None) -> dict | None:
    if not signal:
        return None
    fingerprint = {key: signal.get(key) for key in SIGNAL_KEYS}
    # 预测区间也得进来：自从加了"覆盖不到该期间就不给折扣"这道闸门，
    # 同样的档位和方向，只要区间挪了，折扣就可能从 0.5 跳回 1.0。
    # 只存首尾两个月，不存整段——整段每次重跑都会变。
    months = [row.get("month") for row in (signal.get("forecast") or []) if row.get("month")]
    fingerprint["horizon"] = {"from": min(months), "to": max(months)} if months else None
    return fingerprint


def _diff_value(before, after) -> dict | None:
    if before == after:
        return None
    return {"from": before, "to": after}


def _recommendation_key(row: dict) -> tuple | None:
    period = row.get("period")
    currency = row.get("currency")
    if period is None or currency is None:
        return None
    return period, currency


def _recommendation_label(key: tuple) -> str:
    return f"{key[0]}|{key[1]}"


def _recommendation_changes(frozen_rows: list[dict], current_suggestions: list[dict] | None) -> dict:
    if current_suggestions is None:
        return {}

    frozen = {
        key: row
        for row in frozen_rows
        if (key := _recommendation_key(row)) is not None
    }
    current = {
        key: row
        for row in current_suggestions
        if (key := _recommendation_key(row)) is not None
    }

    changed = {}
    for key in sorted(set(frozen) | set(current)):
        before = frozen.get(key)
        after = current.get(key)
        fields = {}
        if before is None:
            fields["status"] = {"from": "missing", "to": "present"}
        elif after is None:
            fields["status"] = {"from": "present", "to": "missing"}
        for field in RECOMMENDATION_KEYS:
            change = _diff_value(
                before.get(field) if before else None,
                after.get(field) if after else None,
            )
            if change:
                fields[field] = change
        if fields:
            changed[_recommendation_label(key)] = fields
    return changed


def drift(
    plan: dict | None,
    config: dict,
    pair_rates: dict,
    forecast_signals: dict | None = None,
    current_suggestions: list[dict] | None = None,
) -> dict:
    """当前配置相对最近一份方案漂了什么。

    分三类报：影响建议金额的、只影响情景的、汇率变动。
    第一类有变化就意味着**页面上的建议和方案里的建议已经不是一回事**。
    """
    if not plan:
        return {"has_plan": False}

    frozen = plan.get("config") or {}
    decision = {}
    scenario = {}
    for key in DECISION_KEYS:
        change = _diff_value(frozen.get(key), config.get(key))
        if change:
            decision[key] = change
    for key in SCENARIO_KEYS:
        change = _diff_value(frozen.get(key), config.get(key))
        if change:
            scenario[key] = change

    # 预测信号变了，折扣就会变，建议金额跟着变——即使配置一个字没动。
    recommendation_changed = _recommendation_changes(
        plan.get("rows", []),
        current_suggestions,
    )

    signal_changed = {}
    signals = forecast_signals or {}
    for row in plan.get("rows", []):
        currency = row.get("currency")
        if currency in signal_changed:
            continue
        if "forecast_signal" not in row:
            # 加这个字段之前冻的老方案没有它。拿 None 去比会永远报
            # "预测信号已变 空 → {...}"，而且除了重新冻一份没别的办法消掉。
            continue
        before = row.get("forecast_signal")
        after = _signal_fingerprint(signals.get(currency))
        # 指纹的字段是会加的（比如后来加进来的 horizon）。老快照里没有的键
        # 不参与比较，否则加一个字段就把所有历史方案判成过期。
        if isinstance(before, dict) and isinstance(after, dict):
            after = {key: after.get(key) for key in before}
        change = _diff_value(before, after)
        if change:
            signal_changed[currency] = change

    frozen_rates = (plan.get("rate_snapshot") or {}).get("pair_rates") or {}
    rate_moves = {}
    for currency, old in frozen_rates.items():
        new = pair_rates.get(currency)
        if new is None or not old:
            continue
        move = float(new) / float(old) - 1
        if abs(move) >= 0.001:  # 千分之一以内当没动
            rate_moves[currency] = {"from": float(old), "to": float(new), "move": round(move, 6)}

    return {
        "has_plan": True,
        "label": plan.get("label"),
        "created_at": plan.get("created_at"),
        "decision_changed": decision,
        "scenario_changed": scenario,
        "recommendation_changed": recommendation_changed,
        "signal_changed": signal_changed,
        "rate_moved": rate_moves,
        "stale": bool(decision) or bool(signal_changed) or bool(recommendation_changed),
    }
