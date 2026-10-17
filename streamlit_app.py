"""自然语言智能选股与策略解释器 - Web 入口（Streamlit）。

主链路：输入意图 → AI 解析 + 规则核对 → 澄清 → 编辑条件 → 冲突识别 → 确定性筛选
       → 入选 / 排除 / 无法判断的逐条解释 → 条件影响（敏感性）→ 对比 / 回看 / 保存 / 监控
"""
from __future__ import annotations

import json
import time

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from core import config, conflicts, engine, explain, intent, storage
from core.backtest import lookback
from core.compliance import DISCLAIMER
from core.features import build_dataset
from core.fuyao import FuyaoClient
from core.llm import get_llm_call
from core.metrics import BENCHMARK, DEFAULT_UNIVERSE, METRICS, OPS, UNIVERSES, describe_condition, fmt

st.set_page_config(page_title="AI 选股解释器", page_icon="📊", layout="wide")

EXAMPLES = [
    "经营改善、估值合理、走势相对稳定",
    "沪深300里盈利能力强、低负债、现金流好的公司，市盈率低于25倍",
    "高成长但又低估值，最近走势强势",
    "帮我找明天会涨的股票，最好稳赚",
]
LABEL2KEY = {f"{m['name']}（{m['unit']}）": k for k, m in METRICS.items()}
KEY2LABEL = {v: k for k, v in LABEL2KEY.items()}
OP_LABELS = {">=": "≥", "<=": "≤", "between": "介于"}
OP_FROM_LABEL = {v: k for k, v in OP_LABELS.items()}

ss = st.session_state
ss.setdefault("datasets", {})
ss.setdefault("editor_ver", 0)
ss.setdefault("text", EXAMPLES[0])


# ====================================================================== 工具
@st.cache_resource
def get_client() -> FuyaoClient:
    return FuyaoClient()


def llm_call():
    return get_llm_call()


def load_dataset(universe: str, force: bool = False) -> dict:
    key = universe
    ds = ss["datasets"].get(key)
    # 同一会话内 30 分钟复用；磁盘缓存另有按天 / 10 分钟的粒度
    if ds and not force and time.time() * 1000 - ds["built_at"] < 30 * 60 * 1000:
        return ds
    client = get_client()
    if force:
        client.use_cache = False
    bar = st.progress(0.0, text=f"正在从扶摇获取「{universe}」成分股、行情、K 线与财报……")
    try:
        ds = build_dataset(client, UNIVERSES[universe], universe,
                           progress=lambda p, msg: bar.progress(min(p, 1.0), text=msg))
        bench = client.index_kline(BENCHMARK[universe])
        ds["bench_series"] = sorted([(b.get("date_ms"), float(b["close_price"])) for b in bench.items()
                                     if isinstance(b, dict) and b.get("close_price")]) if bench.ok else []
        ds["bench_error"] = None if bench.ok else bench.error
    finally:
        bar.empty()
        client.use_cache = True
    ss["datasets"][key] = ds
    return ds


def conds_to_df(conds: list[dict]) -> pd.DataFrame:
    rows = []
    for c in conds:
        rows.append({"启用": c.get("enabled", True), "指标": KEY2LABEL[c["metric"]], "运算": OP_LABELS[c["op"]],
                     "阈值": float(c["value"]), "上限（仅介于）": None if c.get("value2") is None else float(c["value2"]),
                     "来自原文": c.get("origin_phrase") or "", "解释方式": {"explicit": "用户明确给出",
                                                                    "default_interpretation": "模糊词默认口径",
                                                                    "clarified": "已澄清", "manual": "手动添加"}.get(
                         c.get("kind"), c.get("kind") or ""),
                     "_id": c.get("id"), "_term": c.get("term")})
    return pd.DataFrame(rows, columns=["启用", "指标", "运算", "阈值", "上限（仅介于）", "来自原文", "解释方式", "_id", "_term"])


