"""Daily / weekly / monthly Feishu reports with optional DeepSeek commentary."""

from __future__ import annotations

import logging
from datetime import date

from src.digest import MARKET_TITLE, collect_bundles
from src.fetch_quotes import QuoteBundle
from src.llm import chat, deepseek_enabled
from src.notify import send_alert
from src.perf import (
    PeriodChange,
    change_by_calendar_days,
    change_by_trading_days,
    compute_period_changes,
)

log = logging.getLogger("ma_monitor")

REPORT_TITLES = {
    "daily": "持仓日报",
    "weekly": "持仓周报",
    "monthly": "持仓月报",
}

SYSTEM_PROMPTS = {
    "daily": (
        "你是个人长线投资助手。根据给定的持仓行情事实写中文日报点评。\n"
        "结构固定为四段纯正文（不要标题符号）：\n"
        "1) 今日涨跌分化（点名偏强/偏弱各2～4只，带具体涨跌幅）；\n"
        "2) 近期急跌观察：重点看最近1日、3日、1周跌得最凶的标的（以给定急跌榜为准），"
        "说明它们可否作为机动仓小额加仓的观察对象；一年高点回撤只作补充背景，不要当成唯一标准；\n"
        "3) 均线观察（谁贴近MA30、谁明显偏离）；\n"
        "4) 行动提醒：主仓仍按定投继续；只有机动仓才回应近期急跌/回撤；最后一行免责声明。\n"
        "字数 300～450 字；只基于给定数据，不编造新闻、舆情、基本面；不要给出具体买入金额。"
    ),
    "weekly": (
        "你是个人长线投资助手。根据给定的持仓行情事实写中文周报点评。\n"
        "结构固定为四段纯正文（不要标题符号）：\n"
        "1) 本周强弱分化（带具体区间涨跌）；\n"
        "2) 回撤与急跌：既看一年高点回撤，也点名本周跌幅较大的标的；\n"
        "3) 均线位置（贴近/上方/下方）；\n"
        "4) 纪律提醒：主仓定投、机动仓回应急跌/回撤；不要给买卖点位或仓位百分比；"
        "最后一行免责声明。\n"
        "字数 350～500 字；只基于给定数据，不编造新闻或社区舆论。"
    ),
    "monthly": (
        "你是个人长线投资助手。根据给定的持仓行情事实写中文月报点评。\n"
        "结构固定为四段纯正文（不要标题符号）：\n"
        "1) 一月表现归纳（上涨/下跌阵营，带具体涨跌幅）；\n"
        "2) 回撤深度（一年高点回撤最深的几只）；\n"
        "3) 是否贴近均线（明确点出贴近MA30、明显偏离的标的）；\n"
        "4) 下月观察重点（只谈观察，不预测涨跌）；最后一行免责声明。\n"
        "字数 400～500 字；只基于给定数据，不编造基本面或传闻。"
    ),
}


def _drawdown_line(bundle: QuoteBundle) -> str:
    if bundle.high_252 <= 0:
        return "距一年高点: —"
    dd = (bundle.price / bundle.high_252 - 1.0) * 100.0
    return f"距一年高点: {dd:+.1f}%"


def _short_term_moves(bundle: QuoteBundle) -> dict[str, float | None]:
    """Recent moves used for '急跌' observation (trading-day based where possible)."""
    return {
        "1日": change_by_trading_days(bundle.hist, bundle.price, 1),
        "3日": change_by_trading_days(bundle.hist, bundle.price, 3),
        "1周": change_by_calendar_days(bundle.hist, bundle.price, 7),
    }


def _fmt_pct(v: float | None) -> str:
    return "—" if v is None else f"{v:+.2f}%"


def _fmt_changes(changes: list[PeriodChange]) -> str:
    return "　".join(c.fmt() for c in changes)


def _stock_block_daily(bundle: QuoteBundle) -> str:
    st = _short_term_moves(bundle)
    changes = compute_period_changes(bundle.hist, bundle.price)
    ma_dev = (bundle.price - bundle.ma30) / bundle.ma30 * 100
    return (
        f"**{bundle.name}({bundle.code})**  {bundle.price:.2f}\n"
        f"近1日 {_fmt_pct(st['1日'])}｜近3日 {_fmt_pct(st['3日'])}｜近1周 {_fmt_pct(st['1周'])}\n"
        f"MA30 {bundle.ma30:.2f}（{ma_dev:+.2f}%）｜{_drawdown_line(bundle)}｜截至 {bundle.as_of}\n"
        f"{_fmt_changes(changes)}"
    )


def _stock_block_weekly(bundle: QuoteBundle) -> str:
    price = bundle.price
    hist = bundle.hist
    changes = [
        PeriodChange("1周", change_by_calendar_days(hist, price, 7)),
        PeriodChange("1月", change_by_calendar_days(hist, price, 30)),
        PeriodChange("5日", change_by_trading_days(hist, price, 5)),
        PeriodChange("半年", change_by_calendar_days(hist, price, 182)),
    ]
    ma_dev = (price - bundle.ma30) / bundle.ma30 * 100
    return (
        f"**{bundle.name}({bundle.code})**  {price:.2f}\n"
        f"MA30 {bundle.ma30:.2f}（{ma_dev:+.2f}%）｜{_drawdown_line(bundle)}\n"
        f"{_fmt_changes(changes)}"
    )


