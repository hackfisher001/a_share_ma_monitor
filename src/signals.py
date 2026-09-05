"""Signals: MA30 touch, multi-level 1-year drawdown, and intraday slides.

The three differ in urgency. MA30 and the drawdown bands describe where a symbol
sits and can wait for the daily digest. An intraday slide cannot: it is only
actionable while the session is open, so it is evaluated against the live price
on every scan and is never folded into a report.
"""

from __future__ import annotations

from dataclasses import dataclass

# A 252-trading-day window matches the rolling high used in the backtest.
HIGH_WINDOW = 252
# Below this many daily bars the 1-year high is not yet meaningful.
MIN_HISTORY_FOR_DRAWDOWN = 120
DEFAULT_DRAWDOWN_LEVELS = (5, 10, 15, 20, 30, 40, 50)
# Bands for a same-session slide measured against the previous close.
DEFAULT_INTRADAY_DIP_LEVELS = (3.0, 5.0, 8.0)


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
    # Current change against the previous close; intraday when `live` is True.
    change_pct: float | None = None
    live: bool = False


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


@dataclass
class IntradayDipSignal:
    code: str
    name: str
    price: float
    change_pct: float
    threshold_pct: float
    year_drawdown_pct: float
    as_of: str
    live: bool = True

    @property
    def title(self) -> str:
        return f"急跌提醒 · -{self.threshold_pct:g}%"

    def compact_headline(self, sigma_multiple: float | None = None) -> str:
        """One-line lead: who, how much, how unusual, at what price.

        Threshold / year-drawdown / action boilerplate used to live here and
        were all repeated by the context block that follows, so they left.
        """
        basis = "盘中" if self.live else "最新收盘"
        sigma = (
            f"（{sigma_multiple:.1f}σ）"
            if sigma_multiple is not None and sigma_multiple > 0
            else ""
        )
        line = (
            f"**{self.name}({self.code})** {basis}急跌 "
            f"**{self.change_pct:+.2f}%**{sigma}　现价 **{self.price:.2f}**"
        )
        if not self.live:
            line += "\n（未取到实时价，以上为最近一个收盘价）"
        return line

    @property
    def message(self) -> str:
        return self.compact_headline()


def crossed_intraday_dip_levels(
    snapshot: QuoteSnapshot,
    levels: list[float] | tuple[float, ...],
    already_fired: list[float] | tuple[float, ...] | set[float] = (),
) -> list[IntradayDipSignal]:
    """Bands of a same-session slide, each firing once per day.

    Unlike the drawdown bands this is deliberately unfiltered by percentile: a
    gap-down is worth knowing about even when the symbol is usually calm, and
    the earlier 3-day/20-day percentile tests silently missed exactly that case.
    """
    if snapshot.change_pct is None or snapshot.price <= 0:
        return []
    change = float(snapshot.change_pct)
    year_dd = (
        (snapshot.price / snapshot.high_252 - 1.0) * 100.0
        if snapshot.high_252 > 0
        else 0.0
    )
    fired = {abs(float(x)) for x in already_fired}
    out: list[IntradayDipSignal] = []
    for raw in sorted({abs(float(x)) for x in levels}):
        if change <= -raw and raw not in fired:
            out.append(
                IntradayDipSignal(
                    code=snapshot.code,
                    name=snapshot.name,
                    price=snapshot.price,
                    change_pct=change,
                    threshold_pct=raw,
                    year_drawdown_pct=year_dd,
                    as_of=snapshot.as_of,
                    live=snapshot.live,
                )
            )
    return out


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
