"""Render report tables as crisp PNG images for Feishu."""

from __future__ import annotations

import io
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

TableSpec = dict[str, Any]
_PX_RE = re.compile(r"^(\d+)px$")

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
    return text.strip() or "—"


def _measure(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> tuple[int, int]:
    box = draw.multiline_textbbox((0, 0), text, font=font, spacing=5)
    return box[2] - box[0], box[3] - box[1]


def _draw_sparkline(
    draw: ImageDraw.ImageDraw,
    raw: Any,
    box: tuple[int, int, int, int],
    *,
    scale: int,
) -> None:
    """Draw one-year close path with year boundary, peak, and latest point."""
    if not isinstance(raw, dict):
        return
    values = [float(v) for v in raw.get("values") or []]
    years = [int(v) for v in raw.get("years") or []]
    if len(values) < 2:
        return

    left, top, right, bottom = box
    chart_left = left + 10 * scale
    chart_right = right - 10 * scale
    chart_top = top + 9 * scale
    chart_bottom = bottom - 18 * scale
    low, high = min(values), max(values)
    span = high - low or 1.0
    width = max(1, chart_right - chart_left)
    height = max(1, chart_bottom - chart_top)
    points = [
        (
            chart_left + int(i / (len(values) - 1) * width),
            chart_bottom - int((value - low) / span * height),
        )
        for i, value in enumerate(values)
    ]

    # Light area makes the path easier to scan without resembling a trading signal.
    area = points + [(points[-1][0], chart_bottom), (points[0][0], chart_bottom)]
    draw.polygon(area, fill="#EAF3FF")
    draw.line(points, fill="#1677FF", width=max(2, scale), joint="curve")

    small = _load_font(10 * scale)
    if len(years) == len(values):
        changes = [i for i in range(1, len(years)) if years[i] != years[i - 1]]
        if changes:
            idx = changes[-1]
            x = points[idx][0]
            dash = 4 * scale
            yy = chart_top
            while yy < chart_bottom:
                draw.line([(x, yy), (x, min(yy + dash, chart_bottom))], fill="#AAB4C3")
                yy += dash * 2
            draw.text((chart_left, bottom - 15 * scale), str(years[0]), fill="#86909C", font=small)
            draw.text((x + 3 * scale, bottom - 15 * scale), str(years[idx]), fill="#86909C", font=small)
        elif years:
            draw.text((chart_left, bottom - 15 * scale), str(years[0]), fill="#86909C", font=small)

    peak_idx = max(range(len(values)), key=values.__getitem__)
    px, py = points[peak_idx]
    radius = 3 * scale
    draw.ellipse([px - radius, py - radius, px + radius, py + radius], fill="#F59E0B")
    draw.text((px + 3 * scale, max(top, py - 12 * scale)), "高", fill="#B45309", font=small)

    lx, ly = points[-1]
    draw.ellipse([lx - radius, ly - radius, lx + radius, ly + radius], fill="#0B4F9C")
    draw.text(
        (max(chart_left, lx - 12 * scale), min(chart_bottom - 10 * scale, ly + 3 * scale)),
        "今",
        fill="#0B4F9C",
        font=small,
    )


def render_sparkline_card_png(
    *,
    title: str,
    subtitle: str,
    spark: dict,
    scale: int = 2,
) -> bytes:
    """Standalone one-year path card for MA / drawdown / recent-dip alerts."""
    width, height = 720 * scale // 2, 220 * scale // 2
    img = Image.new("RGB", (width, height), "#FFFFFF")
    draw = ImageDraw.Draw(img)
    font_title = _load_font(16 * scale)
    font_sub = _load_font(12 * scale)
    pad = 12 * scale
    draw.text((pad, 8 * scale), title, fill="#1F2329", font=font_title)
    draw.text((pad, 30 * scale), subtitle, fill="#4E5969", font=font_sub)
    _draw_sparkline(draw, spark, (0, 48 * scale, width, height), scale=scale)
    draw.rectangle([0, 0, width - 1, height - 1], outline="#D0D5DD")
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


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
    raw_data = [[row.get(k, "—") for k in keys] for row in rows]
    data = [
        [
            "走势" if cols[i].get("data_type") == "sparkline" else _cell_text(value)
            for i, value in enumerate(row)
        ]
        for row in raw_data
    ]

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
        configured = 0
        match = _PX_RE.match(str(cols[i].get("width") or ""))
        if match:
            configured = int(match.group(1)) * scale
        if cols[i].get("data_type") == "sparkline":
            col_widths.append(max(configured, 220 * scale))
            continue
        w, _ = _measure(draw, header, font_head)
        for row in data:
            cw, _ = _measure(draw, row[i], font_body)
            w = max(w, cw)
        # Name column a bit wider; keep readable min width.
        min_w = 88 * scale if keys[i] == "name" else 64 * scale
        col_widths.append(max(configured, min_w, w + pad_x * 2))

    content_h = max(
        [
            _measure(draw, cell, font_body)[1]
            for row in data
            for i, cell in enumerate(row)
            if cols[i].get("data_type") != "sparkline"
        ]
        or [_measure(draw, "字高", font_body)[1]]
    )
    has_sparkline = any(c.get("data_type") == "sparkline" for c in cols)
    row_h = max(70 * scale if has_sparkline else 36 * scale, content_h + pad_y * 2)
    header_h = max(38 * scale, _measure(draw, "字高", font_head)[1] + pad_y * 2)
    title_h = 0
    if title:
        title_h = _measure(draw, title, font_title)[1] + title_gap

    width = sum(col_widths) + 2
    height = title_h + header_h + row_h * len(data) + 2
    img = Image.new("RGB", (width, height), "#FFFFFF")
    draw = ImageDraw.Draw(img)

    y = 0
    if title:
        draw.text((pad_x, 4 * scale), title, fill="#1F2329", font=font_title)
        y = title_h

    # Header
    draw.rectangle([0, y, width, y + header_h], fill="#EEF2F6")
    x = 0
    for i, header in enumerate(headers):
        draw.text((x + pad_x, y + pad_y), header, fill="#4E5969", font=font_head)
        x += col_widths[i]
    y += header_h

    # Body
    for r, row in enumerate(data):
        bg = "#FFFFFF" if r % 2 == 0 else "#F7F8FA"
        draw.rectangle([0, y, width, y + row_h], fill=bg)
        x = 0
        for i, cell in enumerate(row):
            if cols[i].get("data_type") == "sparkline":
                _draw_sparkline(
                    draw,
                    raw_data[r][i],
                    (x, y, x + col_widths[i], y + row_h),
                    scale=scale,
                )
                x += col_widths[i]
                continue
            # Emphasize name; tint negative/positive lightly via text color.
            fill = "#1F2329"
            if keys[i] == "name":
                fill = "#0B4F9C"
            elif cell.startswith("-") and cell.endswith("%"):
                fill = "#C2410C"
            elif cell.startswith("+") and cell.endswith("%"):
                fill = "#0F766E"
            draw.multiline_text(
                (x + pad_x, y + pad_y),
                cell,
                fill=fill,
                font=font_body,
                spacing=5,
            )
            x += col_widths[i]
        y += row_h

    # Outer border + vertical grid
    draw.rectangle([0, title_h, width - 1, height - 1], outline="#D0D5DD")
    x = 0
    for w in col_widths[:-1]:
        x += w
        draw.line([(x, title_h), (x, height - 1)], fill="#E5E6EB")
    draw.line([(0, title_h + header_h), (width - 1, title_h + header_h)], fill="#D0D5DD")
    for i in range(1, len(data)):
        yy = title_h + header_h + row_h * i
        draw.line([(0, yy), (width - 1, yy)], fill="#E5E6EB")

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def render_tables_png(tables: list[TableSpec]) -> list[bytes]:
    return [render_table_png(spec) for spec in tables]
