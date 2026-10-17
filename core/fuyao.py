"""扶摇金融数据 API 封装（数据工具层）。

职责边界：只负责「取数 + 显式报错 + 记录来源」，不做任何判断或推断。
- 每次调用返回 FetchResult，包含 endpoint、参数、request_id、取数时间、错误信息
- 业务错误（code != 0）、HTTP 错误、超时、限流都不会被吞掉，而是以 ok=False 返回
- 磁盘缓存：减少重复请求；缓存命中也保留原始 request_id 与取数时间，便于追溯

接口文档：https://fuyao.aicubes.cn/docs/  鉴权头：X-api-key
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

import requests

from . import config

# 扶摇文档中的错误码（节选）
ERROR_CODES = {
    1003: "参数非法",
    2001: "API Key 缺失或无效",
    2003: "无权访问该接口",
    4001: "触发限流",
}


@dataclass
class FetchResult:
    ok: bool
    endpoint: str
    params: dict
    data: Any = None
    error: str | None = None
    code: int | None = None
    request_id: str | None = None
    fetched_at: float = field(default_factory=time.time)
    from_cache: bool = False

    def items(self) -> list:
        """兼容 data.item / data.items / data 本身为列表三种形态。"""
        d = self.data
        if d is None:
            return []
        if isinstance(d, list):
            return d
        if isinstance(d, dict):
            for k in ("item", "items", "list", "records"):
                if isinstance(d.get(k), list):
                    return d[k]
        return []

    def data_timestamp(self) -> int | None:
        if isinstance(self.data, dict):
            ts = self.data.get("timestamp")
            if isinstance(ts, (int, float)):
                return int(ts)
        return None

    def to_dict(self) -> dict:
        return asdict(self)


class FuyaoClient:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = config.FUYAO_BASE_URL,
        timeout: float = 15.0,
        max_retries: int = 3,
        use_cache: bool = True,
        session: Any = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.api_key = api_key if api_key is not None else config.fuyao_api_key()
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.use_cache = use_cache
        self.session = session or requests.Session()
        self._sleep = sleep
        self.call_log: list[dict] = []  # 本次会话的调用记录，用于页面「数据调用日志」

    # ------------------------------------------------------------------ 基础请求
    def _cache_path(self, endpoint: str, params: dict, ttl_key: str) -> str:
        raw = json.dumps({"e": endpoint, "p": params, "t": ttl_key}, sort_keys=True, ensure_ascii=False)
        h = hashlib.md5(raw.encode("utf-8")).hexdigest()
        return os.path.join(config.cache_dir(), f"{h}.json")

    @staticmethod
    def _ttl_key(ttl: str) -> str:
        now = time.localtime()
        if ttl == "day":
            return time.strftime("%Y%m%d", now)
        if ttl == "10min":
            return time.strftime("%Y%m%d%H", now) + str(now.tm_min // 10)
        return "none"

    def get(self, endpoint: str, params: dict | None = None, ttl: str = "day") -> FetchResult:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        if not self.api_key:
            res = FetchResult(False, endpoint, params, error="未配置 FUYAO_API_KEY，无法获取真实数据", code=2001)
            self._log(res)
            return res

        cache_file = self._cache_path(endpoint, params, self._ttl_key(ttl)) if (self.use_cache and ttl != "none") else None
        if cache_file and os.path.exists(cache_file):
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    d = json.load(f)
                res = FetchResult(**d)
                res.from_cache = True
                self._log(res)
                return res
            except Exception:
                pass  # 缓存损坏则重新请求

        url = f"{self.base_url}{endpoint}"
        headers = {"X-api-key": self.api_key, "Accept": "application/json"}
        last_err = "未知错误"
        last_code = None
        for attempt in range(self.max_retries):
            try:
                resp = self.session.get(url, params=params, headers=headers, timeout=self.timeout)
            except requests.Timeout:
                last_err, last_code = f"请求超时（>{self.timeout}s）", None
                self._sleep(1.0 * (attempt + 1))
                continue
            except requests.RequestException as e:
                last_err, last_code = f"网络错误：{type(e).__name__}", None
                self._sleep(1.0 * (attempt + 1))
                continue

            if resp.status_code == 429:
                last_err, last_code = "HTTP 429 限流", 4001
                self._sleep(2.0 * (attempt + 1))
                continue
            if resp.status_code >= 400:
                res = FetchResult(False, endpoint, params, error=f"HTTP {resp.status_code}", code=resp.status_code)
                self._log(res)
                return res
            try:
                body = resp.json()
            except ValueError:
                res = FetchResult(False, endpoint, params, error="响应不是合法 JSON")
                self._log(res)
                return res

            code = body.get("code")
            if code == 4001:
                last_err, last_code = "业务限流 code=4001", 4001
                self._sleep(2.0 * (attempt + 1))
                continue
            if code != 0:
                msg = body.get("message") or ERROR_CODES.get(code, "业务错误")
                res = FetchResult(False, endpoint, params, error=f"code={code}：{msg}", code=code,
                                  request_id=body.get("request_id"))
                self._log(res)
                return res

            res = FetchResult(True, endpoint, params, data=body.get("data"), code=0,
                              request_id=body.get("request_id"))
            if cache_file:
                try:
                    with open(cache_file, "w", encoding="utf-8") as f:
                        json.dump(res.to_dict(), f, ensure_ascii=False)
                except Exception:
                    pass
            self._log(res)
            return res

        res = FetchResult(False, endpoint, params, error=f"重试 {self.max_retries} 次后失败：{last_err}", code=last_code)
        self._log(res)
        return res

    def _log(self, res: FetchResult) -> None:
        self.call_log.append({
            "time": time.strftime("%H:%M:%S", time.localtime(res.fetched_at)),
            "endpoint": res.endpoint,
            "params": json.dumps(res.params, ensure_ascii=False)[:120],
            "ok": res.ok,
            "cache": res.from_cache,
            "request_id": res.request_id,
            "error": res.error,
        })

    # ------------------------------------------------------------------ 业务接口
    def index_constituents(self, thscode: str) -> FetchResult:
        return self.get("/api/a-share-index/constituents/ths-stock-list", {"thscode": thscode})

    def ths_index_list(self, tag: str = "industry") -> FetchResult:
        return self.get("/api/a-share-index/catalog/ths-index-list", {"tag": tag})

    def ticker_search(self, q: str, asset_type: str | None = None, limit: int = 10) -> FetchResult:
        return self.get("/api/meta/tickers/search", {"q": q, "asset_type": asset_type, "limit": limit})

    def snapshot(self, thscodes: list[str]) -> FetchResult:
        """批量快照。文档未写单次上限，这里按 50 只一批分片后合并。"""
        merged: list = []
        first: FetchResult | None = None
        errors = []
        for i in range(0, len(thscodes), 50):
            chunk = thscodes[i:i + 50]
            r = self.get("/api/a-share/prices/snapshot", {"thscodes": ",".join(chunk)}, ttl="10min")
            if first is None:
                first = r
            if r.ok:
                merged.extend(r.items())
            else:
                errors.append(r.error)
        if first is None:
            return FetchResult(False, "/api/a-share/prices/snapshot", {}, error="空代码列表")
        ok = len(errors) == 0
        data = {"timestamp": first.data_timestamp(), "item": merged}
        return FetchResult(ok or bool(merged), first.endpoint, {"thscodes": f"{len(thscodes)} 只"}, data=data,
                           error="; ".join(errors) if errors else None, request_id=first.request_id,
                           fetched_at=first.fetched_at, from_cache=first.from_cache)

    def daily_kline(self, thscode: str, days: int = 400, adjust: str = "forward") -> FetchResult:
        # 起止时间按天对齐，保证同一天内缓存命中
        start_day = time.strftime("%Y-%m-%d", time.localtime(time.time() - days * 86400))
        start = int(time.mktime(time.strptime(start_day, "%Y-%m-%d")) * 1000)
        end_day = int(time.mktime(time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d")) * 1000) + 86400000 - 1
        return self.get("/api/a-share/prices/historical",
                        {"thscode": thscode, "interval": "1d", "start": start, "end": end_day, "adjust": adjust})

    def index_kline(self, thscode: str, days: int = 400) -> FetchResult:
        start_day = time.strftime("%Y-%m-%d", time.localtime(time.time() - days * 86400))
        start = int(time.mktime(time.strptime(start_day, "%Y-%m-%d")) * 1000)
        end_day = int(time.mktime(time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d")) * 1000) + 86400000 - 1
        return self.get("/api/a-share-index/prices/historical",
                        {"thscode": thscode, "interval": "1d", "start": start, "end": end_day})

    def income(self, thscode: str, period: str = "quarterly", limit: int = 5) -> FetchResult:
        return self.get("/api/a-share/financials/income-statements", {"thscode": thscode, "period": period, "limit": limit})

    def balance(self, thscode: str, period: str = "quarterly", limit: int = 1) -> FetchResult:
        return self.get("/api/a-share/financials/balance-sheets", {"thscode": thscode, "period": period, "limit": limit})

    def cashflow(self, thscode: str, period: str = "quarterly", limit: int = 1) -> FetchResult:
        return self.get("/api/a-share/financials/cash-flow-statements", {"thscode": thscode, "period": period, "limit": limit})

    def trading_days(self) -> FetchResult:
        return self.get("/api/a-share/calendar/trading-days")
