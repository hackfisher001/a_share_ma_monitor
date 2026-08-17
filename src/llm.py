"""DeepSeek chat client (OpenAI-compatible)."""

from __future__ import annotations

import logging
import os
import re

import requests

log = logging.getLogger("ma_monitor")

DEFAULT_BASE = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-v4-pro"

# Trailing self-talk that thinking models sometimes append after the real answer.
_META_MARKERS = (
    "这个约",
    "需要确认",
    "潜在问题",
    "可能还需要",
    "必须明确",
    "需要输出",
    "需要决定",
    "可以更详细",
    "不需要 markdown",
    "We need",
    "I need",
)


def deepseek_enabled() -> bool:
    return bool(os.getenv("DEEPSEEK_API_KEY", "").strip())


def _clean_answer(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    # Keep only the part before model self-critique / chain-of-thought leakage.
    cut = len(text)
    for marker in _META_MARKERS:
        idx = text.find(marker)
        if idx > 80:  # don't cut if marker appears too early by coincidence
            cut = min(cut, idx)
    text = text[:cut].strip()
    # Drop orphaned trailing half-sentences after 免责声明 when truncated mid-thought.
    m = re.search(r"免责声明[：:].+", text)
    if m:
        # keep through end of that sentence if present
        end = text.find("\n", m.end())
        if end == -1:
            # already ends near disclaimer — trim anything after next period cluster
            pass
        else:
            # If lots of meta after disclaimer newline, keep disclaimer paragraph only
            after = text[end:].strip()
            if any(k in after for k in _META_MARKERS) or after.startswith("仅供"):
                text = text[:end].strip()
    return text.strip()


def chat(
    system: str,
    user: str,
    *,
    model: str | None = None,
    temperature: float = 0.3,
    max_tokens: int = 4096,
    timeout: float = 180.0,
) -> str:
    """Call DeepSeek chat completions; never returns reasoning_content to callers."""
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise ValueError("未配置 DEEPSEEK_API_KEY")
    base = os.getenv("DEEPSEEK_API_BASE", DEFAULT_BASE).strip().rstrip("/")
    model_name = (model or os.getenv("DEEPSEEK_MODEL", DEFAULT_MODEL)).strip()

    url = f"{base}/chat/completions"
    # Force the final channel to be the only user-visible text.
    hardened_system = (
        system
        + "\n\n输出硬性规则：只输出最终给用户看的中文正文；"
        "禁止输出思考过程、自我检查、字数统计、对提示词的讨论；"
        "不要使用 markdown 标题；正文写完即停。"
    )
    payload = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": hardened_system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    def _request(body: dict) -> dict:
        resp = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=timeout,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"DeepSeek API 错误 {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    try:
        data = _request(payload)
    except RuntimeError as exc:
        if model_name != "deepseek-chat":
            log.warning("DeepSeek %s 失败，回退 deepseek-chat: %s", model_name, exc)
            payload["model"] = "deepseek-chat"
            data = _request(payload)
        else:
            raise

    try:
        message = data["choices"][0]["message"]
        content = _clean_answer(message.get("content") or "")
        finish = data["choices"][0].get("finish_reason")
        reasoning_len = len(message.get("reasoning_content") or "")
        log.info(
            "DeepSeek model=%s finish=%s content_chars=%d reasoning_chars=%d",
            payload["model"],
            finish,
            len(content),
            reasoning_len,
        )
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"DeepSeek 返回格式异常: {data}") from exc

    # Never ship reasoning_content. If final content is empty, retry non-thinking model.
    if not content and payload["model"] != "deepseek-chat":
        log.warning("DeepSeek 正文为空，改用 deepseek-chat 重试")
        payload["model"] = "deepseek-chat"
        data = _request(payload)
        message = data["choices"][0]["message"]
        content = _clean_answer(message.get("content") or "")

    if not content:
        raise RuntimeError("DeepSeek 返回空正文（已忽略 reasoning_content）")
    return content
