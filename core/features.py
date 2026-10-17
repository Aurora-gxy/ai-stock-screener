"""指标计算层：把扶摇原始字段算成可筛选的指标。

原则：
1. 每个指标值都是一个 Feature 字典：value / status / reason / source / as_of / raw
2. status 只有四种：
   - ok      正常可用
   - na      数据存在但指标无意义（例如 EPS ≤ 0 时的 PE）→ 引擎按「不满足」处理并说明原因
   - missing 数据缺失 / 接口失败 / 样本不足 → 引擎按「无法判断」处理，绝不当作通过
   - stale   数据过期（财报超过 200 天无更新、K 线超过 10 天无更新）→ 同样按「无法判断」
3. 本模块只做确定性计算，不调用 LLM。
"""
from __future__ import annotations

import math
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable

from .fuyao import FetchResult, FuyaoClient
from .metrics import METRICS

STALE_REPORT_DAYS = 200
STALE_KLINE_DAYS = 10
DAY_MS = 86400000


def _ms_to_date(ms) -> str:
    if ms is None:
        return "未知"
    try:
        return time.strftime("%Y-%m-%d", time.localtime(int(ms) / 1000))
    except Exception:
        return "未知"


def feat(value=None, status="ok", reason=None, source="", as_of="", raw=None) -> dict:
    if value is not None and isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        value, status, reason = None, "missing", reason or "计算结果非有限数"
    return {"value": value, "status": status, "reason": reason, "source": source, "as_of": as_of, "raw": raw or {}}


def missing(reason: str, source: str = "") -> dict:
    return feat(None, "missing", reason, source)


def _num(x):
    if x is None:
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _report_label(rec: dict) -> str:
    fy = rec.get("fiscal_year")
    fp = rec.get("fiscal_period")
    pe = _ms_to_date(rec.get("period_end_ms"))
    rd = _ms_to_date(rec.get("report_date_ms"))
    return f"{fy}{fp}（报告期末 {pe}，披露 {rd}）"


def _sorted_reports(items: list) -> list:
    return sorted([r for r in items if isinstance(r, dict)], key=lambda r: r.get("period_end_ms") or 0, reverse=True)


def _is_stale(ms, days: int, now_ms: int) -> bool:
    return ms is None or (now_ms - int(ms)) > days * DAY_MS


# ---------------------------------------------------------------- 财务指标
def yoy_feature(res: FetchResult, field: str, metric: str, now_ms: int) -> dict:
    src = f"{res.endpoint}?period=quarterly → {field}"
    if not res.ok:
        return missing(f"财报接口失败：{res.error}", src)
    reps = _sorted_reports(res.items())
    if not reps:
        return missing("接口返回空列表", src)
    latest = reps[0]
    if _is_stale(latest.get("period_end_ms"), STALE_REPORT_DAYS, now_ms):
        return feat(None, "stale", f"最新财报期 {_report_label(latest)} 已超过 {STALE_REPORT_DAYS} 天", src,
                    _report_label(latest))
    base = next((r for r in reps[1:] if r.get("fiscal_period") == latest.get("fiscal_period")
                 and r.get("fiscal_year") is not None and latest.get("fiscal_year") is not None
                 and int(r["fiscal_year"]) == int(latest["fiscal_year"]) - 1), None)
    if base is None:
        return missing(f"找不到 {latest.get('fiscal_year')}{latest.get('fiscal_period')} 的上年同期数据", src)
    cur, prev = _num(latest.get(field)), _num(base.get(field))
    if cur is None or prev is None:
        return missing(f"字段 {field} 未披露（null）", src)
    raw = {"本期": cur, "上年同期": prev, "本期报告": _report_label(latest), "同期报告": _report_label(base)}
    if prev <= 0:
        return feat(None, "na", f"上年同期 {field}={prev:,.0f} 元 ≤ 0，同比增速无意义", src, _report_label(latest), raw)
    return feat((cur / prev - 1) * 100, "ok", None, src, _report_label(latest), raw)


def ocf_feature(res: FetchResult, now_ms: int) -> dict:
    src = f"{res.endpoint}?period=quarterly → act_cash_flow_net"
    if not res.ok:
        return missing(f"现金流量表接口失败：{res.error}", src)
    reps = _sorted_reports(res.items())
    if not reps:
        return missing("接口返回空列表", src)
    r = reps[0]
    if _is_stale(r.get("period_end_ms"), STALE_REPORT_DAYS, now_ms):
        return feat(None, "stale", f"最新财报期已超过 {STALE_REPORT_DAYS} 天", src, _report_label(r))
    v = _num(r.get("act_cash_flow_net"))
    if v is None:
        return missing("act_cash_flow_net 未披露（null）", src)
    return feat(v / 1e8, "ok", None, src, _report_label(r), {"act_cash_flow_net(元)": v})


