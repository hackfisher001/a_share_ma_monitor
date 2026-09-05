"""Per-symbol intraday dip bands, sized by the symbol's own volatility.

A single set of absolute bands cannot serve this watchlist. Measured over the
last 250 sessions, daily σ ranges from 0.86% (长江电力) to 5.15% (美光) — a 6x
spread. A flat -3% band therefore means -3.5σ for the former (it fired 0 times
in a year) and -0.6σ for the latter (48 times, roughly every fifth session).
The first is an alert that never speaks, the second is noise.

Sizing each band as a multiple of the symbol's own σ makes the *rarity* of an
alert comparable across symbols, which is what actually decides whether it
deserves to interrupt you.
"""

from __future__ import annotations

import pandas as pd

# -kσ multiples, mirroring the spirit of the old (3, 5, 8) absolute ladder.
DEFAULT_SIGMA_LEVELS = (2.0, 3.0, 4.0)
# A 2σ move on a very quiet symbol can still be a trivially small number; below
# this it is not worth an interruption regardless of how unusual it is.
DEFAULT_MIN_ABS_PCT = 1.5
# Guards the pathological case of a symbol in crisis, whose σ would otherwise
# push the first band out to an unreachable depth.
DEFAULT_MAX_ABS_PCT = 20.0
# Clamping can squash two multiples onto nearly the same number — 闪迪 (σ 7.33%)
# yields 14.65 and 15.0, which are the same alert twice. Keep them apart.
MIN_BAND_GAP_PCT = 0.5
VOL_WINDOW = 250
# Below this many bars σ is too noisy to size a band on.
MIN_HISTORY_FOR_SIGMA = 60


def daily_sigma(hist: pd.DataFrame, window: int = VOL_WINDOW) -> float | None:
    """Std-dev of daily close returns in percent, or None when too short.

    The last bar is dropped: `attach_live_close` writes the live price there, so
    including it would fold today's in-progress move into the yardstick meant to
    judge it. Dropping one bar out of 250 costs nothing.
    """
    if hist is None or "close" not in hist.columns or len(hist) < 2:
        return None
    closes = pd.to_numeric(hist["close"], errors="coerce").dropna()
    if len(closes) < 2:
        return None
    settled = closes.iloc[:-1]
    if len(settled) < MIN_HISTORY_FOR_SIGMA:
        return None
    returns = settled.pct_change().dropna().tail(window) * 100.0
    if len(returns) < MIN_HISTORY_FOR_SIGMA - 1:
        return None
    sigma = float(returns.std())
    if not sigma > 0:
        return None
    return sigma


def calibrated_dip_levels(
    hist: pd.DataFrame,
    *,
    sigma_levels: tuple[float, ...] | list[float] = DEFAULT_SIGMA_LEVELS,
    min_abs_pct: float = DEFAULT_MIN_ABS_PCT,
    max_abs_pct: float = DEFAULT_MAX_ABS_PCT,
    window: int = VOL_WINDOW,
) -> tuple[float, ...]:
    """Absolute drop bands for one symbol, or () when σ is unavailable.

    Returning () lets the caller fall back to the configured absolute ladder
    rather than silently going quiet on a symbol with a short history.
    """
    sigma = daily_sigma(hist, window=window)
    if sigma is None:
        return ()
    lo, hi = abs(float(min_abs_pct)), abs(float(max_abs_pct))
    if lo > hi:
        lo, hi = hi, lo
    bands = set()
    for k in sigma_levels:
        raw = abs(float(k)) * sigma
        # Rounded so the value is byte-stable across scans within a session,
        # which is what the per-session dedup state matches on.
        bands.add(round(min(max(raw, lo), hi), 2))
    kept: list[float] = []
    for band in sorted(bands):
        if kept and band - kept[-1] < MIN_BAND_GAP_PCT:
            continue
        kept.append(band)
    return tuple(kept)


def sigma_multiple(change_pct: float | None, sigma: float | None) -> float | None:
    """How many σ today's move is, used to rank alerts against each other."""
    if change_pct is None or sigma is None or sigma <= 0:
        return None
    return abs(float(change_pct)) / sigma


def describe_band(band_pct: float, sigma: float | None) -> str:
    """Label a band by its σ multiple so the number has a scale attached."""
    if sigma is None or sigma <= 0:
        return f"-{band_pct:g}%"
    return f"-{band_pct:g}%（-{band_pct / sigma:.1f}σ）"
