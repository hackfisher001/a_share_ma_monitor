"""Swing-T backtest accounting: forced exits, costs, and loss recognition."""

import pandas as pd

from src.t_backtest import (
    TParams,
    bounce_after_dip,
    build_indicators,
    dip_statistics,
    rolling_high_drawdown,
    simulate_t,
)


def _hist(closes: list[float]) -> pd.DataFrame:
    dates = pd.date_range("2020-01-01", periods=len(closes), freq="B")
    return pd.DataFrame({"date": dates, "close": closes})


def _params(**kw) -> TParams:
    base = dict(
        profile="short",
        lookback_high=5,
        buy_dd_pct=2.0,
        sell_bounce_pct=1.0,
        max_hold_days=10,
        cooldown_days=2,
        ma30_filter=False,
        min_year_pos=0.0,
    )
    base.update(kw)
    return TParams(**base)


def test_bounce_after_dip_aligns_indices():
    hist = _hist([100, 98, 96, 95, 97, 99, 101, 100, 99, 98, 97, 99])
    dd = rolling_high_drawdown(hist["close"], 5)

    assert bounce_after_dip(hist["close"], dd, -2.0, 3)


def test_simulate_t_records_round_trip_net_of_cost():
    hist = _hist([10.0] * 20 + [10.0, 9.8, 9.6, 9.5, 9.7, 9.9, 10.1] * 6)
    res = simulate_t(hist, _params(), cost_pct=0.2)

    assert res.count > 0
    first = res.trades[0]
    assert abs(first.net_pct - (first.gross_pct - 0.2)) < 1e-9


def test_open_position_is_closed_at_end_of_data():
    # Falls and never rebounds: the round must be booked as a loss, not dropped.
    hist = _hist([10.0] * 20 + [10.0 - 0.1 * i for i in range(40)])
    res = simulate_t(hist, _params(max_hold_days=999), cost_pct=0.0)

    assert res.count >= 1
    assert res.trades[-1].exit_reason == "open_at_end"
    assert res.trades[-1].net_pct < 0
    assert res.win_rate < 1.0


def test_max_hold_forces_exit_even_without_target():
    hist = _hist([10.0] * 20 + [9.5] * 40)
    res = simulate_t(hist, _params(max_hold_days=5), cost_pct=0.0)

    assert res.count >= 1
    assert "max_hold" in res.exit_reasons()


def test_stop_loss_caps_single_trade_loss():
    hist = _hist([10.0] * 20 + [10.0 - 0.15 * i for i in range(40)])
    res = simulate_t(hist, _params(stop_loss_pct=4.0, max_hold_days=999), cost_pct=0.0)

    stopped = [t for t in res.trades if t.exit_reason == "stop_loss"]
    assert stopped
    assert min(t.gross_pct for t in stopped) <= -4.0


def test_max_drawdown_tracks_losing_streak():
    hist = _hist([10.0] * 20 + [10.0 - 0.1 * i for i in range(60)])
    res = simulate_t(hist, _params(stop_loss_pct=3.0, max_hold_days=20), cost_pct=0.1)

    assert res.max_drawdown < 0


def test_buy_hold_benchmark_is_reported():
    hist = _hist([10.0 + 0.02 * i for i in range(400)])
    res = simulate_t(hist, _params(), cost_pct=0.0)

    assert res.buy_hold_ann_pct > 0


def test_indicators_are_reusable_across_params():
    hist = _hist([10.0] * 20 + [10.0, 9.7, 9.5, 9.8, 10.2] * 20)
    ind = build_indicators(hist, 5)

    a = simulate_t(hist, _params(sell_bounce_pct=1.0), indicators=ind)
    b = simulate_t(hist, _params(sell_bounce_pct=5.0), indicators=ind)

    assert a.count >= b.count


def test_dip_statistics_reports_percentiles():
    hist = _hist([10.0 + (i % 7) * 0.2 for i in range(400)])
    stats = dip_statistics(hist, "short")

    assert stats["dd_p25"] < 0
    assert stats["samples"] > 0
