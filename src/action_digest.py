"""Concise, behavior-aware action text for alerts and daily reports."""

from __future__ import annotations

from src.state import AlertState
from src.trades import TradeLedger


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
