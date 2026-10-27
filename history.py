"""历史波动率与历史模拟 VaR。

参数法 VaR 假设正态、相关性来自配置；历史模拟不假设分布，直接用过去
N 个交易日的联合变动重估整个头寸。两者并列，差异本身就是信息。

数据来源（按优先级）：

1. `data/rates_history/{CCY}CNY_daily.csv`——`python -m forecast.pipeline`
   拉下来的日线，覆盖工作区里出现过的币种；
2. `data/market/` 下的 FRED/CFETS 序列：USD 用中间价（CNY_FIXING）或
   DEXCHUS，JPY 由 `DEXJPUS ÷ USD/CNY` 推出来；
3. 都没有 → 这个币种不参与历史模拟，页面写明缺哪一段历史。

口径与限制：

- 收益率用对数日收益，年化波动率 = 估计窗口内日收益的标准差 × √252。
- 历史模拟按**重叠的 h 日窗口**取损益：样本更充分，但相邻窗口相关，
  有效样本小于名义观测数；页面的 observations 是名义值。
- 组合损益 = Σ w_i × (e^{R_i,h} − 1)，w 是净敞口折人民币。跨期限直接相加，
  和 1 天参数法一致。
- 窗口默认 500 个交易日，可在配置里改。样本不足 250 个交易日时不给结果，
  而不是用几十个点算一个看起来很准的数。
- 历史模拟只覆盖有历史的币种；只要有一笔净敞口没有历史，整块标记不可用。
  混合"部分历史 + 部分参数"会更难解释，不如直接说不可用。
"""
from __future__ import annotations

import csv
import math
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MARKET_DIR = ROOT / "data" / "market"
HISTORY_DIR = ROOT / "data" / "rates_history"

TRADING_DAYS = 252
MIN_OBSERVATIONS = 250

# 币种 -> 本地序列（CNY 每 1 外币）。JPY 是推导出来的，单独处理。
MARKET_SERIES = {
    "USD": ("CNY_FIXING.csv", "DEXCHUS.csv", "USDCNH.csv"),
}

_CACHE: dict[str, tuple[object, list[tuple[str, float]]]] = {}


def _read_csv(path: Path) -> list[tuple[str, float]]:
    """读 date/value 或 ds/y 两种表头。按 (大小, mtime) 缓存。"""
    try:
        stat = path.stat()
        stamp = (stat.st_size, stat.st_mtime_ns)
    except OSError:
        return []
    cached = _CACHE.get(str(path))
    if cached is not None and cached[0] == stamp:
        return cached[1]
    rows: list[tuple[str, float]] = []
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            date_key = "date" if "date" in (reader.fieldnames or []) else "ds"
            value_key = "value" if "value" in (reader.fieldnames or []) else "y"
            for row in reader:
                day = (row.get(date_key) or "").strip()[:10]
                raw = (row.get(value_key) or "").strip()
                if len(day) != 10 or not raw:
                    continue
                try:
                    value = float(raw)
                except ValueError:
                    continue
                if value > 0 and math.isfinite(value):
                    rows.append((day, value))
    except (OSError, csv.Error):
        rows = []
    rows.sort(key=lambda item: item[0])
    _CACHE[str(path)] = (stamp, rows)
    return rows


def _history_file(currency: str) -> Path:
    return HISTORY_DIR / f"{currency}CNY_daily.csv"


def series_for(currency: str) -> list[tuple[str, float]]:
    """CNY 每 1 单位外币的日序列。没有就返回空列表。"""
    path = _history_file(currency)
    if path.exists():
        rows = _read_csv(path)
        if len(rows) >= MIN_OBSERVATIONS:
            return rows
    if currency == "USD":
        for name in MARKET_SERIES["USD"]:
            rows = _read_csv(MARKET_DIR / name)
            if len(rows) >= MIN_OBSERVATIONS:
                return rows
        return []
    if currency == "JPY":
        usd = series_for("USD")
        jpy_per_usd = _read_csv(MARKET_DIR / "DEXJPUS.csv")
        if not usd or not jpy_per_usd:
            return []
        usd_map = dict(usd)
        rows = []
        for day, jpy in jpy_per_usd:
            cny = usd_map.get(day)
            if cny:
                rows.append((day, cny / jpy))
        return rows if len(rows) >= MIN_OBSERVATIONS else []
    return []


def log_returns(rows: list[tuple[str, float]]) -> list[tuple[str, float]]:
    out = []
    for (prev_day, prev), (day, value) in zip(rows, rows[1:]):
        if prev > 0 and value > 0:
            out.append((day, math.log(value / prev)))
    return out


def returns_for(currency: str) -> dict[str, float]:
    """日期 -> 对数日收益。回测和历史模拟共用同一个口径。"""
    return dict(log_returns(series_for(currency)))


