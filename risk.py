"""组合层风险：期限梯度、相关性矩阵、参数法 VaR / ES。

为什么要单独一层：
- 驾驶舱回答"敞口多大"，回测回答"锁得怎么样"，但没人回答"**明天可能亏多少**"；
- 情景表只有 ±3% 的平行冲击，且默认相关性 1，对多币种组合会算错方向；
- 期限梯度是司库最常被问的一句："未来 30/60/90 天，各期限还敞着多少？"

口径（刻意保守、刻意写清楚）：

1. **参数法（方差-协方差）**，不是历史模拟，也不是蒙特卡洛。
   年化波动率 σ 和相关系数 ρ 都来自配置，默认值只是让工具开箱能跑，
   **用之前换成你自己的**。未配置时页面会逐条列出假设。
2. 组合波动率 σ_p = sqrt(Σ_i Σ_j w_i w_j ρ_ij σ_i σ_j)，w 是**净敞口折人民币**
   （收为正、付为负）。同一币种跨期限直接相加——1 天 VaR 里它们一起重估，
   不需要按期限拆；期限只影响流动性和展期，不影响 1 天 VaR。
3. VaR = z(置信度) × σ_p × sqrt(h/252)，ES = σ_p × sqrt(h/252) × φ(z)/(1-α)。
   252 个交易日是汇率市场的惯例，不是自然日。
4. 相关性缺省 **1.0**（和情景表同一个假设）。ρ=1 对同向敞口是保守的，
   对反向敞口（收美元 + 付欧元）会**高估**分散效果、把风险算小——所以
   页面必须显式提示"未配置相关性"，不能让它悄悄发生。
5. VaR 只覆盖汇率风险：不含利率、流动性、对手方和基差。它是一张风险底稿，
   不是监管资本计算。
"""
from __future__ import annotations

import math
from datetime import date
from statistics import NormalDist

TRADING_DAYS = 252

# 期限梯度：按到期月份距今天数分桶。标签只给前端展示用。
TENOR_BUCKETS = (
    ("overdue", "已逾期"),
    ("0-1m", "1 个月内"),
    ("1-3m", "1-3 个月"),
    ("3-6m", "3-6 个月"),
    ("6-12m", "6-12 个月"),
    (">12m", "12 个月以上"),
)
BUCKET_ORDER = {key: index for index, (key, _) in enumerate(TENOR_BUCKETS)}
BUCKET_LABELS = dict(TENOR_BUCKETS)

# 只让工具开箱能跑。默认值不是行情，必须换成自己的资金成本/波动率。
DEFAULT_VOL = {
    "USD": 0.05,
    "EUR": 0.07,
    "JPY": 0.08,
    "HKD": 0.01,
    "GBP": 0.08,
    "AUD": 0.10,
    "SGD": 0.05,
    "CHF": 0.07,
    "CAD": 0.07,
}
FALLBACK_VOL = 0.08


def period_index(period: str) -> int | None:
    """YYYY-MM -> 连续月序号（年*12+月），用于算桶和排序。"""
    if not isinstance(period, str) or len(period) < 7:
        return None
    try:
        return int(period[:4]) * 12 + int(period[5:7])
    except ValueError:
        return None


def tenor_bucket(period: str, today: date | None = None) -> str:
    """把一个期间映射到期限桶。缺日期/非法期间归到 0-1m 之外单列。"""
    today = today or date.today()
    index = period_index(period)
    if index is None:
        return "undated"
    months = index - (today.year * 12 + today.month)
    if months < 0:
        return "overdue"
    if months <= 1:
        return "0-1m"
    if months <= 3:
        return "1-3m"
    if months <= 6:
        return "3-6m"
    if months <= 12:
        return "6-12m"
    return ">12m"


def vol_for(config: dict, currency: str) -> tuple[float, bool]:
    """某币种的年化波动率，返回 (值, 是否配置过)。"""
    table = config.get("annual_vol") or {}
    value = table.get(currency)
    if value is None:
        return FALLBACK_VOL, False
    try:
        rate = float(value)
    except (TypeError, ValueError):
        return FALLBACK_VOL, False
    if not math.isfinite(rate) or rate <= 0:
        return FALLBACK_VOL, False
    return rate, True


def correlation(config: dict, left: str, right: str) -> tuple[float, bool]:
    """两币种相关系数，返回 (值, 是否配置过)。支持 A:B 和 B:A 两种写法。"""
    table = config.get("correlations") or {}
    for key in (f"{left}:{right}", f"{right}:{left}"):
        if key in table:
            try:
                value = float(table[key])
            except (TypeError, ValueError):
                continue
            if math.isfinite(value):
                return max(-1.0, min(1.0, value)), True
    return 1.0, False


