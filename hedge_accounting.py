"""对冲会计底稿（管理会计口径，不替代总账）。

这里只做一件事：把「这笔远期指定对冲了哪笔敞口」算清楚，并给出
有效性测试结果和 OCI / 损益的拆分。**不生成正式凭证，也不替代
总账或审计**——科目表、税、减值、套期关系文档都不在工具里。

口径与假设：

1. 指定关系靠 `designated_period`（或交割日归集）落到「期间 × 币种」。
   同一桶里多笔远期按交易日先进先出分配敞口，分配完为止；分不到
   敞口的远期标成「未指定」，不参与有效性测试。
2. 远期公允价值用 CIP 曲线重估：
       F_t = S_t × (1 + i_本币·τ) / (1 + i_外币·τ)，τ 是剩余期限
       MtM_t = 签署方向 × 名义 × (F_t − 锁定价) / (1 + i_本币·τ)
   这是**折现后的可交割远期市值**，不是"锁定价减即期"那种现货近似。
3. 被套项目的变动用即期平移：ΔItem = 名义 × (S_t − S_0)。
   所以缺 `spot_at_trade` 时只能用锁定价近似，页面会标出来——这不是
   精确的会计计量，是底稿。
4. 有效部分 = 与 ΔItem 反号、且不超过 |ΔItem| 的那一块；剩下的是
   无效部分。80%~125% 是旧 IAS 39 的参考带，CAS 24（2017）与 IFRS 9
   不再设硬阈值，所以这里只提示、不判定通过与否。
5. 现金flow套期：有效部分进 OCI，结算期再重分类进损益；无效部分
   直接进损益。公允价值套期：有效部分也进损益（与被套项目对冲）；
   境外经营净投资套期：有效部分进 OCI（外币报表折算差额），处置时
   才结转。工具不判断你是不是真的有资格用套期会计。
"""
from __future__ import annotations

from decimal import Decimal

import forwards
from money import D, f2

VALID_TYPES = ("cash_flow", "fair_value", "net_investment")


def _period(value: str) -> str:
    return value[:7] if value else ""


def _signed(hedge: dict) -> float:
    amount = abs(float(hedge.get("amount", 0) or 0))
    if hedge.get("action") == "sell_foreign":
        return -amount
    return amount


def default_type(category: str) -> str:
    """按被套项目的性质给默认类型：已入账资产负债走公允价值套期，其余现金流。"""
    if category == "balance_sheet":
        return "fair_value"
    return "cash_flow"


def _years_between(start: str, end: str) -> float:
    from datetime import date

    try:
        first = date.fromisoformat(start)
        second = date.fromisoformat(end)
    except (TypeError, ValueError):
        return 0.0
    return max((second - first).days, 0) / 365.0