def annual_vol(returns: list[float]) -> float | None:
    if len(returns) < 20:
        return None
    mean = sum(returns) / len(returns)
    variance = sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
    return math.sqrt(max(0.0, variance)) * math.sqrt(TRADING_DAYS)


def ewma_vol(returns: list[float], decay: float = 0.94) -> float | None:
    """RiskMetrics 式 EWMA 年化波动率。

    等权样本波动率对 250 天前的冲击和昨天的冲击给同样的权重，波动率从
    3% 跳到 8% 之后要半年才反应得过来。EWMA 按 λ^(k) 衰减，λ=0.94 的
    半衰期约 11 个交易日，是银行风险系统里的默认值。
    """
    if len(returns) < 20:
        return None
    variance = sum(value * value for value in returns[:20]) / 20
    for value in returns[20:]:
        variance = decay * variance + (1 - decay) * value * value
    return math.sqrt(max(0.0, variance)) * math.sqrt(TRADING_DAYS)


def _quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    fraction = position - low
    return ordered[low] * (1 - fraction) + ordered[high] * fraction


def historical_sim(
    weights: dict[str, float],
    config: dict,
    confidence: float,
    horizon: int,
) -> dict:
    """按历史联合变动模拟组合损益。weights 是带符号的人民币敞口。"""
    currencies = sorted(currency for currency, value in weights.items() if abs(value) > 1e-9)
    if not currencies:
        return {"available": False, "reason": "没有净敞口", "missing": [], "vols": {}}

    series = {}
    missing = []
    for currency in currencies:
        rows = series_for(currency)
        if not rows:
            missing.append(currency)
        series[currency] = rows
    try:
        window = int(float(config.get("historical_window_days", 500) or 500))
    except (TypeError, ValueError):
        window = 500
    window = max(60, min(window, 2000))
    # 波动率一律按估计窗口算：全样本年化会把 2019 年的行情和上个月的行情
    # 平均成一个数，摆在世界模拟 VaR 旁边会误导，两者的样本根本不是一批。
    vols = {}
    for currency, rows in series.items():
        if not rows:
            continue
        vol = annual_vol([value for _, value in log_returns(rows)][-window:])
        if vol is not None:
            vols[currency] = round(vol, 4)
    if missing:
        return {
            "available": False,
            "reason": "缺 " + "、".join(missing) + " 的本地日线历史（可跑 forecast.pipeline 补齐）",
            "missing": missing,
            "vols": vols,
        }

    return_maps = {
        currency: dict(log_returns(rows)) for currency, rows in series.items()
    }
    common = sorted(set.intersection(*(set(mapping) for mapping in return_maps.values())))
    if len(common) < max(MIN_OBSERVATIONS, horizon + 20):
        return {
            "available": False,
            "reason": f"对齐后只有 {len(common)} 个共同交易日，不足 {MIN_OBSERVATIONS}",
            "missing": [],
            "vols": vols,
        }
    days = common[-window:]
    horizon = max(1, min(horizon, 60))
    if len(days) <= horizon:
        return {
            "available": False,
            "reason": "窗口长度不足以构造 h 日损益",
            "missing": [],
            "vols": vols,
        }

    # 多币种时各币种自己的尾巴覆盖的日期不完全一致，按对齐后的窗口再算一遍。
    vols = {}
    for currency in currencies:
        vol = annual_vol([return_maps[currency][day] for day in days])
        if vol is not None:
            vols[currency] = round(vol, 4)

    aligned = {currency: [return_maps[currency][day] for day in days] for currency in currencies}
    prefix = {}
    for currency in currencies:
        running = [0.0]
        for value in aligned[currency]:
            running.append(running[-1] + value)
        prefix[currency] = running

    pnls = []
    for index in range(len(days) - horizon + 1):
        pnl = 0.0
        for currency in currencies:
            cumulative = prefix[currency][index + horizon] - prefix[currency][index]
            pnl += weights[currency] * math.expm1(cumulative)
        pnls.append(pnl)

    losses = [-value for value in pnls]
    var = _quantile(losses, confidence)
    tail = [value for value in losses if value >= var]
    es = sum(tail) / len(tail) if tail else var
    notes = [
        f"历史模拟：{len(days)} 个交易日（{days[0]} ~ {days[-1]}），重叠 {horizon} 日窗口，"
        f"{len(pnls)} 个名义观测",
        "历史模拟只反映样本期出现过的情形，样本期没出现过的冲击它看不见",
    ]
    return {
        "available": True,
        "var_cny": round(max(0.0, var), 2),
        "es_cny": round(max(0.0, es), 2),
        "observations": len(pnls),
        "days": len(days),
        "horizon_days": horizon,
        "confidence": confidence,
        "start": days[0],
        "end": days[-1],
        "currencies": currencies,
        "vols": vols,
        "notes": notes,
    }
