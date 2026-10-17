"""主链路 + 数据异常 + 合规边界测试。全部使用 tests/fakes.py 的构造数据，不访问网络。"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import compliance, conflicts, engine, explain, intent  # noqa: E402
from core.backtest import lookback  # noqa: E402
from core.features import build_dataset  # noqa: E402
from core.fuyao import FuyaoClient  # noqa: E402
from tests.fakes import FakeSession  # noqa: E402


@pytest.fixture(autouse=True)
def tmp_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "data"))


def make_client(**kw):
    return FuyaoClient(api_key="test-key", session=FakeSession(**kw), use_cache=False, sleep=lambda s: None)


@pytest.fixture
def ds():
    return build_dataset(make_client(), "000016.SH", "测试池", workers=2)


# ---------------------------------------------------------------- 主链路
def test_rule_parse_default_example():
    r = intent.parse_intent("经营改善、估值合理、走势相对稳定")
    metrics = sorted(c["metric"] for c in r["conditions"])
    assert metrics == ["mdd_120d", "pe_rank", "profit_yoy", "revenue_yoy", "vol_60d"]
    assert {c["term"] for c in r["clarifications"]} == {"经营改善", "估值合理", "走势稳定"}
    assert r["source"] == "rule" and r["llm_error"] is None


def test_explicit_number_overrides_fuzzy():
    r = intent.parse_intent("低估值，市盈率低于15倍，ROE大于15%，负债率40到60")
    got = {(c["metric"], c["op"], c["value"], c.get("value2")) for c in r["conditions"]}
    assert ("pe", "<=", 15.0, None) in got
    assert ("roe", ">=", 15.0, None) in got
    assert ("debt_ratio", "between", 40.0, 60.0) in got


def test_clarification_replaces_term_conditions():
    r = intent.parse_intent("估值合理")
    clar = r["clarifications"][0]
    new = intent.apply_clarification(r["conditions"], clar, 1)
    assert [(c["metric"], c["op"]) for c in new] == [("pe", "between")]


def test_end_to_end_screen(ds):
    assert not ds["errors"]
    r = intent.parse_intent("经营改善、估值合理、走势相对稳定")
    # 构造池只有 4 个有效 PE，分位样本不足 → 应标记为无法判断而不是假装可算
    res0 = engine.screen(ds, r["conditions"], exclude_st=True)
    assert "900001.SH" in res0["undetermined"]
    # 用户在澄清里把「估值合理」切换为绝对 PE 口径
    clar = next(c for c in r["clarifications"] if c["term"] == "估值合理")
    conds = intent.apply_clarification(r["conditions"], clar, 1)
    res = engine.screen(ds, conds, exclude_st=True)
    rows = res["rows"]
    assert "900001.SH" in res["selected"]
    assert rows["900002.SH"]["verdict"] == "排除"          # 高波动
    assert rows["900005.SH"]["st_excluded"]                 # ST
    assert rows["900004.SH"]["verdict"] == "无法判断"       # 次新：K 线不足
    assert rows["900006.SH"]["verdict"] == "无法判断"       # 财报接口失败
    # 每条判定都有来源与时点
    for ck in rows["900001.SH"]["checks"]:
        assert ck["source"] and ck["as_of"]
    # 漏斗单调不增
    left = [f["剩余"] for f in res["funnel"]]
    assert left == sorted(left, reverse=True)


def test_sensitivity_and_binding(ds):
    r = intent.parse_intent("经营改善、估值合理、走势相对稳定")
    sens = engine.sensitivity(ds, r["conditions"])
    assert len(sens) == len(r["conditions"])
    for row in sens:
        assert row["删除该条件"] >= row["当前入选"]
        assert row["收紧一档"] <= row["当前入选"] <= row["放宽一档"]


def test_lookback(ds):
    out = lookback(ds["series"], ["900001.SH", "900002.SH"], [], days=60)
    assert out["ok"] and out["n_days"] == 60 and out["caveats"]


# ---------------------------------------------------------------- 数据缺失 / 接口失败
def test_loss_company_pe_not_meaningful(ds):
    f = ds["features"]["900003.SH"]
    assert f["pe"]["status"] == "na" and "EPS" in f["pe"]["reason"]
    assert f["profit_yoy"]["status"] == "na"   # 上年同期亏损
    res = engine.screen(ds, intent.parse_intent("估值合理")["conditions"])
    assert res["rows"]["900003.SH"]["verdict"] == "排除"


def test_missing_never_passes(ds):
    f = ds["features"]["900006.SH"]
    assert f["revenue_yoy"]["status"] == "missing" and "5000" in f["revenue_yoy"]["reason"]
    res = engine.screen(ds, intent.parse_intent("经营改善")["conditions"])
    assert "900006.SH" not in res["selected"]
    assert "900006.SH" in res["undetermined"]


def test_snapshot_missing_falls_back_to_close(ds):
    f = ds["features"]["900004.SH"]
    assert f["chg_today"]["status"] == "missing"
    assert f["pe"]["status"] == "ok" and "快照缺失" in f["pe"]["raw"]["价格来源"]


@pytest.mark.parametrize("mode", ["timeout", "http500", "badjson", "429", "code"])
def test_api_failures_are_explicit(mode):
    c = make_client(fail={"constituents": mode})
    ds = build_dataset(c, "000016.SH")
    assert ds["errors"] and not ds["stocks"]
    assert any(not x["ok"] for x in c.call_log)


def test_no_api_key():
    c = FuyaoClient(api_key="", session=FakeSession(), use_cache=False)
    c.api_key = None
    r = c.index_constituents("000016.SH")
    assert not r.ok and "FUYAO_API_KEY" in r.error


def test_kline_failure_marks_missing():
    ds = build_dataset(make_client(fail={"a-share/prices/historical": "http500"}), "000016.SH")
    f = ds["features"]["900001.SH"]
    assert f["vol_60d"]["status"] == "missing" and "HTTP 500" in f["vol_60d"]["reason"]


# ---------------------------------------------------------------- LLM 输出校验
def test_llm_invalid_output_falls_back_to_rule():
    r = intent.parse_intent("经营改善", llm_call=lambda s, u: "抱歉我不能回答")
    assert r["source"] == "rule" and r["llm_error"]


def test_llm_hallucinated_metric_dropped():
    fake = json.dumps({"conditions": [
        {"metric": "magic_score", "op": ">=", "value": 90},
        {"metric": "pe", "op": "<=", "value": "二十"},
        {"metric": "roe", "op": ">=", "value": 12, "origin_phrase": "盈利能力强", "term": "盈利能力强"}]})
    r = intent.parse_intent("盈利能力强", llm_call=lambda s, u: fake)
    assert r["source"] == "llm"
    assert [c["metric"] for c in r["conditions"]] == ["roe"]
    assert any("magic_score" in n for n in r["notes"])
    assert any("不是数字" in n for n in r["notes"])


def test_llm_exception_falls_back():
    def boom(s, u):
        raise TimeoutError("llm timeout")
    r = intent.parse_intent("走势稳定", llm_call=boom)
    assert r["source"] == "rule" and "TimeoutError" in r["llm_error"]


# ---------------------------------------------------------------- 冲突识别
def _conds(*specs):
    return [{"id": str(i), "metric": m, "op": o, "value": v, "value2": v2, "enabled": True}
            for i, (m, o, v, v2) in enumerate(specs)]


def test_conflict_same_metric():
    cs = conflicts.detect_conflicts(_conds(("pe", "<=", 10, None), ("pe", ">=", 20, None)))
    assert conflicts.has_blocking(cs)


def test_conflict_cross_metric_drawdown():
    cs = conflicts.detect_conflicts(_conds(("ret_60d", "<=", -20, None), ("mdd_120d", "<=", 10, None)))
    assert conflicts.has_blocking(cs)


def test_tension_growth_vs_cheap():
    cs = conflicts.detect_conflicts(_conds(("profit_yoy", ">=", 30, None), ("pe_rank", "<=", 20, None)))
    assert not conflicts.has_blocking(cs)
    assert any(c["level"] == "张力" for c in cs)


def test_out_of_range():
    cs = conflicts.detect_conflicts(_conds(("pe_rank", ">=", 120, None),))
    assert conflicts.has_blocking(cs)


# ---------------------------------------------------------------- 合规边界
@pytest.mark.parametrize("text,kind", [
    ("帮我选明天会涨的股票", "预测"),
    ("保证收益20%的股票", "收益承诺"),
    ("直接告诉我该买哪只", "买卖建议"),
    ("有没有内幕消息", "违规信息"),
    ("我的证券账户密码是123", "隐私"),
])
def test_input_compliance(text, kind):
    hits = compliance.check_input(text)
    assert kind in [h["type"] for h in hits]


def test_normal_input_no_compliance_hit():
    assert compliance.check_input("经营改善、估值合理、走势相对稳定") == []


def test_output_sanitized():
    text, flags = compliance.sanitize_output("该股必涨，建议买入，目标价30元")
    assert "必涨" not in text and "建议买入" not in text and "目标价" not in text
    assert len(flags) == 3


def test_unsupported_terms_reported():
    r = intent.parse_intent("行业龙头、分红高、估值合理")
    labels = [u["label"] for u in r["unsupported"]]
    assert "行业地位 / 龙头" in labels and "分红 / 股息率" in labels


def test_ai_summary_number_verification(ds):
    r = intent.parse_intent("经营改善、估值合理、走势相对稳定")
    res = engine.screen(ds, r["conditions"])
    facts = explain.build_facts(ds, res)
    n = len(res["selected"])
    good = explain.ai_summary(lambda s, u, json_mode=False: f"事实：共有{n}只入选。", facts)
    assert good["unverified_numbers"] == []
    bad = explain.ai_summary(lambda s, u, json_mode=False: "事实：预计收益率 87.65%，将上涨。", facts)
    assert "87.65" in bad["unverified_numbers"]
    assert bad["compliance_flags"]


def test_storage_roundtrip():
    from core import storage
    s = storage.save_strategy("t", "经营改善", "上证50", [{"metric": "roe"}], True)
    assert storage.list_strategies()[0]["id"] == s["id"]
    m = storage.create_monitor(s, "每个交易日", [{"thscode": "A", "name": "a"}])
    run = storage.record_run(m["id"], [{"thscode": "B", "name": "b"}], [], "x")
    assert [x["thscode"] for x in run["added"]] == ["B"] and [x["thscode"] for x in run["removed"]] == ["A"]


def test_llm_cannot_rewrite_explicit_numbers():
    """真实案例回归：AI 把矛盾条件改写成区间，必须还原并触发冲突识别。"""
    fake = json.dumps({"conditions": [
        {"metric": "pe", "op": "between", "value": 10, "value2": 20, "origin_phrase": "市盈率低于10倍，市盈率高于20倍"}]})
    r = intent.parse_intent("市盈率低于10倍，市盈率高于20倍", llm_call=lambda s, u: fake)
    assert r["source"] == "llm" and r.get("explicit_overridden")
    got = sorted((c["op"], c["value"]) for c in r["conditions"])
    assert got == [("<=", 10.0), (">=", 20.0)]
    assert conflicts.has_blocking(conflicts.detect_conflicts(r["conditions"]))
    assert any("AI 改写" in n for n in r["notes"])


def test_llm_consistent_explicit_no_warning():
    fake = json.dumps({"conditions": [{"metric": "pe", "op": "<=", "value": 15}]})
    r = intent.parse_intent("市盈率低于15倍", llm_call=lambda s, u: fake)
    assert not r.get("explicit_overridden")
    assert [(c["metric"], c["op"], c["value"]) for c in r["conditions"]] == [("pe", "<=", 15.0)]


def test_llm_no_conditions_for_compliance_only_input_is_not_failure():
    fake = json.dumps({"conditions": [], "compliance": ["不提供涨跌预测", "不承诺收益"]})
    r = intent.parse_intent("帮我找明天会涨的股票，最好稳赚", llm_call=lambda s, u: fake)
    assert r["source"] == "llm" and r["llm_error"] is None and r.get("no_executable")
    assert r["conditions"] == [] and len(r["compliance"]) == 2


def test_llm_empty_for_normal_input_still_falls_back():
    r = intent.parse_intent("经营改善", llm_call=lambda s, u: json.dumps({"conditions": []}))
    assert r["source"] == "rule" and r["llm_error"]
