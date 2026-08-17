"""Multi-horizon price change helpers."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


PERIODS: list[tuple[str, dict]] = [
    ("1日", {"trading_days": 1}),
    ("5日", {"trading_days": 5}),
    ("1周", {"calendar_days": 7}),
    ("1月", {"calendar_days": 30}),
    ("半年", {"calendar_days": 182}),
    ("1年", {"calendar_days": 365}),
]


@dataclass
class PeriodChange:
    label: str
    pct: float | None  # None if insufficient history

    def fmt(self) -> str:
        if self.pct is None:
            return f"{self.label}: —"
        sign = "+" if self.pct >= 0 else ""
        return f"{self.label}: {sign}{self.pct:.2f}%"


def _pct(now: float, then: float) -> float:
    if then <= 0:
        raise ValueError("基准价无效")
    return (now - then) / then * 100.0


def change_by_trading_days(hist: pd.DataFrame, price: float, n: int) -> float | None:
    """Return vs close from n trading sessions ago (n=1 → previous close)."""
    if hist is None or hist.empty or len(hist) <= n:
        return None
    then = float(hist.iloc[-(n + 1)]["close"])
    return _pct(price, then)


def change_by_calendar_days(hist: pd.DataFrame, price: float, days: int) -> float | None:
    if hist is None or hist.empty:
        return None
    last_date = pd.Timestamp(hist.iloc[-1]["date"])
    target = last_date - pd.Timedelta(days=days)
    past = hist[hist["date"] <= target]
    if past.empty:
        return None
    then = float(past.iloc[-1]["close"])
    return _pct(price, then)


def compute_period_changes(hist: pd.DataFrame, price: float) -> list[PeriodChange]:
    out: list[PeriodChange] = []
    for label, spec in PERIODS:
        if "trading_days" in spec:
            pct = change_by_trading_days(hist, price, int(spec["trading_days"]))
        else:
            pct = change_by_calendar_days(hist, price, int(spec["calendar_days"]))
        out.append(PeriodChange(label=label, pct=pct))
    return out
