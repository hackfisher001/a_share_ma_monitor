"""DeepSeek chat client (OpenAI-compatible)."""

from __future__ import annotations

import logging
import os

import requests

log = logging.getLogger("ma_monitor")

DEFAULT_BASE = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-v4-pro"


def deepseek_enabled() -> bool:
    return bool(os.getenv("DEEPSEEK_API_KEY", "").strip())


def chat(
    system: str,
    user: str,
    *,
    model: str | None = None,
    temperature: float = 0.3,
    max_tokens: int = 1600,
    timeout: float = 120.0,
) -> str:
    """Call DeepSeek chat completions; raises on hard failure."""
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise ValueError("未配置 DEEPSEEK_API_KEY")
    base = os.getenv("DEEPSEEK_API_BASE", DEFAULT_BASE).strip().rstrip("/")
    model_name = (model or os.getenv("DEEPSEEK_MODEL", DEFAULT_MODEL)).strip()

    url = f"{base}/chat/completions"
    payload = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    resp = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=timeout,
    )
    if resp.status_code >= 400:
        detail = resp.text[:300]
        if model_name != "deepseek-chat" and resp.status_code in (400, 404):
            log.warning(
                "DeepSeek 模型 %s 不可用 (%s)，回退 deepseek-chat",
                model_name,
                resp.status_code,
            )
            payload["model"] = "deepseek-chat"
            resp = requests.post(
                url,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=timeout,
            )
        if resp.status_code >= 400:
            raise RuntimeError(f"DeepSeek API 错误 {resp.status_code}: {detail}")

    data = resp.json()
    try:
        message = data["choices"][0]["message"]
        content = (message.get("content") or "").strip()
        if not content:
            # Some thinking models briefly return empty content; use reasoning tail.
            reasoning = (message.get("reasoning_content") or "").strip()
            content = reasoning[-800:] if reasoning else ""
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"DeepSeek 返回格式异常: {data}") from exc
    if not content:
        raise RuntimeError(f"DeepSeek 返回空内容: {data}")
    return content
