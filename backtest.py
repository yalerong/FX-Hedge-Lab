"""VaR 回测：Kupiec 失败率 + Christoffersen 独立性 + Basel 分区。

为什么要有这一层：

参数法和历史模拟都会给出一个 VaR 数字，那是一个**声明**——"99% 的日子，
单日亏损不超过这个数"。没有回测，这个声明永远不会被证伪：模型系统性低估
风险时，页面只会一直很乐观。Basel 市场风险框架要求数 250 个交易日的例外，
Kupiec(1995) 的失败率检验和 Christoffersen(1998) 的独立性检验早就是行业
惯例；开源的风险分析项目（portfolio-risk-analytics 之类）也都是把回测和
VaR 摆在同一个模块里。

口径（这几条决定了结论能不能用）：

1. **固定今天的头寸、回测模型**，不是回测历史头寸。把当前净敞口按历史
   汇率逐日重估，看当天的 VaR 有没有被突破。它回答"我这个模型靠不靠谱"，
   不回答"我去年的 VaR 准不准"——后者需要历史头寸快照，本工具没有。
2. **没有前视偏差**：第 t 天的 VaR 只用 t 之前的收益估计，实际损益用
   t 到 t+h 的收益。回测最容易犯的错就是拿全样本估参数再回头打分。
3. 三种模型并列，跑在同一个损益序列上：历史模拟、等权参数法、EWMA
   参数法（λ=0.94，RiskMetrics 的经典取值）。可比才有意义。
4. Kupiec 检验失败率是否等于 1-置信度（χ²(1)）；Christoffersen 检验突破
   是否聚集（χ²(1)）；两者相加是联合检验（χ²(2)）。χ² 的尾概率有闭式解，
   不需要 scipy：df=1 是 erfc(√(x/2))，df=2 是 e^(-x/2)。
5. Basel 交通灯（250 日 / 99%）：0-4 绿、5-9 黄、≥10 红。**只在恰好 250 个
   测试日时给分区**——那张表是按 250 天定的，改成 400 天再线性缩放，
   是在假装精确。
6. 只回测有本地日线的币种。缺币种时按覆盖比例算并显式标出 coverage，
   不拿"部分参数 + 部分历史"拼一个看起来完整的数。
"""
from __future__ import annotations

import math
from datetime import date
from statistics import NormalDist

import history

DEFAULT_WINDOW = 250
DEFAULT_DAYS = 250
EWMA_LAMBDA = 0.94
MIN_TEST_DAYS = 60
BASEL_DAYS = 250
BASEL_GREEN_MAX = 4
BASEL_YELLOW_MAX = 9
# EWMA 递归的初始化样本数。少于这个数递归没有意义。
EWMA_BURN_IN = 20


def _clamp_int(value, default: int, low: int, high: int) -> int:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def chi2_sf(stat: float, df: int) -> float:
    """χ² 尾概率。只支持 df=1（erfc）和 df=2（指数），这正是这里用到的两个。"""
    if df not in (1, 2):
        raise ValueError("只实现 df=1 和 df=2")
    if stat is None or stat <= 0:
        return 1.0
    if df == 1:
        return math.erfc(math.sqrt(stat / 2.0))
    return math.exp(-stat / 2.0)


def kupiec(test_days: int, exceptions: int, confidence: float) -> dict:
    """Kupiec 失败率检验（proportion of failures）。

    H0：真实失败率 = 1-置信度。LR = 2(lnL_无约束 - lnL_约束) ~ χ²(1)。
    例外为 0 或全突破时取连续极限，避免 log(0)。
    """
    n = int(test_days)
    x = int(exceptions)
    if n <= 0 or x < 0 or x > n:
        return {"stat": None, "p": None}
    p = 1.0 - float(confidence)
    if p <= 0 or p >= 1:
        return {"stat": None, "p": None}
    if x == 0:
        stat = -2.0 * n * math.log(1.0 - p)
    elif x == n:
        stat = -2.0 * n * math.log(p)
    else:
        rate = x / n
        stat = 2.0 * (
            (n - x) * math.log(1.0 - rate) + x * math.log(rate)
            - (n - x) * math.log(1.0 - p) - x * math.log(p)
        )
    stat = max(0.0, stat)
    return {"stat": stat, "p": chi2_sf(stat, 1)}


