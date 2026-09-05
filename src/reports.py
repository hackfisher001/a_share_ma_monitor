"""Daily / weekly / monthly Feishu reports."""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import Any

from src.action_digest import positions_markdown
from src.digest import MARKET_TITLE, collect_bundles
from src.dividends import income_markdown, load_profile
from src.fetch_quotes import QuoteBundle
from src.notify import send_alert
from src.trades import TradeLedger
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

THEME_LABELS = {
    "stock": "个股",
    "etf": "ETF",
    "tech_etf": "科技ETF",
    "sector_etf": "行业ETF",
    "nasdaq_cn": "跨境纳指ETF",
    "nasdaq_us": "美股指数",
    "macro": "大宗/宏观",
}

CN_STOCK_THEME = "stock"
CN_ETF_TITLE = "A股ETF"
CN_STOCK_TITLE = "A股个股"


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


def _year_position(bundle: QuoteBundle) -> tuple[float | None, float | None]:
    """Return percentile within the one-year range and drawdown from its high."""
    if bundle.hist is None or bundle.hist.empty:
        return None, None
    closes = bundle.hist["close"].tail(252)
    if closes.empty:
        return None, None
    low = float(min(closes.min(), bundle.price))
    high = float(max(closes.max(), bundle.price))
    if high <= 0:
        return None, None
    drawdown = (bundle.price / high - 1.0) * 100
    percentile = 100.0 if high == low else (bundle.price - low) / (high - low) * 100
    return percentile, drawdown


def _fmt_position(v: float | None) -> str:
    return "—" if v is None else f"{v:.0f}%"


def _sparkline(bundle: QuoteBundle) -> dict[str, Any]:
    """One-year close series for the PNG renderer."""
    hist = bundle.hist.tail(252)
    if hist.empty:
        return {"values": [], "years": [], "change": None}
    values = [float(v) for v in hist["close"]]
    years = [int(v) for v in hist["date"].dt.year]
    if values and bundle.price != values[-1]:
        values.append(float(bundle.price))
        years.append(years[-1])
    return {
        "values": values,
        "years": years,
        "change": change_by_calendar_days(bundle.hist, bundle.price, 365),
    }


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


def _row_daily(bundle: QuoteBundle) -> dict[str, Any]:
    st = _short_term_moves(bundle)
    ma_dev = (bundle.price - bundle.ma30) / bundle.ma30 * 100
    m1 = change_by_calendar_days(bundle.hist, bundle.price, 30)
    y1 = change_by_calendar_days(bundle.hist, bundle.price, 365)
    position, dd = _year_position(bundle)
    return {
        "name": f"**{bundle.name}**",
        "price": f"{bundle.price:.2f}",
        "spark": _sparkline(bundle),
        "recent": f"日 {_fmt_pct(st['1日'])}　周 {_fmt_pct(st['1周'])}\n月 {_fmt_pct(m1)}",
        "position": (
            f"MA30 {_fmt_pct(ma_dev)}　年位 {_fmt_position(position)}\n"
            f"一年 {_fmt_pct(y1)}　距高 {_fmt_pct(dd)}"
        ),
    }


def _row_weekly(bundle: QuoteBundle) -> dict[str, Any]:
    ma_dev = (bundle.price - bundle.ma30) / bundle.ma30 * 100
    w1 = change_by_calendar_days(bundle.hist, bundle.price, 7)
    m1 = change_by_calendar_days(bundle.hist, bundle.price, 30)
    m3 = change_by_calendar_days(bundle.hist, bundle.price, 91)
    y1 = change_by_calendar_days(bundle.hist, bundle.price, 365)
    position, dd = _year_position(bundle)
    return {
        "name": f"**{bundle.name}**",
        "price": f"{bundle.price:.2f}",
        "spark": _sparkline(bundle),
        "recent": f"周 {_fmt_pct(w1)}　月 {_fmt_pct(m1)}\n三月 {_fmt_pct(m3)}",
        "position": (
            f"MA30 {_fmt_pct(ma_dev)}　年位 {_fmt_position(position)}\n"
            f"一年 {_fmt_pct(y1)}　距高 {_fmt_pct(dd)}"
        ),
    }


