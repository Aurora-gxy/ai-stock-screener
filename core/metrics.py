"""指标注册表：所有可用于筛选的指标、单位、数据来源与统计口径。

这是 AI 层和筛选引擎之间的「契约」：LLM 只能输出这里登记过的指标 key，
任何未登记的指标都会在校验阶段被拒绝，不会进入筛选。
"""
from __future__ import annotations

METRICS: dict[str, dict] = {
    # ---------------- 基本面（来自财报接口）----------------
    "revenue_yoy": {
        "name": "营业收入同比增速", "unit": "%", "group": "经营",
        "source": "income-statements(period=quarterly).operating_income",
        "caliber": "最新报告期营业收入 / 上年同一报告期营业收入 - 1；两期取同一接口同一口径。上年同期 ≤ 0 时视为不可计算",
        "step": 5.0,
    },
    "profit_yoy": {
        "name": "归母净利润同比增速", "unit": "%", "group": "经营",
        "source": "income-statements(period=quarterly).parent_holder_net_profit",
        "caliber": "最新报告期归母净利润 / 上年同一报告期 - 1；上年同期 ≤ 0（亏损）时同比无意义，标记为不可计算",
        "step": 5.0,
    },
    "ocf": {
        "name": "经营活动现金流净额", "unit": "亿元", "group": "经营",
        "source": "cash-flow-statements(period=quarterly).act_cash_flow_net",
        "caliber": "最新报告期经营活动产生的现金流量净额，元 → 亿元",
        "step": 1.0,
    },
    "roe": {
        "name": "ROE（年报，近似）", "unit": "%", "group": "经营",
        "source": "income-statements(annual).parent_holder_net_profit / balance-sheets.holder_equity_total",
        "caliber": "最近年报归母净利润 / 同一年末所有者权益合计。分母含少数股东权益，结果偏保守（偏低）",
        "step": 2.0,
    },
    "debt_ratio": {
        "name": "资产负债率", "unit": "%", "group": "经营",
        "source": "balance-sheets(period=quarterly).total_debt / assets_total",
        "caliber": "最新报告期负债合计 / 资产总计。金融类公司天然偏高，跨行业比较需谨慎",
        "step": 5.0,
    },
    # ---------------- 估值（自行计算，扶摇未提供直接估值接口）----------------
    "pe": {
        "name": "市盈率 PE（静态）", "unit": "倍", "group": "估值",
        "source": "prices/snapshot.last_price / income-statements(annual).basic_eps",
        "caliber": "最新价 / 最近年报基本每股收益（静态 PE，非 TTM）。EPS ≤ 0 时 PE 无意义，按「不满足估值条件」处理",
        "step": 5.0,
    },
    "pe_rank": {
        "name": "PE 股票池内分位", "unit": "%", "group": "估值",
        "source": "由 pe 在当前股票池内横向排序得到",
        "caliber": "当前股票池内 PE>0 的股票中，PE 小于等于该股的比例（0=最便宜，100=最贵）。只代表相对位置，不代表绝对低估",
        "step": 10.0,
    },
    "pb": {
        "name": "市净率 PB（估算）", "unit": "倍", "group": "估值",
        "source": "last_price / (holder_equity_total / (parent_holder_net_profit / basic_eps))",
        "caliber": "总股本用「年报归母净利润 / 基本 EPS」反推；净资产含少数股东权益。为估算值，仅供参考",
        "step": 0.5,
    },
    # ---------------- 走势（来自日 K 线，前复权）----------------
    "ret_20d": {
        "name": "近20个交易日涨跌幅", "unit": "%", "group": "走势",
        "source": "prices/historical(1d, forward).close_price",
        "caliber": "最新收盘 / 20 个交易日前收盘 - 1（前复权）",
        "step": 5.0,
    },
    "ret_60d": {
        "name": "近60个交易日涨跌幅", "unit": "%", "group": "走势",
        "source": "prices/historical(1d, forward).close_price",
        "caliber": "最新收盘 / 60 个交易日前收盘 - 1（前复权）",
        "step": 5.0,
    },
    "vol_60d": {
        "name": "60日年化波动率", "unit": "%", "group": "走势",
        "source": "prices/historical(1d, forward).close_price",
        "caliber": "近 60 个交易日日收益率标准差 × √252",
        "step": 5.0,
    },
    "mdd_120d": {
        "name": "120日最大回撤", "unit": "%", "group": "走势",
        "source": "prices/historical(1d, forward).close_price",
        "caliber": "近 120 个交易日收盘价自高点的最大跌幅，以正数表示",
        "step": 5.0,
    },
    "ma60_dev": {
        "name": "收盘价相对60日均线偏离", "unit": "%", "group": "走势",
        "source": "prices/historical(1d, forward).close_price",
        "caliber": "最新收盘 / 近 60 日收盘均值 - 1",
        "step": 3.0,
    },
    "amount_20d": {
        "name": "20日日均成交额", "unit": "亿元", "group": "流动性",
        "source": "prices/historical(1d).turnover",
        "caliber": "近 20 个交易日成交额均值，元 → 亿元",
        "step": 1.0,
    },
    "chg_today": {
        "name": "最新涨跌幅", "unit": "%", "group": "走势",
        "source": "prices/snapshot.price_change_ratio_pct",
        "caliber": "快照最新涨跌幅（盘中为实时值，收盘后为当日值）",
        "step": 1.0,
    },
}

OPS = {">=": "≥", "<=": "≤", "between": "介于"}

# 常用股票池（标准指数 thscode，扶摇成分股接口支持）
UNIVERSES = {
    "上证50": "000016.SH",
    "沪深300": "000300.SH",
    "中证500": "000905.SH",
    "科创50": "000688.SH",
    "创业板指": "399006.SZ",
}
DEFAULT_UNIVERSE = "上证50"
BENCHMARK = {"上证50": "000016.SH", "沪深300": "000300.SH", "中证500": "000905.SH", "科创50": "000688.SH",
             "创业板指": "399006.SZ"}


def metric_label(key: str) -> str:
    m = METRICS.get(key)
    return f"{m['name']}（{m['unit']}）" if m else key


def describe_condition(c: dict) -> str:
    m = METRICS.get(c.get("metric"), {})
    name = m.get("name", c.get("metric"))
    unit = m.get("unit", "")
    op = c.get("op")
    if op == "between":
        return f"{name} 介于 {fmt(c.get('value'))}～{fmt(c.get('value2'))}{unit}"
    return f"{name} {OPS.get(op, op)} {fmt(c.get('value'))}{unit}"


def fmt(v) -> str:
    if v is None:
        return "—"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if abs(f) >= 100:
        return f"{f:.0f}"
    return f"{f:.2f}".rstrip("0").rstrip(".")
