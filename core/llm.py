"""LLM 适配（DeepSeek，OpenAI 兼容接口）。未配置 Key 时返回 None，上层走规则兜底。"""
from __future__ import annotations

from . import config


def get_llm_call(timeout: float = 30.0):
    key = config.llm_api_key()
    if not key:
        return None
    try:
        from openai import OpenAI
    except ImportError:
        return None
    client = OpenAI(api_key=key, base_url=config.llm_base_url(), timeout=timeout, max_retries=1)
    model = config.llm_model()

    def call(system: str, user: str, json_mode: bool = True) -> str:
        kwargs = {}
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=0,
            **kwargs,
        )
        return resp.choices[0].message.content or ""

    call.model = model  # type: ignore[attr-defined]
    return call
