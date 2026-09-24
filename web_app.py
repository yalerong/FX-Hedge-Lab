from __future__ import annotations

import argparse
import base64
import binascii
import copy
import csv
import io
import json
import math
import mimetypes
import os
import posixpath
import re
import threading
import uuid
import zipfile
from collections import defaultdict
from datetime import date as dt_date, datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
from pathlib import Path
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape as xml_escape
from urllib.parse import parse_qs, unquote, urlparse
from urllib.error import URLError
from urllib.request import Request, urlopen

from money import D, f2, f as fN, q
import benchmarks
import forwards
import plans
import variance


ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / "web"
DATA_DIR = ROOT / "data"
STATE_FILE = DATA_DIR / "fx_workspace.json"
RATES_CACHE_FILE = DATA_DIR / "rates_cache.json"
FORECAST_SIGNALS_FILE = DATA_DIR / "forecast_signals.json"
AUDIT_LOG_FILE = DATA_DIR / "audit_log.jsonl"
BACKUP_DIR = DATA_DIR / "backups"
BASE_CURRENCY = "CNY"
MAX_BACKUPS = 20
MAX_REQUEST_BODY_BYTES = 5 * 1024 * 1024
# Workspace JSON and base64-encoded XLSX imports are intentionally larger than
# ordinary mutations. 64 MB keeps normal export/import round trips viable while
# still bounding the in-memory JSON parser used by this zero-dependency server.
MAX_IMPORT_REQUEST_BODY_BYTES = 64 * 1024 * 1024
REJECTED_BODY_DRAIN_BYTES = 64 * 1024
REJECTED_BODY_DRAIN_TIMEOUT_SECONDS = 0.05
FORECAST_MAX_AGE_DAYS = 45
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PERIOD_RE = re.compile(r"^\d{4}-\d{2}$")
CURRENCY_RE = re.compile(r"^[A-Z]{3}$")

# Serializes the read-modify-write of the JSON state file so concurrent
# requests on the ThreadingHTTPServer can't clobber each other.
STATE_LOCK = threading.Lock()


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
    # 远期价：优先用 forward_overrides 里的银行报价，没有就按利差推。
    # 这几个默认值只是让工具开箱能跑，**用之前请换成你自己的资金成本**。
    "interest_rates": {"CNY": 0.019, "USD": 0.043, "EUR": 0.025, "JPY": 0.005,
                       "HKD": 0.042, "GBP": 0.045, "AUD": 0.038, "SGD": 0.032},
    "forward_overrides": {},
    "monthly_average_rates": {},
    "confirmed_parameters": {},
    # 情景默认对所有币种同幅同向变动，等于假设币种间相关性为 1。
    # 对"净收美元 + 净付欧元"这种组合会天然对冲、把风险算小，
    # 要按币种分别设就写在这里：{"USD": {"optimistic": 0.03, ...}}
    "scenario_shifts": {},
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


# 风险类型只有这三个合法值。历史数据里出现过表外值（例如 export_order），
# 那时 accounting_bucket 会静默落到公允价值变动科目——挂错科目还不报错。
# 现在的做法是：新数据一律拒收表外值；老数据保留原样但显式标出来，
# **不做猜测性映射**——把 export_order 猜成"合同/订单套保"会悄悄改掉会计科目。
EXPOSURE_CATEGORIES = ("balance_sheet", "cash_flow", "order_contract")
DEFAULT_CATEGORY = "cash_flow"


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
        return copy.deepcopy(default)
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(data, ensure_ascii=False, indent=2))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        # replace 失败时不让临时文件长期堆在数据目录；成功时路径已经不存在。
        tmp.unlink(missing_ok=True)


def ensure_state() -> dict:
    DATA_DIR.mkdir(exist_ok=True)
    if not STATE_FILE.exists():
        state = empty_state()
        save_state(state, reason="first-run", backup=False)
        return state
    try:
        return validate_workspace_state(read_json(STATE_FILE, empty_state()))
    except (OSError, json.JSONDecodeError, ValueError):
        preserve_corrupt_state()
        backup = latest_backup_file()
        if backup:
            state = validate_workspace_state(read_json(backup, empty_state()))
            save_state(state, reason="recover", backup=False)
            return state
        state = empty_state()
        save_state(state, reason="recover-empty", backup=False)
        return state


def _safe_backup_reason(reason: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", reason).strip("-")[:40] or "state"


def backup_dir() -> Path:
    return DATA_DIR / "backups"


def _stat_backups(directory: Path) -> list[tuple[Path, os.stat_result]]:
    """备份文件按修改时间倒序，stat 失败的直接跳过。

    列备份和别的请求剪枝备份是并发的：glob 到的文件在 stat 之前可能已经
    被删掉。排序 key 里直接 stat 会把 FileNotFoundError 抛到整个请求外面。
    """
    entries = []
    for path in directory.glob("*.json"):
        try:
            entries.append((path, path.stat()))
        except OSError:
            continue
    entries.sort(key=lambda item: item[1].st_mtime, reverse=True)
    return entries


def list_backups() -> list[dict]:
    directory = backup_dir()
    if not directory.exists():
        return []
    return [
        {
            "name": path.name,
            "size": stat.st_size,
            "created_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc)
            .replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        }
        for path, stat in _stat_backups(directory)
    ]


def prune_backups() -> None:
    for path, _ in _stat_backups(backup_dir())[MAX_BACKUPS:]:
        try:
            path.unlink()
        except OSError:
            pass


def backup_current_state(reason: str) -> dict | None:
    if not STATE_FILE.exists():
        return None
    directory = backup_dir()
    directory.mkdir(parents=True, exist_ok=True)
    stamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
    target = directory / f"{stamp}-{_safe_backup_reason(reason)}-{uuid.uuid4().hex[:8]}.json"
    try:
        target.write_bytes(STATE_FILE.read_bytes())
        prune_backups()
        return {"name": target.name, "size": target.stat().st_size}
    except OSError:
        return None


def save_state(state: dict, reason: str = "state", backup: bool = True) -> None:
    if isinstance(state.get("metadata"), dict):
        state["metadata"]["updated_at"] = now_iso()
    # 先校验、再轮换备份：端点级检查过了但整体校验不过的请求（比如配置里
    # 删掉了仍有记录在用的币种）什么都没写，却会白吃一格备份槽位；
    # 重复几次就把真正有用的回滚快照挤没了。
    normalized = validate_workspace_state(state)
    if backup:
        backup_current_state(reason)
    write_json(STATE_FILE, normalized)
    if backup:
        # 保存后的快照用于主文件损坏时恢复；用户主动“恢复最近备份”会跳过它，
        # 选择最近一次操作前的快照，见 latest_undo_backup_file。
        backup_current_state(f"{reason}-after")


def parse_iso_date(value: object, field: str) -> dt_date:
    if not isinstance(value, str) or not DATE_RE.match(value):
        raise ValueError(f"{field} 必须是 YYYY-MM-DD 日期")
    try:
        return dt_date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{field} 不是有效日期") from None


def positive_number(value: object, field: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} 必须是数字")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} 必须是数字") from None
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{field} 必须大于 0")
    return number


def non_negative_number(value: object, field: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} 必须是数字")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} 必须是数字") from None
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{field} 不能为负")
    return number


def finite_number(value: object, field: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} 必须是数字")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} 必须是数字") from None
    if not math.isfinite(number):
        raise ValueError(f"{field} 必须是有限数字")
    return number


def validate_period(value: object, field: str) -> str:
    if not isinstance(value, str) or not PERIOD_RE.match(value):
        raise ValueError(f"{field} 的月份必须是 YYYY-MM")
    try:
        dt_date.fromisoformat(f"{value}-01")
    except ValueError:
        raise ValueError(f"{field} 包含无效月份 {value}") from None
    return value


def validate_currency_code(value: object, field: str) -> str:
    if not isinstance(value, str) or not CURRENCY_RE.match(value):
        raise ValueError(f"{field} 的币种必须是三位大写字母")
    return value


def validate_period_currency_map(mapping: dict, field: str, value_validator) -> dict:
    """校验 `YYYY-MM:USD -> 值` 和 `YYYY-MM -> {USD: 值}` 两种公开格式。"""
    validated = {}
    for key, raw in mapping.items():
        if not isinstance(key, str):
            raise ValueError(f"{field} 的键必须是字符串")
        if ":" in key:
            parts = key.split(":")
            if len(parts) != 2:
                raise ValueError(f"{field} 的键必须是 YYYY-MM:币种")
            period, currency = parts
            validate_period(period, field)
            validate_currency_code(currency, field)
            validated[key] = value_validator(raw, f"{field}.{key}")
            continue
        validate_period(key, field)
        if not isinstance(raw, dict) or not raw:
            raise ValueError(f"{field}.{key} 必须是按币种填写的对象")
        nested = {}
        for currency, value in raw.items():
            validate_currency_code(currency, field)
            nested[currency] = value_validator(value, f"{field}.{key}.{currency}")
        validated[key] = nested
    return validated


def validate_currency(row: dict, config: dict) -> str:
    currency = row.get("currency")
    supported = config.get("supported_currencies") or DEFAULT_CONFIG["supported_currencies"]
    if not isinstance(currency, str) or not CURRENCY_RE.match(currency):
        raise ValueError("币种必须是三位大写字母，例如 USD")
    if currency not in supported:
        raise ValueError(f"币种 {currency} 不在 supported_currencies 内")
    return currency


def merge_config(base: dict, patch: dict, replace_maps: set[str] | None = None) -> dict:
    merged = copy.deepcopy(base)
    replace_maps = replace_maps or set()
    for key, value in (patch or {}).items():
        if key in replace_maps:
            merged[key] = copy.deepcopy(value)
        elif isinstance(value, dict) and isinstance(merged.get(key), dict):
            nested = copy.deepcopy(merged[key])
            nested.update(value)
            merged[key] = nested
        else:
            merged[key] = value
    return merged