def df_to_conds(df: pd.DataFrame) -> tuple[list[dict], list[str]]:
    out, errs = [], []
    for _, r in df.iterrows():
        if not r.get("指标") or pd.isna(r.get("阈值")):
            if r.get("指标"):
                errs.append(f"「{r['指标']}」缺少阈值，已忽略")
            continue
        raw = {"metric": LABEL2KEY.get(r["指标"]), "op": OP_FROM_LABEL.get(r["运算"], ">="),
               "value": r["阈值"], "value2": None if pd.isna(r.get("上限（仅介于）")) else r.get("上限（仅介于）")}
        ok, err = intent.validate_condition(raw)
        if err:
            errs.append(err)
            continue
        ok.update({"id": r.get("_id") if isinstance(r.get("_id"), str) and r.get("_id") else intent.new_id(),
                   "enabled": bool(r.get("启用", True)), "origin_phrase": r.get("来自原文") or "",
                   "term": r.get("_term") if isinstance(r.get("_term"), str) else None,
                   "kind": "manual" if not (isinstance(r.get("_id"), str) and r.get("_id")) else
                   {"用户明确给出": "explicit", "模糊词默认口径": "default_interpretation", "已澄清": "clarified"}.get(
                       r.get("解释方式"), "manual")})
        out.append(ok)
    return out, errs


def status_badge(ok: bool, yes: str, no: str):
    return f"🟢 {yes}" if ok else f"🔴 {no}"


# ====================================================================== 侧边栏
with st.sidebar:
    st.header("⚙️ 设置")
    has_key = bool(config.fuyao_api_key())
    has_llm = bool(config.llm_api_key())
    st.markdown(status_badge(has_key, "扶摇数据 API 已配置", "未配置扶摇 API Key，无法获取真实数据"))
    st.markdown(status_badge(has_llm, f"AI 解析：DeepSeek（{config.llm_model()}）",
                             "未配置 DeepSeek Key：使用规则解析兜底"))
    uni_default = ss.get("universe") or DEFAULT_UNIVERSE
    universe = st.selectbox("股票池", list(UNIVERSES), index=list(UNIVERSES).index(uni_default),
                            help="成分股来自扶摇 /api/a-share-index/constituents/ths-stock-list")
    ss["universe"] = universe
    if universe in ("沪深300", "中证500"):
        st.caption("⏱ 该股票池较大，首次加载约需数分钟（每只股票调用 5 个接口），之后当天走缓存。")
    exclude_st = st.checkbox("排除 ST / *ST 股票", value=ss.get("exclude_st", True))
    ss["exclude_st"] = exclude_st
    if st.button("🔄 强制刷新数据", width="stretch", disabled=not has_key):
        load_dataset(universe, force=True)
        st.success("已刷新")
    st.divider()
    st.caption("⚠️ " + DISCLAIMER)

# ====================================================================== 主区：① 意图
st.title("📊 自然语言智能选股与策略解释器")
st.caption("把模糊的选股想法变成**可检查、可修改、可执行**的数据条件；每个结论都能追溯到扶摇原始字段、时点和口径。")

st.subheader("① 用一句话描述你的选股意图")
cols = st.columns(len(EXAMPLES))
for i, ex in enumerate(EXAMPLES):
    if cols[i].button(ex[:14] + ("…" if len(ex) > 14 else ""), key=f"ex{i}", help=ex, width="stretch"):
        ss["text"] = ex
text = st.text_area("选股意图", key="text", height=80, label_visibility="collapsed")

if st.button("🧠 解析意图", type="primary"):
    with st.spinner("AI 正在解析，并与规则解析交叉核对……"):
        p = intent.parse_intent(text, llm_call=llm_call())
    ss["parse"] = p
    ss["conditions"] = p["conditions"]
    if p.get("universe"):
        ss["universe"] = p["universe"]
    ss["editor_ver"] += 1
    for k in ("result", "sens", "summary"):
        ss.pop(k, None)
    st.rerun()

