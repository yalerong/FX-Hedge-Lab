from __future__ import annotations

import argparse
import json
import mimetypes
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from urllib.error import URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / "web"
DATA_DIR = ROOT / "data"
STATE_FILE = DATA_DIR / "fx_workspace.json"
RATES_CACHE_FILE = DATA_DIR / "rates_cache.json"
BASE_CURRENCY = "CNY"


DEFAULT_CONFIG = {
    "base_currency": BASE_CURRENCY,
    "rate_api_url": "https://open.er-api.com/v6/latest/USD",
    "rate_cache_hours": 24,
    "supported_currencies": ["USD", "EUR", "JPY", "HKD", "GBP", "AUD", "SGD"],
    "strategy_type": "standard",
    "enterprise_type": "comprehensive",
    "default_hedge_ratio": 0.8,
    "month_currency_hedge_ratios": {},
    "risk_limit_cny": 200000,
    "optimistic_shift_pct": 0.03,
    "pessimistic_shift_pct": -0.03,
    "custom_scenario_shift_pct": 0.01,
}


DEMO_STATE = {
    "config": DEFAULT_CONFIG,
    "exposures": [
        {
            "id": "demo-exp-1",
            "created_at": "2026-05-12T00:00:00Z",
            "due_date": "2026-06-30",
            "currency": "USD",
            "amount": 1200000,
            "direction": "receipt",
            "category": "order_contract",
            "description": "出口订单预计收款",
            "probability": 1,
        },
        {
            "id": "demo-exp-2",
            "created_at": "2026-05-12T00:00:00Z",
            "due_date": "2026-06-30",
            "currency": "EUR",
            "amount": 350000,
            "direction": "payment",
            "category": "cash_flow",
            "description": "进口采购预计付款",
            "probability": 1,
        },
    ],
    "hedges": [
        {
            "id": "demo-hedge-1",
            "created_at": "2026-05-12T00:00:00Z",
            "trade_date": "2026-05-12",
            "due_date": "2026-06-30",
            "currency": "USD",
            "amount": 500000,
            "action": "sell_foreign",
            "locked_rate": 7.18,
            "description": "远期结汇锁定部分美元收款",
        }
    ],
    "settlements": [
        {
            "id": "demo-settle-1",
            "created_at": "2026-05-12T00:00:00Z",
            "due_date": "2026-06-30",
            "currency": "USD",
            "actual_rate": 7.21,
            "description": "样例到期实际汇率",
        }
    ],
}