def validate_config(payload: dict, base: dict | None = None, replace_maps: set[str] | None = None) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("配置必须是对象")
    config = merge_config(base or DEFAULT_CONFIG, payload, replace_maps=replace_maps)
    # 全站汇率都按「1 外币 = N 元人民币」记，远期定价拿 base_currency 去查
    # 利率。导入的工作区把它改成别的币种，远期价就静默用错利率。
    if config.get("base_currency", BASE_CURRENCY) != BASE_CURRENCY:
        raise ValueError(f"base_currency 只支持 {BASE_CURRENCY}")
    config["base_currency"] = BASE_CURRENCY
    currencies = config.get("supported_currencies")
    if not isinstance(currencies, list) or not currencies:
        raise ValueError("supported_currencies 必须是非空列表")
    for currency in currencies:
        if not isinstance(currency, str) or not CURRENCY_RE.match(currency):
            raise ValueError("supported_currencies 只能包含三位大写币种")
    if len(set(currencies)) != len(currencies):
        raise ValueError("supported_currencies 不能包含重复币种")
    if BASE_CURRENCY in currencies:
        raise ValueError("supported_currencies 不需要包含 CNY")
    config["rate_cache_hours"] = positive_number(config.get("rate_cache_hours"), "rate_cache_hours")
    config["risk_limit_cny"] = non_negative_number(config.get("risk_limit_cny", 0), "risk_limit_cny")
    ratio = non_negative_number(config.get("default_hedge_ratio", 0), "default_hedge_ratio")
    if ratio > 1:
        raise ValueError("default_hedge_ratio 必须在 0 到 1 之间")
    config["default_hedge_ratio"] = ratio
    if config.get("enterprise_type") not in {"comprehensive", "export", "import"}:
        raise ValueError("enterprise_type 不合法")
    for key in ("optimistic_shift_pct", "pessimistic_shift_pct", "custom_scenario_shift_pct"):
        config[key] = finite_number(config.get(key, 0), key)
        if config[key] <= -1:
            raise ValueError(f"{key} 必须大于 -1")
    for key in ("interest_rates", "forward_overrides", "scenario_shifts",
                "month_currency_hedge_ratios", "monthly_average_rates",
                "confirmed_parameters"):
        if not isinstance(config.get(key, {}), dict):
            raise ValueError(f"{key} 必须是对象")

    interest_rates = {}
    for currency, value in config["interest_rates"].items():
        validate_currency_code(currency, "interest_rates")
        rate = finite_number(value, f"interest_rates.{currency}")
        if not -1 < rate <= 1:
            raise ValueError(f"interest_rates.{currency} 必须在 -1 到 1 之间")
        interest_rates[currency] = rate
    config["interest_rates"] = interest_rates

    config["forward_overrides"] = validate_period_currency_map(
        config["forward_overrides"], "forward_overrides", positive_number,
    )
    config["monthly_average_rates"] = validate_period_currency_map(
        config["monthly_average_rates"], "monthly_average_rates", positive_number,
    )

    def validate_ratio(value: object, field: str) -> float:
        ratio_value = non_negative_number(value, field)
        if ratio_value > 1:
            raise ValueError(f"{field} 必须在 0 到 1 之间")
        return ratio_value

    config["month_currency_hedge_ratios"] = validate_period_currency_map(
        config["month_currency_hedge_ratios"], "month_currency_hedge_ratios", validate_ratio,
    )

    scenario_shifts = {}
    valid_scenarios = {"neutral", "optimistic", "pessimistic", "custom"}
    for currency, values in config["scenario_shifts"].items():
        validate_currency_code(currency, "scenario_shifts")
        if not isinstance(values, dict) or not values:
            raise ValueError(f"scenario_shifts.{currency} 必须是按情景填写的对象")
        unknown = set(values) - valid_scenarios
        if unknown:
            raise ValueError(f"scenario_shifts.{currency} 包含未知情景: {', '.join(sorted(unknown))}")
        scenario_shifts[currency] = {}
        for scenario, value in values.items():
            shift = finite_number(value, f"scenario_shifts.{currency}.{scenario}")
            if shift <= -1:
                raise ValueError(f"scenario_shifts.{currency}.{scenario} 必须大于 -1")
            scenario_shifts[currency][scenario] = shift
    config["scenario_shifts"] = scenario_shifts

    for key, value in config["confirmed_parameters"].items():
        if not isinstance(key, str) or not isinstance(value, bool):
            raise ValueError("confirmed_parameters 必须使用字符串键和布尔值")
    return config


def latest_backup_file() -> Path | None:
    rows = _stat_backups(backup_dir())
    return rows[0][0] if rows else None


def latest_undo_backup_file() -> Path | None:
    rows = [path for path, _ in _stat_backups(backup_dir()) if "-after-" not in path.name]
    return rows[0] if rows else None


def preserve_corrupt_state() -> Path | None:
    if not STATE_FILE.exists():
        return None
    stamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
    target = STATE_FILE.with_name(f"{STATE_FILE.name}.corrupt-{stamp}-{uuid.uuid4().hex[:8]}")
    try:
        STATE_FILE.replace(target)
        return target
    except OSError:
        return None


def empty_state(setup_complete: bool = False) -> dict:
    return {
        "metadata": {
            "setup_complete": setup_complete,
            "data_mode": "empty",
            "created_at": now_iso(),
            "updated_at": now_iso(),
        },
        "config": copy.deepcopy(DEFAULT_CONFIG),
        "exposures": [],
        "hedges": [],
        "settlements": [],
        "plans": [],
    }


