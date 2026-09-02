"""Swing-T (低吸/高抛) signals for the few symbols where T actually paid.

The 12-year cash-flow backtest in `docs/定投与做T回测.md` compared every rule
against simply buying on payday. Only 招商银行 and 中国移动 came out ahead under
two independent parameter sets; every growth name and every ETF lost 3–9% a year
to plain payday buying. So this module deliberately implements one rule — variant
G from that run — and it is opt-in per symbol rather than a global feature:

    add   when price sits `buy_drawdown_pct` under the 252-day closing high
    sell  once the sleeve is `sell_bounce_pct` up on its average cost

After an add the trigger disarms, and only re-arms once price recovers to within
`rearm_pct` of the high. That is what keeps one long slide from firing daily.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Union

from src.signals import QuoteSnapshot, drawdown_pct_from_high

DEFAULT_BUY_DRAWDOWN_PCT = 5.0
DEFAULT_SELL_BOUNCE_PCT = 5.0
DEFAULT_REARM_PCT = 4.0
DEFAULT_HIGH_WINDOW = 252
# The backtest averaged 1.7 adds per round trip; this only guards runaway slides.
DEFAULT_MAX_ADDS = 4
MIN_HISTORY_FOR_T = 120


@dataclass(frozen=True)
class TConfig:
    buy_drawdown_pct: float = DEFAULT_BUY_DRAWDOWN_PCT
    sell_bounce_pct: float = DEFAULT_SELL_BOUNCE_PCT
    rearm_pct: float = DEFAULT_REARM_PCT
    high_window: int = DEFAULT_HIGH_WINDOW
    max_adds: int = DEFAULT_MAX_ADDS

    @classmethod
    def from_dict(cls, raw: dict | None) -> "TConfig":
        raw = raw or {}
        return cls(
            buy_drawdown_pct=abs(float(raw.get("buy_drawdown_pct", DEFAULT_BUY_DRAWDOWN_PCT))),
            sell_bounce_pct=abs(float(raw.get("sell_bounce_pct", DEFAULT_SELL_BOUNCE_PCT))),
            rearm_pct=abs(float(raw.get("rearm_pct", DEFAULT_REARM_PCT))),
            high_window=int(raw.get("high_window", DEFAULT_HIGH_WINDOW)),
            max_adds=int(raw.get("max_adds", DEFAULT_MAX_ADDS)),
        )


@dataclass
class TSleeve:
    """The rolling half of a position, tracked in equal money units per add."""

    armed: bool = True
    shares: float = 0.0
    cost: float = 0.0
    adds: int = 0
    opened_on: str = ""
    last_action: str = ""
    history: list[str] = field(default_factory=list)

    @property
    def holding(self) -> bool:
        return self.shares > 1e-12 and self.cost > 1e-12

    @property
    def avg_price(self) -> float:
        if not self.holding:
            return 0.0
        return self.cost / self.shares

    def gain_pct(self, price: float) -> float:
        if not self.holding:
            return 0.0
        return (price / self.avg_price - 1.0) * 100.0

    def add(self, price: float, as_of: str = "", money: float = 1.0) -> None:
        if price <= 0:
            raise ValueError("价格无效，无法记录低吸")
        self.shares += money / price
        self.cost += money
        self.adds += 1
        if not self.opened_on:
            self.opened_on = as_of
        self.last_action = as_of
        self.armed = False

    def close(self, as_of: str = "") -> None:
        self.shares = 0.0
        self.cost = 0.0
        self.adds = 0
        self.opened_on = ""
        self.last_action = as_of

    def to_dict(self) -> dict[str, Any]:
        return {
            "armed": bool(self.armed),
            "shares": float(self.shares),
            "cost": float(self.cost),
            "adds": int(self.adds),
            "opened_on": self.opened_on,
            "last_action": self.last_action,
        }

    @classmethod
    def from_dict(cls, raw: dict | None) -> "TSleeve":
        raw = raw or {}
        try:
            return cls(
                armed=bool(raw.get("armed", True)),
                shares=max(0.0, float(raw.get("shares", 0.0) or 0.0)),
                cost=max(0.0, float(raw.get("cost", 0.0) or 0.0)),
                adds=max(0, int(raw.get("adds", 0) or 0)),
                opened_on=str(raw.get("opened_on") or ""),
                last_action=str(raw.get("last_action") or ""),
            )
        except (TypeError, ValueError):
            return cls()


@dataclass
class TBuySignal:
    code: str
    name: str
    price: float
    high_252: float
    drawdown_pct: float
    add_index: int
    max_adds: int
    prev_avg_price: float
    config: TConfig
    as_of: str

    @property
    def title(self) -> str:
        return "做T · 低吸"

    @property
    def message(self) -> str:
        held = (
            f"机动仓已有 {self.add_index - 1} 笔，均价 **{self.prev_avg_price:.2f}**\n"
            if self.add_index > 1
            else "机动仓当前空仓\n"
        )
        return (
            f"**{self.name}({self.code})** 触发做T低吸（第 {self.add_index}/{self.max_adds} 笔）\n"
            f"现价 **{self.price:.2f}**　一年高点 **{self.high_252:.2f}**\n"
            f"距高点 **{self.drawdown_pct:+.2f}%**（阈值 -{self.config.buy_drawdown_pct:g}%）\n"
            f"{held}"
            f"目标：均价上涨 {self.config.sell_bounce_pct:g}% 后卖出这部分\n"
            f"仅动机动仓，底仓不动；日线截至 {self.as_of}"
        ).strip()


@dataclass
class TSellSignal:
    code: str
    name: str
    price: float
    avg_price: float
    gain_pct: float
    adds: int
    opened_on: str
    config: TConfig
    as_of: str

    @property
    def title(self) -> str:
        return "做T · 高抛"

    @property
    def message(self) -> str:
        since = f"（{self.opened_on} 起）" if self.opened_on else ""
        return (
            f"**{self.name}({self.code})** 触发做T高抛\n"
            f"现价 **{self.price:.2f}**　机动仓均价 **{self.avg_price:.2f}**\n"
            f"浮盈 **{self.gain_pct:+.2f}%**（阈值 +{self.config.sell_bounce_pct:g}%）\n"
            f"共 {self.adds} 笔低吸{since}\n"
            f"卖出机动仓，底仓不动；日线截至 {self.as_of}"
        ).strip()


TSignal = Union[TBuySignal, TSellSignal]


def evaluate_t(
    snapshot: QuoteSnapshot,
    sleeve: TSleeve,
    config: TConfig,
    *,
    min_history: int = MIN_HISTORY_FOR_T,
    commit: bool = True,
) -> TSignal | None:
    """Advance the sleeve one bar; returns a signal when the state changes.

    By default mutates `sleeve`, mirroring the backtest loop order: re-arm, then
    add, then take profit.  Live notifications use ``commit=False``: a signal is
    only committed when its real trade is recorded in the local journal.
    """
    if snapshot.high_252 <= 0 or snapshot.history_rows < min_history:
        return None
    if snapshot.price <= 0:
        return None

    dd = drawdown_pct_from_high(snapshot.price, snapshot.high_252)

    if not sleeve.armed and dd >= -config.rearm_pct:
        sleeve.armed = True

    if sleeve.armed and dd <= -config.buy_drawdown_pct and sleeve.adds < config.max_adds:
        prev_avg = sleeve.avg_price
        signal = TBuySignal(
            code=snapshot.code,
            name=snapshot.name,
            price=snapshot.price,
            high_252=snapshot.high_252,
            drawdown_pct=dd,
            add_index=sleeve.adds + 1,
            max_adds=config.max_adds,
            prev_avg_price=prev_avg,
            config=config,
            as_of=snapshot.as_of,
        )
        if commit:
            sleeve.add(snapshot.price, as_of=snapshot.as_of)
        return signal

    if sleeve.holding and sleeve.gain_pct(snapshot.price) >= config.sell_bounce_pct:
        signal = TSellSignal(
            code=snapshot.code,
            name=snapshot.name,
            price=snapshot.price,
            avg_price=sleeve.avg_price,
            gain_pct=sleeve.gain_pct(snapshot.price),
            adds=sleeve.adds,
            opened_on=sleeve.opened_on,
            config=config,
            as_of=snapshot.as_of,
        )
        if commit:
            sleeve.close(as_of=snapshot.as_of)
        return signal

    return None


def sleeve_status_line(name: str, code: str, sleeve: TSleeve, price: float) -> str:
    """One-line sleeve state for the daily digest."""
    if not sleeve.holding:
        armed = "已武装，等回撤" if sleeve.armed else "等回升后重新武装"
        return f"{name}({code}) 机动仓空仓，{armed}"
    return (
        f"{name}({code}) 机动仓 {sleeve.adds} 笔，均价 {sleeve.avg_price:.2f}，"
        f"浮盈 {sleeve.gain_pct(price):+.2f}%"
    )
