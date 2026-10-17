"""历史回看（不是严格回测）。

做法：把「今天」选出的股票等权组合，看过去 N 个交易日的价格表现，并与基准指数对比。
局限（页面必须展示）：
- 前视偏差：用今天的财务和走势数据选股，再看过去，结果会系统性偏好
- 幸存者偏差：股票池是当前成分股
- 未计交易成本、分红再投资按前复权近似
所以它只用于「理解这组股票过去的波动特征」，不能作为策略有效性的证据。
"""
from __future__ import annotations

import time

CAVEATS = [
    "这是「历史回看」，不是严格回测：选股使用的是今天的数据，存在前视偏差。",
    "股票池为当前成分股，存在幸存者偏差。",
    "未计交易成本与冲击成本；价格为前复权，分红按复权近似处理。",
    "历史表现不代表未来，不能据此推断收益。",
]


def _d(ms):
    return time.strftime("%Y-%m-%d", time.localtime(int(ms) / 1000))


def lookback(series_map: dict, codes: list[str], bench_series: list, days: int = 120) -> dict:
    if not codes:
        return {"ok": False, "error": "没有入选股票"}
    date_sets = [set(d for d, _ in series_map.get(c, [])) for c in codes if series_map.get(c)]
    usable = [c for c in codes if series_map.get(c)]
    if not usable:
        return {"ok": False, "error": "入选股票均无 K 线数据"}
    common = sorted(set.intersection(*date_sets)) if date_sets else []
    if len(common) < 20:
        return {"ok": False, "error": f"入选股票的共同交易日仅 {len(common)} 天，无法回看"}
    window = common[-(days + 1):]
    price = {c: dict(series_map[c]) for c in usable}
    nav = []
    for d in window:
        nav.append(sum(price[c][d] / price[c][window[0]] for c in usable) / len(usable))
    per_stock = {c: (price[c][window[-1]] / price[c][window[0]] - 1) * 100 for c in usable}
    bench = None
    if bench_series:
        bmap = dict(bench_series)
        bdates = [d for d in window if d in bmap]
        if len(bdates) >= len(window) * 0.9:
            bench = [(d, bmap[d] / bmap[bdates[0]]) for d in bdates]
    peak, mdd = nav[0], 0.0
    for v in nav:
        peak = max(peak, v)
        mdd = max(mdd, 1 - v / peak)
    return {
        "ok": True, "start": _d(window[0]), "end": _d(window[-1]), "n_days": len(window) - 1,
        "curve": [(_d(d), v) for d, v in zip(window, nav)],
        "bench": [(_d(d), v) for d, v in bench] if bench else None,
        "total_return_pct": (nav[-1] - 1) * 100,
        "bench_return_pct": (bench[-1][1] - 1) * 100 if bench else None,
        "max_drawdown_pct": mdd * 100,
        "per_stock_pct": per_stock,
        "excluded_no_data": [c for c in codes if c not in usable],
        "caveats": CAVEATS,
    }