def add_months(value: dt_date, months: int) -> dt_date:
    month_index = value.month - 1 + months
    return dt_date(value.year + month_index // 12, month_index % 12 + 1, 1)


def month_end(value: dt_date) -> str:
    return (add_months(value, 1) - timedelta(days=1)).isoformat()


def sample_state(keep_plans: list[dict] | None = None, today: dt_date | None = None) -> dict:
    state = copy.deepcopy(DEMO_STATE)
    asof = today or utc_now().date()
    future_due = month_end(add_months(asof, 3))
    settled_due = month_end(add_months(asof, -1))
    trade_date = add_months(asof, -1).replace(day=5).isoformat()
    created_at = now_iso()
    state["metadata"] = {
        "setup_complete": True,
        "data_mode": "sample",
        "created_at": created_at,
        "updated_at": created_at,
    }
    for row in state["exposures"]:
        row["created_at"] = created_at
        row["due_date"] = future_due
    for row in state["hedges"]:
        row["created_at"] = created_at
        row["trade_date"] = asof.isoformat()
        row["due_date"] = future_due
    state["settlements"] = []
    state["exposures"].append({
        "id": "demo-exp-settled",
        "created_at": created_at,
        "due_date": settled_due,
        "currency": "USD",
        "amount": 900000,
        "direction": "receipt",
        "category": "order_contract",
        "description": "上月出口订单样例",
        "probability": 1,
    })
    state["hedges"].append({
        "id": "demo-hedge-settled",
        "created_at": created_at,
        "trade_date": trade_date,
        "due_date": settled_due,
        "currency": "USD",
        "amount": 900000,
        "action": "sell_foreign",
        "locked_rate": 7.18,
        "description": "上月远期结汇样例",
    })
    state["settlements"].append({
        "id": "demo-settle-1",
        "created_at": created_at,
        "due_date": settled_due,
        "currency": "USD",
        "actual_rate": 7.21,
        "actual_amount": 620000,
        "description": "样例到期实际汇率",
    })
    state["config"] = validate_config(state.get("config", {}), base=DEFAULT_CONFIG)
    state["plans"] = copy.deepcopy(keep_plans or [])
    return state


def validate_workspace_state(state: dict) -> dict:
    if not isinstance(state, dict):
        raise ValueError("工作区数据必须是对象")
    normalized = copy.deepcopy(state)
    if not isinstance(normalized.get("metadata", {}), dict):
        raise ValueError("metadata 必须是对象")
    normalized.setdefault("metadata", {})
    normalized["metadata"].setdefault("setup_complete", True)
    normalized["metadata"].setdefault("data_mode", "imported")
    normalized["metadata"].setdefault("created_at", now_iso())
    normalized["metadata"].setdefault("updated_at", normalized["metadata"]["created_at"])
    normalized["config"] = validate_config(normalized.get("config", {}), base=DEFAULT_CONFIG)
    for key in ("exposures", "hedges", "settlements", "plans"):
        value = normalized.get(key, [])
        if not isinstance(value, list):
            raise ValueError(f"{key} 必须是列表")
        normalized[key] = value
    for key in ("exposures", "hedges", "settlements"):
        if any(not isinstance(row, dict) for row in normalized[key]):
            raise ValueError(f"{key} 只能包含对象")
    for row in normalized["exposures"]:
        validate_exposure(row, normalized["config"], allow_legacy_category=True)
    for row in normalized["hedges"]:
        validate_hedge(row, normalized["config"])
    for row in normalized["settlements"]:
        validate_settlement(row, normalized["config"])
    for row in normalized["plans"]:
        validate_plan_snapshot(row)
    return normalized


def append_audit(action: str, collection: str, record_id: str | None, before: dict | None, after: dict | None) -> dict:
    """把一次改动追加进 append-only 日志。

    只追加、不改写：状态文件是全量覆写的，改完就看不出改了什么、改前是什么。
    这条日志是唯一能回答"谁在什么时候把哪条改成了什么"的地方，
    写失败不能影响主流程（宁可少一条日志，也不能让用户存不进数据）。
    """
    entry = {
        "at": now_iso(),
        "action": action,
        "collection": collection,
        "id": record_id,
        "before": before,
        "after": after,
    }
    try:
        AUDIT_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT_LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass
    return entry


# 只读文件尾部这么多字节。日志只追加不截断，每次刷新面板都整份读一遍的话，
# 开销会随历史线性增长；reset 那种条目还会带整个工作区快照，放大得更快。
AUDIT_TAIL_BYTES = 256 * 1024
AUDIT_SCAN_CHUNK_BYTES = 64 * 1024


def _audit_tail_start(handle, size: int) -> int:
    start = max(0, size - AUDIT_TAIL_BYTES)
    if start == 0:
        return 0
    pos = start
    while pos > 0:
        block_start = max(0, pos - AUDIT_SCAN_CHUNK_BYTES)
        handle.seek(block_start)
        block = handle.read(pos - block_start)
        newline = block.rfind(b"\n")
        if newline >= 0:
            return block_start + newline + 1
        pos = block_start
    return 0


def read_audit(limit: int = 50) -> list[dict]:
    if not AUDIT_LOG_FILE.exists():
        return []
    try:
        with AUDIT_LOG_FILE.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            start = _audit_tail_start(handle, size)
            handle.seek(start)
            chunk = handle.read()
    except OSError:
        return []

    rows = _parse_audit_lines(chunk.decode("utf-8", errors="ignore"), truncated=False, limit=limit)
    if not rows and start > 0:
        # 单条记录就可能超过尾部窗口（reset 会把整个工作区连同全部方案
        # 快照塞进 before/after）。这时尾部里一条完整记录都没有，
        # 直接返回空等于整块「变更记录」凭空消失——退回整份读。
        try:
            rows = _parse_audit_lines(
                AUDIT_LOG_FILE.read_text(encoding="utf-8", errors="ignore"),
                truncated=False,
                limit=limit,
            )
        except OSError:
            return []
    return rows[-limit:][::-1]


def _parse_audit_lines(text: str, truncated: bool, limit: int) -> list[dict]:
    lines = text.split("\n")
    if truncated and lines:
        # 从中间截断的第一行多半是半截的。但只在它真的解析不出来时才丢，
        # 不要无条件丢——正好卡在换行边界时会白扔一条完整记录。
        head = lines[0].strip()
        if head:
            try:
                json.loads(head)
            except json.JSONDecodeError:
                lines.pop(0)

    rows: list[dict] = []
    tail = lines[-(limit * 4):] if limit > 0 else lines
    for line in tail:
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            # 单行坏了不能让整段历史读不出来
            continue
    return rows


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
            pairs[currency] = fN(cny / float(rates[currency]), 6)
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
            write_json(RATES_CACHE_FILE, cache)
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


def load_forecast_signals() -> dict:
    if not FORECAST_SIGNALS_FILE.exists():
        return {}
    try:
        return json.loads(FORECAST_SIGNALS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def forecast_rate_for_period(signal: dict | None, period: str | None = None) -> float | None:
    rows = (signal or {}).get("forecast") or []
    if not rows:
        return None
    selected = rows[-1]
    if period:
        matching = [row for row in rows if row.get("month") == period]
        if matching:
            selected = matching[-1]
        elif any(row.get("month") for row in rows):
            return None
    try:
        return float(selected["rate"])
    except (KeyError, TypeError, ValueError):
        return None


def forecast_expected_move(signal: dict | None, period: str | None = None) -> float | None:
    """预测期末相对当前汇率的变动幅度（绝对值）。"""
    if not signal:
        return None
    forecast_rate = forecast_rate_for_period(signal, period)
    current = signal.get("current")
    if forecast_rate is None or not current:
        return None
    try:
        current = float(current)
    except (TypeError, ValueError):
        return None
    if current == 0:
        return None
    return abs(forecast_rate / current - 1)


def forecast_generated_date(signal: dict | None) -> dt_date | None:
    value = (signal or {}).get("generated_at")
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def forecast_is_fresh(signal: dict | None, today: dt_date | None = None) -> bool:
    generated = forecast_generated_date(signal)
    if generated is None:
        return False
    age_days = ((today or utc_now().date()) - generated).days
    return 0 <= age_days <= FORECAST_MAX_AGE_DAYS


def live_forecast_direction(
    signal: dict | None, live_spot: float | None, period: str | None = None,
) -> str | None:
    final = forecast_rate_for_period(signal, period)
    if final is None or live_spot is None:
        direction = (signal or {}).get("direction")
        return direction if direction in {"up", "down"} else None
    try:
        spot = float(live_spot)
    except (TypeError, ValueError):
        return None
    if final > spot:
        return "up"
    if final < spot:
        return "down"
    return "flat"


def forecast_move_from_live(
    signal: dict | None, live_spot: float | None, period: str | None = None,
) -> float | None:
    if live_spot is None:
        return forecast_expected_move(signal, period)
    final = forecast_rate_for_period(signal, period)
    try:
        spot = float(live_spot)
    except (TypeError, ValueError):
        return None
    if final is None or spot == 0:
        return None
    return abs(final / spot - 1)


def signal_covers_period(signal: dict | None, period: str | None) -> bool:
    """信号的预测区间是否覆盖这个到期期间。

    一份 4 月生成、到 10 月结束的预测，对一笔次年 3 月到期的敞口没有任何
    发言权。以前不做这个检查，等于拿一段已经结束的预测去给不相干的
    到期日打折扣。信号说不上话就该退回足额锁——和其余四道闸门一样，只降不升。
    """
    if not signal or not period:
        return True
    months = [row.get("month") for row in (signal.get("forecast") or []) if row.get("month")]
    if not months:
        return True
    return period in months


def forecast_multiplier(
    signal: dict | None,
    net: float,
    period: str | None = None,
    live_spot: float | None = None,
    today: dt_date | None = None,
) -> tuple[float, str | None]:
    if not signal:
        return 1.0, None
    if not forecast_is_fresh(signal, today):
        return 1.0, "预测信号缺少生成时间或已超过 45 天，按目标比例锁汇"
    if not signal_covers_period(signal, period):
        months = [row.get("month") for row in (signal.get("forecast") or []) if row.get("month")]
        span = f"{min(months)}~{max(months)}" if months else "空"
        return 1.0, f"预测区间 {span} 覆盖不到 {period}，这段时间模型说不上话，按目标比例锁汇"
    tier = signal.get("tier")
    direction = live_forecast_direction(signal, live_spot, period)
    unfavorable = (direction == "down") if net > 0 else (direction == "up")
    if tier not in ("support", "caution"):
        return 1.0, "模型质量不达标，忽略预测方向，按目标比例锁汇"
    if direction not in {"up", "down"}:
        return 1.0, "预测终点与当前即期基本持平，按目标比例锁汇"
    if unfavorable:
        if tier == "support":
            return 1.0, "模型质量达标，预测对你不利，按目标比例锁汇"
        return 1.0, "模型质量一般，预测不利，按目标比例锁汇"
    # 信噪比闸门：预测幅度没超过模型自身误差，就不足以支撑少锁
    move = forecast_move_from_live(signal, live_spot, period)
    mape = signal.get("mape")
    if move is not None and mape is not None and move <= mape:
        return 1.0, f"预测有利但幅度 {move:.1%} 未超过模型误差 {mape:.1%}，不足以支撑少锁，按目标比例锁汇"
    if tier == "support":
        return 0.5, "模型质量达标，预测对你有利，降到目标比例的 50%"
    return 0.7, "模型质量一般，预测有利，降到目标比例的 70%"


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


def current_rate(pair_rates: dict[str, float], currency: str) -> float | None:
    value = pair_rates.get(currency)
    return None if value is None else float(value)


def rate_actionability(rates_cache: dict) -> tuple[bool, bool, list[str]]:
    status = rates_cache.get("status")
    if status == "fallback":
        return False, False, ["内置兜底汇率不可作为执行报价"]
    if status == "cached_after_refresh_error":
        reason = "汇率刷新失败，缓存汇率仅可试算"
        if rates_cache.get("last_error"):
            reason = f"{reason}：{rates_cache['last_error']}"
        return False, True, [reason]
    return True, True, []


def trial_reasons_for(config: dict, currency: str, period: str, forward_basis: str) -> list[str]:
    reasons: list[str] = []
    confirmed = config.get("confirmed_parameters") or {}
    if not confirmed.get("rates"):
        reasons.append("执行前请核对当前即期汇率")
    if forward_basis != "quote":
        reasons.append("执行前请向银行确认远期报价")
    if not confirmed.get("interest_rates"):
        reasons.append("使用利率平价试算前请核对资金利率")
    ratios = config.get("month_currency_hedge_ratios") or {}
    if not (ratios.get(f"{period}:{currency}") is not None or
            (isinstance(ratios.get(period), dict) and ratios[period].get(currency) is not None)):
        reasons.append("请确认本月该币种的目标套保比例")
    if not confirmed.get("scenario_shifts"):
        reasons.append("请确认损益场景假设")
    return reasons


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
    """操作方向**只由净敞口决定**。

    以前企业类型会盖掉净敞口：出口型企业的一笔欧元应付也会拿到
    sell_foreign。而卡片文案 suggestion_text 是按净额符号写的，
    于是出现"文案说买入外币、action 字段却是卖出"，点「按建议填入锁汇单」
    填进去的是一笔加仓交易，情景损益里那条腿也算反了。

    企业类型不该覆盖单笔敞口的方向——它顶多说明"我通常是收外币的"，
    改成用来标出异常方向（见 direction_is_unexpected）。
    """
    if net > 0:
        return "sell_foreign"
    if net < 0:
        return "buy_foreign"
    # 没有净敞口时不会产生建议，这里只是给个确定的返回值
    return "buy_foreign" if config.get("enterprise_type") == "import" else "sell_foreign"


def direction_is_unexpected(config: dict, net: float) -> bool:
    """净敞口方向和企业类型对不上——多半是录错了，值得提示一句。

    出口型企业出现净付、进口型出现净收，都不是不可能（比如采购分支），
    但足够反常，应该让人看一眼再确认，而不是被静默改掉方向。
    """
    enterprise_type = config.get("enterprise_type", "comprehensive")
    if enterprise_type == "export":
        return net < 0
    if enterprise_type == "import":
        return net > 0
    return False


def exposure_category_for(exposures: list[dict], period: str, currency: str) -> str:
    score: dict[str, float] = defaultdict(float)
    for row in exposures:
        if period_from_date(row.get("due_date", "")) == period and row.get("currency", "").upper() == currency:
            score[row.get("category", "cash_flow")] += abs(signed_exposure(row))
    if not score:
        return "cash_flow"
    return max(score.items(), key=lambda item: item[1])[0]


def suggest_category(row: dict) -> tuple[str, str]:
    """从已有字段推荐风险类型，并给出理由。

**让财务自己在下拉框里选，他多半选不对**——不是因为不认真，
    而是这个分类本来就该由凭证形态（是否已入账、金额是否确定）推出来，
    不该靠人记会计准则。选错的代价是挂错科目，而且不会报错。

    这里不猜、也不自动改，只按会计口径给一个推荐 + 理由，用户可以覆盖；
    覆盖了就在页面上标出来，让分歧看得见。
    """
    if row.get("booked"):
        return "balance_sheet", "已入账/已开票的外币资产负债，属于资产负债表套保"
    probability = row.get("probability")
    try:
        probability = 1.0 if probability is None else float(probability)
    except (TypeError, ValueError):
        probability = 1.0
    if probability >= 1:
        return "order_contract", "金额已确定但尚未入账，属于合同/订单套保"
    # 不用 f"{p:.0%}"：那是 round-half-even，和 JS 的 Math.round（half-up）
    # 在 0.125 / 0.005 这类值上会给出不同的百分比，对拍会假过。
    percent = int(q(probability * 100, 0))
    return "cash_flow", f"发生概率 {percent}%，属于高度可能的预期交易，走现金流套保"


def known_category(category: str | None) -> bool:
    return category in EXPOSURE_CATEGORIES


def accounting_bucket(category: str, trade_date: str, due_date: str) -> str:
    same_month = period_from_date(trade_date) == period_from_date(due_date)
    if category in {"balance_sheet", "order_contract"} and same_month:
        return "derivative_investment_income"
    if category == "cash_flow" and same_month:
        return "realized_exchange_gain_loss"
    return "fair_value_change_gain_loss"


def scenario_shifts_for(config: dict) -> dict[str, float]:
    return {
        "neutral": 0.0,
        "optimistic": float(config.get("optimistic_shift_pct", 0.03)),
        "pessimistic": float(config.get("pessimistic_shift_pct", -0.03)),
        "custom": float(config.get("custom_scenario_shift_pct", 0.01)),
    }


def shift_for(config: dict, name: str, currency: str) -> float:
    """某币种在某情景下的涨跌幅。没单独配就用全局值。"""
    default = scenario_shifts_for(config)[name]
    per_currency = (config.get("scenario_shifts") or {}).get(currency)
    if isinstance(per_currency, dict) and per_currency.get(name) is not None:
        try:
            return float(per_currency[name])
        except (TypeError, ValueError):
            return default
    return default


def scenario_rates_for(pair_rates: dict[str, float], config: dict) -> dict[str, dict[str, float]]:
    return {
        name: {
            currency: fN(float(rate) * (1 + shift_for(config, name, currency)), 6)
            for currency, rate in pair_rates.items()
        }
        for name in scenario_shifts_for(config)
    }


def scenario_is_uniform(config: dict, currencies: list[str]) -> bool:
    """所有币种是不是同一套涨跌幅——是的话就等于假设相关性为 1，要在页面上说清楚。"""
    for name in scenario_shifts_for(config):
        values = {shift_for(config, name, currency) for currency in currencies}
        if len(values) > 1:
            return False
    return True


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
        # 这几个数会出现在报表上、也是对账口径，所以用 Decimal 算，
        # 见 money.py 里的范围说明。
        move = D(scenario_rate) - D(current)
        exposure_pnl = D(net) * move
        hedge_pnl = D(signed_recommendation(recommended["action"], recommended["recommended_amount"])) * (
            D(scenario_rate) - D(recommended["trade_rate"])
        )
        rows[name] = {
            "period": period,
            "currency": currency,
            "scenario_rate": scenario_rate,
            "unrealized_exchange_gain_loss": f2(exposure_pnl),
            recommended["accounting_bucket"]: f2(hedge_pnl),
            "total_projected_gain_loss": f2(exposure_pnl + hedge_pnl),
        }
    return rows


def build_dashboard(
    state: dict,
    rates_cache: dict,
    forecast_doc: dict | None = None,
    today: dt_date | None = None,
) -> dict:
    pair_rates = rates_cache.get("pair_rates", {})
    exposures = state.get("exposures", [])
    hedges = state.get("hedges", [])
    settlements = state.get("settlements", [])
    config = merged_config(state)
    if forecast_doc is None:
        forecast_doc = load_forecast_signals()
    forecast_signals = forecast_doc.get("signals", {}) if forecast_doc else {}

    exposure_totals = aggregate_rows(exposures, signed_exposure)
    hedge_totals = aggregate_rows(hedges, signed_hedge)
    keys = sorted(set(exposure_totals) | set(hedge_totals))

    net_rows = []
    suggestions = []
    scenario_rows = []
    scenario_rates = scenario_rates_for(pair_rates, config)
    rates_actionable, rates_can_show_trials, rate_trial_reasons = rate_actionability(rates_cache)
    scenario_summary: dict[str, dict[str, dict]] = {}
    risk_limit = float(config.get("risk_limit_cny", 0) or 0)
    for period, currency in keys:
        gross = exposure_totals.get((period, currency), 0.0)
        hedged = hedge_totals.get((period, currency), 0.0)
        net = gross + hedged
        rate = current_rate(pair_rates, currency)
        rate_available = rate is not None
        cny_risk = abs(net * rate) if rate_available else None
        target_ratio = hedge_ratio_for(config, period, currency)
        category = exposure_category_for(exposures, period, currency)
        # 远期结汇不是按即期价成交的，交易价要用远期价
        # 建议单据的到期日 = 该期间月末。远期定价、会计科目判断、
        # 前端预填的锁汇单必须全部用这同一个日期，否则定价的合约
        # 和实际下的合约不是一张。
        due_date = forwards.period_end_iso(period)
        fwd = forwards.forward_rate(rate, currency, period, config, today=today) if rate_available else None
        trade_rate = fN(fwd["rate"], 6) if fwd else None
        # 到期日已过的敞口还留在建议里，说明它没被处理掉——不自动删，但要标出来
        past_due = bool(fwd and fwd["tenor_years"] <= 0)
        net_rows.append(
            {
                "period": period,
                "currency": currency,
                "risk_category": category,
                "risk_category_known": known_category(category),
                "past_due": past_due,
                "direction_unexpected": direction_is_unexpected(config, net),
                "target_hedge_ratio": target_ratio,
                "business_exposure": f2(gross),
                "locked_exposure": f2(hedged),
                "net_exposure": f2(net),
                "current_rate": rate,
                "rate_available": rate_available,
                "cny_risk": f2(cny_risk) if cny_risk is not None else None,
                # 仅作提示：阈值不参与建议金额的计算，见 README「风险阈值」一节
                "over_risk_limit": bool(rate_available and risk_limit > 0 and cny_risk > risk_limit),
            }
        )
        if not rate_available or not rates_can_show_trials:
            continue

        signal = forecast_signals.get(currency)
        if signal and forecast_doc.get("generated_at") and not signal.get("generated_at"):
            signal = {**signal, "generated_at": forecast_doc["generated_at"]}
        multiplier, multiplier_reason = forecast_multiplier(signal, net, period, live_spot=rate, today=today)
        effective_ratio = target_ratio * multiplier
        target_cover = D(abs(gross)) * D(effective_ratio)
        covered = D(abs(hedged))
        recommended_amount = float(max(D(0), target_cover - covered))
        action = action_for(config, net)
        bucket = accounting_bucket(category, now_iso()[:10], due_date)
        # 远期结汇不是按即期价成交的，交易价要用远期价

        # 情景损益对每一行净敞口都算：建议金额为 0 时套保腿为 0，
        # 但敞口本身的浮动损益依然存在，不能整块消失。
        projection = scenario_projection(
            period,
            currency,
            net,
            {
                "action": action,
                "recommended_amount": recommended_amount,
                "trade_rate": trade_rate,
                "accounting_bucket": bucket,
            },
            rate,
            scenario_rates,
        )
        if abs(net) > 0 or recommended_amount > 0:
            scenario_summary[f"{period}:{currency}"] = projection
            scenario_rows.append(
                {
                    "period": period,
                    "currency": currency,
                    "net_exposure": f2(net),
                    "recommended_amount": f2(recommended_amount),
                    "has_recommendation": recommended_amount > 0,
                    "accounting_bucket": bucket,
                    "bucket_uncertain": not known_category(category),
                    "projection": projection,
                }
            )

        if abs(net) > 0 and recommended_amount > 0:
            trial_reasons = rate_trial_reasons + trial_reasons_for(config, currency, period, fwd["basis"])
            recommendation = {
                "period": period,
                "currency": currency,
                "risk_category": category,
                "risk_category_known": known_category(category),
                "net_exposure": f2(net),
                "business_exposure": f2(gross),
                "covered_exposure": f2(covered),
                "current_rate": rate,
                "trade_rate": trade_rate,
                "spot_rate": rate,
                "forward_rate": trade_rate,
                "forward_points": fN(fwd["points"], 6),
                "forward_basis": fwd["basis"],
                "due_date": due_date,
                "past_due": past_due,
                "forward_note": fwd["note"],
                "tenor_years": fN(fwd["tenor_years"], 4),
                "risk_cny": f2(cny_risk),
                "target_hedge_ratio": target_ratio,
                "effective_hedge_ratio": fN(effective_ratio, 4),
                "forecast_multiplier": fN(multiplier, 4),
                "forecast_reason": multiplier_reason,
                "forecast_signal": signal,
                "recommended_amount": f2(recommended_amount),
                "action": action,
                "direction_unexpected": direction_is_unexpected(config, net),
                "accounting_bucket": bucket,
                "trial": bool(trial_reasons),
                "trial_reasons": trial_reasons,
                "plain_text": suggestion_text(currency, net, effective_ratio, recommended_amount, action),
                "scenario_projection": projection,
            }
            suggestions.append(recommendation)

    backtest_rows = build_backtest(exposures, hedges, settlements, pair_rates, state)
    scenario_totals = aggregate_scenarios(scenario_rows, scenario_rates)
    portfolio = build_portfolio(net_rows, suggestions)
    plan_list = state.get("plans", [])
    latest_plan = plan_list[-1] if plan_list else None
    plan_drift = plans.drift(
        latest_plan,
        config,
        pair_rates,
        forecast_signals,
        current_suggestions=suggestions,
    )
    return {
        "workspace": {
            "metadata": state.get("metadata", {}),
            "data_mode": (state.get("metadata") or {}).get("data_mode", "unknown"),
            "setup_complete": bool((state.get("metadata") or {}).get("setup_complete", False)),
            "data_file": str(STATE_FILE),
            "backup_count": len(list_backups()),
        },
        "config": config,
        "rates": rates_cache,
        "rates_actionable": rates_actionable,
        "rate_trial_reasons": rate_trial_reasons,
        "exposures": exposures,
        "hedges": hedges,
        "settlements": settlements,
        "net_exposures": net_rows,
        "portfolio": portfolio,
        "suggestions": suggestions,
        "scenario_rates": scenario_rates,
        "scenario_uniform": scenario_is_uniform(config, sorted({row["currency"] for row in net_rows})),
        "scenario_rows": scenario_rows,
        "scenario_totals": scenario_totals,
        "scenario_summary": scenario_summary,
        "backtest": backtest_rows,
        "audit": read_audit(30),
        "plans": plan_list[-10:][::-1],
        "plan_drift": plan_drift,
        "forecast": forecast_doc,
        "plain_language": build_plain_language(
            net_rows, suggestions, backtest_rows, rates_cache, scenario_totals
        ),
    }


def suggestion_text(currency: str, net: float, ratio: float, amount: float, action: str) -> str:
    if net > 0:
        exposure_side = f"未来净收 {currency}"
        action_text = "卖出外币/远期结汇"
    else:
        exposure_side = f"未来净付 {currency}"
        action_text = "买入外币/远期购汇"
    return f"{exposure_side}，建议先锁 {ratio:.0%}，即 {amount:,.2f} {currency}，操作方向：{action_text}。"


def build_portfolio(net_rows: list[dict], suggestions: list[dict]) -> dict:
    """驾驶舱口径：把各行折成人民币后汇总。

    **各币种取绝对值后相加，不同币种之间不互相抵消**——USD 净收和 EUR 净付
    是两个独立的风险，凑在一起算净额会把风险做小。同一币种同一期间的收付
    已经在 net_exposures 里抵过了。
    """
    recommended_by_key: dict[tuple[str, str], float] = defaultdict(float)
    for item in suggestions:
        key = (item["period"], item["currency"])
        recommended_by_key[key] += abs(float(item.get("recommended_amount", 0.0)))

    by_currency: dict[str, dict] = {}
    missing_rate: list[str] = []
    for row in net_rows:
        currency = row["currency"]
        bucket = by_currency.setdefault(
            currency,
            {
                "currency": currency,
                "gross_cny": 0.0,
                "locked_cny": 0.0,
                "net_cny": 0.0,
                "added_risk_cny": 0.0,
                "recommended_cny": 0.0,
                "periods": 0,
            },
        )
        bucket["periods"] += 1
        if not row.get("rate_available"):
            if currency not in missing_rate:
                missing_rate.append(currency)
            continue
        rate = float(row["current_rate"])
        gross = float(row["business_exposure"])
        locked = float(row["locked_exposure"])
        net = float(row["net_exposure"])
        # 覆盖只算方向相反、且不超过敞口本身的那部分。
        offset = min(abs(locked), abs(gross)) if gross * locked < 0 else 0.0
        # 剩余风险直接取净敞口的绝对值。**不能**写成 |敞口| − 覆盖：
        # 同向的"锁汇"是加仓（100 收 + 50 买入 = 净敞口 150）、
        # 超额套保是反向裸头寸（100 收 + 150 卖出 = 净空 50），
        # 两种情况下剩余风险都比敞口大。为了保住
        # 「已锁 + 剩余 = 业务敞口」这个等式而把它们抹掉，等于低估风险。
        # 等式只在 0 ≤ 覆盖 ≤ 敞口 时成立，这一点写在页面说明里。
        bucket["gross_cny"] += abs(gross) * rate
        bucket["locked_cny"] += offset * rate
        bucket["net_cny"] += abs(net) * rate
        bucket["added_risk_cny"] += max(0.0, abs(net) - (abs(gross) - offset)) * rate
        bucket["recommended_cny"] += recommended_by_key.get((row["period"], currency), 0.0) * rate

    rows = []
    for bucket in sorted(by_currency.values(), key=lambda item: -item["gross_cny"]):
        gross = bucket["gross_cny"]
        rows.append(
            {
                **{key: f2(value) for key, value in bucket.items() if key != "currency"},
                "currency": bucket["currency"],
                "locked_ratio": fN(bucket["locked_cny"] / gross, 4) if gross else 0.0,
            }
        )

    gross_total = sum(row["gross_cny"] for row in rows)
    locked_total = sum(row["locked_cny"] for row in rows)
    # 两个口径都要给：
    # 绝对值口径答"风险量级有多大"（不同币种不互相抵消）；
    # 净额口径答"在所有币种同向变动这个假设下，真正还敞着的净额是多少"。
    # 后者就是常说的"不同币种间的天然对冲"，但它成立的前提
    # 恰恰是那个相关性为 1 的假设，所以只能并列、不能取代。
    # 抵消只能发生在**同一期间内的不同币种之间**。
    # 跨期间相加是错的：六月净收美元、七月净付美元，六月那天你照样全额敞着，
    # 那是期限错配不是天然对冲。跨期间只能把各期间的净额取绝对值再加。
    by_period: dict[str, float] = defaultdict(float)
    for row in net_rows:
        if row.get("rate_available"):
            by_period[row["period"]] += float(row["net_exposure"]) * float(row["current_rate"])
    signed_net = sum(abs(value) for value in by_period.values())
    return {
        "gross_exposure_cny": f2(gross_total),
        "locked_cny": f2(locked_total),
        "net_exposure_cny": f2(sum(row["net_cny"] for row in rows)),
        # 同向持仓与超额套保带来的、超出"敞口未覆盖部分"的那块风险
        "added_risk_cny": f2(sum(row["added_risk_cny"] for row in rows)),
        "net_after_offset_cny": f2(signed_net),
        "natural_offset_cny": f2(max(0.0, sum(row["net_cny"] for row in rows) - signed_net)),
        "recommended_cny": f2(sum(row["recommended_cny"] for row in rows)),
        "locked_ratio": fN(locked_total / gross_total, 4) if gross_total else 0.0,
        "currency_count": len(rows),
        "period_count": len({row["period"] for row in net_rows}),
        "leg_count": len(net_rows),
        "pending_count": len(suggestions),
        "rate_missing": missing_rate,
        "by_currency": rows,
    }


def aggregate_scenarios(
    scenario_rows: list[dict], scenario_rates: dict[str, dict[str, float]]
) -> dict[str, dict]:
    """把逐个（期间 × 币种）的情景损益汇总成组合层面的一张表。

    各币种的损益本来就已经折成人民币（金额 × 汇率变动），可以直接相加。
    不同到期日的金额也直接相加，不做贴现——这是名义口径，不是现值。
    """
    totals: dict[str, dict] = {}
    for name in scenario_rates:
        bucket_totals: dict[str, float] = defaultdict(float)
        exposure_pnl = 0.0
        hedge_pnl = 0.0
        for entry in scenario_rows:
            row = entry["projection"].get(name)
            if not row:
                continue
            bucket = entry["accounting_bucket"]
            leg = float(row.get(bucket, 0.0))
            exposure_pnl += float(row.get("unrealized_exchange_gain_loss", 0.0))
            hedge_pnl += leg
            bucket_totals[bucket] += leg
        totals[name] = {
            "unrealized_exchange_gain_loss": f2(exposure_pnl),
            "hedge_pnl": f2(hedge_pnl),
            "total_projected_gain_loss": f2(exposure_pnl + hedge_pnl),
            "by_bucket": {key: f2(value) for key, value in sorted(bucket_totals.items())},
            "leg_count": len(scenario_rows),
        }
    return totals


def build_backtest(
    exposures: list[dict],
    hedges: list[dict],
    settlements: list[dict],
    pair_rates: dict[str, float],
    state: dict | None = None,
) -> list[dict]:
    # 同一个期间/币种可能录了多条结算记录。以前汇率取最后一条、实际发生额
    # 取"最后一条填了金额的"，两个数可能来自不同记录，拼出来的分解没有意义。
    # 先选定唯一一条（最后录入的那条），汇率和金额都从它上面取。
    settlement_by_key: dict[tuple[str, str], dict] = {}
    for row in settlements:
        key = (period_from_date(row.get("due_date", "")), row.get("currency", "").upper())
        settlement_by_key[key] = row

    actual_by_key = {
        key: float(row.get("actual_rate", 0)) for key, row in settlement_by_key.items()
    }
    # 计划敞口在这里要用**名义金额**，不能用概率加权后的值。
    # signed_exposure 是 amount × probability（套保规模按期望值定是对的），
    # 但用户填的 actual_amount 是真实结算金额。两者直接相减的话，
    # 一笔概率 60% 的 100 万订单全额兑现，会被报成"多发生 40 万"的量差收益——
    # 那是凭空造出来的。
    # 必须**带方向**净额，不能把收和付的绝对值相加：gross_signed 是净额，
    # 拿一个毛额去和它比会凭空造出量差——一个月收 100 万、付 40 万，
    # 净敞口 60 万，若计划侧记成 140 万，实际结算 60 万就会被报成
    # "订单缩水 80 万"，在一个专门用来避免误判缩水的模块里。
    nominal_signed: dict[tuple[str, str], float] = defaultdict(float)
    for row in exposures:
        key = (period_from_date(row.get("due_date", "")), row.get("currency", "").upper())
        amount = abs(float(row.get("amount", 0) or 0))
        direction = row.get("direction")
        if direction in {"receipt", "asset", "export"}:
            nominal_signed[key] += amount
        elif direction in {"payment", "liability", "import"}:
            nominal_signed[key] -= amount
    nominal_by_key = {key: abs(value) for key, value in nominal_signed.items()}

    actual_amount_by_key = {
        key: float(row["actual_amount"])
        for key, row in settlement_by_key.items()
        if row.get("actual_amount") is not None
    }
    exposure_totals = aggregate_rows(exposures, signed_exposure)
    hedge_totals = aggregate_rows(hedges, signed_hedge)
    locked_by_key: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for hedge in hedges:
        locked_by_key[(period_from_date(hedge.get("due_date", "")), hedge.get("currency", "").upper())].append(hedge)

    rows = []
    for key in sorted(set(exposure_totals) | set(hedge_totals) | set(actual_by_key)):
        period, currency = key
        market_rate = current_rate(pair_rates, currency)
        settled_rate = actual_by_key.get(key)
        settled = settled_rate is not None
        if market_rate is None and not settled:
            continue
        # 没录到期实际汇率时用当前市场价试算，但必须标出来，
        # 否则这一行看不出是已结算还是估的。
        actual_rate = settled_rate if settled else market_rate
        gross = exposure_totals.get(key, 0.0)
        hedge_effect = D(0)
        locked_detail = []
        for hedge in locked_by_key.get(key, []):
            locked_rate = float(hedge.get("locked_rate", 0))
            signed = signed_hedge(hedge)
            effect = D(signed) * (D(actual_rate) - D(locked_rate))
            hedge_effect += effect
            locked_detail.append(
                {
                    "amount": float(hedge.get("amount", 0)),
                    "action": hedge.get("action"),
                    "locked_rate": locked_rate,
                    "effect_cny": f2(effect),
                }
            )
        # 司库口径：结汇均价 vs 当月月均汇率。和上面那个"锁汇贡献"不是一回事——
        # 那个比的是到期即期价（交易员视角），这个比的是财务记账用的月均（司库视角）。
        average_rate, average_source = benchmarks.monthly_average(state or {}, period, currency)
        actual_notional = actual_amount_by_key.get(key)
        benchmark = benchmarks.benchmark_row(
            period, currency, gross, locked_by_key.get(key, []), actual_rate,
            average_rate, average_source, actual_notional=actual_notional,
        )
        variance_row = None
        if benchmark:
            variance_row = variance.decompose(
                planned_notional=nominal_by_key.get(key, benchmark["notional"]),
                actual_notional=actual_notional,
                realized_avg_rate=benchmark["realized_avg_rate"],
                benchmark_rate=benchmark["average_rate"],
                hedged_notional=benchmark["offsetting_total"],
                gross_signed=gross,
            )
            if variance_row:
                variance_row = {
                    **variance_row,
                    "planned_notional": f2(variance_row["planned_notional"]),
                    "actual_notional": f2(variance_row["actual_notional"]),
                    "volume_gap": f2(variance_row["volume_gap"]),
                    "volume_gap_pct": fN(variance_row["volume_gap_pct"], 4),
                    "volume_variance_cny": f2(variance_row["volume_variance_cny"]),
                    "price_variance_cny": f2(variance_row["price_variance_cny"]),
                    "total_variance_cny": f2(variance_row["total_variance_cny"]),
                    "over_hedged_notional": f2(variance_row["over_hedged_notional"]),
                }
        if benchmark:
            benchmark = {
                **benchmark,
                "variance": variance_row,
                # 未结算时 actual_rate 是拿当前市场价试算的，
                # 由它推出来的结汇均价和归因同样是试算值，不能当结算结果读
                "settled": settled,
                "notional": f2(benchmark["notional"]),
                "hedged_notional": f2(benchmark["hedged_notional"]),
                "offsetting_total": f2(benchmark["offsetting_total"]),
                "hedge_coverage": fN(benchmark["hedge_coverage"], 4),
                "realized_avg_rate": fN(benchmark["realized_avg_rate"], 6),
                "average_rate": fN(benchmark["average_rate"], 6),
                "hedge_effect_cny": f2(benchmark["hedge_effect_cny"]),
                "timing_effect_cny": f2(benchmark["timing_effect_cny"]),
                "vs_benchmark_cny": f2(benchmark["vs_benchmark_cny"]),
            }

        rows.append(
            {
                "period": period,
                "currency": currency,
                "benchmark": benchmark,
                "business_exposure": f2(gross),
                "actual_rate": fN(actual_rate, 6),
                "reference_rate": None if market_rate is None else fN(market_rate, 6),
                "settled": settled,
                "rate_basis": "settlement" if settled else "market_estimate",
                "hedge_effect_cny": f2(hedge_effect),
                "locked_detail": locked_detail,
                "plain_text": (
                    f"{period} {currency}: 实际汇率 {actual_rate:.6f}，"
                    f"{'当前市场参考汇率缺失' if market_rate is None else f'参考汇率 {market_rate:.6f}'}，"
                    f"锁汇贡献 {hedge_effect:,.2f} CNY。"
                    if settled
                    else f"{period} {currency}: 尚未录入到期实际汇率，暂按当前市场价 {market_rate:.6f} 试算，"
                    f"锁汇贡献 {hedge_effect:,.2f} CNY（试算值，非结算结果）。"
                ),
            }
        )
    return rows


def build_plain_language(
    net_rows: list[dict],
    suggestions: list[dict],
    backtest_rows: list[dict],
    rates_cache: dict,
    scenario_totals: dict[str, dict] | None = None,
) -> list[str]:
    lines = [
        "这套本地工具按五步跑：先录入外币收付款，再汇总净敞口，再给锁汇建议，再记录实际锁汇，最后用实际汇率回头检查收益。",
        f"当前汇率来源：{rates_cache.get('source')}；状态：{rates_cache.get('status')}；更新时间：{rates_cache.get('fetched_at')}。",
    ]
    if not net_rows:
        lines.append("还没有敞口。先添加一笔未来外币收款或付款。")
    for row in net_rows:
        if not row.get("rate_available"):
            lines.append(
                f"{row['period']} {row['currency']} 暂无汇率，已暂停人民币风险、远期价格和锁汇建议；请先录入或刷新该币种汇率。"
            )
            continue
        side = "净收" if row["net_exposure"] > 0 else "净付"
        lines.append(
            f"{row['period']} {row['currency']} 当前{side} {abs(row['net_exposure']):,.2f}，"
            f"折人民币风险约 {row['cny_risk']:,.2f}。"
        )
    for item in suggestions:
        lines.append(item["plain_text"])
    worst = scenario_totals.get("pessimistic") if scenario_totals else None
    best = scenario_totals.get("optimistic") if scenario_totals else None
    if worst and best:
        lines.append(
            f"把所有敞口和建议锁汇合在一起看：悲观场景合计 {worst['total_projected_gain_loss']:,.2f} CNY，"
            f"乐观场景合计 {best['total_projected_gain_loss']:,.2f} CNY，两端相差 "
            f"{abs(best['total_projected_gain_loss'] - worst['total_projected_gain_loss']):,.2f} CNY。"
        )
    if backtest_rows:
        lines.append("回测不是预测，它只是回答：如果按已记录锁汇执行，到期后相对实际汇率贡献了多少人民币。")
        # 只统计真结算过的期间。未结算的行是拿当前市场价试算的，
        # 把它算进"实际结汇均价 vs 记账基准"的合计里，等于用还没发生的事
        # 去改一个号称是已实现结果的数。
        benched = [row for row in backtest_rows if row.get("benchmark") and row.get("settled")]
        pending_bench = [row for row in backtest_rows if row.get("benchmark") and not row.get("settled")]
        if benched:
            total = sum(row["benchmark"]["vs_benchmark_cny"] for row in benched)
            side = "好于" if total >= 0 else "差于"
            lines.append(
                f"按司库口径再看一遍：已结算的 {len(benched)} 个期间，结汇均价合计{side}当月月均汇率 "
                f"{abs(total):,.2f} CNY。这才是财务考核用的比法。"
            )
        if pending_bench:
            names = "、".join(f"{row['period']} {row['currency']}" for row in pending_bench)
            lines.append(f"{names} 还没结算，它们的司库口径是试算值，没有计入上面的合计。")
        pending = [row for row in backtest_rows if not row.get("settled")]
        if pending:
            names = "、".join(f"{row['period']} {row['currency']}" for row in pending)
            lines.append(f"其中 {names} 还没录入到期实际汇率，用当前市场价试算，数字会随行情变动。")
    return lines


def request_body_limit(path: str) -> int:
    if urlparse(path).path in {"/api/import", "/api/workspace/import", "/api/xlsx/import"}:
        return MAX_IMPORT_REQUEST_BODY_BYTES
    return MAX_REQUEST_BODY_BYTES


def parse_body(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length", "0"))
    if length <= 0:
        return {}
    limit = request_body_limit(handler.path)
    if length > limit:
        raise ValueError(f"请求体不能超过 {limit // (1024 * 1024)} MB")
    content_type = handler.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise TypeError("POST/PUT 请求必须使用 application/json")
    raw = handler.rfile.read(length).decode("utf-8")
    return json.loads(raw) if raw else {}


def close_rejected_request(handler: BaseHTTPRequestHandler) -> None:
    """Bound any drain so rejected slow clients cannot pin a handler thread."""
    handler.close_connection = True
    try:
        length = int(handler.headers.get("Content-Length", "0"))
    except ValueError:
        return
    if length <= 0:
        return
    previous_timeout = handler.connection.gettimeout()
    try:
        handler.connection.settimeout(REJECTED_BODY_DRAIN_TIMEOUT_SECONDS)
        handler.rfile.read(min(length, REJECTED_BODY_DRAIN_BYTES))
    except (OSError, TimeoutError):
        pass
    finally:
        try:
            handler.connection.settimeout(previous_timeout)
        except OSError:
            pass


def add_id(row: dict, preserve_existing: bool = True) -> dict:
    row = dict(row)
    if not preserve_existing:
        row.pop("id", None)
        row.pop("created_at", None)
    row["id"] = row.get("id") or uuid.uuid4().hex[:12]
    row["created_at"] = row.get("created_at") or now_iso()
    if "currency" in row:
        row["currency"] = row["currency"].upper()
    return row


def mutable_collection(name: str) -> str | None:
    return {
        "exposures": "exposures",
        "hedges": "hedges",
        "settlements": "settlements",
        "plans": "plans",
    }.get(name)


def web_static_path(request_path: str) -> Path | None:
    if not request_path.startswith("/web/"):
        return None
    relative = unquote(request_path[len("/web/"):])
    candidate = (WEB_ROOT / relative).resolve()
    try:
        candidate.relative_to(WEB_ROOT.resolve())
    except ValueError:
        return None
    return candidate


def validate_for_collection(collection: str, row: dict, config: dict | None = None) -> None:
    if collection == "exposures":
        validate_exposure(row, config)
        return
    if collection == "hedges":
        validate_hedge(row, config)
        return
    if collection == "settlements":
        validate_settlement(row, config)
        return
    if collection == "plans":
        return
    raise ValueError("unknown collection")


# 方案是 plans.freeze 冻出来的决策快照，前端会把这些字段直接渲染进表格。
# 导入的 JSON 可以被人手改过，所以每个会渲染的字段都要卡住类型：
# 数字字段只能是数字，文本字段只能是字符串，否则一段 HTML 就能存进去。
PLAN_TEXT_FIELDS = ("id", "label", "created_at")
PLAN_ROW_TEXT_FIELDS = ("period", "action", "forecast_reason", "forward_basis", "accounting_bucket")
PLAN_ROW_NUMBER_FIELDS = (
    "business_exposure", "covered_exposure", "net_exposure", "target_hedge_ratio",
    "forecast_multiplier", "effective_hedge_ratio", "recommended_amount", "spot_rate", "trade_rate",
)


def _optional_text(value: object, field: str) -> None:
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{field} 必须是字符串")


def _optional_finite(value: object, field: str) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{field} 必须是数字")


def validate_plan_snapshot(row: object) -> None:
    if not isinstance(row, dict):
        raise ValueError("plans 中的每份方案必须是对象")
    for field in PLAN_TEXT_FIELDS:
        _optional_text(row.get(field), f"方案 {field}")
    if "config" in row and not isinstance(row.get("config"), dict):
        raise ValueError("方案 config 必须是对象")
    if "rate_snapshot" in row and not isinstance(row.get("rate_snapshot"), dict):
        raise ValueError("方案 rate_snapshot 必须是对象")
    rate_snapshot = row.get("rate_snapshot") or {}
    _optional_text(rate_snapshot.get("status"), "方案 rate_snapshot.status")
    if not isinstance(rate_snapshot.get("pair_rates", {}), dict):
        raise ValueError("方案 pair_rates 必须是对象")
    for currency, value in rate_snapshot.get("pair_rates", {}).items():
        validate_currency_code(currency, "方案 pair_rates")
        if finite_number(value, f"方案 pair_rates.{currency}") == 0:
            raise ValueError(f"方案 pair_rates.{currency} 不能为 0")
    rows = row.get("rows", [])
    if not isinstance(rows, list):
        raise ValueError("方案 rows 必须是列表")
    for item in rows:
        if not isinstance(item, dict):
            raise ValueError("方案 rows 只能包含对象")
        if not isinstance(item.get("currency"), str):
            raise ValueError("方案 rows.currency 必须是字符串")
        for field in PLAN_ROW_TEXT_FIELDS:
            _optional_text(item.get(field), f"方案 rows.{field}")
        for field in PLAN_ROW_NUMBER_FIELDS:
            _optional_finite(item.get(field), f"方案 rows.{field}")


def same_origin(handler: BaseHTTPRequestHandler, value: str) -> bool:
    parsed = urlparse(value)
    host = handler.headers.get("Host", "")
    return parsed.scheme in {"http", "https"} and parsed.netloc == host


def check_mutation_headers(handler: BaseHTTPRequestHandler) -> tuple[int, str] | None:
    if handler.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
        return 403, "已拒绝跨站写入请求"
    origin = handler.headers.get("Origin")
    if origin and not same_origin(handler, origin):
        return 403, "请求来源（Origin）与当前工作台不同源"
    referer = handler.headers.get("Referer")
    if referer and not same_origin(handler, referer):
        return 403, "请求页面与当前工作台不同源"
    content_type = handler.headers.get("Content-Type", "")
    if handler.command in {"POST", "PUT"} and not content_type.lower().startswith("application/json"):
        return 415, "请求内容必须是 application/json"
    return None


def restore_backup(name: str | None = None) -> dict:
    directory = backup_dir().resolve()
    if name is None:
        latest = latest_undo_backup_file()
        if latest is None:
            raise FileNotFoundError("没有可恢复的备份")
        name = latest.name
    candidate = (directory / name).resolve()
    if candidate.parent != directory or not candidate.name.endswith(".json"):
        raise ValueError("备份名称不合法")
    if not candidate.exists():
        raise FileNotFoundError("备份不存在")
    state = validate_workspace_state(json.loads(candidate.read_text(encoding="utf-8")))
    save_state(state, reason="restore-backup")
    return state


CSV_FIELDS = {
    "exposures": [
        "id", "created_at", "due_date", "currency", "amount", "direction", "probability",
        "booked", "category", "description",
    ],
    "hedges": [
        "id", "created_at", "trade_date", "due_date", "currency", "action", "amount",
        "locked_rate", "description",
    ],
    "settlements": [
        "id", "created_at", "due_date", "currency", "actual_rate", "actual_amount", "description",
    ],
}


def csv_collection(name: str | None) -> str:
    if name in CSV_FIELDS:
        return name
    raise ValueError("collection 必须是 exposures、hedges 或 settlements")


def csv_safe(value) -> str:
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(("=", "+", "-", "@")) else text


def export_collection_csv(state: dict, collection: str) -> str:
    fields = CSV_FIELDS[collection]
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for row in state.get(collection, []):
        writer.writerow({field: csv_safe(row.get(field, "")) for field in fields})
    return out.getvalue()


def parse_collection_csv(collection: str, text: str, config: dict) -> list[dict]:
    reader = csv.DictReader(io.StringIO(text or ""))
    if not reader.fieldnames:
        raise ValueError("CSV 不能为空")
    required = {
        "exposures": {"due_date", "currency", "amount", "direction"},
        "hedges": {"trade_date", "due_date", "currency", "amount", "action", "locked_rate"},
        "settlements": {"due_date", "currency", "actual_rate"},
    }[collection]
    missing = required - set(reader.fieldnames)
    if missing:
        raise ValueError("CSV 缺少字段: " + ", ".join(sorted(missing)))
    rows = []
    for line_no, raw in enumerate(reader, start=1):
        row = {key: value for key, value in raw.items() if key and value not in {None, ""}}
        try:
            validate_for_collection(collection, row, config)
        except ValueError as exc:
            raise ValueError(f"第 {line_no} 行校验失败: {exc}") from None
        rows.append(add_id(row, preserve_existing=False))
    if not rows:
        raise ValueError("CSV 没有可导入的数据行")
    return rows


XLSX_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
XLSX_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
XLSX_PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


def xlsx_col_name(index: int) -> str:
    name = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


def xlsx_col_index(ref: str) -> int:
    total = 0
    for char in ref:
        if not char.isalpha():
            break
        total = total * 26 + ord(char.upper()) - 64
    return max(total - 1, 0)


def build_xlsx(collection: str, state: dict) -> bytes:
    fields = CSV_FIELDS[collection]
    rows = [fields] + [
        [csv_safe(row.get(field, "")) for field in fields]
        for row in state.get(collection, [])
    ]
    sheet_rows = []
    for row_index, row in enumerate(rows, start=1):
        cells = []
        for col_index, value in enumerate(row, start=1):
            ref = f"{xlsx_col_name(col_index)}{row_index}"
            cells.append(
                f'<c r="{ref}" t="inlineStr"><is><t>{xml_escape(str(value))}</t></is></c>'
            )
        sheet_rows.append(f'<row r="{row_index}">{"".join(cells)}</row>')
    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<worksheet xmlns="{XLSX_MAIN_NS}" xmlns:r="{XLSX_REL_NS}">'
        f'<sheetData>{"".join(sheet_rows)}</sheetData></worksheet>'
    )
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as book:
        book.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            "</Types>",
        )
        book.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<Relationships xmlns="{XLSX_PKG_REL_NS}">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>",
        )
        book.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<workbook xmlns="{XLSX_MAIN_NS}" xmlns:r="{XLSX_REL_NS}">'
            '<sheets><sheet name="Rows" sheetId="1" r:id="rId1"/></sheets></workbook>',
        )
        book.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<Relationships xmlns="{XLSX_PKG_REL_NS}">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            "</Relationships>",
        )
        book.writestr("xl/worksheets/sheet1.xml", sheet_xml)
    return out.getvalue()


