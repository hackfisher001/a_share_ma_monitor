"""Webhook notifiers — Feishu first (default), then WeCom / DingTalk."""

from __future__ import annotations

import logging
import os
import re
from typing import Any

import requests

log = logging.getLogger(__name__)

TableColumn = dict[str, str]  # keys: name, display_name; optional data_type/width
TableSpec = dict[str, Any]  # keys: columns, rows; optional title, page_size

_DISCLAIMER = "仅供个人提醒，不构成投资建议"
_WIDTH_PX_RE = re.compile(r"^(\d+)px$")


def _post_json(url: str, payload: dict, timeout: float = 15.0) -> None:
    resp = requests.post(url, json=payload, timeout=timeout)
    resp.raise_for_status()
    data = resp.json() if resp.content else {}
    # WeCom/DingTalk: errcode; Feishu custom bot: StatusCode / code
    errcode = data.get("errcode", data.get("StatusCode", data.get("code", 0)))
    if errcode not in (0, "0", None):
        raise RuntimeError(f"Webhook 返回错误: {data}")


def _header_template(title: str) -> str:
    if "周报" in title or "月报" in title:
        return "purple"
    if "日报" in title or "DeepSeek" in title or "板块" in title:
        return "blue"
    if "超过" in title or "30%" in title or "40%" in title or "50%" in title:
        return "red"
    if "回撤" in title:
        return "yellow"
    return "orange"


def _normalize_width(width: str | None) -> str:
    """Feishu table column width must be auto, %, or [80px, 600px]."""
    if not width or width == "auto":
        return "auto"
    if width.endswith("%"):
        return width
    m = _WIDTH_PX_RE.match(width)
    if not m:
        return "auto"
    px = max(80, min(600, int(m.group(1))))
    return f"{px}px"


def _table_element(spec: TableSpec) -> dict[str, Any]:
    columns = []
    for col in spec["columns"]:
        data_type = col.get("data_type") or (
            "lark_md" if col["name"] == "name" else "text"
        )
        columns.append(
            {
                "name": col["name"],
                "display_name": col.get("display_name") or col["name"],
                "data_type": data_type,
                "width": _normalize_width(col.get("width")),
                "horizontal_align": col.get("align")
                or ("left" if col["name"] in {"name", "code"} else "right"),
            }
        )
    return {
        "tag": "table",
        "page_size": int(spec.get("page_size") or min(10, max(1, len(spec.get("rows") or [])))),
        "row_height": "low",
        "freeze_first_column": True,
        "header_style": {
            "text_align": "left",
            "text_size": "normal",
            "background_style": "grey",
            "text_color": "default",
            "bold": True,
            "lines": 1,
        },
        "columns": columns,
        "rows": list(spec.get("rows") or []),
    }


def _markdown_fallback_table(spec: TableSpec) -> str:
    cols = spec["columns"]
    headers = [c.get("display_name") or c["name"] for c in cols]
    keys = [c["name"] for c in cols]
    # Keep cells single-line; Feishu lark_md does not render pipe tables.
    lines = [
        f"**{spec['title']}**" if spec.get("title") else "",
        " · ".join(f"**{h}**" for h in headers),
    ]
    for row in spec.get("rows") or []:
        cells = []
        for k in keys:
            val = str(row.get(k, "—")).replace("\n", " ")
            cells.append(val)
        lines.append(" · ".join(cells))
    return "\n".join(x for x in lines if x)


