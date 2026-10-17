# 测试说明

## 1. 自动化测试

```bash
pytest -q
```

- 不访问网络，不需要 API Key
- 数据来自 `tests/fakes.py` 的**构造数据**（代码 9xxxxx，名称以「测试」开头），每只构造股票都对应一种场景
- 覆盖：主链路、数据缺失 / 接口失败、LLM 异常输出、冲突识别、合规边界、页面冒烟

### 1.1 构造股票与对应场景

| 代码 | 设计场景 | 预期 |
|---|---|---|
| 900001.SH | 稳健上涨、增长、低负债 | 入选 |
| 900002.SH | 高波动 | 走势条件「未通过」→ 排除 |
| 900003.SH | 上年同期亏损、EPS<0 | 同比与 PE 为「无意义」→ 排除，原因写明 |
| 900004.SH | 上市不足 60 日、快照缺失 | 走势指标「无法判断」；价格退回最近收盘并注明 |
| 900005.SH | *ST | 默认剔除 |
| 900006.SH | 财报接口返回 code=5000 | 基本面「无法判断」，不当作通过 |

### 1.2 用例清单

| 类别 | 用例 | 文件 / 函数 |
|---|---|---|
| 主链路 | 示例意图解析出 5 个条件和 3 个澄清问题 | `test_rule_parse_default_example` |
| 主链路 | 显式数值优先于模糊词默认口径（含区间） | `test_explicit_number_overrides_fuzzy` |
| 主链路 | 切换澄清选项替换对应条件 | `test_clarification_replaces_term_conditions` |
| 主链路 | 端到端筛选：入选、排除、ST、无法判断；每条判定有来源与时点；漏斗单调 | `test_end_to_end_screen` |
| 主链路 | 敏感性：收紧 ≤ 当前 ≤ 放宽；删除条件不减少结果 | `test_sensitivity_and_binding` |
| 主链路 | 历史回看能计算并附带局限说明 | `test_lookback` |
| 主链路 | 方案保存、监控任务新进入 / 移出对比 | `test_storage_roundtrip` |
| 主链路 | 页面：解析 → 筛选 → 保存 → 监控 → 立即检查，无异常 | `test_app_smoke.py::test_main_flow` |
| 数据异常 | 亏损公司 PE / 同比标为「无意义」，判为不满足 | `test_loss_company_pe_not_meaningful` |
| 数据异常 | 财报接口报错 → 「无法判断」，绝不入选 | `test_missing_never_passes` |
| 数据异常 | 快照缺失 → 退回最近收盘并标注来源 | `test_snapshot_missing_falls_back_to_close` |
| 数据异常 | 超时 / HTTP 500 / 非 JSON / 429 限流 / 业务错误码，均显式失败并进日志 | `test_api_failures_are_explicit`（5 个参数化） |
| 数据异常 | 未配置 API Key | `test_no_api_key` |
| 数据异常 | K 线接口失败 → 走势指标缺失并带 HTTP 状态 | `test_kline_failure_marks_missing` |
| 数据异常 | 股票池 PE 样本 <5 → 分位标为不可靠，不假装可算 | `test_end_to_end_screen` 前半段 |
| AI 异常 | LLM 输出不是 JSON → 规则兜底 | `test_llm_invalid_output_falls_back_to_rule` |
| AI 异常 | LLM 发明指标、阈值非数字 → 丢弃并提示 | `test_llm_hallucinated_metric_dropped` |
| AI 异常 | LLM 超时 → 规则兜底并显示原因 | `test_llm_exception_falls_back` |
| AI 异常 | AI 摘要出现引擎结果中没有的数字 → 标红；违规表述被移除 | `test_ai_summary_number_verification` |
| 冲突 | AI 把矛盾的明确数值改写为区间 → 还原原文并触发冲突（真实案例回归） | `test_llm_cannot_rewrite_explicit_numbers` |
| 冲突 | 同一指标区间无交集 → 阻止执行 | `test_conflict_same_metric`、`test_app_smoke::test_conflict_blocks_run` |
| 冲突 | 「近 60 日跌 ≥20%」与「120 日回撤 ≤10%」数学矛盾 | `test_conflict_cross_metric_drawdown` |
| 冲突 | 高增长 + 低估值 → 张力提示（不阻止） | `test_tension_growth_vs_cheap` |
| 冲突 | 分位 >100 → 超出取值范围 | `test_out_of_range` |
| 合规 | 预测涨跌 / 收益承诺 / 买卖建议 / 内幕 / 隐私输入 | `test_input_compliance`（5 个参数化） |
| 合规 | 正常意图不误伤 | `test_normal_input_no_compliance_hit` |
| 合规 | 输出中的「必涨」「建议买入」「目标价」被移除 | `test_output_sanitized` |
| 合规 | 不支持的诉求（龙头、分红）被明确列出 | `test_unsupported_terms_reported` |
| 合规 | 页面输入「明天会涨、稳赚」显示两条合规提示 | `test_app_smoke::test_compliance_example` |