def xlsx_cell_value(cell: ET.Element, shared_strings: list[str]) -> str:
    ns = {"x": XLSX_MAIN_NS}
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        return "".join(cell.itertext())
    value = cell.find("x:v", ns)
    if value is None or value.text is None:
        return ""
    if cell_type == "s":
        try:
            return shared_strings[int(value.text)]
        except (ValueError, IndexError):
            return ""
    return value.text


def xlsx_first_sheet_part(book: zipfile.ZipFile) -> str:
    """顺着 workbook.xml → workbook.xml.rels 找第一张工作表的实际部件名。

    工作簿删过表之后剩下的可能叫 sheet2.xml，写死 sheet1.xml 会把合法文件拒掉。
    没有工作簿部件的退化包才回退到 sheet1.xml。
    """
    names = set(book.namelist())
    if "xl/workbook.xml" not in names or "xl/_rels/workbook.xml.rels" not in names:
        if "xl/worksheets/sheet1.xml" in names:
            return "xl/worksheets/sheet1.xml"
        raise ValueError("XLSX 缺少 xl/workbook.xml，找不到工作表")
    workbook = ET.fromstring(book.read("xl/workbook.xml"))
    sheet = workbook.find(".//x:sheets/x:sheet", {"x": XLSX_MAIN_NS})
    if sheet is None:
        raise ValueError("XLSX 工作簿里没有工作表")
    rel_id = sheet.attrib.get(f"{{{XLSX_REL_NS}}}id")
    rels = ET.fromstring(book.read("xl/_rels/workbook.xml.rels"))
    target = next(
        (rel.attrib.get("Target") for rel in rels.findall("r:Relationship", {"r": XLSX_PKG_REL_NS})
         if rel.attrib.get("Id") == rel_id),
        None,
    )
    if not target:
        raise ValueError("XLSX 工作簿关系里找不到第一张工作表")
    part = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join("xl", target))
    if part not in names:
        raise ValueError(f"XLSX 缺少工作表部件 {part}")
    return part