def send_feishu(
    text: str = "",
    *,
    title: str = "买入提醒 · MA30",
    webhook_url: str | None = None,
    tables: list[TableSpec] | None = None,
    markdown: str | None = None,
) -> None:
    """
    Send Feishu interactive card.

    Prefer Card JSON 2.0 with native tables; fall back to markdown/text.
    Schema 2.0 does not support the legacy `note` tag — use markdown disclaimer.
    """
    url = webhook_url or os.getenv("FEISHU_WEBHOOK_URL", "").strip()
    if not url:
        raise ValueError("未配置 FEISHU_WEBHOOK_URL")

    template = _header_template(title)
    body_md = (markdown if markdown is not None else text) or ""
    if len(body_md) > 4500:
        body_md = body_md[:4400] + "\n\n…（内容过长已截断）"

    elements: list[dict[str, Any]] = []
    if body_md.strip():
        elements.append({"tag": "markdown", "content": body_md})
    for spec in tables or []:
        if spec.get("title"):
            elements.append({"tag": "markdown", "content": f"**{spec['title']}**"})
        elements.append(_table_element(spec))
    elements.append(
        {"tag": "markdown", "content": f"<font color='grey'>{_DISCLAIMER}</font>"}
    )

    card_v2: dict[str, Any] = {
        "msg_type": "interactive",
        "card": {
            "schema": "2.0",
            "header": {
                "title": {"tag": "plain_text", "content": title[:50]},
                "template": template,
            },
            "body": {"elements": elements},
        },
    }

    # Legacy card (1.0): markdown + flattened tables (no pipe syntax).
    legacy_parts = []
    if body_md.strip():
        legacy_parts.append(body_md)
    for spec in tables or []:
        legacy_parts.append(_markdown_fallback_table(spec))
    legacy_parts.append(_DISCLAIMER)
    legacy_body = "\n\n".join(legacy_parts) or title
    if len(legacy_body) > 4500:
        legacy_body = legacy_body[:4400] + "\n\n…（内容过长已截断）"
    card_v1: dict[str, Any] = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": title[:50]},
                "template": template,
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": legacy_body},
                },
            ],
        },
    }

    try:
        _post_json(url, card_v2)
    except Exception as exc:
        log.warning("飞书 Card 2.0 发送失败，回退旧版卡片: %s", exc)
        try:
            _post_json(url, card_v1)
        except Exception as exc2:
            log.warning("飞书 Card 1.0 发送失败，回退纯文本: %s", exc2)
            _post_json(
                url,
                {"msg_type": "text", "content": {"text": f"{title}\n{legacy_body}"}},
            )


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


def send_alert(
    text: str = "",
    *,
    title: str = "加仓提醒 · MA30",
    tables: list[TableSpec] | None = None,
    markdown: str | None = None,
) -> str:
    """
    Send via first configured channel.
    Priority: Feishu > WeCom > DingTalk.
    Returns channel name used.
    """
    if os.getenv("FEISHU_WEBHOOK_URL", "").strip():
        send_feishu(text, title=title, tables=tables, markdown=markdown)
        return "feishu"
    # Non-Feishu channels: flatten tables into text.
    flat = markdown or text
    if tables:
        flat = (flat + "\n\n" if flat else "") + "\n\n".join(
            _markdown_fallback_table(t) for t in tables
        )
    if os.getenv("WECOM_WEBHOOK_URL", "").strip():
        send_wecom(flat)
        return "wecom"
    if os.getenv("DINGTALK_WEBHOOK_URL", "").strip():
        send_dingtalk(flat)
        return "dingtalk"
    raise ValueError(
        "未配置 FEISHU_WEBHOOK_URL。请复制飞书群机器人 Webhook 到 .env"
    )


def send_test_ping() -> str:
    """Send a one-off connectivity check to the configured channel."""
    return send_alert(
        markdown=(
            "**连通测试成功**\n"
            "若看到本卡片，说明飞书 Webhook 可用，并支持 **加粗** 与表格样式。"
        ),
        title="连通测试",
        tables=[
            {
                "title": "样式预览",
                "columns": [
                    {"name": "item", "display_name": "项目", "width": "100px"},
                    {"name": "value", "display_name": "效果", "width": "160px"},
                ],
                "rows": [
                    {"item": "**加粗**", "value": "名称列可加粗"},
                    {"item": "表格", "value": "行情将按表格展示"},
                ],
            }
        ],
    )
