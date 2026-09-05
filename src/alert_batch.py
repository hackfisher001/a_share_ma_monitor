"""Collect one scan's alerts and emit them as a single message when several fire.

Dispatching inline, one card per symbol per band, breaks down exactly when it
matters most. A systemic down day trips a dozen correlated names at once, and
the watchlist holds far fewer independent bets than it does symbols — measured
on 250 sessions, 纳指100ETF correlates 0.66-0.70 with AMD / 美光 / 英伟达 /
特斯拉. So a market selloff arrives as one event and should read as one message.

A lone alert still renders in full, sparkline included: that is the case where
the detail is affordable and most useful.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from src.price_context import PriceContext
from src.relative import RelativeMove


@dataclass
class ScanAlert:
    """One triggered alert, held until the scan finishes.

    `commit` persists the dedup state and is only invoked after the message is
    actually delivered, so a send failure replays rather than silently drops.
    """

    kind: str
    market: str
    code: str
    name: str
    title: str
    headline: str
    ctx: PriceContext
    summary_head: str
    extra: str = ""
    change_pct: float | None = None
    sigma: float | None = None
    band_pct: float | None = None
    severity: float = 0.0
    commit: Callable[[], None] | None = None
    relative: RelativeMove | None = None

    @property
    def key(self) -> str:
        return f"{self.market}:{self.code}"

    def summary_lines(self) -> list[str]:
        """Compact multi-line entry used inside the aggregate card."""
        out = [f"**{self.name}({self.code})**　{self.summary_head}"]
        facts: list[str] = []
        if self.relative is not None:
            beta = (
                f"β{self.relative.beta:.2f}" if self.relative.beta is not None else "β—"
            )
            facts.append(f"超额 {self.relative.excess_pct:+.2f}%（{beta}）")
        facts.append(f"阶段 {self.ctx.stage}")
        facts.append(f"MA30 {self.ctx.ma_dev:+.1f}%")
        if self.ctx.year_dd is not None:
            facts.append(f"距一年高 {self.ctx.year_dd:+.1f}%")
        out.append("　" + "｜".join(facts))
        if self.relative is not None and self.relative.idiosyncratic:
            out.append(f"　⚠️ {self.relative.verdict}")
        return out


@dataclass
class BatchContext:
    """Market-wide backdrop shown once at the top of an aggregate card."""

    benchmark_moves: list[tuple[str, float]] = field(default_factory=list)

    def header_suffix(self) -> str:
        if not self.benchmark_moves:
            return ""
        return "　" + "　".join(f"{n} {v:+.2f}%" for n, v in self.benchmark_moves)


def dedupe_for_display(alerts: list[ScanAlert]) -> list[ScanAlert]:
    """Collapse one symbol's several crossed bands into its deepest one.

    A -5σ slide trips the 2σ, 3σ and 4σ bands in the same pass. All three still
    need their dedup state recorded so they cannot re-fire later in the session,
    but repeating the symbol three times in the message says nothing new.
    """
    best: dict[tuple[str, str], ScanAlert] = {}
    for alert in alerts:
        slot = (alert.key, alert.kind)
        current = best.get(slot)
        if current is None:
            best[slot] = alert
            continue
        rank = (alert.severity, alert.band_pct or 0.0)
        if rank > (current.severity, current.band_pct or 0.0):
            best[slot] = alert
    return list(best.values())


def sort_alerts(alerts: list[ScanAlert]) -> list[ScanAlert]:
    """Most unusual first.

    Ranking on σ multiples rather than raw percent is the whole point: a -2.5%
    day on 长江电力 (σ 0.86%) is a rarer event than -6% on 美光 (σ 5.15%), and
    burying it under the bigger number would hide the more informative one.
    """
    return sorted(alerts, key=lambda a: (-a.severity, a.name))


KIND_LABELS = {
    "dip": "急跌",
    "pullback": "近期异常回撤",
    "ma30": "贴近 MA30",
    "drawdown": "回撤观察",
}


def batch_title(alerts: list[ScanAlert]) -> str:
    return f"盘中提醒 · {len(alerts)} 只触发"


def batch_markdown(alerts: list[ScanAlert], ctx: BatchContext) -> str:
    ordered = sort_alerts(alerts)
    counts: dict[str, int] = {}
    for alert in ordered:
        label = KIND_LABELS.get(alert.kind, alert.kind)
        counts[label] = counts.get(label, 0) + 1
    lines = [f"**{len(ordered)} 只标的同时触发**{ctx.header_suffix()}"]
    lines.append("　".join(f"{label} {n} 只" for label, n in counts.items()))
    lines.append("")
    for alert in ordered:
        lines.extend(alert.summary_lines())
    lines.append("")
    lines.append("按偏离自身常态的程度排序，不是按跌幅大小。")
    return "\n".join(lines).strip()
