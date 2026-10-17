"""意图解析（AI 层）：自然语言 → 结构化、可检查、可修改的条件。

两条路径，同时运行、互相校验：
- LLM 路径（DeepSeek）：理解自由表达，输出 JSON；输出必须通过 validate_conditions 校验，
  未登记的指标 / 非法运算符 / 非数值阈值一律丢弃并给出警告。
- 规则路径：基于模糊词表 + 显式数值正则，完全确定、可复现；LLM 不可用或输出不合法时兜底。

两条路径的差异会展示给用户（「AI 解析核对」），由用户最终确认条件。
LLM 只决定「用户想要什么条件」，不接触任何行情 / 财务数字。
"""
from __future__ import annotations

import copy
import json
import re
import uuid

from .compliance import check_input
from .metrics import METRICS, UNIVERSES


def _c(metric, op, value, value2=None):
    return {"metric": metric, "op": op, "value": value, "value2": value2}


# 模糊词表：每个模糊表达有多种可解释口径，第一项为默认。用户可在「澄清」里切换。
FUZZY_TERMS: list[dict] = [
    {"term": "经营改善", "aliases": ["经营改善", "业绩改善", "基本面改善", "业绩向好", "经营向好", "业绩增长", "业绩好转", "经营好转"],
     "question": "「经营改善」你更倾向哪种口径？",
     "options": [
         {"label": "营收、归母净利润同比都为正（默认）", "conditions": [_c("revenue_yoy", ">=", 0), _c("profit_yoy", ">=", 0)]},
         {"label": "更严格：营收、净利同比都 ≥10%，且经营现金流为正",
          "conditions": [_c("revenue_yoy", ">=", 10), _c("profit_yoy", ">=", 10), _c("ocf", ">=", 0)]},
         {"label": "只看利润：归母净利润同比 ≥ 0", "conditions": [_c("profit_yoy", ">=", 0)]},
     ]},
    {"term": "估值合理", "aliases": ["估值合理", "估值适中", "估值不高", "估值不贵", "不贵", "估值合适", "估值正常"],
     "question": "「估值合理」用相对口径还是绝对口径？",
     "options": [
         {"label": "相对：PE 为正，且位于股票池内较便宜的 70%（默认）", "conditions": [_c("pe_rank", "<=", 70)]},
         {"label": "绝对：静态 PE 介于 0～30 倍", "conditions": [_c("pe", "between", 0.01, 30)]},
         {"label": "相对且更严：PE 位于股票池较便宜的 50%", "conditions": [_c("pe_rank", "<=", 50)]},
     ]},
    {"term": "低估", "aliases": ["低估值", "低估", "便宜", "估值低", "估值便宜"],
     "question": "「低估」用哪种口径？",
     "options": [
         {"label": "相对：PE 位于股票池最便宜的 30%（默认）", "conditions": [_c("pe_rank", "<=", 30)]},
         {"label": "绝对：静态 PE 介于 0～15 倍", "conditions": [_c("pe", "between", 0.01, 15)]},
         {"label": "PB ≤ 1.5 且 PE 位于股票池较便宜的 50%", "conditions": [_c("pb", "<=", 1.5), _c("pe_rank", "<=", 50)]},
     ]},
    {"term": "走势稳定", "aliases": ["走势相对稳定", "走势稳定", "走势平稳", "走势稳健", "波动小", "低波动", "波动较小", "股价稳定", "相对稳定"],
     "question": "「走势相对稳定」要多稳？",
     "options": [
         {"label": "60日年化波动率 ≤30% 且 120日最大回撤 ≤20%（默认）",
          "conditions": [_c("vol_60d", "<=", 30), _c("mdd_120d", "<=", 20)]},
         {"label": "只看波动：60日年化波动率 ≤25%", "conditions": [_c("vol_60d", "<=", 25)]},
         {"label": "宽松：波动率 ≤40% 且回撤 ≤30%", "conditions": [_c("vol_60d", "<=", 40), _c("mdd_120d", "<=", 30)]},
     ]},
    {"term": "高成长", "aliases": ["高成长", "高增长", "成长性好", "快速增长", "成长股"],
     "question": "「高成长」的门槛？",
     "options": [
         {"label": "营收、归母净利同比都 ≥20%（默认）", "conditions": [_c("revenue_yoy", ">=", 20), _c("profit_yoy", ">=", 20)]},
         {"label": "只看利润：归母净利同比 ≥30%", "conditions": [_c("profit_yoy", ">=", 30)]},
         {"label": "宽松：营收同比 ≥15%", "conditions": [_c("revenue_yoy", ">=", 15)]},
     ]},
    {"term": "现金流好", "aliases": ["现金流好", "现金流健康", "现金流充裕", "现金流为正", "现金流不错"],
     "question": "「现金流好」的口径？",
     "options": [{"label": "最新报告期经营现金流净额 > 0（默认）", "conditions": [_c("ocf", ">=", 0)]},
                 {"label": "经营现金流净额 ≥ 10 亿元", "conditions": [_c("ocf", ">=", 10)]}]},
    {"term": "低负债", "aliases": ["低负债", "负债率低", "负债低", "财务稳健", "杠杆低", "低杠杆"],
     "question": "「低负债」的门槛？（注意：银行、券商等金融股负债率天然很高）",
     "options": [{"label": "资产负债率 ≤60%（默认）", "conditions": [_c("debt_ratio", "<=", 60)]},
                 {"label": "资产负债率 ≤40%", "conditions": [_c("debt_ratio", "<=", 40)]}]},
    {"term": "盈利能力强", "aliases": ["盈利能力强", "高ROE", "ROE高", "赚钱能力强", "盈利能力好", "盈利好"],
     "question": "「盈利能力强」的门槛？",
     "options": [{"label": "ROE（年报）≥12%（默认）", "conditions": [_c("roe", ">=", 12)]},
                 {"label": "ROE（年报）≥15%", "conditions": [_c("roe", ">=", 15)]}]},
    {"term": "趋势向上", "aliases": ["趋势向上", "走势强", "强势", "上升趋势", "多头", "走势向上", "趋势好"],
     "question": "「趋势向上」的口径？",
     "options": [{"label": "收盘价在 60 日均线上方，且近 60 日涨跌幅 ≥0（默认）",
                  "conditions": [_c("ma60_dev", ">=", 0), _c("ret_60d", ">=", 0)]},
                 {"label": "近 20 日涨幅 ≥5%", "conditions": [_c("ret_20d", ">=", 5)]}]},
    {"term": "超跌", "aliases": ["超跌", "跌得多", "回调充分", "跌幅较大", "跌多了"],
     "question": "「超跌」的口径？",
     "options": [{"label": "近 60 日跌幅 ≥15%（默认）", "conditions": [_c("ret_60d", "<=", -15)]},
                 {"label": "120 日最大回撤 ≥25%", "conditions": [_c("mdd_120d", ">=", 25)]}]},
    {"term": "流动性好", "aliases": ["流动性好", "成交活跃", "交易活跃", "成交额大", "流动性强"],
     "question": "「流动性好」的门槛？",
     "options": [{"label": "20 日日均成交额 ≥3 亿元（默认）", "conditions": [_c("amount_20d", ">=", 3)]},
                 {"label": "20 日日均成交额 ≥10 亿元", "conditions": [_c("amount_20d", ">=", 10)]}]},
]