def read_xlsx_rows(data: bytes) -> list[list[str]]:
    ns = {"x": XLSX_MAIN_NS}
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as book:
            sheet_part = xlsx_first_sheet_part(book)
            shared_strings: list[str] = []
            if "xl/sharedStrings.xml" in book.namelist():
                shared_root = ET.fromstring(book.read("xl/sharedStrings.xml"))
                shared_strings = ["".join(item.itertext()) for item in shared_root.findall("x:si", ns)]
            root = ET.fromstring(book.read(sheet_part))
    except zipfile.BadZipFile:
        raise ValueError("XLSX 文件不是有效的 zip 工作簿") from None
    except ET.ParseError:
        raise ValueError("XLSX XML 内容无法解析") from None
    rows: list[list[str]] = []
    for row_node in root.findall(".//x:sheetData/x:row", ns):
        row_values: list[str] = []
        for cell in row_node.findall("x:c", ns):
            ref = cell.attrib.get("r", "")
            index = xlsx_col_index(ref) if ref else len(row_values)
            while len(row_values) < index:
                row_values.append("")
            row_values.append(xlsx_cell_value(cell, shared_strings))
        if any(value != "" for value in row_values):
            rows.append(row_values)
    return rows


def parse_collection_xlsx(collection: str, data_b64: str, config: dict) -> list[dict]:
    try:
        data = base64.b64decode(data_b64, validate=True)
    except (TypeError, ValueError, binascii.Error):
        raise ValueError("data_b64 必须是有效的 base64") from None
    rows = read_xlsx_rows(data)
    if not rows:
        raise ValueError("XLSX 没有可导入的数据")
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerows(rows)
    return parse_collection_csv(collection, out.getvalue(), config)