def _row_monthly(bundle: QuoteBundle) -> dict[str, Any]:
    ma_dev = (bundle.price - bundle.ma30) / bundle.ma30 * 100
    m1 = change_by_calendar_days(bundle.hist, bundle.price, 30)
    m3 = change_by_calendar_days(bundle.hist, bundle.price, 91)
    y1 = change_by_calendar_days(bundle.hist, bundle.price, 365)
    position, dd = _year_position(bundle)
    return {
        "name": f"**{bundle.name}**",
        "price": f"{bundle.price:.2f}",
        "spark": _sparkline(bundle),
        "recent": f"月 {_fmt_pct(m1)}　三月 {_fmt_pct(m3)}",
        "position": (
            f"MA30 {_fmt_pct(ma_dev)}　年位 {_fmt_position(position)}\n"
            f"一年 {_fmt_pct(y1)}　距高 {_fmt_pct(dd)}"
        ),
    }


def _columns_for(kind: str) -> list[dict[str, str]]:
    recent_label = {
        "daily": "近期（日/周/月）",
        "weekly": "近期（周/月/三月）",
        "monthly": "近期（月/三月）",
    }.get(kind, "近期")
    return [
        {"name": "name", "display_name": "名称", "width": "120px", "data_type": "lark_md"},
        {"name": "price", "display_name": "现价", "width": "80px"},
        {
            "name": "spark",
            "display_name": "近一年走势（高/今）",
            "width": "240px",
            "data_type": "sparkline",
        },
        {"name": "recent", "display_name": recent_label, "width": "175px"},
        {"name": "position", "display_name": "位置", "width": "200px"},
    ]


def _row_for(kind: str, bundle: QuoteBundle) -> dict[str, Any]:
    if kind == "weekly":
        return _row_weekly(bundle)
    if kind == "monthly":
        return _row_monthly(bundle)
    return _row_daily(bundle)


def _split_cn_bundles(bundles: list[QuoteBundle]) -> tuple[list[QuoteBundle], list[QuoteBundle]]:
    stocks = [b for b in bundles if b.theme == CN_STOCK_THEME]
    etfs = [b for b in bundles if b.theme != CN_STOCK_THEME]
    return stocks, etfs


def _rank_bundles(kind: str, items: list[QuoteBundle]) -> list[QuoteBundle]:
    rank_days = 7 if kind == "weekly" else 30
    rank_values = {
        id(b): change_by_calendar_days(b.hist, b.price, rank_days) for b in items
    }
    return sorted(
        items,
        key=lambda b: (
            rank_values[id(b)] is not None,
            rank_values[id(b)] if rank_values[id(b)] is not None else float("-inf"),
        ),
        reverse=True,
    )


def _build_tables(
    kind: str,
    bundles: list[QuoteBundle],
    *,
    group_label: str | None = None,
) -> list[dict]:
    """Build image tables; optionally keep one merged group instead of per-theme splits."""
    columns = _columns_for(kind)
    tables: list[dict] = []
    horizon = "近1周" if kind == "weekly" else "近1月"

    if group_label:
        items = _rank_bundles(kind, bundles)
        rows = [_row_for(kind, b) for b in items]
        for i in range(0, len(rows), 10):
            chunk = rows[i : i + 10]
            title = (
                f"{group_label}｜按{horizon}强→弱"
                if i == 0
                else f"{group_label}（续）｜按{horizon}强→弱"
            )
            tables.append(
                {
                    "title": title,
                    "columns": columns,
                    "rows": chunk,
                    "page_size": len(chunk),
                }
            )
        return tables

    order = ("tech_etf", "nasdaq_cn", "sector_etf", "stock", "nasdaq_us", "macro", "")
    grouped: dict[str, list[QuoteBundle]] = {}
    for b in bundles:
        grouped.setdefault(b.theme or "", []).append(b)

    themes = [t for t in order if grouped.get(t)] + [
        t for t in grouped if t not in order
    ]
    for theme in themes:
        items = _rank_bundles(kind, grouped[theme])
        label = THEME_LABELS.get(theme, theme or "其他")
        rows = [_row_for(kind, b) for b in items]
        for i in range(0, len(rows), 10):
            chunk = rows[i : i + 10]
            title = (
                f"{label}｜按{horizon}强→弱"
                if i == 0
                else f"{label}（续）｜按{horizon}强→弱"
            )
            tables.append(
                {
                    "title": title,
                    "columns": columns,
                    "rows": chunk,
                    "page_size": len(chunk),
                }
            )
    return tables


def _market_header(kind: str, bundles: list[QuoteBundle]) -> str:
    return (
        f"**共 {len(bundles)} 只**｜{date.today().isoformat()}\n"
        "走势图标出年度分界、高点和当前点；近期看日/周/月。"
        "年位 0%=近一年低点、100%=高点。"
    )


