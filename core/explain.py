"""解释层。

- 逐股「入选 / 排除」解释：完全由引擎结果模板化生成（确定性，可追溯到字段）
- 整体 AI 摘要（可选）：LLM 只能基于引擎给出的 facts JSON 改写成人话；
  生成后做两道校验：① 数字回查——文中每个数字必须能在 facts 中找到；② 合规审查。
  不通过的数字会被标出，页面提示「以表格为准」。
"""
from __future__ import annotations

import json
import re

from .compliance import sanitize_output
from .metrics import METRICS, fmt


def stock_explanation(row: dict) -> str:
    lines = []
    if row["st_excluded"]:
        lines.append("- 排除：名称含 ST，按默认规则剔除（可在左侧取消）")
    for ck in row["checks"]:
        icon = {"通过": "✅", "未通过": "❌", "无法判断": "⚠️"}[ck["status"]]
        lines.append(f"- {icon} {ck['reason']}｜时点：{ck['as_of'] or '—'}")
    head = {"入选": "全部条件满足，入选。", "排除": "存在未满足条件，被排除。",
            "无法判断": "没有明确不满足的条件，但部分数据缺失 / 过期，无法判断，未入选。"}[row["verdict"]]
    return head + "\n" + "\n".join(lines)


def build_facts(ds: dict, res: dict, sens: list[dict] | None = None, limit: int = 15) -> dict:
    sel = []
    for code in res["selected"][:limit]:
        r = res["rows"][code]
        sel.append({"name": r["name"], "code": code,
                    "metrics": {ck["metric"]: round(ck["value"], 2) for ck in r["checks"] if ck["value"] is not None}})
    return {
        "股票池": ds.get("universe"), "股票池数量": len(ds["stocks"]),
        "条件": [s["desc"] for s in res["stats"]],
        "入选数": len(res["selected"]), "排除数": len(res["excluded"]), "无法判断数": len(res["undetermined"]),
        "各条件通过情况": res["stats"], "漏斗": res["funnel"],
        "入选股票": sel,
        "差一点入选": res["near_miss"][:5],
        "敏感性": [{k: v for k, v in r.items() if not k.endswith("_新增") and not k.endswith("_移出")} for r in (sens or [])],
        "数据时点": {"快照": ds.get("snapshot_asof")},
    }


SUMMARY_PROMPT = """你是选股结果的解释助手。下面是确定性筛选引擎输出的 facts（JSON）。请用中文写一段 150～250 字的结果说明：
1. 只能使用 facts 中出现的数字，不得计算新数字、不得引入外部信息；
2. 用「事实：」「推断：」「不确定：」三个小段分开写；推断必须是对条件和结果关系的解释，不能涉及未来股价；
3. 不得预测涨跌、不得给出买卖建议或目标价、不得承诺收益；
4. 指出哪个条件筛掉最多股票、放宽哪个条件影响最大、哪些数据无法判断。"""


def _numbers_in(obj) -> set[str]:
    s = json.dumps(obj, ensure_ascii=False)
    nums = set()
    for x in re.findall(r"-?\d+(?:\.\d+)?", s):
        f = float(x)
        for v in (f, abs(f)):
            nums.add(f"{v:.0f}")
            nums.add(f"{v:.1f}")
            nums.add(f"{v:.2f}")
    return nums


def verify_numbers(text: str, facts: dict) -> list[str]:
    allowed = _numbers_in(facts) | {"20", "60", "120", "252", "50", "300", "500", "100"}
    bad = []
    for x in re.findall(r"-?\d+(?:\.\d+)?", text):
        f = abs(float(x))
        cands = {f"{f:.0f}", f"{f:.1f}", f"{f:.2f}", x.lstrip("-")}
        if not cands & allowed:
            bad.append(x)
    return bad


def ai_summary(llm_call, facts: dict) -> dict:
    raw = llm_call(SUMMARY_PROMPT, json.dumps(facts, ensure_ascii=False), json_mode=False)
    text, flags = sanitize_output(raw)
    bad = verify_numbers(text, facts)
    return {"text": text, "compliance_flags": flags, "unverified_numbers": bad, "raw": raw}


def fact_line(metric: str, ck: dict) -> str:
    m = METRICS[metric]
    return f"{m['name']} = {fmt(ck['value'])}{m['unit']}（来源：{ck['source']}；时点：{ck['as_of']}）"
