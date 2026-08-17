"""Unit tests that do not require network."""

from src.signals import (
    QuoteSnapshot,
    crossed_drawdown_levels,
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


def test_crossed_levels_fire_new_bands_only():
    # -22% should newly cross 5/10/15/20, not 30+
    sigs = crossed_drawdown_levels(
        _snap(78.0, 100.0),
        [5, 10, 15, 20, 30, 40, 50],
        already_fired=[5, 10],
    )
    assert [s.threshold_pct for s in sigs] == [15, 20]
    assert "仅供观察" in sigs[0].message


def test_crossed_levels_include_deep_bands():
    sigs = crossed_drawdown_levels(
        _snap(55.0, 100.0),
        [5, 10, 15, 20, 30, 40, 50],
        already_fired=(),
    )
    assert [s.threshold_pct for s in sigs] == [5, 10, 15, 20, 30, 40]
    assert "超过 40%" in sigs[-1].band_label