def balance_features(res_q: FetchResult, annual_income: FetchResult, now_ms: int) -> dict:
    out = {}
    src_d = f"{res_q.endpoint}?period=quarterly → total_debt / assets_total"
    if not res_q.ok:
        out["debt_ratio"] = missing(f"资产负债表接口失败：{res_q.error}", src_d)
        out["roe"] = missing(f"资产负债表接口失败：{res_q.error}", "")
        out["_equity_latest"] = None
        return out
    reps = _sorted_reports(res_q.items())
    if not reps:
        out["debt_ratio"] = missing("接口返回空列表", src_d)
        out["roe"] = missing("资产负债表为空", "")
        out["_equity_latest"] = None
        return out
    r = reps[0]
    debt, assets = _num(r.get("total_debt")), _num(r.get("assets_total"))
    if _is_stale(r.get("period_end_ms"), STALE_REPORT_DAYS, now_ms):
        out["debt_ratio"] = feat(None, "stale", "最新资产负债表已超过 200 天", src_d, _report_label(r))
    elif debt is None or not assets:
        out["debt_ratio"] = missing("total_debt 或 assets_total 未披露", src_d)
    else:
        out["debt_ratio"] = feat(debt / assets * 100, "ok", None, src_d, _report_label(r),
                                 {"total_debt(元)": debt, "assets_total(元)": assets})
    out["_equity_latest"] = (_num(r.get("holder_equity_total")), _report_label(r))

    # ROE：年报归母净利润 / 同一年末所有者权益
    src_r = "income-statements(annual).parent_holder_net_profit / balance-sheets.holder_equity_total"
    if not annual_income.ok:
        out["roe"] = missing(f"年报利润表接口失败：{annual_income.error}", src_r)
        return out
    ann = _sorted_reports(annual_income.items())
    if not ann:
        out["roe"] = missing("年报利润表为空", src_r)
        return out
    a = ann[0]
    eq_rec = next((b for b in reps if b.get("period_end_ms") == a.get("period_end_ms")), None)
    np_ = _num(a.get("parent_holder_net_profit"))
    eq = _num(eq_rec.get("holder_equity_total")) if eq_rec else None
    if eq_rec is None:
        out["roe"] = missing(f"资产负债表最近 5 期中找不到年报期末 {_ms_to_date(a.get('period_end_ms'))} 的数据", src_r)
    elif np_ is None or not eq:
        out["roe"] = missing("归母净利润或所有者权益未披露", src_r)
    elif eq <= 0:
        out["roe"] = feat(None, "na", "所有者权益 ≤ 0，ROE 无意义", src_r, _report_label(a))
    else:
        out["roe"] = feat(np_ / eq * 100, "ok", None, src_r, _report_label(a),
                          {"parent_holder_net_profit(元)": np_, "holder_equity_total(元)": eq})
    return out


def valuation_features(price: float | None, price_src: str, price_asof: str, annual_income: FetchResult,
                       equity_latest) -> dict:
    out = {}
    src_pe = "snapshot.last_price / income-statements(annual).basic_eps"
    if price is None or price <= 0:
        out["pe"] = missing("无有效最新价（可能停牌或快照失败）", src_pe)
        out["pb"] = missing("无有效最新价", "")
        return out
    if not annual_income.ok:
        out["pe"] = missing(f"年报利润表接口失败：{annual_income.error}", src_pe)
        out["pb"] = missing(f"年报利润表接口失败：{annual_income.error}", "")
        return out
    ann = _sorted_reports(annual_income.items())
    if not ann:
        out["pe"] = missing("年报利润表为空", src_pe)
        out["pb"] = missing("年报利润表为空", "")
        return out
    a = ann[0]
    eps = _num(a.get("basic_eps"))
    asof = f"价格 {price_asof}；EPS {_report_label(a)}"
    if eps is None:
        out["pe"] = missing("basic_eps 未披露", src_pe)
    elif eps <= 0:
        out["pe"] = feat(None, "na", f"年报基本 EPS = {eps} 元/股 ≤ 0（亏损），PE 无意义", src_pe, asof,
                         {"last_price": price, "basic_eps": eps})
    else:
        out["pe"] = feat(price / eps, "ok", None, src_pe, asof, {"last_price": price, "basic_eps": eps,
                                                                  "价格来源": price_src})
    # PB 估算
    src_pb = "last_price / (holder_equity_total / (parent_holder_net_profit / basic_eps))"
    np_ = _num(a.get("parent_holder_net_profit"))
    eq, eq_label = equity_latest if equity_latest else (None, "")
    if not eps or not np_ or eps <= 0 or np_ <= 0:
        out["pb"] = missing("亏损或 EPS 缺失，无法反推总股本", src_pb)
    elif not eq or eq <= 0:
        out["pb"] = missing("所有者权益缺失或 ≤ 0", src_pb)
    else:
        shares = np_ / eps
        bps = eq / shares
        out["pb"] = feat(price / bps, "ok", None, src_pb, f"价格 {price_asof}；净资产 {eq_label}",
                         {"估算总股本(股)": shares, "每股净资产(元)": bps, "last_price": price})
    return out