def run_report(
    stocks: list[dict],
    kind: str = "daily",
    *,
    dry_run: bool = False,
    markets: list[str] | None = None,
    action_markdown: str | None = None,
    ledger: TradeLedger | None = None,
    income_codes: set[str] | None = None,
) -> int:
    """kind: daily | weekly | monthly — market data cards."""
    kind = (kind or "daily").strip().lower()
    if kind not in REPORT_TITLES:
        raise ValueError(f"未知报告类型: {kind}")

    present = sorted({str(s.get("market") or "cn").lower() for s in stocks})
    targets = markets or [m for m in ("cn", "hk", "us") if m in present]

    all_bundles: list[QuoteBundle] = []
    sent = 0
    fail_markets = 0

    if action_markdown:
        title = "今日行动清单"
        if dry_run:
            log.info("[dry-run] %s\n%s", title, action_markdown)
        else:
            channel = send_alert(title=title, markdown=action_markdown)
            log.info("已通过 %s 发送 %s", channel, title)
        sent += 1

    for market in targets:
        bundles, errors = collect_bundles(stocks, market_filter=market)
        if not bundles and not errors:
            continue
        if not bundles:
            fail_markets += 1
            continue
        all_bundles.extend(bundles)

        market_label = MARKET_TITLE.get(market, market.upper()).replace("日报", "")
        groups: list[tuple[str, list[QuoteBundle], str | None]]
        if market == "cn":
            # NOTE: do not name these `stocks` — that would shadow the watchlist
            # dicts and crash the *next* market's collect_bundles call.
            cn_stocks, cn_etfs = _split_cn_bundles(bundles)
            groups = []
            if cn_stocks:
                groups.append(
                    (
                        f"{REPORT_TITLES[kind]} · {CN_STOCK_TITLE}",
                        cn_stocks,
                        CN_STOCK_TITLE,
                    )
                )
            if cn_etfs:
                groups.append(
                    (
                        f"{REPORT_TITLES[kind]} · {CN_ETF_TITLE}",
                        cn_etfs,
                        CN_ETF_TITLE,
                    )
                )
            if not groups:
                groups = [(f"{REPORT_TITLES[kind]} · {market_label}行情", bundles, None)]
        else:
            groups = [(f"{REPORT_TITLES[kind]} · {market_label}行情", bundles, None)]

        for title, group, group_label in groups:
            header = _market_header(kind, group)
            tables = _build_tables(kind, group, group_label=group_label)
            if errors and group is bundles:
                header += "\n\n**拉取失败：** " + ", ".join(errors)
            elif errors and title.endswith(CN_STOCK_TITLE):
                header += "\n\n**拉取失败：** " + ", ".join(errors)
            if dry_run:
                log.info("[dry-run] %s tables=%d\n%s", title, len(tables), header)
            else:
                channel = send_alert(
                    title=title,
                    markdown=header,
                    tables=tables,
                )
                log.info("已通过 %s 发送 %s（%d 只 / %d 表）", channel, title, len(group), len(tables))
            sent += 1

    # Dividend data is slow and changes a few times a year, so it is fetched
    # here (per report, cached for a week) rather than in the intraday scan.
    if income_codes and all_bundles:
        cache = Path("data") / "dividends.json"
        rows = []
        for b in all_bundles:
            if b.market != "cn" or b.code not in income_codes:
                continue
            try:
                rows.append((b.name, b.code, load_profile(b.code, cache_path=cache), b.price))
            except Exception as exc:
                log.warning("股息率跳过 %s: %s", b.code, exc)
        markdown = income_markdown(rows)
        if markdown:
            title = "收息仓 · 股息率"
            if dry_run:
                log.info("[dry-run] %s\n%s", title, markdown)
            else:
                channel = send_alert(title=title, markdown=markdown, prefer_images=False)
                log.info("已通过 %s 发送 %s", channel, title)
            sent += 1

    # Priced off the bundles already fetched above, so no extra round trips.
    if ledger is not None and all_bundles:
        prices = {f"{b.market}:{b.code.upper()}": b.price for b in all_bundles}
        holdings_md = positions_markdown(ledger, prices)
        if holdings_md:
            title = "持仓与成本"
            if dry_run:
                log.info("[dry-run] %s\n%s", title, holdings_md)
            else:
                channel = send_alert(title=title, markdown=holdings_md, prefer_images=False)
                log.info("已通过 %s 发送 %s", channel, title)
            sent += 1

    log.info("%s完成：发送 %d 组，全失败市场 %d", REPORT_TITLES[kind], sent, fail_markets)
    # Failure means the data was unusable, not that there was nothing to say.
    # The trimmed daily push legitimately sends nothing when no dividend or
    # holding applies, and that must not show up in cron as an error.
    return 1 if fail_markets else 0
