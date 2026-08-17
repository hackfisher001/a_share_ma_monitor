"""Unit tests that do not require network."""

from src.signals import (
    QuoteSnapshot,
    deviation_pct,
    drawdown_pct_from_high,
    is_deep_drawdown,
    is_touching_ma30,
)


def test_deviation_pct():
    assert abs(deviation_pct(100.0, 100.0) - 0.0) < 1e-9
    assert abs(deviation_pct(100.5, 100.0) - 0.5) < 1e-9
    assert abs(deviation_pct(99.5, 100.0) - (-0.5)) < 1e-9


def test_touch_within_threshold():
    snap = QuoteSnapshot(
        code="600519",
        name="贵州茅台",
        price=100.4,
        ma30=100.0,
        as_of="2026-08-17",
        history_rows=60,
    )
    sig = is_touching_ma30(snap, touch_pct=0.5)
    assert sig is not None
    assert "贵州茅台" in sig.message
    assert "MA30" in sig.message


def test_touch_outside_threshold():
    snap = QuoteSnapshot(
        code="600519",
        name="贵州茅台",
        price=101.0,
        ma30=100.0,
        as_of="2026-08-17",
        history_rows=60,
    )
    assert is_touching_ma30(snap, touch_pct=0.5) is None


def _snap(price: float, high: float, rows: int = 300) -> QuoteSnapshot:
    return QuoteSnapshot(
        code="600519",
        name="贵州茅台",
        price=price,
        ma30=price,
        as_of="2026-08-17",
        history_rows=rows,
        high_252=high,
    )


def test_drawdown_pct_from_high():
    assert abs(drawdown_pct_from_high(70.0, 100.0) - (-30.0)) < 1e-9
    assert abs(drawdown_pct_from_high(100.0, 100.0)) < 1e-9


def test_deep_drawdown_triggers_at_threshold():
    sig = is_deep_drawdown(_snap(70.0, 100.0), 30)
    assert sig is not None
    assert sig.drawdown_pct <= -30
    assert "一年高点" in sig.message


def test_deep_drawdown_ignores_shallow_dip():
    assert is_deep_drawdown(_snap(75.0, 100.0), 30) is None


def test_deep_drawdown_needs_enough_history():
    assert is_deep_drawdown(_snap(70.0, 100.0, rows=60), 30) is None


def test_deep_drawdown_requires_valid_high():
    assert is_deep_drawdown(_snap(70.0, 0.0), 30) is None