# 当前未接入数据、不能执行的诉求：明确告知，不静默忽略
UNSUPPORTED = [
    (r"龙头|行业地位|市占率", "行业地位 / 龙头", "暂无可量化的行业地位数据"),
    (r"政策|受益|利好", "政策受益", "政策影响属于主观判断，未接入可结构化的政策数据"),
    (r"题材|概念|热点|风口", "题材 / 概念", "本版本未把概念板块作为筛选条件（扶摇有概念板块列表，可作为后续扩展）"),
    (r"机构重仓|基金重仓|北向|外资|主力资金|资金流入", "资金 / 持仓", "资金流向接口在扶摇文档中标注为客户端专用，本版本未接入"),
    (r"分红|股息|高息", "分红 / 股息率", "本版本未接入分红数据（扶摇 corporate-actions 可作为后续扩展）"),
    (r"市值", "市值", "扶摇快照不含总股本 / 市值字段，本版本无法精确筛选市值"),
    (r"管理层|治理|品牌|护城河", "定性因素", "定性因素无法转为可验证的数据条件"),
]

METRIC_SYNONYMS = [
    ("pe", r"(?:市盈率|PE|pe|P/E)"),
    ("pb", r"(?:市净率|PB|pb|P/B)"),
    ("roe", r"(?:ROE|roe|净资产收益率)"),
    ("debt_ratio", r"(?:资产负债率|负债率)"),
    ("revenue_yoy", r"(?:营收(?:同比)?(?:增速|增长率?)|营业收入(?:同比)?(?:增速|增长率?)|收入增速)"),
    ("profit_yoy", r"(?:(?:归母)?净利润?(?:同比)?(?:增速|增长率?)|利润增速)"),
    ("vol_60d", r"(?:波动率)"),
    ("mdd_120d", r"(?:最大回撤|回撤)"),
    ("ret_60d", r"(?:60日涨幅|近60日涨跌幅|三个月涨幅)"),
    ("ret_20d", r"(?:20日涨幅|近20日涨跌幅|一个月涨幅)"),
    ("amount_20d", r"(?:日均成交额|成交额)"),
]
LE = r"(?:低于|小于|不超过|不高于|不到|<=|≤|<|在)"
GE = r"(?:高于|大于|超过|不低于|至少|>=|≥|>)"
NUM = r"(-?\d+(?:\.\d+)?)"
UNIT = r"\s*(?:%|％|倍|亿元?|个点)?"


