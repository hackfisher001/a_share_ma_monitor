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


def _strip_md(text: str) -> str:
    t = str(text or "")
    if t.startswith("**") and t.endswith("**") and len(t) > 4:
        return t[2:-2]
    return t.replace("\n", " ").strip()


def _mobile_stack_table(spec: TableSpec) -> str:
    """Phone-friendly vertical blocks when image upload is unavailable."""
    cols = spec["columns"]
    keys = [c["name"] for c in cols]
    labels = {c["name"]: (c.get("display_name") or c["name"]) for c in cols}
    lines: list[str] = []
    if spec.get("title"):
        lines.append(f"**{spec['title']}**")
    for row in spec.get("rows") or []:
        name = _strip_md(str(row.get("name", "")))
        code = _strip_md(str(row.get("code", "")))
        head = f"**{name}**" if name else "**—"
        if code:
            head += f" `{code}`"
        lines.append(head)
        parts: list[str] = []
        for k in keys:
            if k in {"name", "code"}:
                continue
            val = _strip_md(str(row.get(k, "—")))
            parts.append(f"{labels[k]} {val}")
        if parts:
            # Two short lines keep phones readable.
            mid = (len(parts) + 1) // 2
            lines.append(" · ".join(parts[:mid]))
            if parts[mid:]:
                lines.append(" · ".join(parts[mid:]))
        lines.append("")
    return "\n".join(lines).rstrip()


def _markdown_fallback_table(spec: TableSpec) -> str:
    return _mobile_stack_table(spec)


def _img_element(img_key: str, alt: str = "行情表") -> dict[str, Any]:
    return {
        "tag": "img",
        "img_key": img_key,
        "alt": {"tag": "plain_text", "content": alt[:100]},
        "mode": "fit_horizontal",
        "preview": True,
        "transparent": False,
        "corner_radius": "4px",
    }


def _try_upload_table_images(tables: list[TableSpec]) -> list[str]:
    from src.feishu_media import feishu_app_configured, upload_png_list
    from src.table_image import render_tables_png

    if not tables or not feishu_app_configured():
        return []
    try:
        pngs = render_tables_png(tables)
        keys = upload_png_list(pngs)
        log.info("已上传 %d 张行情表图片到飞书", len(keys))
        return keys
    except Exception as exc:
        log.warning("行情表转图片失败，改用手机竖排文本: %s", exc)
        return []


def send_feishu(
    text: str = "",
    *,
    title: str = "买入提醒 · MA30",
    webhook_url: str | None = None,
    tables: list[TableSpec] | None = None,
    markdown: str | None = None,
    image_keys: list[str] | None = None,
    prefer_images: bool = True,
) -> None:
    """
    Send Feishu interactive card.

    Preferred path for reports: render tables → upload PNG → img in Card 2.0
    (needs FEISHU_APP_ID/SECRET). Falls back to mobile stacked markdown.
    """
    url = webhook_url or os.getenv("FEISHU_WEBHOOK_URL", "").strip()
    if not url:
        raise ValueError("未配置 FEISHU_WEBHOOK_URL")

    template = _header_template(title)
    body_md = (markdown if markdown is not None else text) or ""
    if len(body_md) > 4500:
        body_md = body_md[:4400] + "\n\n…（内容过长已截断）"

    tables = tables or []
    keys = list(image_keys or [])
    if prefer_images and not keys and tables:
        keys = _try_upload_table_images(tables)

    elements: list[dict[str, Any]] = []
    if body_md.strip():
        elements.append({"tag": "markdown", "content": body_md})

    if keys:
        for i, key in enumerate(keys):
            alt = "行情表"
            if i < len(tables) and tables[i].get("title"):
                alt = str(tables[i]["title"])
            elements.append(_img_element(key, alt=alt))
    elif tables:
        # Avoid native Feishu tables — cramped on mobile, truncated on desktop.
        stack = "\n\n".join(_mobile_stack_table(t) for t in tables)
        elements.append({"tag": "markdown", "content": stack})

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

    legacy_parts = []
    if body_md.strip():
        legacy_parts.append(body_md)
    if not keys:
        for spec in tables:
            legacy_parts.append(_mobile_stack_table(spec))
    elif tables:
        legacy_parts.append("（本条含行情表图片，请在飞书客户端查看）")
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
    image_keys: list[str] | None = None,
    prefer_images: bool = True,
) -> str:
    """
    Send via first configured channel.
    Priority: Feishu > WeCom > DingTalk.
    Returns channel name used.
    """
    if os.getenv("FEISHU_WEBHOOK_URL", "").strip():
        send_feishu(
            text,
            title=title,
            tables=tables,
            markdown=markdown,
            image_keys=image_keys,
            prefer_images=prefer_images,
        )
        return "feishu"
    flat = markdown or text
    if tables:
        flat = (flat + "\n\n" if flat else "") + "\n\n".join(
            _mobile_stack_table(t) for t in tables
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
    """Send a one-off connectivity / style check."""
    return send_alert(
        markdown=(
            "**连通测试成功**\n"
            "若已配置 `FEISHU_APP_ID/SECRET`，下方应为 **图片表格**；"
            "否则为手机竖排文本。"
        ),
        title="连通测试",
        tables=[
            {
                "title": "样式预览",
                "columns": [
                    {"name": "name", "display_name": "名称", "width": "110px"},
                    {"name": "code", "display_name": "代码", "width": "80px"},
                    {"name": "d1", "display_name": "1日", "width": "80px"},
                    {"name": "dd", "display_name": "一年回撤", "width": "90px"},
                ],
                "rows": [
                    {"name": "**招商银行**", "code": "600036", "d1": "-0.68%", "dd": "-11.02%"},
                    {"name": "**长电科技**", "code": "600584", "d1": "-1.23%", "dd": "-8.50%"},
                ],
            }
        ],
    )
