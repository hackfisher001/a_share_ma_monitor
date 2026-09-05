"""Beta-adjusted attribution of a move against its market benchmark."""

import numpy as np
import pandas as pd
import pytest

from src.relative import beta_vs, compare_to_benchmark


def _hist_from_returns(returns: list[float]) -> pd.DataFrame:
    closes = [100.0]
    for r in returns:
        closes.append(closes[-1] * (1 + r / 100.0))
    dates = pd.date_range("2024-01-02", periods=len(closes), freq="B")
    return pd.DataFrame({"date": dates, "close": closes})


def _paired(
    beta: float, noise: float = 1.0, n: int = 260
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Symbol = beta * benchmark + idiosyncratic noise of known σ."""
    rng = np.random.default_rng(7)
    bench = rng.normal(0, 1.0, n)
    sym = beta * bench + rng.normal(0, noise, n)
    return _hist_from_returns(list(sym)), _hist_from_returns(list(bench))


def test_beta_recovers_a_known_relationship():
    sym, bench = _paired(2.0)
    assert beta_vs(sym, bench) == pytest.approx(2.0, rel=0.05)


def test_beta_is_none_without_enough_overlap():
    short = _hist_from_returns([0.5, -0.4, 0.2])
    _, bench = _paired(1.0)
    assert beta_vs(short, bench) is None


def _peers(bench_change, sym_hist, bench_hist):
    return {
        "us:QQQM": ("纳指100ETF", bench_change, bench_hist),
        "us:MU": ("美光", None, sym_hist),
    }


def test_high_beta_selloff_is_not_called_idiosyncratic():
    """A β2 name falling 6% on a -3% index day is ordinary beta, not news."""
    sym, bench = _paired(2.0)
    move = compare_to_benchmark(
        market="us",
        code="MU",
        change_pct=-6.0,
        symbol_hist=sym,
        peers=_peers(-3.0, sym, bench),
    )

    assert move is not None
    assert move.beta == pytest.approx(2.0, rel=0.05)
    assert move.excess_pct == pytest.approx(0.0, abs=0.4)
    assert move.idiosyncratic is False
    assert move.verdict == "与大盘同向"


def test_a_drop_on_a_flat_market_is_idiosyncratic():
    sym, bench = _paired(2.0)
    move = compare_to_benchmark(
        market="us",
        code="MU",
        change_pct=-6.0,
        symbol_hist=sym,
        peers=_peers(0.1, sym, bench),
    )

    assert move is not None
    assert move.idiosyncratic is True
    assert move.verdict == "个股独有"
    line = move.markdown_line()
    assert "归因" in line and "纳指100ETF" in line and "超额" in line
    assert "β" not in line and "常态残差" not in line
    assert "建议先确认" not in line


def test_idiosyncratic_is_judged_against_the_symbols_own_residual():
    """A flat excess threshold would over-flag noisy names and miss quiet ones.

    Both fall 3% more than beta explains, but that is routine for the noisy
    symbol and a 3-sigma event for the quiet one.
    """
    noisy_sym, bench = _paired(1.0, noise=3.0)
    quiet_sym, _ = _paired(1.0, noise=1.0)

    def excess_of(hist):
        return compare_to_benchmark(
            market="us",
            code="MU",
            change_pct=-3.0,
            symbol_hist=hist,
            peers=_peers(0.0, hist, bench),
        )

    noisy, quiet = excess_of(noisy_sym), excess_of(quiet_sym)

    assert noisy is not None and quiet is not None
    assert noisy.excess_pct == pytest.approx(quiet.excess_pct, abs=0.01)
    assert noisy.idiosyncratic is False, "对高噪声标的这是常态波动"
    assert quiet.idiosyncratic is True, "对低噪声标的这是异常事件"


def test_a_statistically_odd_but_tiny_residual_stays_quiet():
    """2σ of a near-zero residual is still a rounding error in practice."""
    sym, bench = _paired(1.0, noise=0.1)
    move = compare_to_benchmark(
        market="us",
        code="MU",
        change_pct=-0.5,
        symbol_hist=sym,
        peers=_peers(0.0, sym, bench),
    )

    assert move is not None
    assert move.residual_multiple is not None and move.residual_multiple > 2.0
    assert move.idiosyncratic is False


def test_benchmark_does_not_compare_against_itself():
    sym, bench = _paired(1.0)
    assert (
        compare_to_benchmark(
            market="us",
            code="QQQM",
            change_pct=-3.0,
            symbol_hist=bench,
            peers=_peers(-3.0, sym, bench),
        )
        is None
    )


def test_missing_benchmark_degrades_to_none():
    sym, bench = _paired(1.0)
    assert (
        compare_to_benchmark(
            market="us",
            code="MU",
            change_pct=-6.0,
            symbol_hist=sym,
            peers={},
        )
        is None
    )
    # An unknown market has no configured benchmark at all.
    assert (
        compare_to_benchmark(
            market="jp",
            code="7203",
            change_pct=-6.0,
            symbol_hist=sym,
            peers=_peers(-3.0, sym, bench),
        )
        is None
    )
