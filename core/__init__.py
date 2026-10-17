"""自然语言智能选股与策略解释器 - 核心模块。

分层：
- fuyao.py      金融数据工具层（扶摇 API 封装，只负责取数与错误上报）
- features.py   指标计算层（把原始字段算成指标，每个值都带来源/时点/口径）
- intent.py     AI 层（自然语言 -> 结构化条件 + 澄清问题；LLM 失败时规则兜底）
- conflicts.py  冲突识别（确定性规则）
- engine.py     确定性筛选引擎（逐股逐条件判定、排除原因、敏感性分析）
- compliance.py 合规边界（输入意图拦截 + 输出文本审查）
- explain.py    AI 摘要 + 数字可追溯校验
- storage.py    方案保存 / 监控任务
- backtest.py   历史回看
"""

from . import config as _config  # noqa: E402,F401  先加载配置：统一北京时间时区