def christoffersen(hits: list[bool]) -> dict:
    """Christoffersen 独立性检验：突破是否扎堆。

    把突破看成两状态的马尔可夫链，检验 P(今天突破 | 昨天突破) 是否等于
    P(今天突破 | 昨天没突破)。扎堆意味着波动率有记忆，等权窗口跟不上。
    """
    hits = [bool(value) for value in hits]
    counts = {"n00": 0, "n01": 0, "n10": 0, "n11": 0}
    for previous, current in zip(hits, hits[1:]):
        if previous and current:
            counts["n11"] += 1
        elif previous:
            counts["n10"] += 1
        elif current:
            counts["n01"] += 1
        else:
            counts["n00"] += 1
    total = sum(counts.values())
    # 一次突破都没有（或次次都突破）时转移矩阵退化，检验没有信息量。
    if total == 0 or counts["n01"] + counts["n11"] == 0 or counts["n00"] + counts["n10"] == 0:
        return {"stat": None, "p": None, **counts}

    def log_likelihood(probability: float, misses: int, hits_: int) -> float:
        value = 0.0
        if misses and probability < 1.0:
            value += misses * math.log(1.0 - probability)
        if hits_ and probability > 0.0:
            value += hits_ * math.log(probability)
        return value

    p01 = counts["n01"] / (counts["n00"] + counts["n01"])
    p11 = counts["n11"] / (counts["n10"] + counts["n11"])
    p_pool = (counts["n01"] + counts["n11"]) / total
    restricted = log_likelihood(p_pool, counts["n00"] + counts["n10"], counts["n01"] + counts["n11"])
    unrestricted = log_likelihood(p01, counts["n00"], counts["n01"]) + log_likelihood(p11, counts["n10"], counts["n11"])
    stat = max(0.0, 2.0 * (unrestricted - restricted))
    return {"stat": stat, "p": chi2_sf(stat, 1), **counts}


def basel_zone(test_days: int, exceptions: int) -> str | None:
    """Basel 交通灯。只在 250 个测试日时给分区，其余情况返回 None。"""
    if int(test_days) != BASEL_DAYS:
        return None
    if exceptions <= BASEL_GREEN_MAX:
        return "green"
    if exceptions <= BASEL_YELLOW_MAX:
        return "yellow"
    return "red"


def _quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    fraction = position - low
    return ordered[low] * (1 - fraction) + ordered[high] * fraction


def _prefix(values: list[float]) -> list[float]:
    running = [0.0]
    total = 0.0
    for value in values:
        total += value
        running.append(total)
    return running


def _window_cov(prefix_x, prefix_y, squares, start, window, scale=1.0):
    """滑动窗口协方差，O(1)：Cov = (Σxy - Σx·Σy/n) / (n-1)。

    squares 是 Σ(x·y) 的前缀和（x=y 时就是平方和）。
    逐窗口重算每个币种对是 O(W·n²)，250 个窗口下会让每次刷新卡住；
    前缀和把它压到常数。
    """
    n = window
    if n < 2:
        return 0.0
    sum_x = prefix_x[start + n] - prefix_x[start]
    sum_y = prefix_y[start + n] - prefix_y[start]
    sum_xy = squares[start + n] - squares[start]
    return (sum_xy - sum_x * sum_y / n) / (n - 1)


def _ewma_states(series: dict[str, list[float]], needed: set[int], currencies: list[str]):
    """EWMA 方差/协方差，只保存回测用得到的那些日期。

    state[k] 是"用前 k 个收益估出来的"参数，递归在 k-1 处更新，天然不含
    第 k 天的信息——前视偏差就是在这里被挡掉的。
    """
    lam = EWMA_LAMBDA
    length = len(next(iter(series.values())))
    burn_in = min(EWMA_BURN_IN, length - 1)
    if burn_in < 2:
        return {}

    def sample_var(values):
        mean = sum(values) / len(values)
        return sum((value - mean) ** 2 for value in values) / (len(values) - 1)

    variances = {currency: sample_var(series[currency][:burn_in]) for currency in currencies}
    covariances = {}
    for i, left in enumerate(currencies):
        for right in currencies[i:]:
            a = series[left][:burn_in]
            b = series[right][:burn_in]
            ma = sum(a) / burn_in
            mb = sum(b) / burn_in
            covariances[(left, right)] = sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (burn_in - 1)

    states = {}
    for index in range(burn_in + 1, length):
        for currency in currencies:
            value = series[currency][index - 1]
            variances[currency] = lam * variances[currency] + (1 - lam) * value * value
        for i, left in enumerate(currencies):
            for right in currencies[i:]:
                value = series[left][index - 1] * series[right][index - 1]
                covariances[(left, right)] = lam * covariances[(left, right)] + (1 - lam) * value
        if index in needed:
            states[index] = (dict(variances), dict(covariances))
    return states


def _portfolio_sigma(weights: dict[str, float], currencies: list[str], variances, covariances) -> float:
    total = 0.0
    for i, left in enumerate(currencies):
        for j, right in enumerate(currencies):
            if j < i:
                continue
            covariance = variances[left] if left == right else covariances[(left, right)]
            weight = weights[left] * weights[right] * covariance
            total += weight if i == j else 2.0 * weight
    return math.sqrt(max(0.0, total))


