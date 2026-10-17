"""确定性筛选引擎：逐股票、逐条件判定，不调用 LLM。

判定规则：
- 指标 status=ok        → 与阈值比较，得到「通过 / 未通过」
- 指标 status=na        → 「未通过」，原因写明为何无意义（如亏损导致 PE 无意义）
- 指标 status=missing/stale → 「无法判断」，原因写明缺失或过期；默认不入选，单独列出
股票整体结论：
- 入选：所有启用条件都「通过」
- 排除：至少一个条件「未通过」（或命中 ST 排除）
- 无法判断：没有「未通过」，但有「无法判断」的条件
"""
from __future__ import annotations

import copy

from .metrics import METRICS, describe_condition, fmt

PASS, FAIL, MISS = "通过", "未通过", "无法判断"


def check(cond: dict, f: dict | None) -> dict:
    m = METRICS[cond["metric"]]
    base = {"cond_id": cond["id"], "metric": cond["metric"], "desc": describe_condition(cond),
            "unit": m["unit"], "value": None, "source": (f or {}).get("source", m["source"]),
            "as_of": (f or {}).get("as_of", ""), "raw": (f or {}).get("raw", {}), "gap_steps": None}
    if not f:
        return base | {"status": MISS, "reason": "未获取到该指标"}
    st = f.get("status")
    if st in ("missing", "stale"):
        tag = "数据过期" if st == "stale" else "数据缺失"
        return base | {"status": MISS, "reason": f"{tag}：{f.get('reason')}"}
    if st == "na":
        return base | {"status": FAIL, "reason": f.get("reason") or "指标无意义"}
    v = float(f["value"])
    base["value"] = v
    lo, hi = float(cond["value"]), None
    if cond["op"] == ">=":
        ok = v >= lo
        gap = 0 if ok else (lo - v)
    elif cond["op"] == "<=":
        ok = v <= lo
        gap = 0 if ok else (v - lo)
    else:
        hi = float(cond["value2"])
        ok = lo <= v <= hi
        gap = 0 if ok else (lo - v if v < lo else v - hi)
    base["gap_steps"] = gap / m["step"] if m["step"] else None
    if ok:
        return base | {"status": PASS, "reason": f"实际值 {fmt(v)}{m['unit']}，满足「{base['desc']}」"}
    return base | {"status": FAIL, "reason": f"实际值 {fmt(v)}{m['unit']}，不满足「{base['desc']}」（差 {fmt(gap)}{m['unit']}）"}


def screen(ds: dict, conditions: list[dict], exclude_st: bool = True) -> dict:
    conds = [c for c in conditions if c.get("enabled", True)]
    rows = {}
    for s in ds["stocks"]:
        code = s["thscode"]
        feats = ds["features"].get(code, {})
        checks = [check(c, feats.get(c["metric"])) for c in conds]
        st_hit = exclude_st and s.get("is_st")
        n_fail = sum(1 for x in checks if x["status"] == FAIL)
        n_miss = sum(1 for x in checks if x["status"] == MISS)
        if st_hit:
            verdict = "排除"
        elif n_fail:
            verdict = "排除"
        elif n_miss:
            verdict = "无法判断"
        else:
            verdict = "入选"
        rows[code] = {"thscode": code, "name": s["name"], "checks": checks, "verdict": verdict,
                      "st_excluded": bool(st_hit), "n_fail": n_fail, "n_miss": n_miss}
    selected = [c for c, r in rows.items() if r["verdict"] == "入选"]
    excluded = [c for c, r in rows.items() if r["verdict"] == "排除"]
    undetermined = [c for c, r in rows.items() if r["verdict"] == "无法判断"]

    stats = []
    for i, c in enumerate(conds):
        col = [r["checks"][i]["status"] for r in rows.values()]
        stats.append({"cond_id": c["id"], "desc": describe_condition(c), "通过": col.count(PASS),
                      "未通过": col.count(FAIL), "无法判断": col.count(MISS)})
    # 逐层漏斗（按条件顺序）
    funnel, alive = [], [c for c, r in rows.items() if not r["st_excluded"]]
    funnel.append({"step": "股票池" + ("（已排除 ST）" if exclude_st else ""), "剩余": len(alive)})
    for i, c in enumerate(conds):
        alive = [code for code in alive if rows[code]["checks"][i]["status"] == PASS]
        funnel.append({"step": describe_condition(c), "剩余": len(alive)})

    near = []
    for code in excluded:
        r = rows[code]
        if r["st_excluded"] or r["n_fail"] != 1 or r["n_miss"]:
            continue
        f = next(x for x in r["checks"] if x["status"] == FAIL)
        if f["gap_steps"] is not None and f["gap_steps"] <= 1:
            near.append({"thscode": code, "name": r["name"], "卡在": f["desc"], "说明": f["reason"]})
    return {"rows": rows, "selected": selected, "excluded": excluded, "undetermined": undetermined,
            "stats": stats, "funnel": funnel, "near_miss": near, "conditions": copy.deepcopy(conds),
            "exclude_st": exclude_st}


def _shift(cond: dict, direction: int) -> dict:
    """direction=+1 放宽，-1 收紧，按指标 step 移动阈值。"""
    c = copy.deepcopy(cond)
    step = METRICS[c["metric"]]["step"] * direction
    if c["op"] == ">=":
        c["value"] = float(c["value"]) - step
    elif c["op"] == "<=":
        c["value"] = float(c["value"]) + step
    else:
        c["value"] = float(c["value"]) - step
        c["value2"] = float(c["value2"]) + step
        if c["value"] > c["value2"]:
            mid = (float(cond["value"]) + float(cond["value2"])) / 2
            c["value"] = c["value2"] = mid
    return c


def sensitivity(ds: dict, conditions: list[dict], exclude_st: bool = True) -> list[dict]:
    """每个条件单独放宽 / 收紧一档、或删除时，入选结果如何变化。"""
    base = set(screen(ds, conditions, exclude_st)["selected"])
    names = {s["thscode"]: s["name"] for s in ds["stocks"]}
    out = []
    enabled = [c for c in conditions if c.get("enabled", True)]
    for c in enabled:
        row = {"cond_id": c["id"], "条件": describe_condition(c), "当前入选": len(base)}
        for label, variant in (("放宽一档", _shift(c, +1)), ("收紧一档", _shift(c, -1)), ("删除该条件", None)):
            new_conds = [variant if x["id"] == c["id"] else x for x in enabled] if variant else \
                [x for x in enabled if x["id"] != c["id"]]
            sel = set(screen(ds, new_conds, exclude_st)["selected"])
            added = [names[x] for x in sel - base]
            removed = [names[x] for x in base - sel]
            row[label] = len(sel)
            row[label + "_新阈值"] = describe_condition(variant) if variant else "（不限）"
            row[label + "_新增"] = added
            row[label + "_移出"] = removed
        out.append(row)
    return out


def binding_conditions(sens: list[dict]) -> list[str]:
    """结果为空或很少时，指出删除后结果增加最多的条件。"""
    ranked = sorted(sens, key=lambda r: r["删除该条件"] - r["当前入选"], reverse=True)
    return [f"{r['条件']}：删除后入选数 {r['当前入选']} → {r['删除该条件']}；放宽一档（{r['放宽一档_新阈值']}）→ {r['放宽一档']}"
            for r in ranked if r["删除该条件"] > r["当前入选"]][:3]