# ---------------------------------------------------------------- 走势指标
def kline_features(res: FetchResult, now_ms: int) -> tuple[dict, list]:
    """返回 (指标字典, [(date_ms, close), ...])"""
    src = f"{res.endpoint}?interval=1d&adjust=forward → close_price / turnover"
    keys = ["ret_20d", "ret_60d", "vol_60d", "mdd_120d", "ma60_dev", "amount_20d"]
    if not res.ok:
        return {k: missing(f"K 线接口失败：{res.error}", src) for k in keys}, []
    bars = sorted([b for b in res.items() if isinstance(b, dict) and _num(b.get("close_price"))],
                  key=lambda b: b.get("date_ms") or 0)
    if not bars:
        return {k: missing("K 线为空", src) for k in keys}, []
    closes = [_num(b["close_price"]) for b in bars]
    series = [(b.get("date_ms"), _num(b["close_price"])) for b in bars]
    last_ms = bars[-1].get("date_ms")
    asof = f"截至 {_ms_to_date(last_ms)} 收盘"
    if _is_stale(last_ms, STALE_KLINE_DAYS, now_ms):
        return {k: feat(None, "stale", f"最新 K 线为 {_ms_to_date(last_ms)}，超过 {STALE_KLINE_DAYS} 天（可能停牌）",
                        src, asof) for k in keys}, series
    out = {}

    def need(n):
        return len(closes) >= n

    out["ret_20d"] = feat((closes[-1] / closes[-21] - 1) * 100, "ok", None, src, asof,
                          {"最新收盘": closes[-1], "20日前收盘": closes[-21]}) if need(21) else missing(f"K 线仅 {len(closes)} 根，不足 21 根", src)
    out["ret_60d"] = feat((closes[-1] / closes[-61] - 1) * 100, "ok", None, src, asof,
                          {"最新收盘": closes[-1], "60日前收盘": closes[-61]}) if need(61) else missing(f"K 线仅 {len(closes)} 根，不足 61 根", src)
    if need(61):
        rets = [closes[i] / closes[i - 1] - 1 for i in range(len(closes) - 60, len(closes))]
        mu = sum(rets) / len(rets)
        sd = math.sqrt(sum((r - mu) ** 2 for r in rets) / (len(rets) - 1))
        out["vol_60d"] = feat(sd * math.sqrt(252) * 100, "ok", None, src, asof, {"样本日数": len(rets)})
    else:
        out["vol_60d"] = missing(f"K 线仅 {len(closes)} 根，不足 61 根", src)
    if need(120):
        window = closes[-120:]
        peak, mdd = window[0], 0.0
        for c in window:
            peak = max(peak, c)
            mdd = max(mdd, 1 - c / peak)
        out["mdd_120d"] = feat(mdd * 100, "ok", None, src, asof, {"窗口": "120 个交易日"})
    else:
        out["mdd_120d"] = missing(f"K 线仅 {len(closes)} 根，不足 120 根", src)
    if need(60):
        ma = sum(closes[-60:]) / 60
        out["ma60_dev"] = feat((closes[-1] / ma - 1) * 100, "ok", None, src, asof, {"最新收盘": closes[-1], "MA60": ma})
    else:
        out["ma60_dev"] = missing(f"K 线仅 {len(closes)} 根，不足 60 根", src)
    amts = [_num(b.get("turnover")) for b in bars[-20:]]
    amts = [a for a in amts if a is not None]
    if len(amts) >= 15:
        out["amount_20d"] = feat(sum(amts) / len(amts) / 1e8, "ok", None, src, asof, {"有效天数": len(amts)})
    else:
        out["amount_20d"] = missing("近 20 日成交额数据不足", src)
    return out, series


