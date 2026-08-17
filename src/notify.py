"""Webhook notifiers — Feishu first (default), then WeCom / DingTalk."""

from __future__ import annotations

import os
from typing import Any

import requests


def _post_json(url: str, payload: dict, timeout: float = 15.0) -> None:
    resp = requests.post(url, json=payload, timeout=timeout)
    resp.raise_for_status()
    data = resp.json() if resp.content else {}
    # WeCom/DingTalk: errcode; Feishu custom bot: StatusCode / code
    errcode = data.get("errcode", data.get("StatusCode", data.get("code", 0)))
    if errcode not in (0, "0", None):
        raise RuntimeError(f"Webhook 返回错误: {data}")


def send_feishu(
    text: str,
    *,
    title: str = "买入提醒 · MA30",
    webhook_url: str | None = None,
) -> None:
    """Send Feishu custom-bot message as an interactive card (falls back to text)."""
    url = webhook_url or os.getenv("FEISHU_WEBHOOK_URL", "").strip()
    if not url:
        raise ValueError("未配置 FEISHU_WEBHOOK_URL")

    if "日报" in title:
        template = "blue"
    elif "回撤" in title:
        template = "red"
    else:
        template = "orange"
    # Feishu card text soft limit — truncate politely if oversized
    body = text
    if len(body) > 4500:
        body = body[:4400] + "\n\n…（内容过长已截断）"

    card: dict[str, Any] = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": title[:50]},
                "template": template,
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": body,
                    },
                },
                {
                    "tag": "note",
                    "elements": [
                        {
                            "tag": "plain_text",
                            "content": "仅供个人提醒，不构成投资建议",
                        }
                    ],
                },
            ],
        },
    }
    try:
        _post_json(url, card)
    except Exception:
        _post_json(url, {"msg_type": "text", "content": {"text": f"{title}\n{text}"}})


def send_wecom(text: str, webhook_url: str | None = None) -> None:
    url = webhook_url or os.getenv("WECOM_WEBHOOK_URL", "").strip()
    if not url:
        raise ValueError("未配置 WECOM_WEBHOOK_URL")
    _post_json(
        url,
        {"msgtype": "text", "text": {"content": text}},
    )


def send_dingtalk(text: str, webhook_url: str | None = None) -> None:
    url = webhook_url or os.getenv("DINGTALK_WEBHOOK_URL", "").strip()
    if not url:
        raise ValueError("未配置 DINGTALK_WEBHOOK_URL")
    _post_json(
        url,
        {"msgtype": "text", "text": {"content": text}},
    )


def send_alert(text: str, *, title: str = "加仓提醒 · MA30") -> str:
    """
    Send via first configured channel.
    Priority: Feishu > WeCom > DingTalk.
    Returns channel name used.
    """
    if os.getenv("FEISHU_WEBHOOK_URL", "").strip():
        send_feishu(text, title=title)
        return "feishu"
    if os.getenv("WECOM_WEBHOOK_URL", "").strip():
        send_wecom(text)
        return "wecom"
    if os.getenv("DINGTALK_WEBHOOK_URL", "").strip():
        send_dingtalk(text)
        return "dingtalk"
    raise ValueError(
        "未配置 FEISHU_WEBHOOK_URL。请复制飞书群机器人 Webhook 到 .env"
    )


def send_test_ping() -> str:
    """Send a one-off connectivity check to the configured channel."""
    return send_alert(
        "股价监控机器人连通测试成功 ✅\n若看到此消息，说明飞书 Webhook 配置正确。",
        title="连通测试",
    )
