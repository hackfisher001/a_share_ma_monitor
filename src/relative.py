"""Separate a symbol's own move from the market move it was carried by.

"跌了 5%" answers almost nothing on its own. If the index fell just as far the
drop carries no information about the company; if the index was flat, something
happened to this name specifically and is worth a look before acting.

The comparison is beta-adjusted on purpose. A raw index subtraction would flag
every high-beta name as having idiosyncratic news: AMD and 美光 routinely move
2-3x the Nasdaq, so on a -3% index day their -8% is ordinary beta, not news.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

# Benchmark per market. Both already sit in the watchlist, so a scan fetches
# them anyway and this costs no extra requests.
DEFAULT_BENCHMARKS = {"cn": "510300", "us": "QQQM"}
BETA_WINDOW = 250
MIN_OVERLAP_FOR_BETA = 60
# How unusual the beta-adjusted residual must be before the move is called the
# symbol's own doing. Measured in residual σ rather than flat percent for the
# same reason the dip bands are: 美光's typical residual is several times
# 长江电力's, so one shared percentage would over-flag the former and
# under-flag the latter.
RESIDUAL_SIGMA_MULT = 2.0
# Floor in absolute terms, so a statistically large residual on an extremely
# quiet name is not announced when it amounts to a rounding error in practice.
IDIOSYNCRATIC_FLOOR_PCT = 1.0
# Fallback when the residual σ is unmeasurable.
IDIOSYNCRATIC_PCT = 1.5


def _returns(hist: pd.DataFrame, window: int) -> pd.Series | None:
    """Daily returns in percent, excluding the live bar, indexed by date."""
    if hist is None or "close" not in hist.columns or "date" not in hist.columns:
        return None
    frame = hist.iloc[:-1]
    if len(frame) < MIN_OVERLAP_FOR_BETA:
        return None
    closes = pd.to_numeric(frame["close"], errors="coerce")
    dates = pd.to_datetime(frame["date"]).dt.normalize()
    series = pd.Series(closes.to_numpy(), index=dates.to_numpy()).dropna()
    series = series[~series.index.duplicated(keep="last")]
    out = series.pct_change().dropna().tail(window) * 100.0
    return out if len(out) >= MIN_OVERLAP_FOR_BETA - 1 else None


@dataclass
class Regression:
    beta: float
    # σ of the part of the symbol's move the benchmark does not explain.
    residual_sigma: float


def regress(
    symbol_hist: pd.DataFrame,
    benchmark_hist: pd.DataFrame,
    *,
    window: int = BETA_WINDOW,
) -> Regression | None:
    """OLS beta plus residual σ, or None when unmeasurable.

    CN and US calendars only partially overlap, so the join is inner: a
    cross-listed pair simply contributes fewer usable days.
    """
    sym = _returns(symbol_hist, window)
    bench = _returns(benchmark_hist, window)
    if sym is None or bench is None:
        return None
    joined = pd.DataFrame({"sym": sym, "bench": bench}).dropna()
    if len(joined) < MIN_OVERLAP_FOR_BETA:
        return None
    variance = float(joined["bench"].var())
    if not variance > 0:
        return None
    beta = float(joined["sym"].cov(joined["bench"])) / variance
    residuals = joined["sym"] - beta * joined["bench"]
    return Regression(beta=beta, residual_sigma=float(residuals.std()))


def beta_vs(
    symbol_hist: pd.DataFrame,
    benchmark_hist: pd.DataFrame,
    *,
    window: int = BETA_WINDOW,
) -> float | None:
    fit = regress(symbol_hist, benchmark_hist, window=window)
    return None if fit is None else fit.beta


@dataclass
class RelativeMove:
    benchmark_name: str
    benchmark_change: float
    beta: float | None
    excess_pct: float
    residual_sigma: float | None = None

    @property
    def residual_multiple(self) -> float | None:
        if self.residual_sigma and self.residual_sigma > 0:
            return abs(self.excess_pct) / self.residual_sigma
        return None

    @property
    def idiosyncratic(self) -> bool:
        multiple = self.residual_multiple
        if multiple is None:
            return self.excess_pct <= -IDIOSYNCRATIC_PCT
        return (
            self.excess_pct <= -IDIOSYNCRATIC_FLOOR_PCT
            and multiple >= RESIDUAL_SIGMA_MULT
        )

    @property
    def verdict(self) -> str:
        if self.idiosyncratic:
            return "个股独有，建议先确认有无消息面"
        if self.benchmark_change <= -1.0:
            return "与大盘同向，属系统性下跌"
        return "跌幅未明显超出大盘"

    def markdown_line(self) -> str:
        beta = f"β{self.beta:.2f}" if self.beta is not None else "β—"
        multiple = self.residual_multiple
        excess = f"超额 {self.excess_pct:+.2f}%"
        if multiple is not None:
            excess += f"（{multiple:.1f}× 常态残差）"
        return (
            f"**基准：** {self.benchmark_name} {self.benchmark_change:+.2f}%"
            f"（{beta}）　{excess}\n"
            f"**归因：** {self.verdict}"
        )


def resolve_benchmark(market: str, benchmarks: dict[str, str] | None = None) -> str | None:
    table = benchmarks if benchmarks is not None else DEFAULT_BENCHMARKS
    return table.get(str(market or "").strip().lower())


def compare_to_benchmark(
    *,
    market: str,
    code: str,
    change_pct: float | None,
    symbol_hist: pd.DataFrame | None,
    peers: dict[str, tuple[str, float | None, pd.DataFrame | None]],
    benchmarks: dict[str, str] | None = None,
) -> RelativeMove | None:
    """Compare one symbol's move against its market benchmark.

    `peers` maps "market:code" to (name, change_pct, hist) for everything the
    scan already fetched, so the benchmark never costs an extra request.
    """
    if change_pct is None:
        return None
    bench_code = resolve_benchmark(market, benchmarks)
    if not bench_code:
        return None
    key = f"{str(market).lower()}:{str(bench_code).upper()}"
    if key == f"{str(market).lower()}:{str(code).upper()}":
        return None
    entry = peers.get(key)
    if not entry:
        return None
    bench_name, bench_change, bench_hist = entry
    if bench_change is None:
        return None
    fit = None
    if symbol_hist is not None and bench_hist is not None:
        fit = regress(symbol_hist, bench_hist)
    # Without a measurable beta, fall back to a plain difference rather than
    # dropping the comparison: the raw index move is still worth showing.
    effective_beta = 1.0 if fit is None else fit.beta
    excess = float(change_pct) - effective_beta * float(bench_change)
    return RelativeMove(
        benchmark_name=bench_name,
        benchmark_change=float(bench_change),
        beta=None if fit is None else fit.beta,
        excess_pct=excess,
        residual_sigma=None if fit is None else fit.residual_sigma,
    )