def correlation_matrix(config: dict, currencies: list[str]) -> tuple[list[list[float]], int, list[list[bool]]]:
    """相关系数矩阵 + 未配置的格子数 + 哪些格子是配置过的。"""
    matrix = []
    configured = []
    missing = 0
    for index, left in enumerate(currencies):
        row = []
        flags = []
        for column, right in enumerate(currencies):
            if index == column:
                row.append(1.0)
                flags.append(True)
                continue
            value, is_configured = correlation(config, left, right)
            if not is_configured:
                missing += 1
            row.append(value)
            flags.append(is_configured)
        matrix.append(row)
        configured.append(flags)
    return matrix, missing // 2, configured


def build_ladder(net_rows: list[dict], today: date | None = None) -> dict:
    """按期限桶汇总：业务敞口 / 已锁 / 剩余 / 覆盖率。

    覆盖沿用驾驶舱口径：只算方向相反、不超过敞口本身的那部分。
    人民币金额按行汇率折算；缺汇率的行计不进金额，但计入行数。
    """
    today = today or date.today()
    buckets: dict[str, dict] = {}
    missing_rate: list[str] = []
    for row in net_rows:
        bucket = tenor_bucket(row.get("period", ""), today)
        entry = buckets.setdefault(
            bucket,
            {
                "bucket": bucket,
                "label": BUCKET_LABELS.get(bucket, "未定期间"),
                "gross_cny": 0.0,
                "locked_cny": 0.0,
                "net_cny": 0.0,
                "coverage": 0.0,
                "periods": 0,
                "currencies": [],
                "rate_missing": 0,
            },
        )
        entry["periods"] += 1
        currency = row.get("currency", "")
        if currency and currency not in entry["currencies"]:
            entry["currencies"].append(currency)
        if not row.get("rate_available") or row.get("current_rate") is None:
            entry["rate_missing"] += 1
            if currency and currency not in missing_rate:
                missing_rate.append(currency)
            continue
        rate = float(row["current_rate"])
        gross = float(row.get("business_exposure", 0) or 0)
        locked = float(row.get("locked_exposure", 0) or 0)
        net = float(row.get("net_exposure", 0) or 0)
        offset = min(abs(locked), abs(gross)) if gross * locked < 0 else 0.0
        entry["gross_cny"] += abs(gross) * rate
        entry["locked_cny"] += offset * rate
        entry["net_cny"] += abs(net) * rate

    rows = []
    for entry in buckets.values():
        if entry["gross_cny"]:
            entry["coverage"] = entry["locked_cny"] / entry["gross_cny"]
        rows.append(entry)
    rows.sort(key=lambda item: BUCKET_ORDER.get(item["bucket"], 99))
    return {
        "buckets": [{"key": key, "label": label} for key, label in TENOR_BUCKETS],
        "rows": rows,
        "rate_missing": missing_rate,
        "asof": today.isoformat(),
    }


def _weights(net_rows: list[dict], field: str) -> tuple[dict[str, float], list[str]]:
    """按币种汇总带符号的人民币敞口。缺汇率的行跳过并记录。"""
    weights: dict[str, float] = {}
    missing: list[str] = []
    for row in net_rows:
        rate = row.get("current_rate")
        if not row.get("rate_available") or rate is None:
            currency = row.get("currency", "")
            if currency and currency not in missing:
                missing.append(currency)
            continue
        currency = row.get("currency", "")
        amount = float(row.get(field, 0) or 0)
        weights[currency] = weights.get(currency, 0.0) + amount * float(rate)
    return {key: value for key, value in weights.items() if value}, missing


def _sigma(weights: dict[str, float], config: dict, matrix: list[list[float]],
           currencies: list[str]) -> tuple[float, dict[str, float], list[str]]:
    """组合波动率（年化，人民币）+ 各币种边际贡献 + 假设清单。"""
    vols = {}
    notes: list[str] = []
    for currency in currencies:
        value, configured = vol_for(config, currency)
        vols[currency] = value
        if not configured:
            notes.append(f"{currency} 未配置年化波动率，按默认 {value:.0%} 计算")
    variance = 0.0
    for i, left in enumerate(currencies):
        for j, right in enumerate(currencies):
            rho = matrix[i][j]
            variance += weights[left] * weights[right] * rho * vols[left] * vols[right]
    sigma = math.sqrt(max(0.0, variance))
    contributions: dict[str, float] = {}
    if sigma > 0:
        for i, left in enumerate(currencies):
            marginal = 0.0
            for j, right in enumerate(currencies):
                marginal += weights[right] * matrix[i][j] * vols[left] * vols[right]
            contributions[left] = weights[left] * marginal / sigma
    return sigma, contributions, notes