class FxRiskHandler(BaseHTTPRequestHandler):
    server_version = "FxRiskLocal/1.0"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path in {"/", "/index.html"}:
            self.serve_file(WEB_ROOT / "index.html")
            return
        if path.startswith("/web/"):
            static_path = web_static_path(path)
            if static_path is None:
                self.send_error(404)
                return
            self.serve_file(static_path)
            return
        if path == "/api/state":
            state = ensure_state()
            rates = load_rates(merged_config(state))
            self.send_json(build_dashboard(state, rates))
            return
        if path in {"/api/export", "/api/workspace/export"}:
            state = validate_workspace_state(ensure_state())
            self.send_json({
                "ok": True,
                "exported_at": now_iso(),
                "data_file": str(STATE_FILE),
                "state": state,
                "workspace": state,
                "metadata": state.get("metadata", {}),
                "backups": list_backups(),
            })
            return
        if path == "/api/backups":
            self.send_json({"backups": list_backups(), "data_file": str(STATE_FILE)})
            return
        if path == "/api/csv/export":
            try:
                collection = csv_collection(parse_qs(parsed.query).get("collection", [""])[0])
                self.send_csv(
                    export_collection_csv(ensure_state(), collection),
                    f"{collection}-{now_iso()[:10]}.csv",
                )
            except ValueError as exc:
                self.send_json({"ok": False, "error": str(exc)}, status=400)
            return
        if path == "/api/xlsx/export":
            try:
                collection = csv_collection(parse_qs(parsed.query).get("collection", [""])[0])
                self.send_binary(
                    build_xlsx(collection, ensure_state()),
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    f"{collection}-{now_iso()[:10]}.xlsx",
                )
            except ValueError as exc:
                self.send_json({"ok": False, "error": str(exc)}, status=400)
            return
        self.send_error(404)

    def do_POST(self) -> None:
        try:
            blocked = check_mutation_headers(self)
            if blocked:
                status, message = blocked
                close_rejected_request(self)
                self.send_json({"ok": False, "error": message}, status=status)
                return
            body = parse_body(self)
            # Network refresh touches only the rate cache, so keep it outside
            # the state lock to avoid holding it during the HTTP fetch.
            if self.path == "/api/rates/refresh":
                self.send_json(load_rates(merged_config(ensure_state()), force=True))
                return
            # 冻结方案要用汇率，先在锁外把它取好：load_rates 缓存过期时会走
            # 一次 urlopen(timeout=12)，在锁里等它等于把所有写请求堵住 12 秒。
            plan_rates = load_rates(merged_config(ensure_state())) if self.path == "/api/plans" else None
            with STATE_LOCK:
                state = ensure_state()
                if self.path == "/api/exposures":
                    validate_exposure(body, merged_config(state))
                    row = add_id(body)
                    state.setdefault("exposures", []).append(row)
                    save_state(state, reason="create-exposure")
                    append_audit("create", "exposures", row.get("id"), None, row)
                    self.send_json({"ok": True})
                    return
                if self.path == "/api/hedges":
                    validate_hedge(body, merged_config(state))
                    row = add_id(body)
                    state.setdefault("hedges", []).append(row)
                    save_state(state, reason="create-hedge")
                    append_audit("create", "hedges", row.get("id"), None, row)
                    self.send_json({"ok": True})
                    return
                if self.path == "/api/settlements":
                    validate_settlement(body, merged_config(state))
                    row = add_id(body)
                    state.setdefault("settlements", []).append(row)
                    save_state(state, reason="create-settlement")
                    append_audit("create", "settlements", row.get("id"), None, row)
                    self.send_json({"ok": True})
                    return
                if self.path == "/api/config":
                    before = merged_config(state)
                    replace_maps = {
                        key for key in (
                            "interest_rates", "forward_overrides", "scenario_shifts",
                            "month_currency_hedge_ratios", "monthly_average_rates",
                            "confirmed_parameters",
                        )
                        if key in body
                    }
                    merged = validate_config(body, base=before, replace_maps=replace_maps)
                    state["config"] = merged
                    save_state(state, reason="update-config")
                    changed = {
                        key: {"from": before.get(key), "to": value}
                        for key, value in merged.items()
                        if before.get(key) != value
                    }
                    if changed:
                        append_audit("update", "config", None, before, changed)
                    self.send_json({"ok": True, "config": merged})
                    return
                if self.path == "/api/plans":
                    dashboard = build_dashboard(state, plan_rates)
                    if not dashboard.get("rates_actionable", True):
                        raise ValueError("当前汇率不可作为执行报价，请刷新汇率后再冻结方案")
                    if dashboard["portfolio"].get("rate_missing"):
                        raise ValueError("存在缺失汇率的敞口，不能冻结为完整方案")
                    if not dashboard["suggestions"]:
                        raise ValueError("没有待锁汇建议，无法冻结方案")
                    plan = add_id(plans.freeze(dashboard, body.get("label"), now_iso()))
                    state.setdefault("plans", []).append(plan)
                    save_state(state, reason="freeze-plan")
                    append_audit("freeze", "plans", plan.get("id"), None,
                                 {"label": plan["label"], "rows": len(plan["rows"])})
                    self.send_json({"ok": True, "plan": plan})
                    return
                if self.path == "/api/reset-demo":
                    before_reset = state
                    # 方案是只读存档，"恢复样例"的语义是重置工作数据，
                    # 不该连历史快照一起抹掉——那是不可逆的，确认框也没提。
                    reset_state = sample_state(keep_plans=state.get("plans", []))
                    save_state(reset_state, reason="reset-demo")
                    # 先写盘再记日志：反过来的话，写盘失败会在只追加的历史里
                    # 永久留下一条"重置过"的假记录。
                    append_audit("reset", "workspace", None, before_reset, reset_state)
                    self.send_json({"ok": True})
                    return
                if self.path == "/api/clear-business":
                    before_clear = state
                    cleared_state = {
                        **state,
                        "metadata": {**state.get("metadata", {}), "setup_complete": True, "data_mode": "empty"},
                        "exposures": [],
                        "hedges": [],
                        "settlements": [],
                    }
                    save_state(cleared_state, reason="clear-business")
                    append_audit("clear", "workspace", None, before_clear, cleared_state)
                    self.send_json({"ok": True})
                    return
                if self.path in {"/api/import", "/api/workspace/import"}:
                    imported = validate_workspace_state(body.get("workspace", body.get("state", body)))
                    before_import = state
                    save_state(imported, reason="import-workspace")
                    append_audit("import", "workspace", None, before_import,
                                 {"rows": {key: len(imported.get(key, []))
                                           for key in ("exposures", "hedges", "settlements", "plans")}})
                    self.send_json({"ok": True, "metadata": imported.get("metadata", {}), "backup_count": len(list_backups())})
                    return
                if self.path == "/api/csv/import":
                    collection = csv_collection(body.get("collection"))
                    imported_rows = parse_collection_csv(collection, body.get("csv", ""), merged_config(state))
                    before_rows = list(state.get(collection, []))
                    state.setdefault(collection, []).extend(imported_rows)
                    save_state(state, reason=f"import-csv-{collection}")
                    append_audit("import", collection, None, before_rows, {"rows": len(imported_rows)})
                    self.send_json({"ok": True, "collection": collection, "imported": len(imported_rows)})
                    return
                if self.path == "/api/xlsx/import":
                    collection = csv_collection(body.get("collection"))
                    imported_rows = parse_collection_xlsx(collection, body.get("data_b64", ""), merged_config(state))
                    before_rows = list(state.get(collection, []))
                    state.setdefault(collection, []).extend(imported_rows)
                    save_state(state, reason=f"import-xlsx-{collection}")
                    append_audit("import", collection, None, before_rows, {"rows": len(imported_rows)})
                    self.send_json({"ok": True, "collection": collection, "imported": len(imported_rows)})
                    return
                if self.path == "/api/workspace/empty":
                    before_empty = state
                    cleared_state = empty_state(setup_complete=True)
                    save_state(cleared_state, reason="empty-workspace")
                    append_audit("reset", "workspace", None, before_empty, cleared_state)
                    self.send_json({"ok": True, "metadata": cleared_state["metadata"]})
                    return
                if self.path == "/api/workspace/sample":
                    before_sample = state
                    next_state = sample_state(keep_plans=state.get("plans", []))
                    save_state(next_state, reason="sample-workspace")
                    append_audit("reset", "workspace", None, before_sample, next_state)
                    self.send_json({"ok": True, "metadata": next_state["metadata"]})
                    return
                if self.path == "/api/backups/latest/restore":
                    before_restore = state
                    restored = restore_backup()
                    append_audit("restore", "workspace", None, before_restore,
                                 {"backup": "latest", "rows": {key: len(restored.get(key, []))
                                                               for key in ("exposures", "hedges", "settlements", "plans")}})
                    self.send_json({"ok": True})
                    return
                parts = self.path.strip("/").split("/")
                if len(parts) == 4 and parts[0] == "api" and parts[1] == "backups" and parts[3] == "restore":
                    before_restore = state
                    restored = restore_backup(parts[2])
                    append_audit("restore", "workspace", None, before_restore,
                                 {"backup": parts[2], "rows": {key: len(restored.get(key, []))
                                                               for key in ("exposures", "hedges", "settlements", "plans")}})
                    self.send_json({"ok": True})
                    return
        except TypeError as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=415)
            return
        except FileNotFoundError as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=404)
            return
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=400)
            return
        self.send_error(404)

    def do_PUT(self) -> None:
        try:
            blocked = check_mutation_headers(self)
            if blocked:
                status, message = blocked
                close_rejected_request(self)
                self.send_json({"ok": False, "error": message}, status=status)
                return
            parts = self.path.strip("/").split("/")
            if len(parts) == 3 and parts[0] == "api":
                collection = mutable_collection(parts[1])
                if collection and collection != "plans":
                    record_id = parts[2]
                    with STATE_LOCK:
                        state = ensure_state()
                        body = parse_body(self)
                        config = merged_config(state)
                        rows = state.get(collection, [])
                        for index, row in enumerate(rows):
                            if row.get("id") == record_id:
                                before = dict(row)
                                updated = {**row, **body, "id": record_id}
                                if row.get("created_at"):
                                    updated["created_at"] = row["created_at"]
                                validate_for_collection(collection, updated, config)
                                updated = add_id(updated)
                                rows[index] = updated
                                save_state(state, reason=f"update-{collection}")
                                append_audit("update", collection, record_id, before, updated)
                                self.send_json({"ok": True, "record": updated})
                                return
                        self.send_json({"ok": False, "error": "record not found"}, status=404)
                        return
        except TypeError as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=415)
            return
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=400)
            return
        self.send_error(404)

    def do_DELETE(self) -> None:
        blocked = check_mutation_headers(self)
        if blocked:
            status, message = blocked
            close_rejected_request(self)
            self.send_json({"ok": False, "error": message}, status=status)
            return
        parts = self.path.strip("/").split("/")
        if len(parts) == 3 and parts[0] == "api":
            collection = mutable_collection(parts[1])
            if collection:
                record_id = parts[2]
                with STATE_LOCK:
                    state = ensure_state()
                    rows = state.get(collection, [])
                    removed = [row for row in rows if row.get("id") == record_id]
                    if not removed:
                        # 删一条不存在的记录以前也回 200/deleted:0，等于把
                        # "前端拿着过期 id" 这种 bug 悄悄咽掉。
                        self.send_json({"ok": False, "error": "record not found"}, status=404)
                        return
                    state[collection] = [row for row in rows if row.get("id") != record_id]
                    save_state(state, reason=f"delete-{collection}")
                    # 审计必须在锁内追加：放到锁外的话，另一个请求可能先提交
                    # 并写完自己的日志，倒序展示出来就是"删除比它之后发生的
                    # 改动还新"——日志顺序和实际状态变更顺序对不上。
                    for row in removed:
                        append_audit("delete", collection, record_id, row, None)
                self.send_json({"ok": True, "deleted": len(removed)})
                return
        self.send_error(404)

    def send_security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; connect-src 'self'; img-src 'self' data:; "
            "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; frame-ancestors 'none'",
        )

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
        self.send_security_headers()
        self.end_headers()
        self.wfile.write(payload)

    def send_json(self, data: dict, status: int = 200) -> None:
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_security_headers()
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def send_csv(self, text: str, filename: str, status: int = 200) -> None:
        payload = ("\ufeff" + text).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_security_headers()
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def send_binary(self, payload: bytes, content_type: str, filename: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_security_headers()
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt: str, *args) -> None:
        print(f"[{now_iso()}] {self.address_string()} {fmt % args}")


