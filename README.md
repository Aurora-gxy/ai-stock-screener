# 自然语言智能选股与策略解释器

同花顺 26/27 届校招 AI 题，题目 02。

- 在线体验：https://ai-stock-screener-xinyi.streamlit.app
  （免费托管，长时间没人访问会休眠，打开后点一下唤醒按钮，等大约 30 秒）
- 代码仓库：https://github.com/Aurora-gxy/ai-stock-screener

## 这个产品解决什么问题

很多人选股时说的是“经营改善、估值合理、走势相对稳定”这样的话，但这些词没法直接拿去筛选。这个产品做的事情是：

1. 把一句话拆成具体的数据条件，比如“营收同比 ≥ 0%”“60 日波动率 ≤ 30%”；
2. 遇到模糊的词，先问清楚用哪种口径，条件也可以直接手动改；
3. 用扶摇的真实数据筛选，告诉用户每只股票为什么入选、为什么被排除；
4. 告诉用户哪个条件筛掉的股票最多，放宽或收紧一点结果会怎么变。

它不做的事：不预测涨跌，不给目标价，不给买卖建议，不承诺收益，也不给股票打一个看不懂来源的综合分。数据缺失或过期的股票单独列为“无法判断”，不会被当作满足条件。

## 使用流程

```
输入一句话
  ├─ 合规检查：识别预测涨跌、收益承诺、买卖建议等，明确告知不提供
  ├─ AI 解析（DeepSeek）
  └─ 规则解析（词表 + 正则）        两者对照，有差异就展示给用户
        ↓
澄清模糊词：每个模糊词有 2～3 种口径可选，已预选默认值
        ↓
条件表：可以改阈值、增删条件、临时关闭某个条件
        ↓
冲突识别：逻辑上不可能同时满足时不允许执行；互相制约时给出提醒
        ↓
筛选：每只股票逐条判定，分为入选、排除、无法判断
        ↓
解释：每条判定的实际值、来源和时点；逐层漏斗；每个条件放宽或收紧后的变化；可选的 AI 文字总结
        ↓
后续操作：股票对比、历史回看、保存方案、转为监控任务
```

## AI、筛选和数据各管什么

| 部分 | 代码 | 负责 | 不负责 |
|---|---|---|---|
| AI | `core/intent.py`、`core/llm.py`、`core/explain.py` | 把一句话转成条件；找出模糊词和暂不支持的诉求；把结果写成文字说明 | 不接触任何行情和财务数字，不做判定，不能自创指标 |
| 校验 | `core/intent.py`、`core/explain.py`、`core/compliance.py` | AI 输出的指标必须在白名单里、阈值必须是数字；用户写明的数字不允许 AI 改写；摘要里的数字必须能在结果里找到；去掉违规说法 | |
| 筛选 | `core/engine.py`、`core/conflicts.py` | 逐股逐条件比较、汇总、漏斗、敏感性分析、冲突识别 | 不调用 AI，同样的输入一定得到同样的结果 |
| 数据 | `core/fuyao.py`、`core/features.py` | 调用扶摇接口（含重试和限流处理），记录每次调用；把原始字段算成指标，并标注来源、时点和口径 | 不做判断，接口出错就如实报错 |

AI 不可用（没配置、超时、返回格式不对）时，会自动改用规则解析，页面上会说明原因。

## 数据来源和口径

数据全部来自扶摇金融数据 API（`https://fuyao.aicubes.cn`，请求头 `X-api-key`）。时间统一按北京时间显示。

| 用途 | 接口 |
|---|---|
| 股票池成分股 | `/api/a-share-index/constituents/ths-stock-list` |
| 最新价、涨跌幅 | `/api/a-share/prices/snapshot` |
| 日 K 线（前复权） | `/api/a-share/prices/historical` |
| 基准指数 K 线 | `/api/a-share-index/prices/historical` |
| 利润表、资产负债表、现金流量表 | `/api/a-share/financials/*` |

