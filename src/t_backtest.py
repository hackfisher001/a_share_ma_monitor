"""Swing-T parameter research: walk-forward backtest with costs and forced exits.

Every position is closed eventually (target, stop, max hold, or end of data), so
losing and stuck rounds are counted instead of silently dropped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product
from typing import Any

import numpy as np
import pandas as pd

PROFILE_SPECS: dict[str, dict[str, Any]] = {
    "short": {
        "lookback_high": 10,
        "max_hold_days": 10,
        "min_trades_per_year": 4.0,
        "max_trades_per_year": 40.0,
        "bounce_horizon": 7,
    },
    "mid": {
        "lookback_high": 20,
        "max_hold_days": 30,
        "min_trades_per_year": 2.0,
        "max_trades_per_year": 25.0,
        "bounce_horizon": 15,
    },
    "slow": {
        "lookback_high": 252,
        "max_hold_days": 120,
        "min_trades_per_year": 0.6,
        "max_trades_per_year": 8.0,
        "bounce_horizon": 45,
    },
}

# Round-trip cost in percentage points (commission + stamp duty + slippage).
ROUND_TRIP_COST_PCT = {
    "cn_stock": 0.20,
    "cn_etf": 0.10,
    "us": 0.10,
}

CALIBRATION_UNIVERSE: dict[str, list[tuple[str, str, str, str]]] = {
    "short": [
        ("600036", "cn", "招商银行", "cn_stock"),
        ("600377", "cn", "宁沪高速", "cn_stock"),
        ("600900", "cn", "长江电力", "cn_stock"),
        ("601088", "cn", "中国神华", "cn_stock"),
        ("600941", "cn", "中国移动", "cn_stock"),
    ],
    "mid": [
        ("601899", "cn", "紫金矿业", "cn_stock"),
        ("TSLA", "us", "特斯拉", "us"),
        ("AMD", "us", "AMD", "us"),
        ("NVDA", "us", "英伟达", "us"),
        ("MU", "us", "美光", "us"),
    ],
    "slow": [
        ("159509", "cn", "纳指科技ETF", "cn_etf"),
        ("159941", "cn", "纳指ETF广发", "cn_etf"),
        ("518880", "cn", "黄金ETF华安", "cn_etf"),
        ("510300", "cn", "沪深300ETF", "cn_etf"),
        ("QQQM", "us", "纳指100ETF", "us"),
    ],
}


@dataclass
class TParams:
    profile: str
    lookback_high: int
    buy_dd_pct: float
    sell_bounce_pct: float
    max_hold_days: int
    cooldown_days: int
    stop_loss_pct: float | None = None
    sell_near_high_pct: float | None = None
    ma30_filter: bool = True
    min_year_pos: float = 0.0


@dataclass
class TradeRound:
    entry_date: str
    exit_date: str
    entry: float
    exit: float
    gross_pct: float
    net_pct: float
    hold_days: int
    exit_reason: str


@dataclass
class SimResult:
    params: TParams
    trades: list[TradeRound] = field(default_factory=list)
    bars: int = 0
    years: float = 0.0
    buy_hold_ann_pct: float = 0.0

    @property
    def count(self) -> int:
        return len(self.trades)

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        return sum(1 for t in self.trades if t.net_pct > 0) / len(self.trades)

    @property
    def avg_net(self) -> float:
        if not self.trades:
            return 0.0
        return sum(t.net_pct for t in self.trades) / len(self.trades)

    @property
    def worst_net(self) -> float:
        if not self.trades:
            return 0.0
        return min(t.net_pct for t in self.trades)

    @property
    def total_net(self) -> float:
        return sum(t.net_pct for t in self.trades)

    @property
    def trades_per_year(self) -> float:
        if self.years <= 0:
            return 0.0
        return self.count / self.years

    @property
    def ann_net_pct(self) -> float:
        if self.years <= 0:
            return 0.0
        return self.total_net / self.years

    @property
    def avg_hold(self) -> float:
        if not self.trades:
            return 0.0
        return sum(t.hold_days for t in self.trades) / len(self.trades)

    @property
    def exposure(self) -> float:
        """Share of calendar bars where the T sleeve held a position."""
        if self.bars <= 0:
            return 0.0
        return sum(t.hold_days for t in self.trades) / self.bars

    @property
    def max_drawdown(self) -> float:
        """Worst peak-to-trough of the cumulative net P&L curve, in points."""
        peak = 0.0
        equity = 0.0
        worst = 0.0
        for t in self.trades:
            equity += t.net_pct
            peak = max(peak, equity)
            worst = min(worst, equity - peak)
        return worst

    @property
    def vs_buy_hold_pct(self) -> float:
        return self.ann_net_pct - self.buy_hold_ann_pct

    @property
    def ann_net_per_exposure_pct(self) -> float:
        """Return per unit of time actually invested, so idle cash isn't penalised."""
        if self.exposure <= 0:
            return 0.0
        return self.ann_net_pct / self.exposure

    @property
    def combo_ann_pct(self) -> float:
        """Half core-hold + half T sleeve, which is how the sleeve is really used."""
        return 0.5 * self.buy_hold_ann_pct + 0.5 * self.ann_net_pct

    @property
    def combo_edge_pct(self) -> float:
        return self.combo_ann_pct - self.buy_hold_ann_pct

    def exit_reasons(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for t in self.trades:
            out[t.exit_reason] = out.get(t.exit_reason, 0) + 1
        return out


def _ensure_hist(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["date"] = pd.to_datetime(out["date"])
    out["close"] = pd.to_numeric(out["close"], errors="coerce")
    out = out.dropna(subset=["close"]).sort_values("date")
    return out.reset_index(drop=True)


def rolling_high_drawdown(closes: pd.Series, window: int) -> pd.Series:
    high = closes.rolling(window, min_periods=max(5, window // 4)).max()
    return (closes / high - 1.0) * 100.0


def year_position(closes: pd.Series, window: int = 252) -> pd.Series:
    low = closes.rolling(window, min_periods=40).min()
    high = closes.rolling(window, min_periods=40).max()
    span = high - low
    pos = pd.Series(50.0, index=closes.index, dtype=float)
    mask = span > 0
    pos[mask] = (closes[mask] - low[mask]) / span[mask] * 100.0
    return pos


@dataclass
class Indicators:
    """Precomputed arrays so a parameter grid can reuse one pass over history.

    Plain numpy arrays keep the grid search fast; scalar `.iloc` access on a
    Series dominates runtime once the grid has a few hundred combinations.
    """

    closes: np.ndarray
    dates: list[str]
    dd: np.ndarray
    ma30: np.ndarray
    ypos: np.ndarray
    high252: np.ndarray

    def __len__(self) -> int:
        return len(self.closes)


def build_indicators(hist: pd.DataFrame, lookback_high: int) -> Indicators:
    hist = _ensure_hist(hist)
    closes = hist["close"]
    return Indicators(
        closes=closes.to_numpy(dtype=float),
        dates=[str(d.date()) for d in hist["date"]],
        dd=rolling_high_drawdown(closes, lookback_high).to_numpy(dtype=float),
        ma30=closes.rolling(30, min_periods=20).mean().to_numpy(dtype=float),
        ypos=year_position(closes).to_numpy(dtype=float),
        high252=closes.rolling(252, min_periods=40).max().to_numpy(dtype=float),
    )


def bounce_after_dip(
    closes: pd.Series,
    dd: pd.Series,
    dip_pct: float,
    horizon: int,
) -> list[float]:
    """Max bounce within `horizon` bars after drawdown reaches dip_pct."""
    aligned = pd.concat([closes.rename("close"), dd.rename("dd")], axis=1).dropna()
    if len(aligned) <= horizon + 1:
        return []
    out: list[float] = []
    for i in range(len(aligned) - horizon - 1):
        if not bool(aligned["dd"].iloc[i] <= dip_pct):
            continue
        entry = float(aligned["close"].iloc[i])
        if entry <= 0:
            continue
        future = aligned["close"].iloc[i + 1 : i + 1 + horizon]
        if future.empty:
            continue
        out.append((float(future.max()) / entry - 1.0) * 100.0)
    return out


def simulate_t(
    hist: pd.DataFrame,
    params: TParams,
    *,
    cost_pct: float = 0.0,
    indicators: Indicators | None = None,
    start: int = 0,
    end: int | None = None,
) -> SimResult:
    ind = indicators or build_indicators(hist, params.lookback_high)
    closes = ind.closes
    n = len(closes)
    end = n if end is None else min(end, n)
    start = max(start, params.lookback_high)
    if end - start < 20:
        return SimResult(params=params)

    dd = ind.dd
    ma30 = ind.ma30
    ypos = ind.ypos
    high252 = ind.high252
    buy_level = -params.buy_dd_pct
    stop = params.stop_loss_pct
    near_high = params.sell_near_high_pct

    holding = False
    entry_px = 0.0
    entry_idx = 0
    cooldown_until = -1
    trades: list[TradeRound] = []

    def _close(i: int, reason: str) -> None:
        nonlocal holding, cooldown_until
        px = closes[i]
        gross = (px / entry_px - 1.0) * 100.0
        trades.append(
            TradeRound(
                entry_date=ind.dates[entry_idx],
                exit_date=ind.dates[i],
                entry=entry_px,
                exit=px,
                gross_pct=gross,
                net_pct=gross - cost_pct,
                hold_days=i - entry_idx,
                exit_reason=reason,
            )
        )
        holding = False
        cooldown_until = i + params.cooldown_days

    for i in range(start, end):
        px = closes[i]
        if px <= 0:
            continue

        if not holding:
            if i <= cooldown_until:
                continue
            dip = dd[i]
            if dip != dip or dip > buy_level:
                continue
            if params.ma30_filter:
                ma = ma30[i]
                if ma == ma and px < ma * 0.97:
                    continue
            if params.min_year_pos > 0:
                yp = ypos[i]
                if yp == yp and yp < params.min_year_pos:
                    continue
            holding = True
            entry_px = px
            entry_idx = i
            continue

        gain = (px / entry_px - 1.0) * 100.0
        hold_days = i - entry_idx

        if stop is not None and gain <= -stop:
            _close(i, "stop_loss")
            continue
        if gain >= params.sell_bounce_pct:
            _close(i, "target")
            continue
        if near_high is not None and hold_days >= 3:
            h = high252[i]
            if h == h and h > 0 and (px / h - 1.0) * 100.0 >= -near_high:
                _close(i, "near_high")
                continue
        if hold_days >= params.max_hold_days:
            _close(i, "max_hold")
            continue

    if holding:
        _close(end - 1, "open_at_end")

    first = closes[start]
    last = closes[end - 1]
    days = (pd.Timestamp(ind.dates[end - 1]) - pd.Timestamp(ind.dates[start])).days
    years = max(days / 365.25, 0.1)
    bh_total = (last / first - 1.0) * 100.0 if first > 0 else 0.0

    return SimResult(
        params=params,
        trades=trades,
        bars=end - start,
        years=years,
        buy_hold_ann_pct=bh_total / years,
    )


def _param_grid(profile: str) -> list[dict[str, Any]]:
    spec = PROFILE_SPECS[profile]
    if profile == "short":
        buys = [1.5, 2.0, 2.5, 3.0, 3.5, 4.0]
        sells = [1.0, 1.5, 2.0, 2.5, 3.0]
        cooldowns = [3, 5]
        stops = [None, 4.0, 6.0]
        near_highs = [None]
        ma_filter = [True]
        min_ypos = [30.0]
    elif profile == "mid":
        buys = [4.0, 5.0, 6.0, 8.0, 10.0, 12.0]
        sells = [4.0, 5.0, 6.0, 8.0, 10.0]
        cooldowns = [5, 7, 10]
        stops = [None, 10.0, 15.0]
        near_highs = [None]
        ma_filter = [True]
        min_ypos = [20.0]
    else:
        buys = [8.0, 10.0, 12.0, 15.0, 18.0]
        sells = [5.0, 6.0, 8.0, 10.0]
        cooldowns = [15, 21, 30]
        stops = [None, 20.0]
        near_highs = [None, 3.0]
        ma_filter = [False]
        min_ypos = [0.0]

    out: list[dict[str, Any]] = []
    for buy, sell, cd, stop, nh, maf, myp in product(
        buys, sells, cooldowns, stops, near_highs, ma_filter, min_ypos
    ):
        out.append(
            {
                "profile": profile,
                "lookback_high": spec["lookback_high"],
                "max_hold_days": spec["max_hold_days"],
                "buy_dd_pct": buy,
                "sell_bounce_pct": sell,
                "cooldown_days": cd,
                "stop_loss_pct": stop,
                "sell_near_high_pct": nh,
                "ma30_filter": maf,
                "min_year_pos": myp,
            }
        )
    return out


def score_result(res: SimResult, profile: str) -> float | None:
    """Annualised net edge over buy-and-hold, penalised for drawdown."""
    spec = PROFILE_SPECS[profile]
    tpy = res.trades_per_year
    if res.count < 4:
        return None
    if tpy < spec["min_trades_per_year"] or tpy > spec["max_trades_per_year"]:
        return None
    return res.ann_net_pct - 0.5 * abs(res.max_drawdown)


def search_params(
    hist: pd.DataFrame,
    profile: str,
    *,
    cost_pct: float,
    train_end: int,
) -> tuple[TParams, SimResult] | None:
    """Grid-search parameters using only bars before `train_end`."""
    spec = PROFILE_SPECS[profile]
    ind = build_indicators(hist, spec["lookback_high"])
    best: tuple[float, TParams, SimResult] | None = None
    for cfg in _param_grid(profile):
        params = TParams(**cfg)
        res = simulate_t(
            hist,
            params,
            cost_pct=cost_pct,
            indicators=ind,
            start=0,
            end=train_end,
        )
        score = score_result(res, profile)
        if score is None:
            continue
        if best is None or score > best[0]:
            best = (score, params, res)
    if best is None:
        return None
    return best[1], best[2]


@dataclass
class WalkForwardResult:
    name: str
    profile: str
    folds: list[tuple[TParams, SimResult]] = field(default_factory=list)
    note: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.folds)

    def oos_trades(self) -> list[TradeRound]:
        out: list[TradeRound] = []
        for _, res in self.folds:
            out.extend(res.trades)
        return out

    def summary(self) -> dict[str, Any]:
        trades = self.oos_trades()
        years = sum(res.years for _, res in self.folds)
        bars = sum(res.bars for _, res in self.folds)
        merged = SimResult(
            params=self.folds[0][0] if self.folds else None,  # type: ignore[arg-type]
            trades=trades,
            bars=bars,
            years=max(years, 0.1),
            buy_hold_ann_pct=(
                sum(res.buy_hold_ann_pct * res.years for _, res in self.folds) / years
                if years > 0
                else 0.0
            ),
        )
        return {
            "name": self.name,
            "profile": self.profile,
            "trades": merged.count,
            "trades_per_year": round(merged.trades_per_year, 1),
            "win_rate": round(merged.win_rate * 100, 1),
            "avg_net_pct": round(merged.avg_net, 2),
            "worst_net_pct": round(merged.worst_net, 2),
            "ann_net_pct": round(merged.ann_net_pct, 2),
            "ann_net_per_exposure_pct": round(merged.ann_net_per_exposure_pct, 2),
            "buy_hold_ann_pct": round(merged.buy_hold_ann_pct, 2),
            "edge_vs_hold_pct": round(merged.vs_buy_hold_pct, 2),
            "combo_ann_pct": round(merged.combo_ann_pct, 2),
            "combo_edge_pct": round(merged.combo_edge_pct, 2),
            "max_drawdown_pct": round(merged.max_drawdown, 2),
            "avg_hold_days": round(merged.avg_hold, 1),
            "exposure": round(merged.exposure, 2),
            "exit_reasons": merged.exit_reasons(),
            "fold_params": [
                {
                    "buy_dd_pct": p.buy_dd_pct,
                    "sell_bounce_pct": p.sell_bounce_pct,
                    "stop_loss_pct": p.stop_loss_pct,
                    "cooldown_days": p.cooldown_days,
                    "sell_near_high_pct": p.sell_near_high_pct,
                }
                for p, _ in self.folds
            ],
        }


def walk_forward(
    hist: pd.DataFrame,
    profile: str,
    name: str,
    *,
    cost_pct: float,
    folds: int = 3,
) -> WalkForwardResult:
    """Expanding-window walk-forward: fit on the past, measure on the next slice."""
    hist = _ensure_hist(hist)
    spec = PROFILE_SPECS[profile]
    warmup = spec["lookback_high"] + 40
    n = len(hist)
    out = WalkForwardResult(name=name, profile=profile)

    min_fold = 120 if profile != "slow" else 180
    if n < warmup + min_fold * 2:
        out.note = f"历史仅 {n} 根，样本外验证需要至少 {warmup + min_fold * 2} 根"
        return out

    usable = n - warmup
    fold_size = usable // (folds + 1)
    if fold_size < min_fold:
        folds = max(1, usable // min_fold - 1)
        fold_size = usable // (folds + 1)
    if fold_size < min_fold:
        out.note = f"历史仅 {n} 根，不足以切分样本外区间"
        return out

    ind = build_indicators(hist, spec["lookback_high"])
    for k in range(folds):
        train_end = warmup + fold_size * (k + 1)
        test_end = min(train_end + fold_size, n)
        if test_end - train_end < min_fold // 2:
            break
        found = search_params(hist, profile, cost_pct=cost_pct, train_end=train_end)
        if found is None:
            continue
        params, _ = found
        oos = simulate_t(
            hist,
            params,
            cost_pct=cost_pct,
            indicators=ind,
            start=train_end,
            end=test_end,
        )
        out.folds.append((params, oos))

    if not out.folds:
        out.note = "训练区间内没有满足频次约束的参数组合"
    return out


def dip_statistics(hist: pd.DataFrame, profile: str) -> dict[str, Any]:
    """Descriptive drawdown / bounce stats, independent of any parameter search."""
    spec = PROFILE_SPECS[profile]
    hist = _ensure_hist(hist)
    dd = rolling_high_drawdown(hist["close"], spec["lookback_high"]).dropna()
    neg = dd[dd < 0]
    if neg.empty:
        return {}
    probe = float(neg.quantile(0.25))
    bounces = bounce_after_dip(hist["close"], dd, probe, spec["bounce_horizon"])
    bs = pd.Series(bounces)
    return {
        "dd_p25": round(float(neg.quantile(0.25)), 2),
        "dd_p10": round(float(neg.quantile(0.10)), 2),
        "dd_median": round(float(neg.median()), 2),
        "bounce_p50": round(float(bs.median()), 2) if not bs.empty else None,
        "bounce_p25": round(float(bs.quantile(0.25)), 2) if not bs.empty else None,
        "samples": int(len(bs)),
    }


def recommend_profile_params(
    results: list[tuple[WalkForwardResult, dict[str, Any]]],
    profile: str,
) -> dict[str, Any]:
    """Aggregate only the symbols whose out-of-sample edge is positive."""
    spec = PROFILE_SPECS[profile]
    usable = [(w, s) for w, s in results if w.ok]
    positive = [
        (w, s)
        for w, s in usable
        if w.summary()["combo_edge_pct"] > 0 and w.summary()["ann_net_pct"] > 0
    ]

    def _median(xs: list[float]) -> float | None:
        clean = sorted(x for x in xs if x is not None)
        if not clean:
            return None
        mid = len(clean) // 2
        if len(clean) % 2:
            return round(clean[mid], 2)
        return round((clean[mid - 1] + clean[mid]) / 2, 2)

    source = positive or usable
    buys: list[float] = []
    sells: list[float] = []
    stops: list[float] = []
    for w, _ in source:
        for p, _res in w.folds:
            buys.append(p.buy_dd_pct)
            sells.append(p.sell_bounce_pct)
            if p.stop_loss_pct is not None:
                stops.append(p.stop_loss_pct)

    dd_ref = _median([s.get("dd_p25") for _, s in results if s.get("dd_p25") is not None])
    bounce_ref = _median(
        [s.get("bounce_p50") for _, s in results if s.get("bounce_p50") is not None]
    )

    return {
        "profile": profile,
        "lookback_high": spec["lookback_high"],
        "max_hold_days": spec["max_hold_days"],
        "buy_dd_pct": _median(buys),
        "sell_bounce_pct": _median(sells),
        "stop_loss_pct": _median(stops),
        "empirical_dd_p25": dd_ref,
        "empirical_bounce_p50": bounce_ref,
        "symbols_tested": len(usable),
        "symbols_with_edge": len(positive),
        "symbols_with_edge_names": [w.name for w, _ in positive],
    }
