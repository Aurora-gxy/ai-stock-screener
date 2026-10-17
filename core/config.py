"""配置读取：环境变量优先，其次 Streamlit secrets。Key 不写进代码和仓库。"""
from __future__ import annotations

import os
import time

# 扶摇时间戳以 Asia/Shanghai 为准（日 K 的 date_ms 是北京时间 0 点）。
# 云服务器默认 UTC，会把日期显示成前一天、把快照时间显示早 8 小时，所以统一设为北京时间。
os.environ["TZ"] = "Asia/Shanghai"
try:
    time.tzset()
except AttributeError:  # Windows 无 tzset
    pass

try:  # 本地开发读取 .env
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover
    pass


def _from_streamlit_secrets(name: str) -> str | None:
    try:
        import streamlit as st

        if name in st.secrets:
            return str(st.secrets[name])
    except Exception:
        return None
    return None


def get_setting(name: str, default: str | None = None) -> str | None:
    val = os.environ.get(name)
    if val:
        return val
    val = _from_streamlit_secrets(name)
    if val:
        return val
    return default


FUYAO_BASE_URL = "https://fuyao.aicubes.cn"
DEEPSEEK_BASE_URL_DEFAULT = "https://api.deepseek.com"
DEEPSEEK_MODEL_DEFAULT = "deepseek-chat"


def _clean_key(name: str) -> str | None:
    """未填写（仍是模板占位符）或含非 ASCII 字符的 Key 视为未配置，避免请求头编码崩溃。"""
    v = (get_setting(name) or "").strip().strip('"').strip("'")
    if not v or v.startswith("你的") or any(ord(c) > 127 for c in v) or " " in v:
        return None
    return v


def key_problem(name: str) -> str | None:
    raw = (get_setting(name) or "").strip()
    if not raw:
        return f"{name} 未设置"
    if raw.startswith("你的") or any(ord(c) > 127 for c in raw):
        return f"{name} 还是模板占位文字或含中文字符，请在 .env 里换成真实 Key"
    return None


def fuyao_api_key() -> str | None:
    return _clean_key("FUYAO_API_KEY")


def llm_api_key() -> str | None:
    return _clean_key("DEEPSEEK_API_KEY")


def llm_base_url() -> str:
    return get_setting("DEEPSEEK_BASE_URL", DEEPSEEK_BASE_URL_DEFAULT) or DEEPSEEK_BASE_URL_DEFAULT


def llm_model() -> str:
    return get_setting("DEEPSEEK_MODEL", DEEPSEEK_MODEL_DEFAULT) or DEEPSEEK_MODEL_DEFAULT


def cache_dir() -> str:
    d = get_setting("APP_CACHE_DIR", os.path.join(os.path.dirname(os.path.dirname(__file__)), ".cache"))
    os.makedirs(d, exist_ok=True)
    return d


def data_dir() -> str:
    d = get_setting("APP_DATA_DIR", os.path.join(os.path.dirname(os.path.dirname(__file__)), "data"))
    os.makedirs(d, exist_ok=True)
    return d
