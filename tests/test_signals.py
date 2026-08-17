"""Unit tests that do not require network."""

from src.signals import QuoteSnapshot, deviation_pct, is_touching_ma30


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