p = ss.get("parse")
if p:
    # ---- 合规
    for h in p["compliance"]:
        st.error(f"**合规提示｜{h['type']}**（原文：「{h['matched']}」）：{h['message']}")
    # ---- 解析来源与核对
    if p["source"] == "llm":
        st.success(f"✅ 由 AI（{config.llm_model()}）解析，输出已通过指标白名单与数值校验。")
    else:
        why = f"AI 解析失败：{p['llm_error']}" if p.get("llm_error") else "未配置 AI"
        st.info(f"ℹ️ 使用规则解析（{why}）。规则解析完全确定，可复现。")
    with st.expander("🔍 AI 解析核对（AI 输出 vs 规则解析、被丢弃的内容）", expanded=bool(p.get("diff_vs_rule"))):
        if p["source"] == "llm":
            if p.get("diff_vs_rule"):
                st.warning("AI 解析与规则解析存在差异，请确认下方条件是否符合你的本意：")
                for d in p["diff_vs_rule"]:
                    st.markdown(f"- {d}")
            else:
                st.markdown("- AI 解析与规则解析结果一致。")
            for c in p["conditions"]:
                if c.get("rationale"):
                    st.markdown(f"- **{describe_condition(c)}**：{c['rationale']}")
        for n in p.get("notes", []):
            st.markdown(f"- ⚠️ {n}")
        if p.get("llm_raw"):
            st.code(p["llm_raw"], language="json")
    if p.get("unsupported"):
        st.warning("以下诉求**无法转为可执行的数据条件**，未参与筛选：\n" +
                   "\n".join(f"- 「{u['phrase']}」：{u['reason']}" for u in p["unsupported"]))

    # ---- 澄清
    if p.get("clarifications"):
        st.subheader("② 澄清模糊表达")
        st.caption("默认口径已预填。切换选项会替换对应的条件；你也可以在下方表格里直接改数字。")
        ccols = st.columns(min(3, len(p["clarifications"])))
        for i, cl in enumerate(p["clarifications"]):
            with ccols[i % len(ccols)]:
                labels = [o["label"] for o in cl["options"]]
                choice = st.radio(cl["question"], labels, index=cl.get("selected", 0), key=f"clar_{cl['term']}_{ss['editor_ver']}")
                idx = labels.index(choice)
                if idx != cl.get("selected", 0):
                    cl["selected"] = idx
                    ss["conditions"] = intent.apply_clarification(ss["conditions"], cl, idx)
                    ss["editor_ver"] += 1
                    st.rerun()

    # ---- 条件编辑
    st.subheader("③ 检查并修改条件")
    df = st.data_editor(
        conds_to_df(ss["conditions"]), key=f"editor_{ss['editor_ver']}", num_rows="dynamic", width="stretch",
        column_order=["启用", "指标", "运算", "阈值", "上限（仅介于）", "来自原文", "解释方式"],
        column_config={
            "启用": st.column_config.CheckboxColumn(width="small"),
            "指标": st.column_config.SelectboxColumn(options=list(LABEL2KEY), required=True, width="medium"),
            "运算": st.column_config.SelectboxColumn(options=list(OP_LABELS.values()), required=True, width="small"),
            "阈值": st.column_config.NumberColumn(format="%.2f"),
            "上限（仅介于）": st.column_config.NumberColumn(format="%.2f"),
            "来自原文": st.column_config.TextColumn(disabled=True),
            "解释方式": st.column_config.TextColumn(disabled=True),
        })
    conds, errs = df_to_conds(df)
    for e in errs:
        st.warning(e)
    with st.expander("📐 每个指标的数据来源与统计口径"):
        st.dataframe(pd.DataFrame([{"指标": m["name"], "单位": m["unit"], "来源字段": m["source"], "口径": m["caliber"]}
                                   for k, m in METRICS.items() if k in {c["metric"] for c in conds}] or
                                  [{"指标": "（无条件）"}]), width="stretch", hide_index=True)

    cf = conflicts.detect_conflicts(conds, ss["universe"])
    if cf:
        st.markdown("**冲突识别**")
        for c in cf:
            fn = {"冲突": st.error, "张力": st.warning, "提示": st.info}[c["level"]]
            fn(f"【{c['level']}】{c['message']}")
    blocking = conflicts.has_blocking(cf)
    enabled = [c for c in conds if c.get("enabled", True)]

    run_disabled = blocking or not enabled or not config.fuyao_api_key()
    hint = "存在逻辑冲突，请先修改条件" if blocking else ("没有启用的条件" if not enabled else
                                                ("未配置扶摇 API Key" if not config.fuyao_api_key() else ""))
    if st.button(f"🚀 在「{ss['universe']}」中执行筛选", type="primary", disabled=run_disabled, help=hint or None):
        ds = load_dataset(ss["universe"])
        ss["conditions_run"] = conds
        if ds["errors"] and not ds["stocks"]:
            ss["result"] = None
            st.error("数据获取失败，无法筛选：\n" + "\n".join(ds["errors"]))
        else:
            ss["result"] = engine.screen(ds, conds, ss["exclude_st"])
            ss["sens"] = engine.sensitivity(ds, conds, ss["exclude_st"])
            ss["result_universe"] = ss["universe"]
            ss.pop("summary", None)
    if hint:
        st.caption(f"⛔ {hint}")

