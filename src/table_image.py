"""Render report tables as crisp PNG images for Feishu."""

from __future__ import annotations

import io
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

TableSpec = dict[str, Any]

_FONT_CANDIDATES = (
    # macOS
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
    # Linux (Aliyun)
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)


@lru_cache(maxsize=8)
def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    override = os.getenv("TABLE_IMAGE_FONT", "").strip()
    paths = [override] if override else []
    paths.extend(_FONT_CANDIDATES)
    for path in paths:
        if not path or not Path(path).exists():
            continue
        try:
            return ImageFont.truetype(path, size=size, index=0)
        except OSError:
            continue
    return ImageFont.load_default()


def _cell_text(value: Any) -> str:
    text = str(value if value is not None else "—")
    # Strip markdown bold used by Feishu table cells.
    if text.startswith("**") and text.endswith("**") and len(text) > 4:
        text = text[2:-2]
    return text.replace("\n", " ").strip() or "—"


def _measure(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> tuple[int, int]:
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0], box[3] - box[1]


def render_table_png(spec: TableSpec, *, scale: int = 2) -> bytes:
    """
    Render one table spec to PNG bytes (Retina-ish scale for mobile clarity).
    """
    cols = list(spec.get("columns") or [])
    rows = list(spec.get("rows") or [])
    title = str(spec.get("title") or "").strip()
    if not cols:
        raise ValueError("表格缺少列定义")

    headers = [_cell_text(c.get("display_name") or c.get("name")) for c in cols]
    keys = [str(c["name"]) for c in cols]
    data = [[_cell_text(row.get(k, "—")) for k in keys] for row in rows]

    pad_x = 14 * scale
    pad_y = 10 * scale
    title_gap = 12 * scale
    font_title = _load_font(17 * scale)
    font_head = _load_font(14 * scale)
    font_body = _load_font(14 * scale)

    probe = Image.new("RGB", (10, 10), "white")
    draw = ImageDraw.Draw(probe)

    col_widths: list[int] = []
    for i, header in enumerate(headers):
        w, _ = _measure(draw, header, font_head)
        for row in data:
            cw, _ = _measure(draw, row[i], font_body)
            w = max(w, cw)
        # Name column a bit wider; keep readable min width.
        min_w = 88 * scale if keys[i] == "name" else 64 * scale
        col_widths.append(max(min_w, w + pad_x * 2))

    row_h = max(36 * scale, _measure(draw, "字高", font_body)[1] + pad_y * 2)
    title_h = 0
    if title:
        title_h = _measure(draw, title, font_title)[1] + title_gap

    width = sum(col_widths) + 2
    height = title_h + row_h * (1 + len(data)) + 2
    img = Image.new("RGB", (width, height), "#FFFFFF")
    draw = ImageDraw.Draw(img)

    y = 0
    if title:
        draw.text((pad_x, 4 * scale), title, fill="#1F2329", font=font_title)
        y = title_h

    # Header
    draw.rectangle([0, y, width, y + row_h], fill="#EEF2F6")
    x = 0
    for i, header in enumerate(headers):
        draw.text((x + pad_x, y + pad_y), header, fill="#4E5969", font=font_head)
        x += col_widths[i]
    y += row_h

    # Body
    for r, row in enumerate(data):
        bg = "#FFFFFF" if r % 2 == 0 else "#F7F8FA"
        draw.rectangle([0, y, width, y + row_h], fill=bg)
        x = 0
        for i, cell in enumerate(row):
            # Emphasize name; tint negative/positive lightly via text color.
            fill = "#1F2329"
            if keys[i] == "name":
                fill = "#0B4F9C"
            elif cell.startswith("-") and cell.endswith("%"):
                fill = "#C2410C"
            elif cell.startswith("+") and cell.endswith("%"):
                fill = "#0F766E"
            draw.text((x + pad_x, y + pad_y), cell, fill=fill, font=font_body)
            x += col_widths[i]
        y += row_h

    # Outer border + vertical grid
    draw.rectangle([0, title_h, width - 1, height - 1], outline="#D0D5DD")
    x = 0
    for w in col_widths[:-1]:
        x += w
        draw.line([(x, title_h), (x, height - 1)], fill="#E5E6EB")
    for i in range(1, len(data) + 1):
        yy = title_h + row_h * i
        draw.line([(0, yy), (width - 1, yy)], fill="#E5E6EB")

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def render_tables_png(tables: list[TableSpec]) -> list[bytes]:
    return [render_table_png(spec) for spec in tables]