def build(net_rows: list[dict], config: dict, today: date | None = None) -> dict:
    """组合风险总览。返回 VaR/ES、期限梯度、相关性矩阵与假设清单。"""
    today = today or date.today()
    confidence = float(config.get("var_confidence", 0.99) or 0.99)
    horizon = int(float(config.get("var_horizon_days", 1) or 1))
    limit = float(config.get("var_limit_cny", 0) or 0)

    net_weights, missing = _weights(net_rows, "net_exposure")
    gross_weights, _ = _weights(net_rows, "business_exposure")
    currencies = sorted(net_weights)
    gross_currencies = sorted(gross_weights)
    all_currencies = sorted(set(currencies) | set(gross_currencies))
    matrix, unconfigured_pairs, configured = correlation_matrix(config, all_currencies)

    sigma_net, contributions, vol_notes = _sigma(net_weights, config, matrix, currencies) if currencies else (0.0, {}, [])
    sigma_gross = 0.0
    if gross_currencies:
        sigma_gross, _, _ = _sigma(gross_weights, config, matrix, gross_currencies)

    z_value = NormalDist().inv_cdf(min(max(confidence, 0.5), 0.9999))
    scale = math.sqrt(max(horizon, 0) / TRADING_DAYS)
    var_net = z_value * sigma_net * scale
    var_gross = z_value * sigma_gross * scale
    density = math.exp(-0.5 * z_value * z_value) / math.sqrt(2 * math.pi)
    es_net = sigma_net * scale * density / max(1e-9, 1 - confidence)

    gross_cny = sum(abs(value) for value in net_weights.values())
    positions = []
    for currency in currencies:
        contribution = contributions.get(currency, 0.0)
        positions.append(
            {
                "currency": currency,
                "net_cny": round(net_weights[currency], 2),
                "vol": round(vol_for(config, currency)[0], 4),
                "contribution_cny": round(contribution, 2),
                "contribution_pct": (contribution / sigma_net) if sigma_net else 0.0,
            }
        )
    positions.sort(key=lambda item: -abs(item["contribution_cny"]))

    notes = list(vol_notes)
    if unconfigured_pairs:
        notes.append(
            f"有 {unconfigured_pairs} 对币种未配置相关系数，按 1.0（完全同向）计算；"
            "反向敞口的分散效果会被高估，风险会被算小"
        )
    if missing:
        notes.append("、".join(missing) + " 缺汇率，未计入 VaR")
    overdue = [row for row in net_rows if tenor_bucket(row.get("period", ""), today) == "overdue"]
    if overdue:
        notes.append("已逾期但未结算的敞口仍计入 VaR；请到「到期与回测」登记实际结果")
    notes.append("VaR 只覆盖汇率风险；不含利率、流动性、对手方与基差风险")

    return {
        "asof": today.isoformat(),
        "confidence": confidence,
        "horizon_days": horizon,
        "z": round(z_value, 4),
        "var_cny": round(var_net, 2),
        "es_cny": round(es_net, 2),
        "var_gross_cny": round(var_gross, 2),
        "hedge_benefit_cny": round(var_gross - var_net, 2),
        "sigma_cny": round(sigma_net, 2),
        "sigma_pct": round(sigma_net / gross_cny, 6) if gross_cny else 0.0,
        "gross_cny": round(gross_cny, 2),
        "net_cny": round(sum(net_weights.values()), 2),
        "limit_cny": round(limit, 2),
        "limit_utilization": round(var_net / limit, 4) if limit > 0 else None,
        "over_limit": bool(limit > 0 and var_net > limit),
        "positions": positions,
        "correlations": {
            "currencies": all_currencies,
            "matrix": [[round(value, 4) for value in row] for row in matrix],
            "configured": configured,
            "configured_pairs": len(all_currencies) * (len(all_currencies) - 1) // 2 - unconfigured_pairs,
            "missing_pairs": unconfigured_pairs,
        },
        "ladder": build_ladder(net_rows, today),
        "notes": notes,
    }
