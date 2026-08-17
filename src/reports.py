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

THEME_LABELS = {
    "stock": "个股",
    "tech_etf": "科技ETF",
    "sector_etf": "行业ETF",
    "nasdaq_cn": "跨境纳指ETF",
    "nasdaq_us": "美股指数",
    "macro": "大宗/宏观",
}

SYSTEM_PROMPTS = {
    "daily": (
        "你是个人长线投资助手。根据给定的持仓行情事实写中文日报点评。\n"
        "结构固定为五段纯正文；每段第一行用加粗小标题，例如 **一、涨跌分化**：\n"
        "1) 个股与美股涨跌分化（点名偏强/偏弱，带具体涨跌幅）；\n"
        "2) 板块ETF强弱：重点比较科技类ETF相对沪深300及其他行业ETF的1日、3日、1周表现；\n"
        "3) 近期急跌观察：以急跌榜为准，点名近1日/3日/1周跌得最凶的标的，"
        "说明是否可作为机动仓小额加仓观察对象；一年高点回撤只作补充；\n"
        "4) 均线观察（谁贴近MA30、谁明显偏离）；\n"
        "5) 行动提醒：主仓仍按定投继续；只有机动仓才回应急跌/弱势板块；最后一行免责声明。\n"
        "字数 350～500 字；只基于给定数据，不编造新闻、舆情、基本面；不要给出具体买入金额。"
    ),
    "weekly": (
        "你是个人长线投资助手。根据给定的持仓行情事实写中文周报点评。\n"
        "结构固定为五段纯正文；每段第一行用加粗小标题，例如 **一、本周强弱**：\n"
        "1) 个股与美股本周强弱；\n"
        "2) 板块ETF轮动：科技类相对其他行业及沪深300谁强谁弱；\n"
        "3) 回撤与急跌：一年高点回撤 + 本周跌幅较大标的；\n"
        "4) 均线位置；\n"
        "5) 纪律提醒：主仓定投、机动仓回应急跌/弱势板块；不要给买卖点位；最后一行免责声明。\n"
        "字数 400～500 字；只基于给定数据，不编造新闻或社区舆论。"
    ),
    "monthly": (
        "你是个人长线投资助手。根据给定的持仓行情事实写中文月报点评。\n"
        "结构固定为五段纯正文；每段第一行用加粗小标题，例如 **一、一月表现**：\n"
        "1) 一月个股/美股表现归纳；\n"
        "2) 板块ETF一月表现：科技类 vs 医药/银行/白酒/军工/沪深300；\n"
        "3) 回撤深度；\n"
        "4) 是否贴近均线；\n"
        "5) 下月观察重点（只谈观察，不预测涨跌）；最后一行免责声明。\n"
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


def _row_daily(bundle: QuoteBundle) -> dict[str, str]:
    st = _short_term_moves(bundle)
    ma_dev = (bundle.price - bundle.ma30) / bundle.ma30 * 100
    dd = (
        (bundle.price / bundle.high_252 - 1.0) * 100
        if bundle.high_252 > 0
        else None
    )
    return {
        "name": f"**{bundle.name}**",
        "code": str(bundle.code),
        "price": f"{bundle.price:.2f}",
        "d1": _fmt_pct(st["1日"]),
        "d3": _fmt_pct(st["3日"]),
        "w1": _fmt_pct(st["1周"]),
        "ma": f"{ma_dev:+.2f}%",
        "dd": _fmt_pct(dd) if dd is not None else "—",
    }


def _row_weekly(bundle: QuoteBundle) -> dict[str, str]:
    ma_dev = (bundle.price - bundle.ma30) / bundle.ma30 * 100
    dd = (
        (bundle.price / bundle.high_252 - 1.0) * 100
        if bundle.high_252 > 0
        else None
    )
    return {
        "name": f"**{bundle.name}**",
        "code": str(bundle.code),
        "price": f"{bundle.price:.2f}",
        "w1": _fmt_pct(change_by_calendar_days(bundle.hist, bundle.price, 7)),
        "m1": _fmt_pct(change_by_calendar_days(bundle.hist, bundle.price, 30)),
        "ma": f"{ma_dev:+.2f}%",
        "dd": _fmt_pct(dd) if dd is not None else "—",
    }


def _row_monthly(bundle: QuoteBundle) -> dict[str, str]:
    ma_dev = (bundle.price - bundle.ma30) / bundle.ma30 * 100
    dd = (
        (bundle.price / bundle.high_252 - 1.0) * 100
        if bundle.high_252 > 0
        else None
    )
    return {
        "name": f"**{bundle.name}**",
        "code": str(bundle.code),
        "price": f"{bundle.price:.2f}",
        "m1": _fmt_pct(change_by_calendar_days(bundle.hist, bundle.price, 30)),
        "m3": _fmt_pct(change_by_calendar_days(bundle.hist, bundle.price, 91)),
        "h1": _fmt_pct(change_by_calendar_days(bundle.hist, bundle.price, 182)),
        "y1": _fmt_pct(change_by_calendar_days(bundle.hist, bundle.price, 365)),
        "ma": f"{ma_dev:+.2f}%",
        "dd": _fmt_pct(dd) if dd is not None else "—",
    }


def _columns_for(kind: str) -> list[dict[str, str]]:
    # Feishu table column width minimum is 80px.
    name_cols = [
        {"name": "name", "display_name": "名称", "width": "110px", "data_type": "lark_md"},
        {"name": "code", "display_name": "代码", "width": "80px", "data_type": "text"},
    ]
    if kind == "weekly":
        return name_cols + [
            {"name": "price", "display_name": "现价", "width": "80px"},
            {"name": "w1", "display_name": "1周", "width": "80px"},
            {"name": "m1", "display_name": "1月", "width": "80px"},
            {"name": "ma", "display_name": "较MA30", "width": "90px"},
            {"name": "dd", "display_name": "一年回撤", "width": "90px"},
        ]
    if kind == "monthly":
        return name_cols + [
            {"name": "price", "display_name": "现价", "width": "80px"},
            {"name": "m1", "display_name": "1月", "width": "80px"},
            {"name": "m3", "display_name": "3月", "width": "80px"},
            {"name": "h1", "display_name": "半年", "width": "80px"},
            {"name": "y1", "display_name": "1年", "width": "80px"},
            {"name": "ma", "display_name": "较MA30", "width": "90px"},
            {"name": "dd", "display_name": "一年回撤", "width": "90px"},
        ]
    return name_cols + [
        {"name": "price", "display_name": "现价", "width": "80px"},
        {"name": "d1", "display_name": "1日", "width": "80px"},
        {"name": "d3", "display_name": "3日", "width": "80px"},
        {"name": "w1", "display_name": "1周", "width": "80px"},
        {"name": "ma", "display_name": "较MA30", "width": "90px"},
        {"name": "dd", "display_name": "一年回撤", "width": "90px"},
    ]


def _row_for(kind: str, bundle: QuoteBundle) -> dict[str, str]:
    if kind == "weekly":
        return _row_weekly(bundle)
    if kind == "monthly":
        return _row_monthly(bundle)
    return _row_daily(bundle)


def _build_tables(kind: str, bundles: list[QuoteBundle]) -> list[dict]:
    """One Feishu table per theme group; chunk to page_size<=10."""
    order = ("tech_etf", "nasdaq_cn", "sector_etf", "stock", "nasdaq_us", "macro", "")
    grouped: dict[str, list[QuoteBundle]] = {}
    for b in bundles:
        grouped.setdefault(b.theme or "", []).append(b)

    columns = _columns_for(kind)
    tables: list[dict] = []
    themes = [t for t in order if grouped.get(t)] + [
        t for t in grouped if t not in order
    ]
    for theme in themes:
        items = grouped[theme]
        label = THEME_LABELS.get(theme, theme or "其他")
        rows = [_row_for(kind, b) for b in items]
        for i in range(0, len(rows), 10):
            chunk = rows[i : i + 10]
            title = f"{label}" if i == 0 else f"{label}（续）"
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
        f"请直接对比涨跌幅列；名称已 **加粗**。"
    )


def _sector_board(bundles: list[QuoteBundle], kind: str) -> list[str]:
    """Relative strength board for sector/tech ETFs."""
    etfs = [b for b in bundles if b.theme in {"tech_etf", "sector_etf", "nasdaq_cn"}]
    if not etfs:
        return []
    horizon = "1周" if kind != "daily" else "1日"
    rows: list[tuple[str, str, float]] = []
    for b in etfs:
        if kind == "monthly":
            val = change_by_calendar_days(b.hist, b.price, 30)
            horizon = "1月"
        elif kind == "weekly":
            val = change_by_calendar_days(b.hist, b.price, 7)
            horizon = "1周"
        else:
            val = change_by_trading_days(b.hist, b.price, 1)
            horizon = "1日"
        if val is None:
            continue
        label = THEME_LABELS.get(b.theme, b.theme)
        rows.append((f"{b.name}({b.code})", label, val))
    if not rows:
        return ["板块ETF榜: 数据不足"]
    rows.sort(key=lambda x: x[2], reverse=True)
    top = rows[:5]
    bottom = list(reversed(rows[-5:]))
    lines = [f"板块ETF强弱榜（按{horizon}）:"]
    lines.append(
        "偏强: " + "；".join(f"{n}[{t}] {_fmt_pct(v)}" for n, t, v in top)
    )
    lines.append(
        "偏弱: " + "；".join(f"{n}[{t}] {_fmt_pct(v)}" for n, t, v in bottom)
    )
    tech = [r for r in rows if "科技" in r[1] or r[1] == "跨境纳指ETF"]
    if tech:
        tech_sorted = sorted(tech, key=lambda x: x[2], reverse=True)
        lines.append(
            "科技相关排序: "
            + "；".join(f"{n} {_fmt_pct(v)}" for n, _, v in tech_sorted)
        )
    return lines


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
    lines.extend(_sector_board(bundles, kind))
    lines.append("")
    if kind in {"daily", "weekly"}:
        lines.extend(_sharp_drop_board(bundles))
        lines.append("")
    for b in bundles:
        ma_dev = (b.price - b.ma30) / b.ma30 * 100
        dd = (b.price / b.high_252 - 1.0) * 100 if b.high_252 > 0 else None
        theme = THEME_LABELS.get(b.theme, b.theme or "其他")

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
            f"- [{theme}/{b.market}] {b.name}({b.code}) 价={b.price:.2f} "
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

        market_label = MARKET_TITLE.get(market, market.upper()).replace("日报", "")
        # A股拆成「个股/跨境」与「板块ETF」两张卡，避免飞书正文过长截断。
        groups: list[tuple[str, list[QuoteBundle]]]
        if market == "cn":
            core = [b for b in bundles if b.theme in {"stock", "nasdaq_cn", ""}]
            etfs = [b for b in bundles if b.theme in {"tech_etf", "sector_etf"}]
            groups = []
            if core:
                groups.append((f"{REPORT_TITLES[kind]} · {market_label}个股", core))
            if etfs:
                groups.append((f"{REPORT_TITLES[kind]} · 板块ETF", etfs))
            if not groups:
                groups = [(f"{REPORT_TITLES[kind]} · {market_label}行情", bundles)]
        else:
            groups = [(f"{REPORT_TITLES[kind]} · {market_label}行情", bundles)]

        for title, group in groups:
            header = _market_header(kind, group)
            tables = _build_tables(kind, group)
            if errors and (group is bundles or title.endswith("个股")):
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

    if all_bundles:
        comment = _llm_comment(kind, all_bundles)
        if comment:
            title = f"{REPORT_TITLES[kind]} · DeepSeek 点评"
            # Ensure section labels render as bold even if model omits markers.
            md = comment if "**" in comment else f"**点评摘要**\n{comment}"
            if dry_run:
                log.info("[dry-run] %s\n%s", title, md)
            else:
                channel = send_alert(title=title, markdown=md)
                log.info("已通过 %s 发送 %s", channel, title)
            sent += 1

    log.info("%s完成：发送 %d 组，全失败市场 %d", REPORT_TITLES[kind], sent, fail_markets)
    return 1 if sent == 0 else 0