def validate_exposure(row: dict, config: dict | None = None, allow_legacy_category: bool = False) -> None:
    required = ["due_date", "currency", "amount", "direction"]
    missing = [key for key in required if not row.get(key)]
    if missing:
        raise ValueError(f"敞口缺少字段: {', '.join(missing)}")
    validate_currency(row, config or DEFAULT_CONFIG)
    parse_iso_date(row["due_date"], "due_date")
    if row["direction"] not in {"receipt", "payment"}:
        raise ValueError("敞口方向必须是 receipt 或 payment")
    row["amount"] = positive_number(row["amount"], "amount")
    category = row.get("category") or DEFAULT_CATEGORY
    if not known_category(category) and not allow_legacy_category:
        raise ValueError("风险类型必须是 " + "/".join(EXPOSURE_CATEGORIES))
    row["category"] = category
    raw_probability = row.get("probability")
    # 不能写成 `or 1`：0 是假值，会被悄悄换成 1，
    # 等于把一笔已取消的订单按全额记进敞口，而校验根本不会触发。
    if raw_probability is None or raw_probability == "":
        raw_probability = 1
    try:
        probability = float(raw_probability)
    except (TypeError, ValueError):
        raise ValueError("probability 必须是 (0, 1] 内的数字") from None
    if not 0 < probability <= 1:
        raise ValueError("probability 必须在 (0, 1] 内")
    row["probability"] = probability
    booked = row.get("booked")
    if isinstance(booked, str):
        # JSON 客户端传 "false" 时 bool("false") 是 True
        booked = booked.strip().lower() not in {"", "false", "0", "no"}
    row["booked"] = bool(booked)
    # 后端也算一遍推荐，这样绕过浏览器的 API 客户端同样能拿到，
    # 而不是只有表单里有提示。
    suggested, reason = suggest_category(row)
    row["suggested_category"] = suggested
    row["suggestion_reason"] = reason