def _model_result(key, label, hits, test_days, confidence):
    exceptions = sum(1 for value in hits if value)
    rate = exceptions / test_days if test_days else 0.0
    uc = kupiec(test_days, exceptions, confidence)
    ind = christoffersen(hits)
    combined_stat = None
    combined_p = None
    if uc["stat"] is not None and ind["stat"] is not None:
        combined_stat = uc["stat"] + ind["stat"]
        combined_p = chi2_sf(combined_stat, 2)
    zone = basel_zone(test_days, exceptions)
    expected_rate = 1.0 - confidence
    if combined_p is not None and combined_p >= 0.05:
        level = "ok"
        verdict = "不能拒绝"
    elif uc["p"] is not None and uc["p"] < 0.05 and rate > expected_rate:
        level = "bad"
        verdict = "低估风险：突破次数偏多"
    elif uc["p"] is not None and uc["p"] < 0.05 and rate < expected_rate:
        level = "warn"
        verdict = "过于保守：突破次数偏少"
    elif ind["p"] is not None and ind["p"] < 0.05:
        level = "warn"
        verdict = "突破扎堆：波动率有记忆"
    else:
        level = "ok"
        verdict = "不能拒绝"
    if zone == "red" and level != "bad":
        level = "bad"
    return {
        "key": key,
        "label": label,
        "exceptions": exceptions,
        "rate": round(rate, 4),
        "kupiec_stat": None if uc["stat"] is None else round(uc["stat"], 4),
        "kupiec_p": None if uc["p"] is None else round(uc["p"], 4),
        "independence_stat": None if ind["stat"] is None else round(ind["stat"], 4),
        "independence_p": None if ind["p"] is None else round(ind["p"], 4),
        "combined_stat": None if combined_stat is None else round(combined_stat, 4),
        "combined_p": None if combined_p is None else round(combined_p, 4),
        "zone": zone,
        "level": level,
        "verdict": verdict,
    }