def new_id() -> str:
    return uuid.uuid4().hex[:8]


def _with_meta(conds: list[dict], term: str | None, phrase: str, kind: str) -> list[dict]:
    out = []
    for c in conds:
        c = copy.deepcopy(c)
        c.update({"id": new_id(), "enabled": True, "term": term, "origin_phrase": phrase, "kind": kind})
        out.append(c)
    return out


def detect_universe(text: str) -> tuple[str | None, str | None]:
    for name in UNIVERSES:
        if name in text:
            return name, None
    if re.search(r"蓝筹|大盘|权重", text):
        return "上证50", "「蓝筹 / 大盘」被映射为上证50 成分股（可在左侧修改）"
    if re.search(r"中盘|中小盘", text):
        return "中证500", "「中盘」被映射为中证500 成分股（可修改）"
    if re.search(r"科创", text):
        return "科创50", None
    if re.search(r"创业板", text):
        return "创业板指", None
    return None, None


def parse_explicit(text: str) -> list[dict]:
    """显式数值条件，如「PE 低于 20」「ROE 大于 15%」「负债率 40-60%」。"""
    conds = []
    for metric, syn in METRIC_SYNONYMS:
        used: list[tuple[int, int]] = []

        def free(m):
            return all(m.end() <= a or m.start() >= b for a, b in used)

        for m in re.finditer(syn + r"\s*(?:要|在|处于)?\s*" + NUM + r"\s*(?:到|至|-|~|～)\s*" + NUM + UNIT, text):
            a, b = float(m.group(1)), float(m.group(2))
            conds.append(_c(metric, "between", min(a, b), max(a, b)) | {"_phrase": m.group(0)})
            used.append(m.span())
        patterns = [
            ("<=", syn + r"\s*(?:要|需要)?\s*" + LE + r"\s*" + NUM + UNIT + r"(?:以下|以内)?"),
            ("<=", syn + r"\s*" + NUM + UNIT + r"\s*(?:以下|以内)"),
            (">=", syn + r"\s*(?:要|需要)?\s*" + GE + r"\s*" + NUM + UNIT),
            (">=", syn + r"\s*" + NUM + UNIT + r"\s*以上"),
        ]
        for op, pat in patterns:
            for m in re.finditer(pat, text):
                if free(m):
                    conds.append(_c(metric, op, float(m.group(1))) | {"_phrase": m.group(0)})
                    used.append(m.span())
    return conds


