"""Parity check: demo/index.html forecastMultiplier must match web_app.py.

The static demo keeps a small JavaScript copy of the backend forecast gate.
This script extracts that JS function, runs it in Node, and compares its
multiplier and reason branch with the Python implementation.

    python demo/parity_check.py
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from web_app import forecast_multiplier  # noqa: E402

HTML = Path(__file__).resolve().parent / "index.html"
BEGIN = "// ---- gate:begin ----"
END = "// ---- gate:end ----"

TODAY = date(2026, 6, 15)
TIERS = ["support", "caution", "reject", None]
DIRECTIONS = ["up", "down"]
NETS = [700000.0, -350000.0]
MOVES = [0.0, 0.005, 0.0139, 0.018, 0.025, 0.05]


def extract_js() -> str:
    text = HTML.read_text(encoding="utf-8")
    start = text.index(BEGIN)
    end = text.index(END)
    body = text[start:end]
    return 'function pct1(x){return (x*100).toFixed(1)+"%";}\n' + body


def normalize(reason: str | None) -> str:
    """Ignore numeric formatting differences in percentage values."""
    if reason is None:
        return ""
    return re.sub(r"[0-9.]+%", "<pct>", reason)


def generated(days: int = 0) -> str:
    return (TODAY + timedelta(days=days)).isoformat() + "T00:00:00Z"


def rate_for(direction: str, move: float, spot: float = 1.0) -> float:
    return spot * (1 + move if direction == "up" else 1 - move)


def signal_for(
    tier: str | None,
    direction: str = "up",
    move: float = 0.025,
    *,
    current: float = 1.0,
    mape: float = 0.018,
    generated_at: str | None = None,
    forecast: list[dict] | None = None,
) -> dict | None:
    if tier is None:
        return None
    return {
        "tier": tier,
        "direction": direction,
        "mape": mape,
        "generated_at": generated_at or generated(),
        "current": current,
        "forecast": forecast if forecast is not None else [{"rate": rate_for(direction, move, current)}],
    }


def add_case(cases: list[dict], name: str, signal: dict | None, net: float, **kwargs) -> None:
    cases.append(
        {
            "name": name,
            "signal": signal,
            "net": net,
            "period": kwargs.get("period"),
            "live_spot": kwargs.get("live_spot"),
            "today": kwargs.get("today", TODAY.isoformat()),
        }
    )


def build_cases() -> list[dict]:
    cases: list[dict] = []
    for tier in TIERS:
        for direction in DIRECTIONS:
            for net in NETS:
                for move in MOVES:
                    add_case(cases, "legacy-no-period", signal_for(tier, direction, move), net)
                    add_case(
                        cases,
                        "period-live-spot",
                        signal_for(
                            tier,
                            direction,
                            move,
                            forecast=[{"month": "2026-06", "rate": rate_for(direction, move)}],
                        ),
                        net,
                        period="2026-06",
                        live_spot=1.0,
                    )

    add_case(
        cases,
        "stale-generated-at",
        signal_for("support", "up", generated_at=generated(-46)),
        700000.0,
    )
    add_case(
        cases,
        "future-generated-at",
        signal_for("support", "up", generated_at=generated(1)),
        700000.0,
    )
    add_case(
        cases,
        "live-spot-crosses-forecast-endpoint",
        signal_for("support", "up", forecast=[{"month": "2026-06", "rate": 1.02}]),
        700000.0,
        period="2026-06",
        live_spot=1.03,
    )
    multi_month = signal_for(
        "support",
        "up",
        forecast=[
            {"month": "2026-06", "rate": 1.03},
            {"month": "2026-07", "rate": 0.97},
        ],
    )
    add_case(cases, "multi-month-first-period", multi_month, 700000.0, period="2026-06", live_spot=1.0)
    add_case(cases, "multi-month-opposite-period", multi_month, 700000.0, period="2026-07", live_spot=1.0)
    add_case(cases, "missing-exact-period", multi_month, 700000.0, period="2026-08", live_spot=1.0)
    add_case(cases, "missing-period-argument", multi_month, 700000.0, live_spot=1.0)
    add_case(
        cases,
        "flat-live-spot",
        signal_for("support", "up", forecast=[{"month": "2026-06", "rate": 1.0}]),
        700000.0,
        period="2026-06",
        live_spot=1.0,
    )
    return cases


def main() -> int:
    cases = build_cases()
    script = (
        extract_js()
        + "\nconst cases = "
        + json.dumps(cases, ensure_ascii=False)
        + ";\nconst out = cases.map(c => {"
        + "  const r = forecastMultiplier(c.signal, c.net, c.period, c.live_spot, c.today);"
        + "  return [r[0], r[1]];"
        + "});\nconsole.log(JSON.stringify(out));\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as handle:
        handle.write(script)
        script_path = handle.name
    try:
        proc = subprocess.run(
            ["node", script_path], capture_output=True, text=True, encoding="utf-8"
        )
    finally:
        Path(script_path).unlink(missing_ok=True)
    if proc.returncode != 0:
        print("node execution failed:\n" + proc.stderr)
        return 2
    js_results = json.loads(proc.stdout)

    mismatches = []
    for case, (js_mult, js_reason) in zip(cases, js_results):
        today = date.fromisoformat(case["today"]) if case.get("today") else None
        py_mult, py_reason = forecast_multiplier(
            case["signal"],
            case["net"],
            case.get("period"),
            live_spot=case.get("live_spot"),
            today=today,
        )
        if py_mult != js_mult or normalize(py_reason) != normalize(js_reason):
            mismatches.append((case, (py_mult, py_reason), (js_mult, js_reason)))

    print(f"parity cases: {len(cases)}")
    if mismatches:
        print(f"mismatches: {len(mismatches)}")
        for case, py, js in mismatches[:10]:
            print(f"  {case['name']}: {case}\n    py={py}\n    js={js}")
        return 1
    print("all parity cases match")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
