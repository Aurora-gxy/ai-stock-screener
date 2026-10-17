"""方案保存与监控任务（本地 JSON 持久化）。

注意：Streamlit Community Cloud 的文件系统在应用重启后会被重置，
因此页面同时提供 JSON 导出 / 导入，作为跨会话保存的方式（README 已注明）。
"""
from __future__ import annotations

import json
import os
import time
import uuid

from . import config


def _path(name: str) -> str:
    return os.path.join(config.data_dir(), name)


def _load(name: str) -> list:
    p = _path(name)
    if not os.path.exists(p):
        return []
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save(name: str, items: list) -> None:
    with open(_path(name), "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)


def now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- 方案
def list_strategies() -> list:
    return _load("strategies.json")


def save_strategy(name: str, text: str, universe: str, conditions: list, exclude_st: bool,
                  result_snapshot: dict | None = None) -> dict:
    items = list_strategies()
    s = {"id": uuid.uuid4().hex[:8], "name": name or f"方案 {len(items) + 1}", "text": text, "universe": universe,
         "conditions": conditions, "exclude_st": exclude_st, "created_at": now_str(), "result": result_snapshot}
    items.append(s)
    _save("strategies.json", items)
    return s


def delete_strategy(sid: str) -> None:
    _save("strategies.json", [s for s in list_strategies() if s["id"] != sid])


def import_strategies(payload: str) -> int:
    data = json.loads(payload)
    if isinstance(data, dict):
        data = [data]
    items = list_strategies()
    n = 0
    for s in data:
        if isinstance(s, dict) and isinstance(s.get("conditions"), list):
            s["id"] = uuid.uuid4().hex[:8]
            items.append(s)
            n += 1
    _save("strategies.json", items)
    return n


# ---------------------------------------------------------------- 监控
def list_monitors() -> list:
    return _load("monitors.json")


def create_monitor(strategy: dict, frequency: str, baseline: list[dict]) -> dict:
    items = list_monitors()
    m = {"id": uuid.uuid4().hex[:8], "name": strategy["name"], "strategy": strategy, "frequency": frequency,
         "created_at": now_str(), "runs": [{"time": now_str(), "selected": baseline, "added": [], "removed": [],
                                             "undetermined": [], "note": "创建时基线"}]}
    items.append(m)
    _save("monitors.json", items)
    return m


def record_run(mid: str, selected: list[dict], undetermined: list[dict], data_asof: str) -> dict | None:
    items = list_monitors()
    for m in items:
        if m["id"] == mid:
            prev = {x["thscode"] for x in m["runs"][-1]["selected"]} if m["runs"] else set()
            cur = {x["thscode"] for x in selected}
            run = {"time": now_str(), "data_asof": data_asof, "selected": selected,
                   "added": [x for x in selected if x["thscode"] not in prev],
                   "removed": [x for x in m["runs"][-1]["selected"] if x["thscode"] not in cur] if m["runs"] else [],
                   "undetermined": undetermined}
            m["runs"].append(run)
            _save("monitors.json", items)
            return run
    return None


def delete_monitor(mid: str) -> None:
    _save("monitors.json", [m for m in list_monitors() if m["id"] != mid])