| 指标 | 怎么算 |
|---|---|
| 营收、归母净利润同比 | 最新报告期和上年同一报告期比较；上年同期 ≤ 0 时视为没有意义 |
| 经营现金流 | 最新报告期的经营活动现金流净额 |
| ROE（近似） | 最近年报的归母净利润 ÷ 同年末所有者权益（分母含少数股东权益，结果偏低一些） |
| 资产负债率 | 最新报告期的负债合计 ÷ 资产总计 |
| PE（静态） | 最新价 ÷ 最近年报的基本每股收益；每股收益 ≤ 0 时视为没有意义，判为不满足 |
| PE 股票池分位 | 在当前股票池 PE 为正的股票里排在什么位置 |
| PB（估算） | 用“归母净利润 ÷ 每股收益”反推总股本，再算每股净资产 |
| 波动率、回撤、涨跌幅、均线偏离、成交额 | 用前复权日 K 线计算，窗口长度见指标名称 |

扶摇文档里没有直接提供 PE、PB 或市值的接口，所以估值是自己算的，页面上标明了“静态”和“估算”。

每个指标都有一个状态：正常、无意义（比如亏损公司的 PE）、缺失（接口失败或数据不够）、过期（财报超过 200 天没有更新，或 K 线超过 10 天没有更新）。缺失和过期的股票归入“无法判断”，不会算作通过。

## 本地运行

```bash
git clone https://github.com/Aurora-gxy/ai-stock-screener.git
cd ai-stock-screener
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # 填入 FUYAO_API_KEY 和 DEEPSEEK_API_KEY
python scripts/probe_api.py   # 可选：先检查扶摇接口是否正常
streamlit run streamlit_app.py
```

运行测试（不需要网络和 Key）：

```bash
pytest -q
```

### 环境变量

| 变量 | 是否必填 | 说明 |
|---|---|---|
| `FUYAO_API_KEY` | 必填 | 扶摇 API Key。没有配置时页面不允许筛选，也不会用假数据代替 |
| `DEEPSEEK_API_KEY` | 选填 | 不配置时用规则解析，AI 摘要不可用 |
| `DEEPSEEK_BASE_URL`、`DEEPSEEK_MODEL` | 选填 | 默认为 `https://api.deepseek.com` 和 `deepseek-chat` |

### 部署到 Streamlit Community Cloud

1. 把代码推到 GitHub（`.env` 已在 `.gitignore` 里，不会被提交）
2. 在 share.streamlit.io 新建应用，选这个仓库，入口文件填 `streamlit_app.py`，Python 版本选 3.12
3. 在 Advanced settings 的 Secrets 里按 `.streamlit/secrets.toml.example` 的格式填入真实 Key
4. 点 Deploy

## 目录结构

```
streamlit_app.py        页面
core/
  fuyao.py              扶摇接口调用（重试、限流、错误码、缓存、调用日志）
  features.py           指标计算（来源、时点、口径、状态）
  metrics.py            指标清单和口径（AI 只能使用这里登记过的指标）
  intent.py             一句话转条件（AI + 规则）、澄清、校验
  llm.py                DeepSeek 调用
  conflicts.py          冲突识别
  engine.py             筛选和敏感性分析
  explain.py            逐股解释、AI 摘要和数字检查
  compliance.py         合规检查
  backtest.py           历史回看
  storage.py            方案和监控任务的保存
scripts/probe_api.py    真实接口检查脚本
tests/                  自动化测试（使用构造数据）
AI_USAGE.md             AI 使用与验证记录
TESTING.md              测试说明
```

## 已知限制和没做的部分

- 估值是自己算的：静态 PE 和估算 PB，不是 TTM，也不是官方口径。
- ROE 是近似值，分母包含少数股东权益。
- 同比用的是每家公司各自最新的报告期，不同公司可能处在不同报告期，页面上会逐只显示时点。
- 历史回看不是严格的回测：用今天的数据选出股票再看过去，存在前视偏差和幸存者偏差，也没有计算交易成本。
- 监控任务需要手动点“检查”，没有部署后台定时任务；云端应用重启后本地保存的数据会清空，可以用导出功能备份。
- 股票池目前只有 5 个指数可选。沪深 300、中证 500 第一次加载要几分钟（每只股票要调 5 个接口），之后当天会用缓存。
- 还没接入行业板块、资金流向、分红、市值、新闻和 iFinD MCP。用户提到这些时，页面会明确说明“暂时无法执行”，不会悄悄忽略。
- 还没做用户账号、多用户隔离、行业内估值比较和财报 TTM 计算。

## 合规声明

本产品只根据公开数据和用户设定的条件做客观筛选和解释，不构成投资建议，不预测股价涨跌，不承诺收益。仓库里没有任何 API Key 或用户隐私数据，测试用的都是构造数据。