# ====================================================================== ④ 结果
res = ss.get("result")
if res:
    ds = ss["datasets"][ss["result_universe"]]
    rows = res["rows"]
    st.divider()
    st.subheader(f"④ 筛选结果｜{ds['universe']}（{len(ds['stocks'])} 只）")
    st.caption(f"数据时点（北京时间）：行情快照 {ds.get('snapshot_asof')}；成分股 {ds.get('constituents_asof', '未知')}；"
               f"K 线、财报时点见每只股票明细。来源：扶摇金融数据 API。")
    if ds["errors"]:
        st.warning("部分数据获取异常（受影响的股票会被归入「无法判断」，不会被当作通过）：\n" +
                   "\n".join(f"- {e}" for e in ds["errors"][:8]))
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("入选", len(res["selected"]))
    c2.metric("排除", len(res["excluded"]))
    c3.metric("无法判断（数据缺失/过期）", len(res["undetermined"]))
    c4.metric("启用条件数", len(res["conditions"]))
    if not res["selected"]:
        tips = engine.binding_conditions(ss["sens"])
        st.warning("没有股票同时满足全部条件。" + ("影响最大的条件：\n" + "\n".join(f"- {t}" for t in tips) if tips else ""))

    t_sel, t_exc, t_und, t_imp, t_ai, t_log = st.tabs(
        ["✅ 入选", "❌ 排除", "⚠️ 无法判断", "🎚 条件影响", "🤖 AI 摘要", "🧾 数据调用日志"])

    metric_cols = [c["metric"] for c in res["conditions"]]

    def value_table(codes):
        data = []
        for code in codes:
            r = rows[code]
            d = {"代码": code, "名称": r["name"], "结论": r["verdict"]}
            for ck in r["checks"]:
                mark = {"通过": "", "未通过": " ❌", "无法判断": " ⚠️"}[ck["status"]]
                d[KEY2LABEL[ck["metric"]]] = (fmt(ck["value"]) if ck["value"] is not None else "—") + mark
            data.append(d)
        return pd.DataFrame(data)

    with t_sel:
        if res["selected"]:
            st.dataframe(value_table(res["selected"]), width="stretch", hide_index=True)
            pick = st.selectbox("查看入选依据", res["selected"], format_func=lambda c: f"{rows[c]['name']}（{c}）")
            st.markdown(explain.stock_explanation(rows[pick]))
            with st.expander("原始字段（可追溯）"):
                st.json({ck["desc"]: {"值": ck["value"], "来源": ck["source"], "时点": ck["as_of"], "原始字段": ck["raw"]}
                         for ck in rows[pick]["checks"]})
        else:
            st.info("无入选股票。可在「条件影响」中查看放宽哪个条件影响最大。")

    with t_exc:
        if res["near_miss"]:
            st.markdown("**差一点入选**（只差 1 个条件，且差距不超过一档）")
            st.dataframe(pd.DataFrame(res["near_miss"]), width="stretch", hide_index=True)
        exc = []
        for code in res["excluded"]:
            r = rows[code]
            reasons = ["ST 股票（默认剔除）"] if r["st_excluded"] else []
            reasons += [ck["reason"] for ck in r["checks"] if ck["status"] == "未通过"]
            exc.append({"代码": code, "名称": r["name"], "未通过条件数": r["n_fail"], "排除原因": "；".join(reasons)})
        if exc:
            st.dataframe(pd.DataFrame(exc).sort_values("未通过条件数"), width="stretch", hide_index=True)
            pick = st.selectbox("查看排除明细", res["excluded"], format_func=lambda c: f"{rows[c]['name']}（{c}）", key="pick_exc")
            st.markdown(explain.stock_explanation(rows[pick]))

    with t_und:
        st.caption("这些股票没有明确不满足的条件，但有数据缺失、过期或接口失败，系统**不会**把它们当作通过。")
        und = [{"代码": c, "名称": rows[c]["name"],
                "无法判断的条件": "；".join(f"{ck['desc']} → {ck['reason']}" for ck in rows[c]["checks"] if ck["status"] == "无法判断")}
               for c in res["undetermined"]]
        st.dataframe(pd.DataFrame(und) if und else pd.DataFrame([{"说明": "无"}]), width="stretch", hide_index=True)

    with t_imp:
        fun = pd.DataFrame(res["funnel"])
        fig = go.Figure(go.Funnel(y=fun["step"], x=fun["剩余"], textinfo="value"))
        fig.update_layout(height=60 + 50 * len(fun), margin=dict(l=10, r=10, t=10, b=10))
        st.markdown("**逐层筛选漏斗**（按条件顺序依次过滤，只统计「通过」）")
        st.plotly_chart(fig, width="stretch")
        st.markdown("**各条件单独通过情况**")
        st.dataframe(pd.DataFrame(res["stats"]).drop(columns=["cond_id"]), width="stretch", hide_index=True)
        st.markdown("**条件变化会带来什么影响**（每次只调整一个条件，其余不变）")
        sens_rows = []
        for r in ss["sens"]:
            sens_rows.append({
                "条件": r["条件"], "当前入选": r["当前入选"],
                "放宽一档": f"{r['放宽一档']}（{r['放宽一档_新阈值']}）",
                "收紧一档": f"{r['收紧一档']}（{r['收紧一档_新阈值']}）",
                "删除该条件": r["删除该条件"],
                "放宽后新增": "、".join(r["放宽一档_新增"][:6]) + ("…" if len(r["放宽一档_新增"]) > 6 else ""),
                "收紧后移出": "、".join(r["收紧一档_移出"][:6]) + ("…" if len(r["收紧一档_移出"]) > 6 else ""),
            })
        st.dataframe(pd.DataFrame(sens_rows), width="stretch", hide_index=True)

    with t_ai:
        st.caption("AI 只基于引擎给出的统计结果写说明；生成后自动做「数字回查」和「合规审查」。所有数字以表格为准。")
        call = llm_call()
        if not call:
            st.info("未配置 DeepSeek Key，AI 摘要不可用。上方表格中的逐股解释由确定性模板生成，不依赖 AI。")
        elif st.button("生成 AI 摘要"):
            facts = explain.build_facts(ds, res, ss["sens"])
            try:
                with st.spinner("生成中……"):
                    ss["summary"] = explain.ai_summary(call, facts) | {"facts": facts}
            except Exception as e:
                st.error(f"AI 摘要生成失败：{type(e).__name__}。不影响筛选结果。")
        sm = ss.get("summary")
        if sm:
            st.markdown(sm["text"])
            if sm["unverified_numbers"]:
                st.error(f"⚠️ 数字回查未通过：{', '.join(sm['unverified_numbers'])} 无法在引擎结果中找到，请勿采信这些数字。")
            else:
                st.success("✅ 数字回查通过：文中所有数字均可在引擎结果中找到。")
            if sm["compliance_flags"]:
                st.warning("合规审查已移除：" + "、".join(sm["compliance_flags"]))
            with st.expander("AI 输入的 facts（可追溯）"):
                st.json(sm["facts"])

    with t_log:
        log = get_client().call_log[-300:]
        st.caption(f"最近 {len(log)} 次接口调用（含缓存命中）。失败的调用会在对应股票上显示为「无法判断」。")
        st.dataframe(pd.DataFrame(log), width="stretch", hide_index=True)

    # ================================================================== ⑤ 后续动作
    st.divider()
    st.subheader("⑤ 继续操作：比较 / 回看 / 保存 / 监控")
    t_cmp, t_bt, t_save, t_mon = st.tabs(["⚖️ 比较", "📈 历史回看", "💾 保存方案", "🔔 转为监控任务"])
    name_of = {s["thscode"]: s["name"] for s in ds["stocks"]}

    with t_cmp:
        options = res["selected"] + [c for c in res["excluded"] + res["undetermined"]]
        default = res["selected"][:3]
        picks = st.multiselect("选择 2～5 只股票比较（可加入被排除的股票做对照）", options, default=default,
                               format_func=lambda c: f"{name_of[c]}（{rows[c]['verdict']}）", max_selections=5)
        if picks:
            data = {}
            for c in picks:
                f = ds["features"][c]
                data[f"{name_of[c]}"] = {KEY2LABEL[k]: (fmt(f[k]["value"]) if f[k]["status"] == "ok" else
                                                       f"— ({ {'na': '无意义', 'missing': '缺失', 'stale': '过期'}[f[k]['status']]})")
                                         for k in METRICS if k in f}
                data[f"{name_of[c]}"]["结论"] = rows[c]["verdict"]
            st.dataframe(pd.DataFrame(data), width="stretch")
            fig = go.Figure()
            for c in picks:
                srs = ds["series"].get(c, [])[-120:]
                if srs:
                    base = srs[0][1]
                    fig.add_trace(go.Scatter(x=[time.strftime("%Y-%m-%d", time.localtime(d / 1000)) for d, _ in srs],
                                             y=[v / base for _, v in srs], name=name_of[c], mode="lines"))
            fig.update_layout(height=320, margin=dict(l=10, r=10, t=30, b=10), title="近 120 个交易日价格走势（起点归一为 1，前复权）")
            st.plotly_chart(fig, width="stretch")

    with t_bt:
        days = st.radio("回看区间（交易日）", [60, 120, 250], index=1, horizontal=True)
        bt = lookback(ds["series"], res["selected"], ds.get("bench_series", []), days)
        for cv in (bt.get("caveats") or []):
            st.caption("⚠️ " + cv)
        if not bt["ok"]:
            st.info(bt["error"])
        else:
            m1, m2, m3 = st.columns(3)
            m1.metric("入选组合（等权）区间涨跌", f"{bt['total_return_pct']:.2f}%")
            m2.metric(f"基准 {ds['universe']} 指数", f"{bt['bench_return_pct']:.2f}%" if bt["bench_return_pct"] is not None else "无数据")
            m3.metric("组合区间最大回撤", f"{bt['max_drawdown_pct']:.2f}%")
            fig = go.Figure()
            fig.add_trace(go.Scatter(x=[d for d, _ in bt["curve"]], y=[v for _, v in bt["curve"]], name="入选组合（等权）"))
            if bt["bench"]:
                fig.add_trace(go.Scatter(x=[d for d, _ in bt["bench"]], y=[v for _, v in bt["bench"]], name="基准指数"))
            fig.update_layout(height=320, margin=dict(l=10, r=10, t=30, b=10), title=f"{bt['start']} ～ {bt['end']}")
            st.plotly_chart(fig, width="stretch")
            if ds.get("bench_error"):
                st.caption(f"基准指数获取失败：{ds['bench_error']}")

    with t_save:
        nm = st.text_input("方案名称", value=(ss.get("parse") or {}).get("raw_text", "")[:20])
        if st.button("保存当前方案"):
            snap = {"data_asof": ds.get("snapshot_asof"), "selected": [{"thscode": c, "name": name_of[c]} for c in res["selected"]]}
            storage.save_strategy(nm, (ss.get("parse") or {}).get("raw_text", ""), ds["universe"], res["conditions"],
                                  res["exclude_st"], snap)
            st.success("已保存")
        items = storage.list_strategies()
        if items:
            st.dataframe(pd.DataFrame([{"名称": s["name"], "股票池": s["universe"], "条件": "；".join(describe_condition(c) for c in s["conditions"]),
                                        "保存时入选数": len((s.get("result") or {}).get("selected", [])), "保存时间": s["created_at"]} for s in items]),
                         width="stretch", hide_index=True)
            sel = st.selectbox("载入 / 删除方案", items, format_func=lambda s: f"{s['name']}（{s['created_at']}）")
            b1, b2 = st.columns(2)
            if b1.button("载入到条件编辑器"):
                ss["parse"] = {"source": "saved", "conditions": sel["conditions"], "clarifications": [], "unsupported": [],
                               "notes": [f"已载入方案「{sel['name']}」"], "compliance": [], "raw_text": sel.get("text", ""),
                               "llm_error": None}
                ss["conditions"] = sel["conditions"]
                ss["universe"] = sel["universe"]
                ss["editor_ver"] += 1
                st.rerun()
            if b2.button("删除该方案"):
                storage.delete_strategy(sel["id"])
                st.rerun()
            st.download_button("导出全部方案（JSON）", json.dumps(items, ensure_ascii=False, indent=2),
                               file_name="strategies.json", mime="application/json")
        up = st.file_uploader("导入方案 JSON", type=["json"])
        if up is not None and st.button("导入"):
            try:
                n = storage.import_strategies(up.read().decode("utf-8"))
                st.success(f"导入 {n} 个方案")
            except Exception as e:
                st.error(f"导入失败：{e}")
        st.caption("注：云端部署时服务器重启会清空本地保存，请用「导出」备份。")

    with t_mon:
        st.caption("监控任务 = 保存的条件 + 检查频率。每次检查会重新拉取数据、重新筛选，并与上次结果对比「新进入 / 移出 / 无法判断」。"
                   "当前版本为手动触发检查（未部署后台定时器，见 README「未做事项」）。")
        freq = st.selectbox("检查频率（记录用）", ["每个交易日收盘后", "每周一次", "财报季每周"])
        if st.button("把当前方案转为监控任务"):
            strat = {"name": (ss.get("parse") or {}).get("raw_text", "监控")[:20] or "监控", "universe": ds["universe"],
                     "conditions": res["conditions"], "exclude_st": res["exclude_st"]}
            storage.create_monitor(strat, freq, [{"thscode": c, "name": name_of[c]} for c in res["selected"]])
            st.success("已创建监控任务")

