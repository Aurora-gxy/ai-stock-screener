"""测试用的「构造数据」假接口。

⚠️ 这里的所有股票、价格、财务数字都是人为构造的测试数据，仅用于验证代码逻辑，
不代表任何真实公司，也不会出现在产品页面中。代码名使用 9xxxxx 以避免与真实股票混淆。
"""
from __future__ import annotations

import math
import time

DAY = 86400000
NOW = int(time.time() * 1000)
TODAY0 = int(time.mktime(time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d")) * 1000)

# 最新报告期：取最近一个已结束季度末之前约 60 天的季度（保证不过期）
def _period_end(year, q):
    month_day = {1: "03-31", 2: "06-30", 3: "09-30", 4: "12-31"}[q]
    return int(time.mktime(time.strptime(f"{year}-{month_day}", "%Y-%m-%d")) * 1000)


def _latest_quarter():
    t = time.localtime(time.time() - 45 * 86400)
    q = (t.tm_mon - 1) // 3  # 上一个完整季度
    y = t.tm_year
    if q == 0:
        return y - 1, 4
    return y, q


LY, LQ = _latest_quarter()

STOCKS = [
    # code, name, 设计意图
    ("900001.SH", "测试稳健", "稳健上涨、增长、低估、低负债 → 应入选"),
    ("900002.SH", "测试高波", "高波动 → 走势条件不通过"),
    ("900003.SH", "测试亏损", "去年亏损 / EPS<0 → 同比无意义、PE 无意义"),
    ("900004.SH", "测试次新", "K 线不足 → 走势指标缺失 → 无法判断"),
    ("900005.SH", "*ST测试", "ST → 默认排除"),
    ("900006.SH", "测试财报失败", "财报接口报错 → 基本面缺失"),
]


def closes_for(code: str, n: int = 260) -> list[float]:
    if code == "900001.SH":
        return [10 * (1 + 0.0008 * i) * (1 + 0.003 * math.sin(i)) for i in range(n)]
    if code == "900002.SH":
        return [10 * (1 + 0.06 * math.sin(i * 1.3)) * (1 + 0.001 * i) for i in range(n)]
    if code == "900003.SH":
        return [10 * (1 - 0.001 * i) for i in range(n)]
    if code == "900004.SH":
        return [10 + 0.01 * i for i in range(30)]
    return [5 + 0.001 * i for i in range(n)]


def bars_for(code: str):
    cs = closes_for(code)
    start = TODAY0 - (len(cs) - 1) * DAY
    return [{"date_ms": start + i * DAY, "open_price": c, "high_price": c * 1.01, "low_price": c * 0.99,
             "close_price": c, "volume": 1e7, "turnover": 5e8} for i, c in enumerate(cs)]


def income_q(code):
    base = {"900001.SH": (1.2e10, 2.0e9, 1.0e10, 1.6e9), "900002.SH": (5e9, 5e8, 4.8e9, 4.9e8),
            "900003.SH": (3e9, 1e8, 3.2e9, -2e8), "900004.SH": (1e9, 1e8, 8e8, 8e7),
            "900005.SH": (1e9, 1e7, 1.1e9, 2e7)}[code]
    rev, np_, rev0, np0 = base
    recs = []
    for k in range(5):  # 最近 5 个季度
        y, q = LY, LQ - k
        while q <= 0:
            q += 4
            y -= 1
        if k == 0:
            r, n = rev, np_
        elif k == 4:
            r, n = rev0, np0
        else:
            r, n = (rev + rev0) / 2, (np_ + np0) / 2
        recs.append({"thscode": code, "fiscal_year": y, "fiscal_period": f"Q{q}", "period_end_ms": _period_end(y, q),
                     "report_date_ms": _period_end(y, q) + 40 * DAY, "currency": "CNY", "operating_income": r,
                     "parent_holder_net_profit": n})
    return recs


def income_a(code):
    eps = {"900001.SH": 1.0, "900002.SH": 0.5, "900003.SH": -0.2, "900004.SH": 0.5, "900005.SH": 0.01}[code]
    np_ = {"900001.SH": 6e9, "900002.SH": 1.5e9, "900003.SH": -6e8, "900004.SH": 3e8, "900005.SH": 3e7}[code]
    y = LY - 1
    return [{"thscode": code, "fiscal_year": y, "fiscal_period": "FY", "period_end_ms": _period_end(y, 4),
             "report_date_ms": _period_end(y, 4) + 100 * DAY, "basic_eps": eps, "parent_holder_net_profit": np_}]


def balance_q(code):
    eq = {"900001.SH": 4e10, "900002.SH": 2e10, "900003.SH": 5e9, "900004.SH": 3e9, "900005.SH": 1e8}[code]
    debt_ratio = {"900001.SH": 0.35, "900002.SH": 0.55, "900003.SH": 0.8, "900004.SH": 0.3, "900005.SH": 0.95}[code]
    recs = []
    for k in range(5):
        y, q = LY, LQ - k
        while q <= 0:
            q += 4
            y -= 1
        assets = eq / (1 - debt_ratio)
        recs.append({"thscode": code, "fiscal_year": y, "fiscal_period": f"Q{q}", "period_end_ms": _period_end(y, q),
                     "assets_total": assets, "total_debt": assets * debt_ratio, "holder_equity_total": eq})
    return recs


def cash_q(code):
    v = {"900001.SH": 3e9, "900002.SH": -1e8, "900003.SH": 1e8, "900004.SH": 2e8, "900005.SH": -5e7}[code]
    return [{"thscode": code, "fiscal_year": LY, "fiscal_period": f"Q{LQ}", "period_end_ms": _period_end(LY, LQ),
             "act_cash_flow_net": v}]


class FakeResp:
    def __init__(self, body, status=200):
        self._body = body
        self.status_code = status

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def ok(data):
    return FakeResp({"code": 0, "message": "ok", "request_id": "test-req", "data": data})


class FakeSession:
    """按 URL 路由返回构造数据。fail 可注入故障：{endpoint 片段: 'code'|'http500'|'timeout'|'badjson'|'429'}"""

    def __init__(self, fail: dict | None = None, snapshot_missing=("900004.SH",)):
        self.fail = fail or {}
        self.calls = []
        self.snapshot_missing = set(snapshot_missing)

    def get(self, url, params=None, headers=None, timeout=None):
        import requests
        self.calls.append((url, dict(params or {})))
        code = (params or {}).get("thscode")
        for frag, mode in self.fail.items():
            if frag in url and (code is None or ":" not in frag):
                if mode == "timeout":
                    raise requests.Timeout()
                if mode == "http500":
                    return FakeResp({}, 500)
                if mode == "badjson":
                    return FakeResp(ValueError("bad json"))
                if mode == "429":
                    return FakeResp({}, 429)
                if mode == "code":
                    return FakeResp({"code": 1003, "message": "参数非法", "request_id": "x"})
        if code == "900006.SH" and "financials" in url:
            return FakeResp({"code": 5000, "message": "服务内部错误", "request_id": "x"})
        if "constituents" in url:
            return ok({"timestamp": NOW, "item": [{"thscode": c, "ticker": c[:6], "name": n} for c, n, _ in STOCKS]})
        if "prices/snapshot" in url:
            codes = params["thscodes"].split(",")
            items = []
            for c in codes:
                if c in self.snapshot_missing:
                    continue
                last = closes_for(c)[-1]
                items.append({"thscode": c, "last_price": last, "price_change_ratio_pct": 0.5})
            return ok({"timestamp": NOW, "item": items})
        if "a-share-index/prices/historical" in url:
            return ok({"timestamp": NOW, "item": bars_for("900001.SH")})
        if "prices/historical" in url:
            return ok({"timestamp": NOW, "item": bars_for(code)})
        if "income-statements" in url:
            return ok({"item": income_q(code) if params.get("period") == "quarterly" else income_a(code)})
        if "balance-sheets" in url:
            return ok({"item": balance_q(code)})
        if "cash-flow-statements" in url:
            return ok({"item": cash_q(code)})
        return FakeResp({"code": 1003, "message": "未知接口"})