def validate_hedge(row: dict, config: dict | None = None) -> None:
    required = ["trade_date", "due_date", "currency", "amount", "action", "locked_rate"]
    missing = [key for key in required if not row.get(key)]
    if missing:
        raise ValueError(f"锁汇缺少字段: {', '.join(missing)}")
    validate_currency(row, config or DEFAULT_CONFIG)
    trade_date = parse_iso_date(row["trade_date"], "trade_date")
    due_date = parse_iso_date(row["due_date"], "due_date")
    if trade_date > due_date:
        raise ValueError("trade_date 不能晚于 due_date")
    if row["action"] not in {"buy_foreign", "sell_foreign"}:
        raise ValueError("锁汇方向必须是 buy_foreign 或 sell_foreign")
    row["amount"] = positive_number(row["amount"], "amount")
    row["locked_rate"] = positive_number(row["locked_rate"], "locked_rate")


def validate_settlement(row: dict, config: dict | None = None) -> None:
    required = ["due_date", "currency", "actual_rate"]
    missing = [key for key in required if not row.get(key)]
    if missing:
        raise ValueError(f"结算缺少字段: {', '.join(missing)}")
    validate_currency(row, config or DEFAULT_CONFIG)
    parse_iso_date(row["due_date"], "due_date")
    row["actual_rate"] = positive_number(row["actual_rate"], "actual_rate")
    # 实际发生额是可选的，但一旦填了就必须是非负数。
    # 填 0 是有意义的（订单黄了），所以这里**不能**用 `or` 兜底。
    raw_amount = row.get("actual_amount")
    if raw_amount is None or raw_amount == "":
        row.pop("actual_amount", None)
        return
    try:
        actual_amount = float(raw_amount)
    except (TypeError, ValueError):
        raise ValueError("actual_amount 必须是数字") from None
    if actual_amount < 0:
        raise ValueError("actual_amount 不能为负")
    row["actual_amount"] = actual_amount


class FxRiskServer(ThreadingHTTPServer):
    """默认 request_queue_size=5，十来个并发请求就会有连接被内核直接重置
    （客户端看到的是 ConnectionResetError，不是超时，很难猜）。
    单用户本地工具平时碰不到，但页面一次刷新就要打好几个接口，
    把 backlog 放大是一行的事。"""

    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64


def is_loopback_host(host: str) -> bool:
    normalized = host.strip().strip("[]").lower()
    if normalized == "localhost":
        return True
    try:
        return ip_address(normalized).is_loopback
    except ValueError:
        return False


def require_allowed_bind(host: str, allow_network: bool = False) -> None:
    if allow_network or is_loopback_host(host):
        return
    raise ValueError("非本机地址绑定需要显式传入 --allow-network")


def run(host: str, port: int, allow_network: bool = False) -> None:
    ensure_state()
    require_allowed_bind(host, allow_network)
    server = FxRiskServer((host, port), FxRiskHandler)
    if not is_loopback_host(host):
        print("安全警告：当前服务可被其他设备访问，但未提供身份认证；请勿在公网或不可信网络中使用。")
    print(f"FX risk web app running at http://{host}:{port}")
    server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local FX risk web app.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--allow-network", action="store_true")
    args = parser.parse_args()
    run(args.host, args.port, allow_network=args.allow_network)


if __name__ == "__main__":
    main()