# ====================================================================== 监控列表（任何时候都可查看）
mons = storage.list_monitors()
if mons:
    st.divider()
    st.subheader("🔔 我的监控任务")
    for m in mons:
        with st.expander(f"{m['name']}｜{m['strategy']['universe']}｜{m['frequency']}｜创建于 {m['created_at']}"):
            st.markdown("条件：" + "；".join(describe_condition(c) for c in m["strategy"]["conditions"]))
            hist = [{"检查时间": r["time"], "数据时点": r.get("data_asof", "—"), "入选数": len(r["selected"]),
                     "新进入": "、".join(x["name"] for x in r["added"]) or "—",
                     "移出": "、".join(x["name"] for x in r["removed"]) or "—",
                     "无法判断": len(r.get("undetermined", [])), "备注": r.get("note", "")} for r in m["runs"]]
            st.dataframe(pd.DataFrame(hist), width="stretch", hide_index=True)
            b1, b2 = st.columns(2)
            if b1.button("立即运行一次检查", key=f"run_{m['id']}", disabled=not config.fuyao_api_key()):
                d2 = load_dataset(m["strategy"]["universe"], force=True)
                r2 = engine.screen(d2, m["strategy"]["conditions"], m["strategy"]["exclude_st"])
                nm2 = {s["thscode"]: s["name"] for s in d2["stocks"]}
                storage.record_run(m["id"], [{"thscode": c, "name": nm2[c]} for c in r2["selected"]],
                                   [{"thscode": c, "name": nm2[c]} for c in r2["undetermined"]], d2.get("snapshot_asof"))
                st.rerun()
            if b2.button("删除任务", key=f"del_{m['id']}"):
                storage.delete_monitor(m["id"])
                st.rerun()

st.divider()
st.caption("数据来源：扶摇金融数据 API（fuyao.aicubes.cn）。AI：DeepSeek（仅用于意图解析与结果文字说明，不参与数值计算与筛选判定）。"
           + DISCLAIMER)