def _base_rate(config: dict) -> float | None:
    rates = config.get("interest_rates") or {}
    value = rates.get(config.get("base_currency", "CNY"))
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _foreign_rate(config: dict, currency: str) -> float | None:
    rates = config.get("interest_rates") or {}
    try:
        value = rates.get(currency)
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def build(
    groups: list[dict],
    pair_rates: dict[str, float],
    config: dict,
    today,
    settled_rates: dict[tuple[str, str], float] | None = None,
) -> dict:
    """每组是 {period, currency, gross, category, hedges}。

    `settled_rates` 里是已录入的实际结算汇率：结算期用真实价，不用当前市场价，
    否则重分类金额会跟着行情漂。
    """
    settled_rates = settled_rates or {}
    rows: list[dict] = []
    warnings: list[str] = []
    totals = {
        "mtm_cny": Decimal(0),
        "delta_item_cny": Decimal(0),
        "effective_cny": Decimal(0),
        "ineffective_cny": Decimal(0),
        "oci_cny": Decimal(0),
        "reclassified_cny": Decimal(0),
        "pl_cny": Decimal(0),
    }

    for group in groups:
        period = group.get("period", "")
        currency = group.get("currency", "")
        gross = float(group.get("gross", 0) or 0)
        category = group.get("category", "cash_flow")
        key = (period, currency)
        settled_rate = settled_rates.get(key)
        is_settled = settled_rate is not None
        rate = settled_rate if is_settled else pair_rates.get(currency)
        hedges = group.get("hedges") or []
        if not hedges:
            continue
        if rate is None:
            warnings.append(f"{period} {currency} 缺当前汇率，无法做有效性测试")
            continue
        spot = float(rate)
        remaining = abs(gross)
        for hedge in sorted(hedges, key=lambda row: (str(row.get("trade_date") or ""), str(row.get("id")))):
            amount = abs(float(hedge.get("amount", 0) or 0))
            signed = _signed(hedge)
            offsetting = gross * signed < 0
            allocated = min(amount, remaining) if offsetting else 0.0
            remaining = max(0.0, remaining - allocated)
            hedge_type = hedge.get("hedge_type") or default_type(category)

            warnings_for_row: list[str] = []
            if not offsetting:
                warnings_for_row.append("方向与敞口相同，不是对冲，未指定")
            elif allocated <= 0:
                warnings_for_row.append("对应敞口已被前面的远期分配完，超额部分未指定")
            elif allocated < amount - 1e-9:
                warnings_for_row.append(
                    f"超出敞口部分未指定：名义 {amount:,.2f}，已指定 {allocated:,.2f}"
                )
            if hedge_type not in VALID_TYPES:
                warnings_for_row.append(f"未知套期类型 {hedge_type}，按现金流套期处理")
                hedge_type = "cash_flow"

            locked = float(hedge.get("locked_rate", 0) or 0)
            if locked <= 0:
                warnings_for_row.append("锁定价缺失，跳过")
                rows.append(_skip_row(period, currency, hedge, hedge_type, warnings_for_row))
                continue

            due_period = _period(str(hedge.get("due_date") or ""))
            tenor = forwards.tenor_years(due_period, today)
            forward = forwards.forward_rate(spot, currency, due_period, config, today=today)
            forward_rate = float(forward["rate"])
            base_rate = _base_rate(config)
            discount = 1.0 / (1 + base_rate * tenor) if base_rate is not None and tenor > 0 else 1.0
            mtm = signed * (forward_rate - locked) * discount

            spot_at_trade = hedge.get("spot_at_trade")
            try:
                spot_at_trade = None if spot_at_trade in (None, "") else float(spot_at_trade)
            except (TypeError, ValueError):
                spot_at_trade = None
            at_market_proxy = spot_at_trade is None
            if at_market_proxy:
                spot_at_trade = locked
                warnings_for_row.append(
                    "缺交易日即期：按起息日平价（MtM=0）近似，被套项目变动用锁定价近似"
                )
            if forward["basis"] == "spot" and not is_settled:
                warnings_for_row.append(f"远期按即期重估：{forward['note']}")

            trade_date = str(hedge.get("trade_date") or "")
            due_date = str(hedge.get("due_date") or "")
            initial_tenor = _years_between(trade_date, due_date)
            foreign_rate = _foreign_rate(config, currency)
            if at_market_proxy:
                # 平价起息是行业默认假设：把锁定价当成当时的市场远期，
                # 期初公允价值为 0。用"锁定价当即期再反推理论远期"会凭空
                # 造出一个期初损益，把有效性比率算歪。
                theoretical_forward = locked
                mtm0 = 0.0
            else:
                if base_rate is not None and foreign_rate is not None and initial_tenor > 0:
                    theoretical_forward = spot_at_trade * (1 + base_rate * initial_tenor) / (1 + foreign_rate * initial_tenor)
                else:
                    theoretical_forward = spot_at_trade
                mtm0 = signed * (theoretical_forward - locked) / (
                    1 + base_rate * initial_tenor if base_rate is not None and initial_tenor > 0 else 1.0
                )
            delta_fv = mtm - mtm0

            item_signed = allocated if gross > 0 else -allocated
            if hedge_type == "cash_flow":
                # 现金流套期套的是预期交易，被套项目的公允价值变动要用
                # **假设衍生工具**（同一期限的远期）计量，不是即期平移。
                # 拿即期对远期 MtM，会系统性算出 30%~60% 的"无效"，
                # 而那只是 carry，不是套期失效。折现因子要和套保腿一致，
                # 否则一笔完全匹配的套保会永远差几十个 bp。
                delta_item = item_signed * (forward_rate - theoretical_forward) * discount
                item_basis = "forward"
            else:
                # 已确认的资产负债按即期重估（公允价值套期/净投资套期）
                delta_item = item_signed * (spot - spot_at_trade)
                item_basis = "spot"

            if abs(delta_item) > 1e-9:
                ratio = -delta_fv / delta_item
            else:
                ratio = None
                warnings_for_row.append("被套项目变动为 0，有效性比率不适用")

            if delta_fv * delta_item < 0 and delta_item != 0:
                effective = (1.0 if delta_fv > 0 else -1.0) * min(abs(delta_fv), abs(delta_item))
            else:
                effective = 0.0
            ineffective = delta_fv - effective

            oci = reclassified = pl_effective = 0.0
            if hedge_type == "cash_flow":
                if is_settled:
                    reclassified = effective
                else:
                    oci = effective
            elif hedge_type == "fair_value":
                pl_effective = effective
            else:  # net_investment：OCI，处置前不结转
                oci = effective

            if ratio is not None and not (0.8 <= abs(ratio) <= 1.25):
                warnings_for_row.append(f"有效性比率 {ratio:.0%} 落在 80%~125% 参考带之外")

            row = {
                "period": period,
                "currency": currency,
                "hedge_id": hedge.get("id"),
                "hedge_type": hedge_type,
                "trade_date": trade_date,
                "due_date": due_date,
                "notional": round(amount, 2),
                "allocated": round(allocated, 2),
                "locked_rate": locked,
                "spot_rate": spot,
                "forward_rate": round(forward_rate, 6),
                "spot_at_trade": round(spot_at_trade, 6),
                "mtm_cny": f2(mtm),
                "delta_fv_cny": f2(delta_fv),
                "delta_item_cny": f2(delta_item),
                "effectiveness": None if ratio is None else round(ratio, 4),
                "item_basis": item_basis,
                "rate_basis": "settlement" if is_settled else "market",
                "effective_cny": f2(effective),
                "ineffective_cny": f2(ineffective),
                "oci_cny": f2(oci),
                "reclassified_cny": f2(reclassified),
                "pl_cny": f2(ineffective + pl_effective + reclassified),
                "settled": is_settled,
                "warnings": warnings_for_row,
            }
            rows.append(row)

            totals["mtm_cny"] += D(mtm)
            totals["delta_item_cny"] += D(delta_item)
            totals["effective_cny"] += D(effective)
            totals["ineffective_cny"] += D(ineffective)
            totals["oci_cny"] += D(oci)
            totals["reclassified_cny"] += D(reclassified)
            totals["pl_cny"] += D(ineffective + pl_effective + reclassified)
            warnings.extend(f"{period} {currency}：{note}" for note in warnings_for_row)

    return {
        "asof": today.isoformat(),
        "rows": rows,
        "totals": {key: f2(value) for key, value in totals.items()},
        "warnings": warnings,
        "designated_count": sum(1 for row in rows if row.get("allocated")),
        "open_count": sum(1 for row in rows if not row.get("settled")),
        "ineffective_count": sum(
            1 for row in rows if abs(float(row.get("ineffective_cny", 0) or 0)) > 0.005
        ),
    }


def _skip_row(period: str, currency: str, hedge: dict, hedge_type: str, warnings: list[str]) -> dict:
    """锁定价缺失也要留一行，否则明细里"少了一笔"没人发现。"""
    return {
        "period": period,
        "currency": currency,
        "hedge_id": hedge.get("id"),
        "hedge_type": hedge_type,
        "trade_date": str(hedge.get("trade_date") or ""),
        "due_date": str(hedge.get("due_date") or ""),
        "notional": round(abs(float(hedge.get("amount", 0) or 0)), 2),
        "allocated": 0.0,
        "locked_rate": 0.0,
        "spot_rate": None,
        "forward_rate": None,
        "spot_at_trade": None,
        "mtm_cny": 0.0,
        "delta_fv_cny": 0.0,
        "delta_item_cny": 0.0,
        "effectiveness": None,
        "item_basis": "n/a",
        "rate_basis": "n/a",
        "effective_cny": 0.0,
        "ineffective_cny": 0.0,
        "oci_cny": 0.0,
        "reclassified_cny": 0.0,
        "pl_cny": 0.0,
        "settled": False,
        "warnings": warnings,
    }
