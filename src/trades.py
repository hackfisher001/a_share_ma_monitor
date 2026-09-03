"""Small, local trade journal used to close the loop on action reminders.

The monitor must not assume a notification was executed.  A trade is only
considered done after the owner records it here; this deliberately avoids
turning a signal into a fictional position.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from pathlib import Path


CSV_COLUMNS = ("date", "market", "code", "side", "quantity", "price", "note")


@dataclass(frozen=True)
class Trade:
    date: str
    market: str
    code: str
    side: str
    quantity: float
    price: float
    note: str = ""


class TradeLedger:
    """Append-only CSV journal.  It is intentionally easy to inspect/edit."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def record(
        self,
        *,
        market: str,
        code: str,
        side: str,
        quantity: float,
        price: float,
        note: str = "",
        traded_on: str | None = None,
    ) -> Trade:
        side = side.strip().upper()
        if side not in {"BUY", "SELL"}:
            raise ValueError("side 必须为 BUY 或 SELL")
        if quantity <= 0 or price <= 0:
            raise ValueError("数量和价格必须大于 0")
        trade = Trade(
            date=traded_on or date.today().isoformat(),
            market=market.strip().lower(),
            code=code.strip().upper(),
            side=side,
            quantity=float(quantity),
            price=float(price),
            note=note.strip(),
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        write_header = not self.path.exists() or self.path.stat().st_size == 0
        with self.path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
            if write_header:
                writer.writeheader()
            writer.writerow(trade.__dict__)
        return trade

    def all(self) -> list[Trade]:
        if not self.path.exists():
            return []
        rows: list[Trade] = []
        with self.path.open(newline="", encoding="utf-8") as handle:
            for raw in csv.DictReader(handle):
                try:
                    rows.append(
                        Trade(
                            date=str(raw["date"]),
                            market=str(raw["market"]).lower(),
                            code=str(raw["code"]).upper(),
                            side=str(raw["side"]).upper(),
                            quantity=float(raw["quantity"]),
                            price=float(raw["price"]),
                            note=str(raw.get("note") or ""),
                        )
                    )
                except (KeyError, TypeError, ValueError):
                    continue
        return rows

    def recent(self, limit: int = 5) -> list[Trade]:
        return list(reversed(self.all()[-max(0, limit) :]))

    def positions(self) -> dict[str, "Position"]:
        """Replay the journal into holdings keyed by `market:code`.

        Sells reduce quantity at average cost, so realised profit is separated
        from the cost basis of what is still held. A sell larger than the
        recorded holding is clamped rather than allowed to go negative — the
        journal may simply have started mid-position.
        """
        book: dict[str, Position] = {}
        for trade in self.all():
            key = f"{trade.market}:{trade.code}"
            pos = book.get(key) or Position(market=trade.market, code=trade.code)
            if trade.side == "BUY":
                pos.quantity += trade.quantity
                pos.cost += trade.quantity * trade.price
            else:
                sold = min(trade.quantity, pos.quantity)
                if sold > 0:
                    unit_cost = pos.cost / pos.quantity
                    pos.realised += sold * (trade.price - unit_cost)
                    pos.quantity -= sold
                    pos.cost -= sold * unit_cost
            pos.last_date = trade.date
            book[key] = pos
        return book


@dataclass
class Position:
    market: str
    code: str
    quantity: float = 0.0
    cost: float = 0.0
    realised: float = 0.0
    last_date: str = ""

    @property
    def holding(self) -> bool:
        return self.quantity > 1e-9 and self.cost > 1e-9

    @property
    def avg_price(self) -> float:
        return self.cost / self.quantity if self.holding else 0.0

    def unrealised_pct(self, price: float) -> float | None:
        if not self.holding or price <= 0:
            return None
        return (price / self.avg_price - 1.0) * 100.0