def _stock_block_monthly(bundle: QuoteBundle) -> str:
    price = bundle.price
    hist = bundle.hist
    changes = [
        PeriodChange("1月", change_by_calendar_days(hist, price, 30)),
        PeriodChange("3月", change_by_calendar_days(hist, price, 91)),
        PeriodChange("半年", change_by_calendar_days(hist, price, 182)),
        PeriodChange("1年", change_by_calendar_days(hist, price, 365)),
    ]
    ma_dev = (price - bundle.ma30) / bundle.ma30 * 100
    return (
        f"**{bundle.name}({bundle.code})**  {price:.2f}\n"
        f"MA30 {bundle.ma30:.2f}（{ma_dev:+.2f}%）｜{_drawdown_line(bundle)}\n"
        f"{_fmt_changes(changes)}"
    )


def _market_data_text(kind: str, bundles: list[QuoteBundle]) -> str:
    header = f"共 {len(bundles)} 只｜{date.today().isoformat()}"
    if kind == "weekly":
        blocks = [_stock_block_weekly(b) for b in bundles]
    elif kind == "monthly":
        blocks = [_stock_block_monthly(b) for b in bundles]
    else:
        blocks = [_stock_block_daily(b) for b in bundles]
    return header + "\n\n" + "\n\n".join(blocks)


def _sharp_drop_board(bundles: list[QuoteBundle], top_n: int = 5) -> list[str]:
    """Rank names by worst 1d / 3d / 1w moves for the LLM."""
    rows: list[tuple[str, float, str]] = []
    for b in bundles:
        st = _short_term_moves(b)
        for label, val in st.items():
            if val is not None and val < 0:
                rows.append((f"{b.name}({b.code})", val, label))
    lines: list[str] = []
    for label in ("1日", "3日", "1周"):
        subset = sorted([r for r in rows if r[2] == label], key=lambda x: x[1])[:top_n]
        if not subset:
            lines.append(f"急跌榜/{label}: 无下跌标的")
            continue
        parts = [f"{name} {val:+.2f}%" for name, val, _ in subset]
        lines.append(f"急跌榜/{label}: " + "；".join(parts))
    return lines


def _facts_for_llm(kind: str, bundles: list[QuoteBundle]) -> str:
    today = date.today().isoformat()
    lines = [f"报告类型: {kind}", f"日期: {today}", f"标的数: {len(bundles)}", ""]
    if kind == "daily":
        lines.extend(_sharp_drop_board(bundles))
        lines.append("")
    for b in bundles:
        ma_dev = (b.price - b.ma30) / b.ma30 * 100
        dd = (b.price / b.high_252 - 1.0) * 100 if b.high_252 > 0 else None

        if kind == "weekly":
            chg = (
                f"1周={_fmt_pct(change_by_calendar_days(b.hist, b.price, 7))}, "
                f"1月={_fmt_pct(change_by_calendar_days(b.hist, b.price, 30))}"
            )
        elif kind == "monthly":
            chg = (
                f"1月={_fmt_pct(change_by_calendar_days(b.hist, b.price, 30))}, "
                f"1年={_fmt_pct(change_by_calendar_days(b.hist, b.price, 365))}"
            )
        else:
            st = _short_term_moves(b)
            chg = (
                f"1日={_fmt_pct(st['1日'])}, 3日={_fmt_pct(st['3日'])}, "
                f"1周={_fmt_pct(st['1周'])}"
            )
        lines.append(
            f"- [{b.market}] {b.name}({b.code}) 价={b.price:.2f} "
            f"MA30偏离={ma_dev:+.2f}% 一年高点回撤="
            f"{'—' if dd is None else f'{dd:+.1f}%'} | {chg}"
        )
    return "\n".join(lines)


def _llm_comment(kind: str, bundles: list[QuoteBundle]) -> str | None:
    if not deepseek_enabled():
        log.info("未配置 DEEPSEEK_API_KEY，跳过模型点评")
        return None
    try:
        return chat(SYSTEM_PROMPTS[kind], _facts_for_llm(kind, bundles))
    except Exception as exc:
        log.exception("DeepSeek 点评失败: %s", exc)
        return f"（模型点评暂不可用：{exc}）"


def run_report(
    stocks: list[dict],
    kind: str = "daily",
    *,
    dry_run: bool = False,
    markets: list[str] | None = None,
) -> int:
    """kind: daily | weekly | monthly — market data cards + one DeepSeek summary."""
    kind = (kind or "daily").strip().lower()
    if kind not in REPORT_TITLES:
        raise ValueError(f"未知报告类型: {kind}")

    present = sorted({str(s.get("market") or "cn").lower() for s in stocks})
    targets = markets or [m for m in ("cn", "hk", "us") if m in present]

    all_bundles: list[QuoteBundle] = []
    sent = 0
    fail_markets = 0

    for market in targets:
        bundles, errors = collect_bundles(stocks, market_filter=market)
        if not bundles and not errors:
            continue
        if not bundles:
            fail_markets += 1
            continue
        all_bundles.extend(bundles)
        text = _market_data_text(kind, bundles)
        if errors:
            text += "\n\n拉取失败: " + ", ".join(errors)

        market_label = MARKET_TITLE.get(market, market.upper()).replace("日报", "")
        title = f"{REPORT_TITLES[kind]} · {market_label}行情"
        if dry_run:
            log.info("[dry-run] %s\n%s", title, text[:1200])
        else:
            channel = send_alert(text, title=title)
            log.info("已通过 %s 发送 %s（%d 只）", channel, title, len(bundles))
        sent += 1

    if all_bundles:
        comment = _llm_comment(kind, all_bundles)
        if comment:
            title = f"{REPORT_TITLES[kind]} · DeepSeek 点评"
            if dry_run:
                log.info("[dry-run] %s\n%s", title, comment)
            else:
                channel = send_alert(comment, title=title)
                log.info("已通过 %s 发送 %s", channel, title)
            sent += 1

    log.info("%s完成：发送 %d 组，全失败市场 %d", REPORT_TITLES[kind], sent, fail_markets)
    return 1 if sent == 0 else 0