FALLBACK_PAIR_RATES = {
    "USD": 7.15,
    "EUR": 7.72,
    "JPY": 0.049,
    "HKD": 0.915,
    "GBP": 9.05,
    "AUD": 4.72,
    "SGD": 5.31,
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return utc_now().replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json(path: Path, default: dict) -> dict:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def ensure_state() -> dict:
    DATA_DIR.mkdir(exist_ok=True)
    if not STATE_FILE.exists():
        write_json(STATE_FILE, DEMO_STATE)
    return read_json(STATE_FILE, DEMO_STATE)


def save_state(state: dict) -> None:
    write_json(STATE_FILE, state)


def merged_config(state: dict) -> dict:
    config = dict(DEFAULT_CONFIG)
    config.update(state.get("config", {}))
    return config


def stale(iso_value: str | None, hours: float) -> bool:
    if not iso_value:
        return True
    try:
        last = datetime.fromisoformat(iso_value.replace("Z", "+00:00"))
    except ValueError:
        return True
    age_hours = (utc_now() - last).total_seconds() / 3600
    return age_hours >= hours


def pair_rates_from_payload(payload: dict, currencies: list[str]) -> dict[str, float]:
    rates = payload.get("rates", {})
    cny = float(rates[BASE_CURRENCY])
    pairs = {}
    for currency in currencies:
        if currency == BASE_CURRENCY:
            continue
        if currency in rates and float(rates[currency]) != 0:
            pairs[currency] = round(cny / float(rates[currency]), 6)
    return pairs


def load_rates(config: dict, force: bool = False) -> dict:
    cache = read_json(RATES_CACHE_FILE, {})
    if not force and cache and not stale(cache.get("fetched_at"), float(config.get("rate_cache_hours", 24))):
        return cache

    url = config.get("rate_api_url") or DEFAULT_CONFIG["rate_api_url"]
    currencies = config.get("supported_currencies") or DEFAULT_CONFIG["supported_currencies"]
    try:
        request = Request(url, headers={"User-Agent": "local-fx-risk-simulator/1.0"})
        with urlopen(request, timeout=12) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if payload.get("result") not in {None, "success"}:
            raise ValueError(payload.get("error-type", "exchange rate api returned an error"))
        pair_rates = pair_rates_from_payload(payload, currencies)
        cache = {
            "source": "ExchangeRate-API open endpoint",
            "source_url": "https://www.exchangerate-api.com",
            "api_url": url,
            "fetched_at": now_iso(),
            "status": "live",
            "base_code": payload.get("base_code", "USD"),
            "time_last_update_utc": payload.get("time_last_update_utc"),
            "pair_rates": pair_rates,
            "raw_result": payload.get("result"),
        }
        write_json(RATES_CACHE_FILE, cache)
        return cache
    except (OSError, URLError, ValueError, KeyError, json.JSONDecodeError) as exc:
        if cache:
            cache["status"] = "cached_after_refresh_error"
            cache["last_error"] = str(exc)
            return cache
        cache = {
            "source": "built-in fallback rates",
            "source_url": "manual fallback",
            "api_url": url,
            "fetched_at": now_iso(),
            "status": "fallback",
            "last_error": str(exc),
            "pair_rates": {currency: rate for currency, rate in FALLBACK_PAIR_RATES.items() if currency in currencies},
        }
        write_json(RATES_CACHE_FILE, cache)
        return cache


def signed_exposure(row: dict) -> float:
    amount = float(row.get("amount", 0)) * float(row.get("probability", 1))
    if row.get("direction") in {"receipt", "asset", "export"}:
        return amount
    if row.get("direction") in {"payment", "liability", "import"}:
        return -amount
    raise ValueError("direction must be receipt/payment")


def signed_hedge(row: dict) -> float:
    amount = float(row.get("amount", 0))
    if row.get("action") == "buy_foreign":
        return amount
    if row.get("action") == "sell_foreign":
        return -amount
    raise ValueError("action must be buy_foreign/sell_foreign")


def period_from_date(value: str) -> str:
    return value[:7] if value else "未填日期"


def aggregate_rows(rows: list[dict], sign_fn) -> dict[tuple[str, str], float]:
    totals: dict[tuple[str, str], float] = defaultdict(float)
    for row in rows:
        totals[(period_from_date(row.get("due_date", "")), row.get("currency", "").upper())] += sign_fn(row)
    return totals


def current_rate(pair_rates: dict[str, float], currency: str) -> float:
    return float(pair_rates.get(currency, FALLBACK_PAIR_RATES.get(currency, 1.0)))


def hedge_ratio_for(config: dict, period: str, currency: str) -> float:
    ratios = config.get("month_currency_hedge_ratios") or {}
    direct = ratios.get(f"{period}:{currency}")
    if direct is not None:
        return float(direct)
    monthly = ratios.get(period)
    if isinstance(monthly, dict) and monthly.get(currency) is not None:
        return float(monthly[currency])
    return float(config.get("default_hedge_ratio", 0.8))


def action_for(config: dict, net: float) -> str:
    enterprise_type = config.get("enterprise_type", "comprehensive")
    if enterprise_type == "export":
        return "sell_foreign"
    if enterprise_type == "import":
        return "buy_foreign"
    return "sell_foreign" if net > 0 else "buy_foreign"


def exposure_category_for(exposures: list[dict], period: str, currency: str) -> str:
    score: dict[str, float] = defaultdict(float)
    for row in exposures:
        if period_from_date(row.get("due_date", "")) == period and row.get("currency", "").upper() == currency:
            score[row.get("category", "cash_flow")] += abs(signed_exposure(row))
    if not score:
        return "cash_flow"
    return max(score.items(), key=lambda item: item[1])[0]


def accounting_bucket(category: str, trade_date: str, due_date: str) -> str:
    same_month = period_from_date(trade_date) == period_from_date(due_date)
    if category in {"balance_sheet", "order_contract"} and same_month:
        return "derivative_investment_income"
    if category == "cash_flow" and same_month:
        return "realized_exchange_gain_loss"
    return "fair_value_change_gain_loss"


def scenario_rates_for(pair_rates: dict[str, float], config: dict) -> dict[str, dict[str, float]]:
    shifts = {
        "neutral": 0.0,
        "optimistic": float(config.get("optimistic_shift_pct", 0.03)),
        "pessimistic": float(config.get("pessimistic_shift_pct", -0.03)),
        "custom": float(config.get("custom_scenario_shift_pct", 0.01)),
    }
    return {
        name: {currency: round(float(rate) * (1 + shift), 6) for currency, rate in pair_rates.items()}
        for name, shift in shifts.items()
    }


def signed_recommendation(action: str, amount: float) -> float:
    return -amount if action == "sell_foreign" else amount


def scenario_projection(
    period: str,
    currency: str,
    net: float,
    recommended: dict,
    current: float,
    scenario_rates: dict[str, dict[str, float]],
) -> dict[str, dict]:
    rows = {}
    for name, rates in scenario_rates.items():
        scenario_rate = float(rates.get(currency, current))
        exposure_pnl = net * (scenario_rate - current)
        hedge_pnl = signed_recommendation(recommended["action"], recommended["recommended_amount"]) * (
            scenario_rate - recommended["trade_rate"]
        )
        rows[name] = {
            "period": period,
            "currency": currency,
            "scenario_rate": scenario_rate,
            "unrealized_exchange_gain_loss": round(exposure_pnl, 2),
            recommended["accounting_bucket"]: round(hedge_pnl, 2),
            "total_projected_gain_loss": round(exposure_pnl + hedge_pnl, 2),
        }
    return rows


def build_dashboard(state: dict, rates_cache: dict) -> dict:
    pair_rates = rates_cache.get("pair_rates", {})
    exposures = state.get("exposures", [])
    hedges = state.get("hedges", [])
    settlements = state.get("settlements", [])
    config = merged_config(state)

    exposure_totals = aggregate_rows(exposures, signed_exposure)
    hedge_totals = aggregate_rows(hedges, signed_hedge)
    keys = sorted(set(exposure_totals) | set(hedge_totals))

    net_rows = []
    suggestions = []
    scenario_rates = scenario_rates_for(pair_rates, config)
    scenario_summary: dict[str, dict[str, dict]] = {}
    for period, currency in keys:
        gross = exposure_totals.get((period, currency), 0.0)
        hedged = hedge_totals.get((period, currency), 0.0)
        net = gross + hedged
        rate = current_rate(pair_rates, currency)
        cny_risk = abs(net * rate)
        target_ratio = hedge_ratio_for(config, period, currency)
        category = exposure_category_for(exposures, period, currency)
        net_rows.append(
            {
                "period": period,
                "currency": currency,
                "risk_category": category,
                "target_hedge_ratio": target_ratio,
                "business_exposure": round(gross, 2),
                "locked_exposure": round(hedged, 2),
                "net_exposure": round(net, 2),
                "current_rate": rate,
                "cny_risk": round(cny_risk, 2),
            }
        )
        target_cover = abs(gross) * target_ratio
        covered = abs(hedged)
        recommended_amount = max(0.0, target_cover - covered)
        if abs(net) > 0 and recommended_amount > 0:
            action = action_for(config, net)
            recommendation = {
                "period": period,
                "currency": currency,
                "risk_category": category,
                "net_exposure": round(net, 2),
                "business_exposure": round(gross, 2),
                "covered_exposure": round(covered, 2),
                "current_rate": rate,
                "trade_rate": rate,
                "risk_cny": round(cny_risk, 2),
                "target_hedge_ratio": target_ratio,
                "recommended_amount": round(recommended_amount, 2),
                "action": action,
                "accounting_bucket": accounting_bucket(category, now_iso()[:10], f"{period}-28"),
                "plain_text": suggestion_text(currency, net, target_ratio, recommended_amount, action),
            }
            recommendation["scenario_projection"] = scenario_projection(
                period, currency, net, recommendation, rate, scenario_rates
            )
            scenario_summary[f"{period}:{currency}"] = recommendation["scenario_projection"]
            suggestions.append(recommendation)

    backtest_rows = build_backtest(exposures, hedges, settlements, pair_rates)
    return {
        "config": config,
        "rates": rates_cache,
        "exposures": exposures,
        "hedges": hedges,
        "settlements": settlements,
        "net_exposures": net_rows,
        "suggestions": suggestions,
        "scenario_rates": scenario_rates,
        "scenario_summary": scenario_summary,
        "backtest": backtest_rows,
        "plain_language": build_plain_language(net_rows, suggestions, backtest_rows, rates_cache),
    }


def suggestion_text(currency: str, net: float, ratio: float, amount: float, action: str) -> str:
    if net > 0:
        exposure_side = f"未来净收 {currency}"
        action_text = "卖出外币/远期结汇"
    else:
        exposure_side = f"未来净付 {currency}"
        action_text = "买入外币/远期购汇"
    return f"{exposure_side}，建议先锁 {ratio:.0%}，即 {amount:,.2f} {currency}，操作方向：{action_text}。"


def build_backtest(exposures: list[dict], hedges: list[dict], settlements: list[dict], pair_rates: dict[str, float]) -> list[dict]:
    actual_by_key = {
        (period_from_date(row.get("due_date", "")), row.get("currency", "").upper()): float(row.get("actual_rate", 0))
        for row in settlements
    }
    exposure_totals = aggregate_rows(exposures, signed_exposure)
    hedge_totals = aggregate_rows(hedges, signed_hedge)
    locked_by_key: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for hedge in hedges:
        locked_by_key[(period_from_date(hedge.get("due_date", "")), hedge.get("currency", "").upper())].append(hedge)

    rows = []
    for key in sorted(set(exposure_totals) | set(hedge_totals) | set(actual_by_key)):
        period, currency = key
        actual_rate = actual_by_key.get(key) or current_rate(pair_rates, currency)
        market_rate = current_rate(pair_rates, currency)
        gross = exposure_totals.get(key, 0.0)
        hedge_effect = 0.0
        locked_detail = []
        for hedge in locked_by_key.get(key, []):
            locked_rate = float(hedge.get("locked_rate", 0))
            signed = signed_hedge(hedge)
            effect = signed * (actual_rate - locked_rate)
            hedge_effect += effect
            locked_detail.append(
                {
                    "amount": float(hedge.get("amount", 0)),
                    "action": hedge.get("action"),
                    "locked_rate": locked_rate,
                    "effect_cny": round(effect, 2),
                }
            )
        unhedged_result = gross * (actual_rate - market_rate)
        rows.append(
            {
                "period": period,
                "currency": currency,
                "business_exposure": round(gross, 2),
                "actual_rate": round(actual_rate, 6),
                "reference_rate": round(market_rate, 6),
                "unhedged_mark_to_market_cny": round(unhedged_result, 2),
                "hedge_effect_cny": round(hedge_effect, 2),
                "locked_detail": locked_detail,
                "plain_text": (
                    f"{period} {currency}: 实际汇率 {actual_rate:.6f}，参考汇率 {market_rate:.6f}，"
                    f"锁汇贡献 {hedge_effect:,.2f} CNY。"
                ),
            }
        )
    return rows


def build_plain_language(net_rows: list[dict], suggestions: list[dict], backtest_rows: list[dict], rates_cache: dict) -> list[str]:
    lines = [
        "这套本地工具按五步跑：先录入外币收付款，再汇总净敞口，再给锁汇建议，再记录实际锁汇，最后用实际汇率回头检查收益。",
        f"当前汇率来源：{rates_cache.get('source')}；状态：{rates_cache.get('status')}；更新时间：{rates_cache.get('fetched_at')}。",
    ]
    if not net_rows:
        lines.append("还没有敞口。先添加一笔未来外币收款或付款。")
    for row in net_rows:
        side = "净收" if row["net_exposure"] > 0 else "净付"
        lines.append(
            f"{row['period']} {row['currency']} 当前{side} {abs(row['net_exposure']):,.2f}，"
            f"折人民币风险约 {row['cny_risk']:,.2f}。"
        )
    for item in suggestions:
        lines.append(item["plain_text"])
    if backtest_rows:
        lines.append("回测不是预测，它只是回答：如果按已记录锁汇执行，到期后相对实际汇率贡献了多少人民币。")
    return lines


def parse_body(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length", "0"))
    if length <= 0:
        return {}
    raw = handler.rfile.read(length).decode("utf-8")
    return json.loads(raw) if raw else {}


def add_id(row: dict) -> dict:
    row = dict(row)
    row["id"] = row.get("id") or uuid.uuid4().hex[:12]
    row["created_at"] = row.get("created_at") or now_iso()
    if "currency" in row:
        row["currency"] = row["currency"].upper()
    return row


class FxRiskHandler(BaseHTTPRequestHandler):
    server_version = "FxRiskLocal/1.0"

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in {"/", "/index.html"}:
            self.serve_file(WEB_ROOT / "index.html")
            return
        if path.startswith("/web/"):
            self.serve_file(ROOT / path.lstrip("/"))
            return
        if path == "/api/state":
            state = ensure_state()
            rates = load_rates(merged_config(state))
            self.send_json(build_dashboard(state, rates))
            return
        self.send_error(404)

    def do_POST(self) -> None:
        state = ensure_state()
        try:
            body = parse_body(self)
            if self.path == "/api/exposures":
                validate_exposure(body)
                state.setdefault("exposures", []).append(add_id(body))
                save_state(state)
                self.send_json({"ok": True})
                return
            if self.path == "/api/hedges":
                validate_hedge(body)
                state.setdefault("hedges", []).append(add_id(body))
                save_state(state)
                self.send_json({"ok": True})
                return
            if self.path == "/api/settlements":
                validate_settlement(body)
                state.setdefault("settlements", []).append(add_id(body))
                save_state(state)
                self.send_json({"ok": True})
                return
            if self.path == "/api/config":
                merged = dict(DEFAULT_CONFIG)
                merged.update(state.get("config", {}))
                merged.update(body)
                state["config"] = merged
                save_state(state)
                self.send_json({"ok": True, "config": merged})
                return
            if self.path == "/api/rates/refresh":
                self.send_json(load_rates(merged_config(state), force=True))
                return
            if self.path == "/api/reset-demo":
                save_state(DEMO_STATE)
                self.send_json({"ok": True})
                return
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=400)
            return
        self.send_error(404)

    def do_DELETE(self) -> None:
        state = ensure_state()
        parts = self.path.strip("/").split("/")
        if len(parts) == 3 and parts[0] == "api":
            collection = {"exposures": "exposures", "hedges": "hedges", "settlements": "settlements"}.get(parts[1])
            if collection:
                before = len(state.get(collection, []))
                state[collection] = [row for row in state.get(collection, []) if row.get("id") != parts[2]]
                save_state(state)
                self.send_json({"ok": True, "deleted": before - len(state[collection])})
                return
        self.send_error(404)

    def serve_file(self, path: Path) -> None:
        if not path.exists() or not path.is_file():
            self.send_error(404)
            return
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        payload = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def send_json(self, data: dict, status: int = 200) -> None:
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt: str, *args) -> None:
        print(f"[{now_iso()}] {self.address_string()} {fmt % args}")


