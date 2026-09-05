"""Per-symbol dip band calibration."""

import pandas as pd
import pytest

from src.dip_bands import (
    calibrated_dip_levels,
    daily_sigma,
    describe_band,
    sigma_multiple,
)


def _hist(closes: list[float]) -> pd.DataFrame:
    dates = pd.date_range("2024-01-02", periods=len(closes), freq="B")
    return pd.DataFrame({"date": dates, "close": closes})


def _wobble(sigma_pct: float, n: int = 300) -> pd.DataFrame:
    """Deterministic series alternating ±sigma_pct so σ is known up front."""
    closes = [100.0]
    for i in range(n - 1):
        step = sigma_pct if i % 2 == 0 else -sigma_pct
        closes.append(closes[-1] * (1 + step / 100.0))
    return _hist(closes)


def test_sigma_ignores_the_live_bar():
    """The live bar holds today's in-progress move and must not set the yardstick."""
    calm = _wobble(1.0)
    baseline = daily_sigma(calm)

    crashed = calm.copy()
    crashed.iat[-1, crashed.columns.get_loc("close")] = float(
        crashed.iloc[-2]["close"] * 0.5
    )

    assert baseline is not None
    assert daily_sigma(crashed) == pytest.approx(baseline, rel=1e-9)


def test_bands_scale_with_the_symbols_own_volatility():
    quiet = calibrated_dip_levels(_wobble(0.9), sigma_levels=(2.0, 3.0))
    wild = calibrated_dip_levels(_wobble(5.0), sigma_levels=(2.0, 3.0))

    assert len(quiet) == 2 and len(wild) == 2
    # The whole point: a wild symbol must need a far bigger drop to speak up.
    assert wild[0] > quiet[-1] * 2


def test_bands_are_clamped_at_both_ends():
    tiny = calibrated_dip_levels(
        _wobble(0.05), sigma_levels=(2.0,), min_abs_pct=1.5, max_abs_pct=20.0
    )
    huge = calibrated_dip_levels(
        _wobble(20.0), sigma_levels=(4.0,), min_abs_pct=1.5, max_abs_pct=20.0
    )

    assert tiny == (1.5,)
    assert huge == (20.0,)


def test_clamping_never_yields_two_bands_at_the_same_depth():
    """闪迪-like case: σ 7.33% put 2σ at 14.65 and a capped 3σ at 15.0."""
    bands = calibrated_dip_levels(
        _wobble(7.33), sigma_levels=(2.0, 3.0, 4.0), max_abs_pct=15.0
    )

    # 2σ lands just under the cap and 3σ/4σ get squashed onto it, so only one
    # band survives instead of three alerts at effectively the same depth.
    assert len(bands) == 1
    assert 14.0 < bands[0] <= 15.0

    # With the wider default cap the escalation band survives as its own step.
    wide = calibrated_dip_levels(_wobble(7.33), sigma_levels=(2.0, 3.0, 4.0))
    assert len(wide) == 2 and wide[1] - wide[0] >= 0.5


def test_short_history_returns_nothing_so_the_caller_can_fall_back():
    assert calibrated_dip_levels(_hist([100.0 + i for i in range(20)])) == ()
    assert daily_sigma(_hist([100.0, 101.0])) is None


def test_bands_are_stable_across_repeated_calls():
    """Session dedup matches on the numeric band, so it must not drift."""
    hist = _wobble(2.3)
    assert calibrated_dip_levels(hist) == calibrated_dip_levels(hist)


def test_sigma_multiple_and_band_label():
    assert sigma_multiple(-6.0, 2.0) == pytest.approx(3.0)
    assert sigma_multiple(None, 2.0) is None
    assert sigma_multiple(-6.0, 0.0) is None
    assert describe_band(9.0, 4.5) == "-9%（-2.0σ）"
    assert describe_band(3.0, None) == "-3%"
