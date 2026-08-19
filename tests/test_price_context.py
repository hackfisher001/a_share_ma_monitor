"""Price-path context and relative recent-pullback tests."""

import pandas as pd

from src.price_context import (
    build_price_context,
    compose_alert_markdown,
    detect_recent_pullback,
    stage_label,
)
from src.table_image import render_sparkline_card_png


def _hist(closes: list[float]) -> pd.DataFrame:
    dates = pd.date_range("2025-01-02", periods=len(closes), freq="B")
    return pd.DataFrame({"date": dates, "close": closes})


def test_stage_label_distinguishes_paths():
    assert (
        stage_label(week=1.0, month=8.0, dd20=-1.0, year_dd=-2.0, ma_dev=3.0, ma_up=True)
        == "接近一年高位震荡"
    )
    assert (
        stage_label(week=-3.0, month=6.0, dd20=-6.0, year_dd=-12.0, ma_dev=1.0, ma_up=True)
        == "上涨后开始走弱"
    )
    assert (
        stage_label(week=2.0, month=-10.0, dd20=-4.0, year_dd=-20.0, ma_dev=-3.0, ma_up=False)
        == "深度回撤后反弹"
    )
    assert (
        stage_label(week=-2.0, month=-8.0, dd20=-7.0, year_dd=-18.0, ma_dev=-4.0, ma_up=False)
        == "持续下跌"
    )


def test_recent_pullback_uses_own_history_not_fixed_percent():
    # Quiet grind, then a sharp 3-day drop that is extreme for this series.
    closes = [100.0 + i * 0.05 for i in range(200)]
    closes[-3] = closes[-4] * 0.99
    closes[-2] = closes[-3] * 0.97
    closes[-1] = closes[-2] * 0.96
    hist = _hist(closes)

    hit, reason = detect_recent_pullback(hist, closes[-1])
    assert hit is True
    assert "近3日" in reason


def test_recent_pullback_ignores_ordinary_noise():
    closes = [100.0 + (i % 7 - 3) * 0.2 for i in range(200)]
    hist = _hist(closes)
    hit, reason = detect_recent_pullback(hist, closes[-1])
    assert hit is False
    assert reason == ""


def test_context_markdown_includes_stage_and_horizons():
    closes = [80.0 + i * 0.2 for i in range(260)]
    hist = _hist(closes)
    price = closes[-1]
    ma30 = sum(closes[-30:]) / 30
    ctx = build_price_context(hist, price, ma30)
    md = compose_alert_markdown("**测试** 贴近均线", ctx)

    assert "阶段" in md
    assert "近期" in md
    assert "距一年高" in md
    assert "MA30" in md
    assert len(ctx.spark["values"]) >= 250


def test_sparkline_card_png_smoke():
    png = render_sparkline_card_png(
        title="国际黄金",
        subtitle="深度回撤后反弹",
        spark={
            "values": [100, 130, 160, 150, 120, 125, 138],
            "years": [2025, 2025, 2025, 2025, 2026, 2026, 2026],
        },
    )
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(png) > 800
