"""条件冲突识别（确定性规则）。

三个等级：
- 冲突：逻辑上不可能同时满足（结果必然为空），必须修改后才能执行
- 张力：理论上可同时满足，但通常互相制约，结果可能很少
- 提示：口径或范围上需要注意的地方
"""
from __future__ import annotations

import math

from .metrics import METRICS, describe_condition

RANGES = {  # 指标取值的合理范围
    "pe_rank": (0, 100), "vol_60d": (0, math.inf), "mdd_120d": (0, 100), "debt_ratio": (0, 100),
    "pe": (0, math.inf), "pb": (0, math.inf), "amount_20d": (0, math.inf),
}


def _interval(c: dict) -> tuple[float, float]:
    v = float(c["value"])
    if c["op"] == ">=":
        return v, math.inf
    if c["op"] == "<=":
        return -math.inf, v
    return v, float(c["value2"])


def _bound(conds, metric):
    lo, hi, ids = -math.inf, math.inf, []
    for c in conds:
        if c["metric"] == metric:
            a, b = _interval(c)
            lo, hi = max(lo, a), min(hi, b)
            ids.append(c["id"])
    return lo, hi, ids


def detect_conflicts(conditions: list[dict], universe: str | None = None) -> list[dict]:
    conds = [c for c in conditions if c.get("enabled", True)]
    out: list[dict] = []
    metrics = {c["metric"] for c in conds}

    # 1) 同一指标区间无交集
    for m in metrics:
        lo, hi, ids = _bound(conds, m)
        if lo > hi:
            out.append({"level": "冲突", "condition_ids": ids,
                        "message": f"「{METRICS[m]['name']}」的多个条件没有交集（要求 ≥{lo:g} 且 ≤{hi:g}），结果必然为空。"})
        rng = RANGES.get(m)
        if rng and (hi < rng[0] or lo > rng[1]):
            out.append({"level": "冲突", "condition_ids": ids,
                        "message": f"「{METRICS[m]['name']}」的取值范围是 {rng[0]}～{rng[1]}，当前条件不可能满足。"})
        elif rng and m in ("pe", "pb") and hi <= 0:
            out.append({"level": "冲突", "condition_ids": ids,
                        "message": f"「{METRICS[m]['name']}」≤0 的公司为亏损或净资产为负，本产品按「估值无意义」处理，不会入选。"})

    # 2) 跨指标的数学矛盾：近60日跌幅 ≥ y% 必然意味着 120 日最大回撤 ≥ y%
    if "ret_60d" in metrics and "mdd_120d" in metrics:
        _, r_hi, r_ids = _bound(conds, "ret_60d")
        _, d_hi, d_ids = _bound(conds, "mdd_120d")
        if r_hi < 0 and d_hi < -r_hi:
            out.append({"level": "冲突", "condition_ids": r_ids + d_ids,
                        "message": f"要求近60日跌幅至少 {-r_hi:g}%，但又要求120日最大回撤不超过 {d_hi:g}%。"
                                   f"60 日内下跌 {-r_hi:g}% 本身就构成至少 {-r_hi:g}% 的回撤，二者不可能同时满足。"})

    # 3) 张力
    def has(metric, op=None, pred=None):
        for c in conds:
            if c["metric"] == metric and (op is None or c["op"] == op) and (pred is None or pred(float(c["value"]))):
                return c
        return None

    growth = has("revenue_yoy", ">=", lambda v: v >= 20) or has("profit_yoy", ">=", lambda v: v >= 20)
    cheap = has("pe_rank", "<=", lambda v: v <= 30) or has("pe", "<=", lambda v: v <= 15) \
        or (has("pe", "between") if has("pe", "between") and float(has("pe", "between")["value2"]) <= 15 else None)
    if growth and cheap:
        out.append({"level": "张力", "condition_ids": [growth["id"], cheap["id"]],
                    "message": "同时要求「高增长」和「低估值」：市场通常给高增长公司更高估值，满足两者的股票往往很少。"})
    calm = has("vol_60d", "<=", lambda v: v <= 25)
    hot = has("ret_20d", ">=", lambda v: v >= 10) or has("ret_60d", ">=", lambda v: v >= 20)
    if calm and hot:
        out.append({"level": "张力", "condition_ids": [calm["id"], hot["id"]],
                    "message": "同时要求「低波动」和「短期大涨」：大幅上涨通常伴随较高波动，结果可能很少。"})
    above = has("ma60_dev", ">=", lambda v: v >= 0)
    fell = has("ret_60d", "<=", lambda v: v <= -10)
    if above and fell:
        out.append({"level": "张力", "condition_ids": [above["id"], fell["id"]],
                    "message": "同时要求「站上 60 日均线」和「近 60 日明显下跌」：只有近期快速反弹的股票才可能满足。"})

    # 4) 提示
    if has("debt_ratio", "<=") and universe in ("上证50", "沪深300"):
        out.append({"level": "提示", "condition_ids": [has("debt_ratio", "<=")["id"]],
                    "message": "资产负债率条件会系统性排除银行、券商、保险等金融股（其负债率天然在 85% 以上），这是口径特性而非公司经营问题。"})
    if has("pe_rank"):
        out.append({"level": "提示", "condition_ids": [has("pe_rank")["id"]],
                    "message": "PE 分位是股票池内的相对位置，换股票池或股票池整体估值变化时，同一只股票的分位会变化。"})
    if any(c["metric"] in ("revenue_yoy", "profit_yoy") for c in conds):
        out.append({"level": "提示", "condition_ids": [],
                    "message": "同比增速基于各公司「最新已披露」报告期，不同公司的最新报告期可能不同（详见每只股票的时点）。"})
    return out


def has_blocking(conflicts: list[dict]) -> bool:
    return any(c["level"] == "冲突" for c in conflicts)


def summarize(conflicts):
    return [f"[{c['level']}] {c['message']}" for c in conflicts]


__all__ = ["detect_conflicts", "has_blocking", "summarize", "describe_condition"]