# ---------------------------------------------------------------- 组装数据集
def build_dataset(client: FuyaoClient, universe_code: str, universe_name: str = "",
                  max_stocks: int | None = None, workers: int = 4,
                  progress: Callable[[float, str], None] | None = None) -> dict:
    now_ms = int(time.time() * 1000)
    ds = {"universe": universe_name or universe_code, "universe_code": universe_code, "built_at": now_ms,
          "stocks": [], "features": {}, "series": {}, "errors": [], "snapshot_asof": None}

    cons = client.index_constituents(universe_code)
    if not cons.ok:
        ds["errors"].append(f"成分股接口失败：{cons.error}")
        return ds
    stocks = []
    for it in cons.items():
        code = it.get("thscode")
        if code:
            name = it.get("name") or code
            stocks.append({"thscode": code, "name": name, "is_st": "ST" in name.upper()})
    if not stocks:
        ds["errors"].append("成分股接口返回为空")
        return ds
    if max_stocks:
        stocks = stocks[:max_stocks]
    ds["stocks"] = stocks
    ds["constituents_asof"] = _ms_to_date(cons.data_timestamp())

    snap = client.snapshot([s["thscode"] for s in stocks])
    snap_map = {it.get("thscode"): it for it in snap.items()}
    ds["snapshot_asof"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(snap.data_timestamp() / 1000)) \
        if snap.data_timestamp() else "未知"
    if not snap.ok:
        ds["errors"].append(f"快照接口部分失败：{snap.error}")

    def work(s):
        code = s["thscode"]
        f = {}
        kres = client.daily_kline(code)
        kf, series = kline_features(kres, now_ms)
        f.update(kf)
        inc_q = client.income(code, "quarterly", 5)
        inc_a = client.income(code, "annual", 1)
        bal_q = client.balance(code, "quarterly", 5)
        cf_q = client.cashflow(code, "quarterly", 1)
        f["revenue_yoy"] = yoy_feature(inc_q, "operating_income", "revenue_yoy", now_ms)
        f["profit_yoy"] = yoy_feature(inc_q, "parent_holder_net_profit", "profit_yoy", now_ms)
        f["ocf"] = ocf_feature(cf_q, now_ms)
        bf = balance_features(bal_q, inc_a, now_ms)
        eq_latest = bf.pop("_equity_latest", None)
        f.update(bf)
        # 价格：优先快照，快照缺失则退回最近收盘并注明
        sn = snap_map.get(code)
        price, psrc, pasof = None, "", ""
        if sn and _num(sn.get("last_price")) and _num(sn.get("last_price")) > 0:
            price, psrc, pasof = _num(sn["last_price"]), "snapshot.last_price", ds["snapshot_asof"]
            chg = _num(sn.get("price_change_ratio_pct"))
            f["chg_today"] = feat(chg, "ok", None, "prices/snapshot → price_change_ratio_pct", ds["snapshot_asof"]) \
                if chg is not None else missing("快照无涨跌幅字段", "prices/snapshot")
        else:
            f["chg_today"] = missing("快照缺失或最新价为 0（可能停牌）", "prices/snapshot")
            if series:
                price, psrc = series[-1][1], "historical.close_price（快照缺失，退回最近收盘）"
                pasof = f"{_ms_to_date(series[-1][0])} 收盘"
        f.update(valuation_features(price, psrc, pasof, inc_a, eq_latest))
        return code, f, series

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(work, s) for s in stocks]
        for fu in as_completed(futs):
            try:
                code, f, series = fu.result()
                ds["features"][code] = f
                ds["series"][code] = series
            except Exception as e:  # 单只股票失败不影响整体，但要记录
                ds["errors"].append(f"计算异常：{type(e).__name__}: {e}")
            done += 1
            if progress:
                progress(done / len(stocks), f"已处理 {done}/{len(stocks)}")

    # 缺失计算失败的股票补 missing，保证每只股票每个指标都有状态
    for s in stocks:
        fs = ds["features"].setdefault(s["thscode"], {})
        for k in METRICS:
            if k != "pe_rank" and k not in fs:
                fs[k] = missing("计算过程异常，未产出该指标")
    add_pe_rank(ds)
    return ds


def add_pe_rank(ds: dict) -> None:
    pes = {c: f["pe"]["value"] for c, f in ds["features"].items() if f.get("pe", {}).get("status") == "ok"}
    vals = sorted(pes.values())
    n = len(vals)
    src = f"pe 在股票池「{ds.get('universe')}」内 {n} 只 PE>0 股票中的分位"
    for c, f in ds["features"].items():
        pe = f.get("pe", {})
        if pe.get("status") == "ok" and n >= 5:
            rank = sum(1 for v in vals if v <= pe["value"]) / n * 100
            f["pe_rank"] = feat(rank, "ok", None, src, pe.get("as_of", ""), {"样本数": n})
        elif pe.get("status") == "na":
            f["pe_rank"] = feat(None, "na", pe.get("reason"), src, pe.get("as_of", ""))
        elif n < 5:
            f["pe_rank"] = missing(f"股票池内有效 PE 仅 {n} 个，分位不可靠", src)
        else:
            f["pe_rank"] = missing(f"PE 不可用：{pe.get('reason')}", src)