def run(weights: dict[str, float], config: dict, confidence: float, horizon: int, today: date | None = None) -> dict:
    """weights 是带符号的人民币净敞口（收为正、付为负）。"""
    today = today or date.today()
    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        confidence = 0.99
    confidence = min(max(confidence, 0.5), 0.9999)
    try:
        horizon = int(horizon or 1)
    except (TypeError, ValueError):
        horizon = 1
    horizon = max(1, min(horizon, 10))
    window = _clamp_int(config.get("backtest_window_days"), DEFAULT_WINDOW, 60, 750)
    test_days = _clamp_int(config.get("backtest_days"), DEFAULT_DAYS, MIN_TEST_DAYS, 500)

    active = {currency: float(value) for currency, value in weights.items() if abs(float(value)) > 1e-9}
    if not active:
        return {"available": False, "reason": "没有净敞口", "models": [], "notes": []}

    returns = {}
    missing = []
    for currency in sorted(active):
        mapping = history.returns_for(currency)
        if mapping:
            returns[currency] = mapping
        else:
            missing.append(currency)
    if not returns:
        return {
            "available": False,
            "reason": "缺 " + "、".join(missing) + " 的本地日线历史（可跑 forecast.pipeline 补齐）",
            "models": [],
            "notes": [],
            "missing": missing,
        }

    covered = sum(abs(active[currency]) for currency in returns)
    total = sum(abs(value) for value in active.values())
    coverage = covered / total if total else 0.0
    currencies = sorted(returns)

    common = sorted(set.intersection(*(set(returns[currency]) for currency in currencies)))
    # 至少要 window 个估计样本 + horizon 天的实现期，否则第一天就没有 VaR 可谈。
    if len(common) < window + horizon + 1:
        return {
            "available": False,
            "reason": f"对齐后只有 {len(common)} 个共同交易日，不足 {window + horizon + 1}",
            "models": [],
            "notes": [],
            "missing": missing,
        }
    usable = len(common) - horizon - window + 1
    test_days = min(test_days, usable)
    if test_days < MIN_TEST_DAYS:
        return {
            "available": False,
            "reason": f"可用于回测的交易日只有 {test_days} 个，不足 {MIN_TEST_DAYS}",
            "models": [],
            "notes": [],
            "missing": missing,
        }

    matrix = {currency: [returns[currency][day] for day in common] for currency in currencies}
    prefixes = {currency: _prefix(matrix[currency]) for currency in currencies}
    squares = {(left, right): _prefix([x * y for x, y in zip(matrix[left], matrix[right])])
               for i, left in enumerate(currencies) for right in currencies[i:]}

    # 每个起点的 h 日组合损益，HS 的样本和"实际损益"共用这一个数组。
    pnl_by_start = []
    for start in range(len(common) - horizon + 1):
        total_pnl = 0.0
        for currency in currencies:
            cumulative = prefixes[currency][start + horizon] - prefixes[currency][start]
            total_pnl += active[currency] * math.expm1(cumulative)
        pnl_by_start.append(total_pnl)

    first_test = len(common) - horizon - test_days + 1
    test_index = list(range(first_test, len(common) - horizon + 1))
    dates = [common[index] for index in test_index]
    realized = [pnl_by_start[index] for index in test_index]

    states = _ewma_states(matrix, set(test_index), currencies)
    parametric_var = []
    ewma_var = []
    historical_var = []
    for index in test_index:
        start = index - window
        variances = {}
        covariances = {}
        for i, left in enumerate(currencies):
            variances[left] = _window_cov(prefixes[left], prefixes[left], squares[(left, left)], start, window)
            for right in currencies[i + 1:]:
                covariances[(left, right)] = _window_cov(
                    prefixes[left], prefixes[right], squares[(left, right)], start, window
                )
        sigma = _portfolio_sigma(active, currencies, variances, covariances)
        parametric_var.append(sigma)
        if index in states:
            ewma_variances, ewma_covariances = states[index]
            ewma_var.append(_portfolio_sigma(active, currencies, ewma_variances, ewma_covariances))
        else:
            ewma_var.append(None)
        samples = pnl_by_start[start:index - horizon + 1]
        historical_var.append(max(0.0, _quantile([-value for value in samples], confidence)))

    scale = math.sqrt(horizon)
    z_value = NormalDist().inv_cdf(confidence)
    for position, sigma in enumerate(parametric_var):
        parametric_var[position] = z_value * sigma * scale
    for position, sigma in enumerate(ewma_var):
        ewma_var[position] = None if sigma is None else z_value * sigma * scale

    hits = {
        "historical": [loss > var for loss, var in zip([-value for value in realized], historical_var)],
        "parametric": [loss > var for loss, var in zip([-value for value in realized], parametric_var)],
        "ewma": [
            (value is not None and loss > value)
            for loss, value in zip([-value for value in realized], ewma_var)
        ],
    }
    # EWMA 前 20 天没有估计值，那几天的"未突破"是缺失不是通过，单独扣掉。
    ewma_first = next((position for position, value in enumerate(ewma_var) if value is not None), None)
    models = [
        _model_result("historical", "历史模拟", hits["historical"], test_days, confidence),
        _model_result("parametric", "参数法（等权）", hits["parametric"], test_days, confidence),
    ]
    if ewma_first is not None:
        ewma_hits = hits["ewma"][ewma_first:]
        if len(ewma_hits) >= MIN_TEST_DAYS:
            models.append(_model_result("ewma", "参数法（EWMA λ=0.94）", ewma_hits, len(ewma_hits), confidence))

    notes = [
        "固定今天的头寸回测模型，不是回测历史头寸：它回答「这个模型靠不靠谱」，"
        "不回答「我们去年报的 VaR 准不准」。",
        f"估计窗口 {window} 个交易日，检验最近 {test_days} 个交易日，"
        f"每日 VaR 只用当天之前的收益估计（无前视偏差）。",
    ]
    if horizon > 1:
        notes.append(f"损益按 {horizon} 日重叠窗口计算，相邻观测相关，有效样本小于名义天数。")
    if missing:
        notes.append(
            "、".join(missing) + f" 缺本地日线，未计入；回测只覆盖 {coverage:.0%} 的净敞口，"
            "覆盖不全时的例外次数不能直接对照 Basel 分区。"
        )
    if test_days != BASEL_DAYS:
        notes.append(f"Basel 分区按 250 个交易日制定，这里是 {test_days} 天，只报统计量不给分区。")
    notes.append("回测通过不等于模型对：样本期内没发生过的冲击，它照样看不见。")

    return {
        "available": True,
        "asof": today.isoformat(),
        "confidence": confidence,
        "horizon_days": horizon,
        "window_days": window,
        "test_days": test_days,
        "expected": round(test_days * (1.0 - confidence), 2),
        "currencies": currencies,
        "missing": missing,
        "coverage": round(coverage, 4),
        "partial": bool(missing),
        "start": dates[0],
        "end": dates[-1],
        "models": models,
        "series": {
            "dates": dates,
            "loss": [round(-value, 2) for value in realized],
            "var": {
                "historical": [round(value, 2) for value in historical_var],
                "parametric": [round(value, 2) for value in parametric_var],
                "ewma": [None if value is None else round(value, 2) for value in ewma_var],
            },
            "exceptions": {key: [index for index, hit in enumerate(value) if hit] for key, value in hits.items()},
        },
        "notes": notes,
    }
