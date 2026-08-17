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
        "你是个人长线投资助手。根据给定的持仓行情事实写中文日报点评。"
        "要求：1) 不超过 350 字；2) 只基于事实，不编造新闻或舆情；"
        "3) 明确区分观察与行动，默认建议主仓按定投继续，不因短期波动空仓等待；"
        "4) 用条目列出今日值得注意的 2～4 只标的；5) 结尾一句免责声明。"
    ),
    "weekly": (
        "你是个人长线投资助手。根据给定的持仓行情事实写中文周报点评。"
        "要求：1) 不超过 450 字；2) 总结本周强弱分化与回撤档变化；"
        "3) 不编造新闻或社区舆论；4) 不要给出具体买卖点位或仓位百分比；"
        "5) 强调纪律：主仓定投、机动仓才回应回撤；6) 结尾免责声明。"
    ),
    "monthly": (
        "你是个人长线投资助手。根据给定的持仓行情事实写中文月报点评。"
        "要求：1) 不超过 500 字；2) 归纳一月表现、回撤深度、是否贴近均线；"
        "3) 不编造基本面或传闻；4) 可提示下月观察重点，但不预测涨跌；"
        "5) 结尾免责声明。"
    ),
}


def _drawdown_line(bundle: QuoteBundle) -> str:
    if bundle.high_252 <= 0:
        return "距一年高点: —"
    dd = (bundle.price / bundle.high_252 - 1.0) * 100.0
    return f"距一年高点: {dd:+.1f}%"


def _fmt_changes(changes: list[PeriodChange]) -> str:
    return "　".join(c.fmt() for c in changes)


def _stock_block_daily(bundle: QuoteBundle) -> str:
    changes = compute_period_changes(bundle.hist, bundle.price)
    ma_dev = (bundle.price - bundle.ma30) / bundle.ma30 * 100
    return (
        f"**{bundle.name}({bundle.code})**  {bundle.price:.2f}\n"
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


def _facts_for_llm(kind: str, bundles: list[QuoteBundle]) -> str:
    today = date.today().isoformat()
    lines = [f"报告类型: {kind}", f"日期: {today}", f"标的数: {len(bundles)}", ""]
    for b in bundles:
        ma_dev = (b.price - b.ma30) / b.ma30 * 100
        dd = (b.price / b.high_252 - 1.0) * 100 if b.high_252 > 0 else None

        def _r(v: float | None) -> str:
            return "—" if v is None else f"{v:.2f}%"

        if kind == "weekly":
            chg = (
                f"1周={_r(change_by_calendar_days(b.hist, b.price, 7))}, "
                f"1月={_r(change_by_calendar_days(b.hist, b.price, 30))}"
            )
        elif kind == "monthly":
            chg = (
                f"1月={_r(change_by_calendar_days(b.hist, b.price, 30))}, "
                f"1年={_r(change_by_calendar_days(b.hist, b.price, 365))}"
            )
        else:
            chg = (
                f"1日={_r(change_by_trading_days(b.hist, b.price, 1))}, "
                f"5日={_r(change_by_trading_days(b.hist, b.price, 5))}"
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
