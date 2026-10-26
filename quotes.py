"""报价对象：来源、时点、买卖价。

为什么要有这一层：以前价格是裸的 float，来源和时点只存在于缓存文件里，
下游拿到的只是「一个数」。于是三件事做不到：

1. **可成交价**：卖出外币拿 bid、买入外币拿 ask。用中间价出的建议，
   到了银行柜台一定差半个价差。
2. **可追溯**：一笔建议用的是 API 即期、银行远期报价，还是 CIP 推算？
   过期了没有？必须跟着数字一起传下去。
3. **可替换**：接银行或 CFETS 时只换这一层，不用改敞口、VaR、会计。

价格优先级从高到低：

    quote_overrides（手工/银行双向报价）
    → forward_overrides（旧版单边报价，按中间价 + 配置价差）
    → CIP 推算 / 即期兜底（见 forwards.py）
    → 最后统一叠加 basis_pips（CNH/在岸基差、银行点差调整）

没有配价差时按 `default_spread_bp`（默认 10bp）双边各一半，并在每张报价上
标 `spread_source: "default"`。**工具不会假装中间价可以成交。**
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

PIP = 0.0001


@dataclass(frozen=True)
class Quote:
    pair: str
    instrument: str          # spot / forward
    tenor: str | None        # forward 的期间 YYYY-MM
    bid: float
    ask: float
    source: str              # api / manual / cip / spot
    asof: str | None
    basis_pips: float = 0.0
    spread_source: str = "default"

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread_bp(self) -> float:
        if not self.mid:
            return 0.0
        return (self.ask - self.bid) / self.mid * 10000

    def executable(self, action: str) -> float:
        """卖出外币（银行买入）用 bid；买入外币（银行卖出）用 ask。"""
        return self.bid if action == "sell_foreign" else self.ask

    def to_dict(self) -> dict:
        return {
            "pair": self.pair,
            "instrument": self.instrument,
            "tenor": self.tenor,
            "bid": round(self.bid, 6),
            "ask": round(self.ask, 6),
            "mid": round(self.mid, 6),
            "spread_bp": round(self.spread_bp, 2),
            "source": self.source,
            "asof": self.asof,
            "basis_pips": self.basis_pips,
            "spread_source": self.spread_source,
        }


def pair_name(currency: str, base: str = "CNY") -> str:
    return f"{currency}/{base}"


def spread_bp(config: dict, currency: str) -> tuple[float, str]:
    """该币种的买卖价差（bp）及来源。"""
    table = config.get("spreads_bp") or {}
    value = table.get(currency)
    if value is not None:
        try:
            return max(0.0, float(value)), "config"
        except (TypeError, ValueError):
            pass
    try:
        default = max(0.0, float(config.get("default_spread_bp", 10.0)))
    except (TypeError, ValueError):
        default = 10.0
    return default, "default"


def basis_pips(config: dict, currency: str) -> float:
    table = config.get("basis_pips") or {}
    try:
        return float(table.get(currency, 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _two_way(mid: float, spread_bp_value: float) -> tuple[float, float]:
    half = spread_bp_value / 2 / 10000
    return mid * (1 - half), mid * (1 + half)


def _from_override(mid: float, override: dict, spread_bp_value: float) -> tuple[float, float, str] | None:
    """手工/银行报价：支持 bid+ask，或 mid+spread_bp。"""
    if not isinstance(override, dict):
        return None
    try:
        bid = override.get("bid")
        ask = override.get("ask")
        if bid is not None and ask is not None:
            bid, ask = float(bid), float(ask)
            if bid > 0 and ask >= bid:
                return bid, ask, "override"
        center = override.get("mid")
        if center is not None:
            center = float(center)
            spread = override.get("spread_bp")
            spread = float(spread) if spread is not None else spread_bp_value
            if center > 0 and spread >= 0:
                low, high = _two_way(center, spread)
                return low, high, "override"
    except (TypeError, ValueError):
        return None
    return None


def _lookup_override(config: dict, currency: str, period: str | None) -> dict | None:
    table = config.get("quote_overrides") or {}
    if period:
        value = table.get(f"{period}:{currency}")
        if value is None:
            monthly = table.get(period)
            if isinstance(monthly, dict):
                value = monthly.get(currency)
        if value is not None:
            return value if isinstance(value, dict) else None
    return None


def spot_quote(
    config: dict, currency: str, mid: float, asof: str | None, source_hint: str | None = None
) -> Quote:
    """即期报价。basis_pips 先加，再套价差。"""
    basis = basis_pips(config, currency)
    center = float(mid) + basis * PIP
    spread, spread_source = spread_bp(config, currency)
    override = (config.get("spot_overrides") or {}).get(currency)
    resolved = _from_override(center, override, spread) if isinstance(override, dict) else None
    if resolved:
        bid, ask, kind = resolved
        source, spread_source = "manual", kind
    else:
        bid, ask = _two_way(center, spread)
        source = source_hint or "api"
    return Quote(pair_name(currency, config.get("base_currency", "CNY")),
                 "spot", None, bid, ask, source, asof, basis, spread_source)


def forward_quote(
    config: dict,
    currency: str,
    period: str,
    mid: float,
    asof: str | None,
    basis: str = "cip",
    source_hint: str | None = None,
) -> Quote:
    """远期报价。mid 已含远期点；这里只负责价差、基差与来源标注。"""
    basis_value = basis_pips(config, currency)
    center = float(mid) + basis_value * PIP
    spread, spread_source = spread_bp(config, currency)
    override = _lookup_override(config, currency, period)
    resolved = _from_override(center, override, spread) if override else None
    if resolved:
        bid, ask, kind = resolved
        source, spread_source = "manual", kind
    else:
        bid, ask = _two_way(center, spread)
        source = source_hint or basis
    return Quote(pair_name(currency, config.get("base_currency", "CNY")),
                 "forward", period, bid, ask, source, asof, basis_value, spread_source)


def age_hours(asof: str | None, now: datetime | None = None) -> float | None:
    if not asof:
        return None
    now = now or datetime.now(timezone.utc)
    try:
        stamp = datetime.fromisoformat(str(asof).replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return max(0.0, (now - stamp).total_seconds() / 3600)


def staleness(
    asof: str | None, config: dict, now: datetime | None = None
) -> tuple[bool, float | None, str | None]:
    """报价是否过期。返回 (stale, 年龄小时, 原因)。"""
    age = age_hours(asof, now)
    if age is None:
        return True, None, "报价缺少可解析的时点，按过期处理"
    try:
        limit = float(config.get("quote_max_age_hours", 24) or 0)
    except (TypeError, ValueError):
        limit = 24.0
    if limit > 0 and age > limit:
        return True, age, f"报价已 {age:.1f} 小时未更新（上限 {limit:g} 小时），请刷新后重试"
    return False, age, None


def build_book(
    config: dict,
    spot_rates: dict[str, float],
    forward_mids: dict[tuple[str, str], tuple[float, str]],
    asof: str | None,
    now: datetime | None = None,
    source_hint: str | None = None,
) -> dict:
    """整本报价：即期 + 已用到的远期，附带过期与价差来源。"""
    stale, age, reason = staleness(asof, config)
    spots = []
    for currency in sorted(spot_rates):
        try:
            quote = spot_quote(config, currency, spot_rates[currency], asof, source_hint)
        except (TypeError, ValueError):
            continue
        row = quote.to_dict()
        row.update({
            "currency": currency,
            "age_hours": None if age is None else round(age, 2),
            "stale": stale,
        })
        spots.append(row)
    forwards = []
    for (period, currency), (mid, basis) in sorted(forward_mids.items()):
        try:
            quote = forward_quote(config, currency, period, mid, asof, basis, source_hint)
        except (TypeError, ValueError):
            continue
        row = quote.to_dict()
        row.update({
            "currency": currency,
            "period": period,
            "age_hours": None if age is None else round(age, 2),
            "stale": stale,
            "executable_sell": round(quote.executable("sell_foreign"), 6),
            "executable_buy": round(quote.executable("buy_foreign"), 6),
        })
        forwards.append(row)
    return {
        "asof": asof,
        "max_age_hours": config.get("quote_max_age_hours", 24),
        "stale": stale,
        "stale_reason": reason,
        "spot": spots,
        "forwards": forwards,
    }