## 2. 真实接口联调

```bash
python scripts/probe_api.py 600519.SH
```

逐个调用项目用到的 8 个扶摇接口，核对返回字段是否与代码一致，结果写入 `probe_report.json`。

## 3. 手工验收（真实数据，在线版 https://ai-stock-screener-xinyi.streamlit.app ，2026-10-17）

截图编号对应提交包 `01_产品截图/` 中的文件。

| # | 操作 | 预期 | 结果 |
|---|---|---|---|
| M1 | 输入「经营改善、估值合理、走势相对稳定」，股票池上证50，解析并执行 | 出现 3 个澄清问题、5 个条件；结果分入选 / 排除 / 无法判断三栏；每只股票可展开原始字段和时点 | ✅ 通过（2026-10-17 在线版）：3 个澄清问题、5 个条件；入选 5 / 排除 45 / 无法判断 0；逐股展示实际值、来源与时点。见截图 01、02、07、08 与演示视频 |
| M2 | 任选一只入选股，用同花顺 App 核对最新价与年报 EPS，手算 PE | 与页面 PE 一致（允许快照时差） | 未执行（时间所限）。接口层面已用 probe 脚本核对 600519.SH：价格 1329 元、2025 年报 EPS 65.66 元，PE≈20.2，数量级合理 |
| M3 | 把「估值合理」切换为「绝对：PE 0～30」 | 条件表随之变化，结果重新计算 | 未单独截图；条件切换逻辑由自动化测试 `test_clarification_replaces_term_conditions` 覆盖 |
| M4 | 输入「市盈率低于10倍，市盈率高于20倍」 | 红色冲突提示，执行按钮禁用 | ✅ 通过：两行 PE 条件 + 红色冲突提示 + 执行按钮禁用。首次测试发现 AI 将矛盾条件改写为区间（见 AI_USAGE H8），修复后复测通过。见截图 03、04、05 |
| M5 | 输入「帮我找明天会涨的股票，最好稳赚」 | 两条合规提示；不产生预测类条件 | ✅ 通过：「预测」「收益承诺」两条合规提示，未生成任何条件。见截图 06 |
| M6 | 输入「行业龙头、高分红」 | 两项被列为「无法执行的诉求」 | 未在线执行；由自动化测试 `test_unsupported_terms_reported` 覆盖 |
| M7 | 「条件影响」页 | 漏斗、各条件通过数、放宽 / 收紧 / 删除后的数量和新增 / 移出名单 | ✅ 通过：漏斗（50→28→19→6→5→5→5）与各条件通过数正常显示。见截图 09 |
| M8 | 生成 AI 摘要 | 显示「数字回查通过」或标红未通过的数字 | ✅ 通过：摘要按「事实 / 推断 / 不确定」分段，显示「数字回查通过」。见截图 10 |
| M9 | 比较 3 只股票、历史回看 120 日 | 对比表与走势图；回看页显示 4 条局限说明 | ✅ 通过：3 只股票对比表 + 120 日归一走势图；回看页显示 4 条局限说明及组合 / 基准对比。见截图 12、13 |
| M10 | 保存方案 → 导出 JSON → 删除 → 导入 | 方案恢复 | 部分：在线保存方案成功（见演示视频）；导出 / 导入未单独验证，由 `test_storage_roundtrip` 覆盖 |
| M11 | 转为监控任务 → 立即运行一次检查 | 历史表新增一行，显示新进入 / 移出 | ✅ 通过：创建监控任务并生成基线记录，「立即运行一次检查」重新拉取数据（进度条可见）。见截图 14 与演示视频 |
| M12 | 在 Secrets 中临时删除 DeepSeek Key 后刷新 | 侧边栏显示规则兜底，主链路仍可用 | ✅ 以等价场景验证：本地测试时 DeepSeek 返回 402 余额不足，页面显示失败原因并自动切换规则解析，主链路正常（见 AI_USAGE H6） |
