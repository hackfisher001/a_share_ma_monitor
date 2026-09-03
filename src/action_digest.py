"""Concise, behavior-aware action text for alerts and daily reports."""

from __future__ import annotations

from datetime import date

from src.state import AlertState
from src.trades import TradeLedger

DEFAULT_ESCALATE_PCT = 3.0
DEFAULT_PENDING_TTL_DAYS = 5


def pending_key(market: str, code: str, side: str) -> str:
    return f"action:{market.lower()}:{code.upper()}:{side.upper()}"


def action_payload(
    *, market: str, code: str, name: str, side: str, price: float, text: str, kind: str = ""
) -> dict:
    return {
        "market": market.lower(),
        "code": code.upper(),
        "name": name,
        "side": side.upper(),
        "price": round(float(price), 4),
        "text": text,
        "kind": kind,
    }


def _parse_day(raw: object) -> date | None:
    try:
        return date.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None


def should_realert(
    pending: dict,
    *,
    price: float,
    side: str,
    escalate_pct: float = DEFAULT_ESCALATE_PCT,
    ttl_days: int = DEFAULT_PENDING_TTL_DAYS,
    today: date | None = None,
) -> tuple[bool, str]:
    """Decide whether an unrecorded reminder should speak up again.

    Suppressing repeats stops one dip from alerting daily, but suppressing them
    forever means a reminder ignored at -5% stays silent at -15%, and the sleeve
    never advances. So a repeat is allowed when the price moved materially
    further in the signal's direction, or when the reminder has simply gone
    stale.
    """
    if not pending:
        return True, ""
    today = today or date.today()
    side = side.upper()
    try:
        prior = float(pending.get("price") or 0.0)
    except (TypeError, ValueError):
        prior = 0.0

    if prior > 0 and price > 0 and escalate_pct > 0:
        move_pct = (price / prior - 1.0) * 100.0
        if side == "BUY" and move_pct <= -escalate_pct:
            return True, f"较上次提醒又跌了 {abs(move_pct):.2f}%"
        if side == "SELL" and move_pct >= escalate_pct:
            return True, f"较上次提醒又涨了 {move_pct:.2f}%"

    last = _parse_day(pending.get("last_alerted")) or _parse_day(pending.get("first_seen"))
    if last is not None and ttl_days > 0 and (today - last).days >= ttl_days:
        age = (today - (_parse_day(pending.get("first_seen")) or last)).days
        return True, f"这条提醒已挂 {age} 天仍未记录成交"

    return False, ""


def positions_markdown(ledger: TradeLedger, prices: dict[str, float]) -> str:
    """Real holdings from the journal, priced with `market:code` -> price."""
    # Reports are market-scoped, so `prices` only covers the market being sent.
    # Skipping unpriced holdings beats printing 现价 0.00 for the other market.
    held = [
        p
        for p in ledger.positions().values()
        if p.holding and float(prices.get(f"{p.market}:{p.code}") or 0.0) > 0
    ]
    if not held:
        return ""
    lines = ["**持仓（按已记录的成交计算）**"]
    total_pnl = 0.0
    total_cost = 0.0
    for pos in sorted(held, key=lambda p: (p.market, p.code)):
        price = float(prices[f"{pos.market}:{pos.code}"])
        gain = pos.unrealised_pct(price)
        gain_txt = "—" if gain is None else f"{gain:+.2f}%"
        lines.append(
            f"- {pos.code}　{pos.quantity:g} 股　成本 {pos.avg_price:.2f}"
            f"　现价 {price:.2f}　浮盈 {gain_txt}"
        )
        if gain is not None:
            total_pnl += pos.quantity * price - pos.cost
            total_cost += pos.cost
    if total_cost > 0:
        lines.append(f"**合计浮盈：** {total_pnl:+.2f}（{total_pnl / total_cost * 100:+.2f}%）")
    return "\n".join(lines)


def action_summary_markdown(state: AlertState, ledger: TradeLedger) -> str:
    """Return a short card: unfinished commitments first, then recent behavior."""
    pending = state.pending_actions()
    recent = ledger.recent(3)
    lines = ["**今天只看行动，不看 K 线连续剧。**"]
    if pending:
        lines.append(f"**待你确认：{len(pending)} 件**")
        for action in pending[:3]:
            side = "买入" if str(action.get("side")).upper() == "BUY" else "卖出"
            lines.append(
                f"- 👉 {side} **{action.get('name')}({action.get('code')})**"
                f"｜参考 {float(action.get('price') or 0):.2f}"
            )
        if len(pending) > 3:
            lines.append(f"- 还有 {len(pending) - 3} 件待确认，今天先别让待办排队成龙。")
    else:
        lines.append("**无需操作**｜今天没有待确认的纪律信号，手别痒就是胜利。")
    if recent:
        rendered = "；".join(
            f"{t.date} {('买' if t.side == 'BUY' else '卖')}{t.code} {t.quantity:g}@{t.price:g}"
            for t in recent
        )
        lines.append(f"**最近已记录：** {rendered}")
    else:
        lines.append("**行为记录：** 暂无。执行后用 `--record-trade` 记一笔，系统才知道你真的动手了。")
    return "\n".join(lines)
