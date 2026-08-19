"""Period return unit tests."""

import pandas as pd

from src.perf import change_by_calendar_days, change_by_trading_days, compute_period_changes


def _hist(closes):
    dates = pd.date_range("2025-01-01", periods=len(closes), freq="B")
    return pd.DataFrame({"date": dates, "close": closes})


def test_trading_day_change():
    hist = _hist([100, 110, 121])
    # price 121 vs 1 session ago 110 → +10%
    assert abs(change_by_trading_days(hist, 121.0, 1) - 10.0) < 1e-9
    assert abs(change_by_trading_days(hist, 121.0, 2) - 21.0) < 1e-9


def test_calendar_change():
    hist = _hist([100 + i for i in range(30)])
    pct = change_by_calendar_days(hist, float(hist.iloc[-1]["close"]), 7)
    assert pct is not None


def test_compute_periods_labels():
    hist = _hist([100 + i * 0.5 for i in range(260)])
    ch = compute_period_changes(hist, float(hist.iloc[-1]["close"]))
    assert [c.label for c in ch] == ["1日", "5日", "1周", "1月", "半年", "1年"]


def test_attach_live_close_makes_intraday_change_use_prev_close():
    from datetime import date

    from src.fetch_quotes import attach_live_close

    hist = _hist([15.42, 15.65, 16.66])
    aligned = attach_live_close(hist, 15.25, date(2025, 1, 6))
    # Last hist bar is 2025-01-03; live session is 2025-01-06 → append.
    pct = change_by_trading_days(aligned, 15.25, 1)
    assert abs(pct - ((15.25 / 16.66) - 1) * 100) < 1e-9


def test_attach_live_close_updates_same_session_bar():
    from datetime import date

    from src.fetch_quotes import attach_live_close

    hist = _hist([15.65, 16.66])
    last = pd.Timestamp(hist.iloc[-1]["date"]).date()
    aligned = attach_live_close(hist, 15.25, last)
    assert len(aligned) == len(hist)
    pct = change_by_trading_days(aligned, 15.25, 1)
    assert abs(pct - ((15.25 / 15.65) - 1) * 100) < 1e-9