def validate_exposure(row: dict) -> None:
    required = ["due_date", "currency", "amount", "direction"]
    missing = [key for key in required if not row.get(key)]
    if missing:
        raise ValueError(f"missing exposure fields: {', '.join(missing)}")
    if row["direction"] not in {"receipt", "payment"}:
        raise ValueError("exposure direction must be receipt or payment")
    if float(row["amount"]) <= 0:
        raise ValueError("amount must be positive")


def validate_hedge(row: dict) -> None:
    required = ["trade_date", "due_date", "currency", "amount", "action", "locked_rate"]
    missing = [key for key in required if not row.get(key)]
    if missing:
        raise ValueError(f"missing hedge fields: {', '.join(missing)}")
    if row["action"] not in {"buy_foreign", "sell_foreign"}:
        raise ValueError("hedge action must be buy_foreign or sell_foreign")
    if float(row["amount"]) <= 0 or float(row["locked_rate"]) <= 0:
        raise ValueError("amount and locked_rate must be positive")


def validate_settlement(row: dict) -> None:
    required = ["due_date", "currency", "actual_rate"]
    missing = [key for key in required if not row.get(key)]
    if missing:
        raise ValueError(f"missing settlement fields: {', '.join(missing)}")
    if float(row["actual_rate"]) <= 0:
        raise ValueError("actual_rate must be positive")


def run(host: str, port: int) -> None:
    ensure_state()
    server = ThreadingHTTPServer((host, port), FxRiskHandler)
    print(f"FX risk web app running at http://{host}:{port}")
    server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local FX risk web app.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    run(args.host, args.port)


if __name__ == "__main__":
    main()
