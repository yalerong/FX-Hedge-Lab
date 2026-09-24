"""对拍：demo/index.html 里的 forecastMultiplier 必须与 web_app.forecast_multiplier 逐格相同。

demo 页是仓库逻辑的静态复刻，两边各写一遍就有漂移风险。这个脚本枚举全部输入组合，
在 Node 里跑 JS 版本，和 Python 版本比较倍数与理由（数字格式差异不计）。

    python demo/parity_check.py
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from web_app import forecast_multiplier  # noqa: E402

HTML = Path(__file__).resolve().parent / "index.html"
BEGIN = "// ---- gate:begin ----"
END = "// ---- gate:end ----"

TIERS = ["support", "caution", "reject", None]
DIRECTIONS = ["up", "down"]
NETS = [700000.0, -350000.0]
MOVES = [0.0, 0.005, 0.0139, 0.018, 0.025, 0.05]


def extract_js() -> str:
    text = HTML.read_text(encoding="utf-8")
    start = text.index(BEGIN)
    end = text.index(END)
    body = text[start:end]
    # pct1 定义在标记之外，对拍时补一个等价实现
    return 'function pct1(x){return (x*100).toFixed(1)+"%";}\n' + body


def normalize(reason: str | None) -> str:
    """去掉百分数，只比较理由的语义分支。"""
    if reason is None:
        return ""
    return re.sub(r"[0-9.]+%", "<pct>", reason)


def main() -> int:
    # Python 侧的 move 是 abs(forecast[-1]/current - 1) 反推出来的，用 current=1.0
    # 让两侧拿到位级相同的浮点数，否则 move == mape 的边界会因舍入而假报不一致。
    generated_at = date.today().isoformat() + "T00:00:00Z"
    cases = []
    for tier in TIERS:
        for direction in DIRECTIONS:
            for net in NETS:
                for move in MOVES:
                    eff = abs((1.0 + move) / 1.0 - 1.0)
                    cases.append(
                        {"tier": tier, "direction": direction, "move": eff, "mape": 0.018,
                         "net": net, "generated_at": generated_at}
                    )

    script = (
        extract_js()
        + "\nconst cases = "
        + json.dumps(cases)
        + ";\nconst out = cases.map(c => {"
        + "  const signal = c.tier === null ? null : {tier: c.tier, direction: c.direction, move: c.move, mape: c.mape, generated_at: c.generated_at};"
        + "  const r = forecastMultiplier(signal, c.net);"
        + "  return [r[0], r[1]];"
        + "});\nconsole.log(JSON.stringify(out));\n"
    )
    proc = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, encoding="utf-8"
    )
    if proc.returncode != 0:
        print("node 执行失败：\n" + proc.stderr)
        return 2
    js_results = json.loads(proc.stdout)

    mismatches = []
    for case, (js_mult, js_reason) in zip(cases, js_results):
        signal = None
        if case["tier"] is not None:
            signal = {
                "tier": case["tier"],
                "direction": case["direction"],
                "mape": case["mape"],
                "generated_at": case["generated_at"],
                "current": 1.0,
                "forecast": [{"rate": 1.0 + case["move"]}],
            }
        py_mult, py_reason = forecast_multiplier(signal, case["net"])
        if py_mult != js_mult or normalize(py_reason) != normalize(js_reason):
            mismatches.append((case, (py_mult, py_reason), (js_mult, js_reason)))

    print(f"对拍组合数：{len(cases)}")
    if mismatches:
        print(f"不一致 {len(mismatches)} 组：")
        for case, py, js in mismatches[:10]:
            print(f"  {case}\n    py={py}\n    js={js}")
        return 1
    print("全部一致。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