def rule_parse(text: str) -> dict:
    text = text or ""
    result = {"source": "rule", "conditions": [], "clarifications": [], "unsupported": [], "notes": []}
    explicit = parse_explicit(text)
    explicit_metrics = {c["metric"] for c in explicit}
    for c in explicit:
        phrase = c.pop("_phrase")
        result["conditions"] += _with_meta([c], None, phrase, "explicit")

    consumed = text
    for ft in FUZZY_TERMS:
        hit = next((a for a in sorted(ft["aliases"], key=len, reverse=True) if a in consumed), None)
        if not hit:
            continue
        consumed = consumed.replace(hit, "　" * len(hit))  # 防止「低估值」和「估值」重复命中
        default = [c for c in ft["options"][0]["conditions"] if c["metric"] not in explicit_metrics]
        if len(default) < len(ft["options"][0]["conditions"]):
            result["notes"].append(f"「{hit}」中与显式数值重复的指标已以显式数值为准")
        result["conditions"] += _with_meta(default, ft["term"], hit, "default_interpretation")
        if len(ft["options"]) > 1:
            result["clarifications"].append({
                "term": ft["term"], "phrase": hit, "question": ft["question"],
                "options": ft["options"], "selected": 0,
            })
    for pat, label, reason in UNSUPPORTED:
        m = re.search(pat, text)
        if m:
            result["unsupported"].append({"phrase": m.group(0), "label": label, "reason": reason})
    return result


# ---------------------------------------------------------------- 校验（LLM 输出必须过这一关）
def validate_condition(c: dict) -> tuple[dict | None, str | None]:
    if not isinstance(c, dict):
        return None, "条件不是对象"
    metric = c.get("metric")
    if metric not in METRICS:
        return None, f"未登记的指标「{metric}」已丢弃（AI 不能发明数据字段）"
    op = c.get("op")
    if op not in (">=", "<=", "between"):
        return None, f"指标 {metric} 的运算符「{op}」非法，已丢弃"
    try:
        v = float(c.get("value"))
    except (TypeError, ValueError):
        return None, f"指标 {metric} 的阈值「{c.get('value')}」不是数字，已丢弃"
    v2 = None
    if op == "between":
        try:
            v2 = float(c.get("value2"))
        except (TypeError, ValueError):
            return None, f"指标 {metric} 为区间条件但缺少上限，已丢弃"
        if v2 < v:
            v, v2 = v2, v
    out = {"metric": metric, "op": op, "value": v, "value2": v2}
    return out, None


SYSTEM_PROMPT = """你是 A 股选股产品里的「意图解析器」。你的唯一任务：把用户的自然语言选股意图转成结构化筛选条件 JSON。
严格规则：
1. 只能使用下列指标 key，不得发明新指标：
{metrics}
2. op 只能是 ">="、"<="、"between"（between 需提供 value 和 value2）。阈值单位与指标单位一致（百分比写 15 表示 15%）。
3. 对模糊表达，优先采用下面的默认口径，并把它列入 clarifications，让用户确认：
{fuzzy}
4. 无法用上述指标表达的诉求（如行业地位、政策、题材、资金流、市值、分红），放进 unsupported 并说明原因，不要强行映射。
5. 若用户要求预测涨跌、承诺收益或直接给买卖建议，在 compliance 中说明拒绝，只保留可客观筛选的部分。
6. 你不知道任何股票的实际数据，不得输出任何股票代码、名称或数据数值。
只输出 JSON，格式：
{{"universe": "上证50|沪深300|中证500|科创50|创业板指|null",
  "conditions": [{{"metric": "...", "op": "...", "value": 0, "value2": null, "origin_phrase": "用户原文片段", "term": "对应模糊词或null", "rationale": "为什么这样映射"}}],
  "clarifications": [{{"term": "...", "question": "..."}}],
  "unsupported": [{{"phrase": "...", "reason": "..."}}],
  "compliance": ["..."]}}"""


def build_system_prompt() -> str:
    metrics = "\n".join(f"- {k}: {m['name']}（单位 {m['unit']}；口径：{m['caliber']}）" for k, m in METRICS.items())
    fuzzy = "\n".join(
        f"- {ft['term']}（同义：{'/'.join(ft['aliases'][:4])}）→ 默认：{ft['options'][0]['label']} = "
        + json.dumps(ft["options"][0]["conditions"], ensure_ascii=False)
        for ft in FUZZY_TERMS)
    return SYSTEM_PROMPT.format(metrics=metrics, fuzzy=fuzzy)


