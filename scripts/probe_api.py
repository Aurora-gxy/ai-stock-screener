"""扶摇接口联调脚本：用一只真实股票逐个调用本项目用到的接口，核对返回字段是否与代码假设一致。

用法（在项目根目录）：
    python scripts/probe_api.py            # 默认 600519.SH
    python scripts/probe_api.py 000001.SZ

输出：每个接口的成功 / 失败、request_id、实际返回字段、缺失的预期字段。
结果同时写入 probe_report.json（已在 .gitignore 中），可作为「AI 使用与验证记录」的证据。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.fuyao import FuyaoClient  # noqa: E402

EXPECT = {
    "constituents(000016.SH)": ["thscode", "name"],
    "snapshot": ["thscode", "last_price", "price_change_ratio_pct"],
    "daily_kline": ["date_ms", "close_price", "turnover"],
    "index_kline(000016.SH)": ["date_ms", "close_price"],
    "income(quarterly)": ["fiscal_year", "fiscal_period", "period_end_ms", "report_date_ms", "operating_income",
                          "parent_holder_net_profit"],
    "income(annual)": ["fiscal_year", "fiscal_period", "period_end_ms", "basic_eps", "parent_holder_net_profit"],
    "balance(quarterly)": ["period_end_ms", "total_debt", "assets_total", "holder_equity_total"],
    "cashflow(quarterly)": ["period_end_ms", "act_cash_flow_net"],
}


def main():
    code = sys.argv[1] if len(sys.argv) > 1 else "600519.SH"
    from core.config import key_problem
    c = FuyaoClient(use_cache=False)
    if not c.api_key:
        print("❌ " + (key_problem("FUYAO_API_KEY") or "FUYAO_API_KEY 无效"))
        sys.exit(1)
    calls = {
        "constituents(000016.SH)": lambda: c.index_constituents("000016.SH"),
        "snapshot": lambda: c.snapshot([code]),
        "daily_kline": lambda: c.daily_kline(code, 200),
        "index_kline(000016.SH)": lambda: c.index_kline("000016.SH", 60),
        "income(quarterly)": lambda: c.income(code, "quarterly", 5),
        "income(annual)": lambda: c.income(code, "annual", 1),
        "balance(quarterly)": lambda: c.balance(code, "quarterly", 5),
        "cashflow(quarterly)": lambda: c.cashflow(code, "quarterly", 1),
    }
    report = {}
    all_ok = True
    for name, fn in calls.items():
        r = fn()
        items = r.items()
        first = items[0] if items else {}
        keys = sorted(first.keys()) if isinstance(first, dict) else []
        miss = [k for k in EXPECT[name] if k not in keys]
        ok = r.ok and bool(items) and not miss
        all_ok &= ok
        print(f"{'✅' if ok else '❌'} {name}: ok={r.ok} n={len(items)} request_id={r.request_id} error={r.error}")
        if miss:
            print(f"   缺少预期字段：{miss}")
            print(f"   实际字段：{keys}")
        if name.startswith("income(quarterly)") and items:
            print("   报告期：", [(x.get("fiscal_year"), x.get("fiscal_period")) for x in items])
        report[name] = {"ok": r.ok, "n": len(items), "request_id": r.request_id, "error": r.error,
                        "keys": keys, "missing_expected": miss, "first_item": first}
    with open("probe_report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)
    print("\n全部通过 ✅" if all_ok else "\n存在问题 ❌：把上面的输出发给 AI 助手或按字段名修改 core/features.py")


if __name__ == "__main__":
    main()
