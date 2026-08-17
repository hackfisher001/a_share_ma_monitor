"""Signal: price touches 30-day moving average within a percentage threshold."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class QuoteSnapshot:
    """Price snapshot used by signal logic (kept free of network deps)."""

    code: str
    name: str
    price: float
    ma30: float
    as_of: str
    history_rows: int


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