def llm_parse(text: str, llm_call) -> dict:
    """llm_call(system, user) -> str。返回经过校验的结果；失败抛异常由上层兜底。"""
    raw = llm_call(build_system_prompt(), text)
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        raise ValueError("LLM 输出中找不到 JSON")
    data = json.loads(m.group(0))
    result = {"source": "llm", "conditions": [], "clarifications": [], "unsupported": [], "notes": [],
              "llm_raw": raw, "universe": data.get("universe") if data.get("universe") in UNIVERSES else None}
    for c in data.get("conditions") or []:
        ok, err = validate_condition(c)
        if err:
            result["notes"].append(err)
            continue
        term = c.get("term") if c.get("term") in {t["term"] for t in FUZZY_TERMS} else None
        result["conditions"] += _with_meta([ok], term, str(c.get("origin_phrase") or ""),
                                           "default_interpretation" if term else "explicit")
        if c.get("rationale"):
            result["conditions"][-1]["rationale"] = str(c["rationale"])[:200]
    # 澄清选项以确定性词表为准（保证每个选项都能执行）
    seen = set()
    for cl in data.get("clarifications") or []:
        ft = next((t for t in FUZZY_TERMS if t["term"] == (cl or {}).get("term")), None)
        if ft and ft["term"] not in seen and len(ft["options"]) > 1:
            seen.add(ft["term"])
            result["clarifications"].append({"term": ft["term"], "phrase": ft["term"], "question": ft["question"],
                                             "options": ft["options"], "selected": 0})
    for u in data.get("unsupported") or []:
        if isinstance(u, dict) and u.get("phrase"):
            result["unsupported"].append({"phrase": str(u["phrase"]), "label": str(u["phrase"]),
                                          "reason": str(u.get("reason") or "")})
    result["llm_compliance"] = [str(x) for x in (data.get("compliance") or [])]
    if not result["conditions"]:
        raise ValueError("LLM 未产出任何合法条件")
    return result


def diff_parses(a: dict, b: dict) -> list[str]:
    """比较 LLM 与规则解析的条件差异，供用户核对。"""
    def key(c):
        return (c["metric"], c["op"], round(float(c["value"]), 4), None if c.get("value2") is None else round(float(c["value2"]), 4))
    ka, kb = {key(c) for c in a["conditions"]}, {key(c) for c in b["conditions"]}
    from .metrics import describe_condition
    out = []
    for k in sorted(ka - kb, key=str):
        out.append("仅 AI 解析有：" + describe_condition({"metric": k[0], "op": k[1], "value": k[2], "value2": k[3]}))
    for k in sorted(kb - ka, key=str):
        out.append("仅规则解析有：" + describe_condition({"metric": k[0], "op": k[1], "value": k[2], "value2": k[3]}))
    return out


def parse_intent(text: str, llm_call=None) -> dict:
    """主入口：合规检查 → 规则解析 → （可选）LLM 解析并校验 → 选择结果 + 差异。"""
    compliance = check_input(text)
    rule = rule_parse(text)
    universe, uni_note = detect_universe(text)
    result = rule
    llm_error = None
    if llm_call is not None:
        try:
            llm = llm_parse(text, llm_call)
            llm["diff_vs_rule"] = diff_parses(llm, rule)
            # 规则识别到但 LLM 漏掉的不支持项，也要告知
            have = {u["label"] for u in llm["unsupported"]} | {u["phrase"] for u in llm["unsupported"]}
            for u in rule["unsupported"]:
                if u["label"] not in have and u["phrase"] not in have:
                    llm["unsupported"].append(u)
            result = llm
            universe = llm.get("universe") or universe
        except Exception as e:  # LLM 失败 → 规则兜底，并显式告知
            llm_error = f"{type(e).__name__}: {str(e)[:150]}"
    result["compliance"] = compliance
    result["universe"] = universe
    if uni_note:
        result["notes"].append(uni_note)
    result["exclude_st"] = True
    result["llm_error"] = llm_error
    result["raw_text"] = text
    return result


def apply_clarification(conditions: list[dict], clar: dict, option_index: int) -> list[dict]:
    """把某个模糊词对应的条件替换为用户选择的口径。"""
    term = clar["term"]
    kept = [c for c in conditions if c.get("term") != term]
    opt = clar["options"][option_index]
    return kept + _with_meta(opt["conditions"], term, clar.get("phrase") or term, "clarified")
