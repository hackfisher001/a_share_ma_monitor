"""Cash-flow backtest: monthly salary + annual bonus, not a day-one lump sum.

The relevant question for a saver is not "T vs buy-and-hold" but "money just
landed — buy now, or hold it for a dip?". Every strategy here receives the same
contributions on the same dates, so final value is directly comparable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

TRADING_DAYS_PER_YEAR = 252


@dataclass
class DcaConfig:
    monthly: float = 1.0
    bonus: float = 3.0
    bonus_month: int = 2  # 年终奖通常春节前后到账
    cash_yield_pct: float = 1.5  # 闲置现金按货币基金计息，避免高估等待成本


@dataclass
class Strategy:
    """One deployment rule. `sell_gain_pct=None` means the sleeve never sells."""

    name: str
    core_frac: float  # 到账即买的比例，其余进储备现金
    dip_pct: float | None = None  # 距 dip_window 高点的回撤触发买入
    dip_window: int = 252
    rearm_pct: float = 4.0  # 回升到 -rearm_pct 以内后重新武装
    sell_gain_pct: float | None = None  # 机动仓浮盈达标即卖出
    tranche: float = 1.0  # 每次触发投入储备现金的比例
    cost_pct: float = 0.0  # 单边交易成本


@dataclass
class StrategyResult:
    name: str
    contributed: float
    final_value: float
    core_shares: float
    sleeve_shares: float
    end_cash: float
    buys: int
    sells: int
    mwr_pct: float
    max_drawdown_pct: float
    worst_unrealized_pct: float
    avg_cash_frac: float

    @property
    def multiple(self) -> float:
        if self.contributed <= 0:
            return 0.0
        return self.final_value / self.contributed


def _ensure(hist: pd.DataFrame) -> pd.DataFrame:
    out = hist.copy()
    out["date"] = pd.to_datetime(out["date"])
    out["close"] = pd.to_numeric(out["close"], errors="coerce")
    return out.dropna(subset=["close"]).sort_values("date").reset_index(drop=True)


def contribution_schedule(dates: pd.Series, cfg: DcaConfig) -> dict[int, float]:
    """First trading bar of each month gets salary; bonus month gets extra."""
    periods = dates.dt.to_period("M")
    first_idx = {}
    for i, p in enumerate(periods):
        if p not in first_idx:
            first_idx[p] = i
    out: dict[int, float] = {}
    for p, i in first_idx.items():
        amount = cfg.monthly
        if p.month == cfg.bonus_month:
            amount += cfg.bonus
        out[i] = out.get(i, 0.0) + amount
    return out


def money_weighted_return(
    flows: list[tuple[pd.Timestamp, float]],
    final_value: float,
    final_date: pd.Timestamp,
) -> float:
    """Annualised money-weighted return, solved by bisection on NPV."""
    if not flows or final_value <= 0:
        return 0.0

    def npv(rate: float) -> float:
        total = 0.0
        for d, amt in flows:
            years = (final_date - d).days / 365.25
            total += amt * (1.0 + rate) ** years
        return total - final_value

    lo, hi = -0.95, 3.0
    if npv(lo) * npv(hi) > 0:
        return 0.0
    for _ in range(200):
        mid = (lo + hi) / 2
        if npv(mid) > 0:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2 * 100.0


def simulate(hist: pd.DataFrame, cfg: DcaConfig, strat: Strategy) -> StrategyResult:
    h = _ensure(hist)
    closes = h["close"].to_numpy(dtype=float)
    dates = h["date"]
    n = len(closes)
    if n < 60:
        raise ValueError("历史不足")

    window = strat.dip_window
    roll_high = (
        h["close"].rolling(window, min_periods=max(20, window // 5)).max().to_numpy(dtype=float)
    )

    schedule = contribution_schedule(dates, cfg)
    daily_cash_rate = (1.0 + cfg.cash_yield_pct / 100.0) ** (1.0 / TRADING_DAYS_PER_YEAR) - 1.0

    core_shares = 0.0
    sleeve_shares = 0.0
    sleeve_cost = 0.0
    cash = 0.0
    contributed = 0.0
    buys = 0
    sells = 0
    armed = True
    flows: list[tuple[pd.Timestamp, float]] = []
    cash_fracs: list[float] = []
    peak = 0.0
    max_dd = 0.0
    worst_unrealized = 0.0

    for i in range(n):
        px = closes[i]
        cash *= 1.0 + daily_cash_rate

        if i in schedule:
            amount = schedule[i]
            contributed += amount
            flows.append((dates.iloc[i], amount))
            core_amount = amount * strat.core_frac
            if core_amount > 0 and px > 0:
                core_shares += core_amount * (1 - strat.cost_pct / 100.0) / px
                buys += 1
            cash += amount - core_amount

        high = roll_high[i]
        dd = (px / high - 1.0) * 100.0 if high == high and high > 0 else 0.0

        if strat.dip_pct is not None:
            if not armed and dd >= -strat.rearm_pct:
                armed = True
            if armed and cash > 1e-9 and dd <= -strat.dip_pct:
                deploy = cash * strat.tranche
                sleeve_shares += deploy * (1 - strat.cost_pct / 100.0) / px
                sleeve_cost += deploy
                cash -= deploy
                buys += 1
                armed = False

        if (
            strat.sell_gain_pct is not None
            and sleeve_shares > 0
            and sleeve_cost > 0
        ):
            value = sleeve_shares * px
            if (value / sleeve_cost - 1.0) * 100.0 >= strat.sell_gain_pct:
                cash += value * (1 - strat.cost_pct / 100.0)
                sleeve_shares = 0.0
                sleeve_cost = 0.0
                sells += 1

        total = (core_shares + sleeve_shares) * px + cash
        peak = max(peak, total)
        if peak > 0:
            max_dd = min(max_dd, (total / peak - 1.0) * 100.0)
        if contributed > 0:
            worst_unrealized = min(worst_unrealized, (total / contributed - 1.0) * 100.0)
            cash_fracs.append(cash / total if total > 0 else 0.0)

    final_px = closes[-1]
    final_value = (core_shares + sleeve_shares) * final_px + cash
    return StrategyResult(
        name=strat.name,
        contributed=contributed,
        final_value=final_value,
        core_shares=core_shares,
        sleeve_shares=sleeve_shares,
        end_cash=cash,
        buys=buys,
        sells=sells,
        mwr_pct=money_weighted_return(flows, final_value, dates.iloc[-1]),
        max_drawdown_pct=max_dd,
        worst_unrealized_pct=worst_unrealized,
        avg_cash_frac=float(np.mean(cash_fracs)) if cash_fracs else 0.0,
    )


def default_strategies(cost_pct: float) -> list[Strategy]:
    """Fixed, pre-declared variants so nothing is fitted in sample."""
    return [
        Strategy(name="A 无脑定投", core_frac=1.0, cost_pct=cost_pct),
        Strategy(
            name="B 定投+回撤加仓(-10%)",
            core_frac=0.6,
            dip_pct=10.0,
            cost_pct=cost_pct,
        ),
        Strategy(
            name="C 纯等回撤(-10%)",
            core_frac=0.0,
            dip_pct=10.0,
            cost_pct=cost_pct,
        ),
        Strategy(
            name="D 纯等回撤(-20%)",
            core_frac=0.0,
            dip_pct=20.0,
            cost_pct=cost_pct,
        ),
        Strategy(
            name="E 半底仓+机动仓做T(-8%/+8%)",
            core_frac=0.5,
            dip_pct=8.0,
            sell_gain_pct=8.0,
            cost_pct=cost_pct,
        ),
        Strategy(
            name="F 半底仓+机动仓只买不卖(-8%)",
            core_frac=0.5,
            dip_pct=8.0,
            cost_pct=cost_pct,
        ),
        Strategy(
            name="G 半底仓+机动仓做T(-5%/+5%)",
            core_frac=0.5,
            dip_pct=5.0,
            sell_gain_pct=5.0,
            cost_pct=cost_pct,
        ),
    ]


@dataclass
class SymbolReport:
    name: str
    code: str
    span_years: float
    results: list[StrategyResult] = field(default_factory=list)

    def baseline(self) -> StrategyResult | None:
        for r in self.results:
            if r.name.startswith("A "):
                return r
        return None

    def rows(self) -> list[dict[str, Any]]:
        base = self.baseline()
        out = []
        for r in self.results:
            edge = r.mwr_pct - base.mwr_pct if base else 0.0
            out.append(
                {
                    "strategy": r.name,
                    "multiple": round(r.multiple, 3),
                    "mwr_pct": round(r.mwr_pct, 2),
                    "edge_vs_dca_pct": round(edge, 2),
                    "max_drawdown_pct": round(r.max_drawdown_pct, 1),
                    "worst_unrealized_pct": round(r.worst_unrealized_pct, 1),
                    "avg_cash_frac": round(r.avg_cash_frac, 2),
                    "buys": r.buys,
                    "sells": r.sells,
                }
            )
        return out
