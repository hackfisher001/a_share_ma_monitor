"""Buy / observe signals: MA30 touch, and multi-level drawdown from the 1-year high."""

from __future__ import annotations

from dataclasses import dataclass

# A 252-trading-day window matches the rolling high used in the backtest.
HIGH_WINDOW = 252
# Below this many daily bars the 1-year high is not yet meaningful.
MIN_HISTORY_FOR_DRAWDOWN = 120
DEFAULT_DRAWDOWN_LEVELS = (5, 10, 15, 20, 30, 40, 50)


@dataclass
class QuoteSnapshot:
    """Price snapshot used by signal logic (kept free of network deps)."""

    code: str
    name: str
    price: float
    ma30: float
    as_of: str
    history_rows: int
    high_252: float = 0.0


@dataclass
class TouchSignal:
    code: str
    name: str
    price: float
    ma30: float
    deviation_pct: float
    touch_pct: float
    as_of: str

    @property
    def message(self) -> str:
        """Plain / Feishu markdown body."""
        return (
            f"**{self.name}({self.code})** 现价贴近 30 日均线\n"
            f"现价 **{self.price:.2f}**　MA30 **{self.ma30:.2f}**\n"
            f"偏离 **{self.deviation_pct:+.2f}%**（阈值 ±{self.touch_pct}%）\n"
            f"日线截至 {self.as_of}"
        ).strip()


@dataclass
class DrawdownSignal:
    code: str
    name: str
    price: float
    high_252: float
    drawdown_pct: float
    threshold_pct: float
    as_of: str

    @property
    def band_label(self) -> str:
        level = abs(self.threshold_pct)
        if level >= 30:
            return f"回撤超过 {level:g}%"
        return f"回撤达到 {level:g}%"

    @property
    def message(self) -> str:
        return (
            f"**{self.name}({self.code})** 较一年高点{self.band_label}\n"
            f"现价 **{self.price:.2f}**　一年高点 **{self.high_252:.2f}**\n"
            f"当前回撤 **{self.drawdown_pct:+.2f}%**（本档阈值 -{self.threshold_pct:g}%）\n"
            f"日线截至 {self.as_of}\n"
            f"仅供观察；主仓定投请按计划继续，勿因单档回撤空仓等待。"
        ).strip()


def deviation_pct(price: float, ma30: float) -> float:
    if ma30 <= 0:
        raise ValueError("MA30 无效")
    return (price - ma30) / ma30 * 100.0


def is_touching_ma30(snapshot: QuoteSnapshot, touch_pct: float) -> TouchSignal | None:
    """Trigger when |price - ma30| / ma30 * 100 <= touch_pct."""
    dev = deviation_pct(snapshot.price, snapshot.ma30)
    if abs(dev) > touch_pct:
        return None
    return TouchSignal(
        code=snapshot.code,
        name=snapshot.name,
        price=snapshot.price,
        ma30=snapshot.ma30,
        deviation_pct=dev,
        touch_pct=touch_pct,
        as_of=snapshot.as_of,
    )


def drawdown_pct_from_high(price: float, high: float) -> float:
    if high <= 0:
        raise ValueError("一年高点无效")
    return (price / high - 1.0) * 100.0


def is_deep_drawdown(
    snapshot: QuoteSnapshot,
    threshold_pct: float,
    *,
    min_history: int = MIN_HISTORY_FOR_DRAWDOWN,
) -> DrawdownSignal | None:
    """Trigger when price sits at or below -threshold_pct off the 1-year high."""
    signals = crossed_drawdown_levels(
        snapshot,
        [threshold_pct],
        already_fired=(),
        min_history=min_history,
    )
    return signals[0] if signals else None


def crossed_drawdown_levels(
    snapshot: QuoteSnapshot,
    levels: list[float] | tuple[float, ...],
    already_fired: list[float] | tuple[float, ...] | set[float] = (),
    *,
    min_history: int = MIN_HISTORY_FOR_DRAWDOWN,
) -> list[DrawdownSignal]:
    """Return newly crossed drawdown bands (each band fires once per episode)."""
    if snapshot.high_252 <= 0 or snapshot.history_rows < min_history:
        return []
    dd = drawdown_pct_from_high(snapshot.price, snapshot.high_252)
    fired = {abs(float(x)) for x in already_fired}
    out: list[DrawdownSignal] = []
    for raw in sorted({abs(float(x)) for x in levels}):
        if dd <= -raw and raw not in fired:
            out.append(
                DrawdownSignal(
                    code=snapshot.code,
                    name=snapshot.name,
                    price=snapshot.price,
                    high_252=snapshot.high_252,
                    drawdown_pct=dd,
                    threshold_pct=raw,
                    as_of=snapshot.as_of,
                )
            )
    return out
