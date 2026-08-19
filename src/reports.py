"""Daily / weekly / monthly Feishu reports with optional DeepSeek commentary."""

from __future__ import annotations

import logging
import re
from datetime import date
from typing import Any

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

_SCAN_FORMAT = (
    "\n\n输出必须严格采用下面五块，块标题单独一行，不得增加其他板块：\n"
    "🔎 **一句话结论**\n"
    "只写一句，概括当前主趋势、最明显的强弱分化。\n"
    "🔥 **领涨与强势**\n"
    "- 只列最值得注意的 2～3 个强势标的，每条必须带周期和涨幅。\n"
    "❄️ **走弱与异常**\n"
    "- 只列最值得注意的 2～3 个弱势或急跌标的，区分持续走弱、上涨后回吐、下跌后反弹。\n"
    "📍 **阶段与位置**\n"
    "- 只列 2～3 个处于典型阶段的标的，用 MA30、年位、距一年高点解释，不重复涨跌榜。\n"
    "🎯 **行动优先级**\n"
    "- 第一行写“优先观察：…”，第二行写“保持不动：…”，说明理由；不预测、不报买卖点。\n"
    "硬性要求：总字数 260～380 字；每条一行；先结论后证据；"
    "不要逐个复述全部标的，不要使用空泛措辞，不要重复同一个数字；最后一行写免责声明。"
)


SYSTEM_PROMPTS = {
    "daily": (
        "你是个人长线投资助手。根据给定的持仓行情事实写中文日报点评。\n"
        "优先判断近1日、1周、1月是否同向，以及板块相对强弱；"
        "一年位置只用于判断所处阶段。只基于数据，不编造新闻、舆情或基本面。"
    )
    + _SCAN_FORMAT,
    "weekly": (
        "你是个人长线投资助手。根据给定的持仓行情事实写中文周报点评。\n"
        "优先判断近1周、1月、3月的趋势延续或反转，比较科技、宽基和行业轮动。"
        "只基于数据，不编造新闻、舆情或基本面。"
    )
    + _SCAN_FORMAT,
    "monthly": (
        "你是个人长线投资助手。根据给定的持仓行情事实写中文月报点评。\n"
        "优先判断近1月、3月、1年的趋势层级，比较板块轮动与历史位置。"
        "只基于数据，不编造新闻、舆情或基本面。"
    )
    + _SCAN_FORMAT,
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


def _build_tables(kind: str, bundles: list[QuoteBundle]) -> list[dict]:
    """One image table per theme, ranked by the report's main recent horizon."""
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
        rank_days = 7 if kind == "weekly" else 30
        rank_values = {
            id(b): change_by_calendar_days(b.hist, b.price, rank_days) for b in items
        }
        items = sorted(
            items,
            key=lambda b: (
                rank_values[id(b)] is not None,
                rank_values[id(b)]
                if rank_values[id(b)] is not None
                else float("-inf"),
            ),
            reverse=True,
        )
        label = THEME_LABELS.get(theme, theme or "其他")
        rows = [_row_for(kind, b) for b in items]
        for i in range(0, len(rows), 10):
            chunk = rows[i : i + 10]
            horizon = "近1周" if kind == "weekly" else "近1月"
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


def _stage_label(bundle: QuoteBundle) -> str:
    """Compact, deterministic path label to prevent vague model commentary."""
    week = change_by_calendar_days(bundle.hist, bundle.price, 7)
    month = change_by_calendar_days(bundle.hist, bundle.price, 30)
    if week is None or month is None:
        return "数据不足"
    ma_dev = (bundle.price - bundle.ma30) / bundle.ma30 * 100
    if month >= 0 and week >= 0:
        return "持续走强" if ma_dev >= 0 else "反弹但仍在MA30下"
    if month >= 0 and week < 0:
        return "月内上涨、近周回吐"
    if month < 0 and week >= 0:
        return "月内偏弱、近周反弹"
    return "持续走弱" if ma_dev < 0 else "回调但仍在MA30上"


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
        position, dd = _year_position(b)
        theme = THEME_LABELS.get(b.theme, b.theme or "其他")
        chg = (
            f"1日={_fmt_pct(change_by_trading_days(b.hist, b.price, 1))}, "
            f"1周={_fmt_pct(change_by_calendar_days(b.hist, b.price, 7))}, "
            f"1月={_fmt_pct(change_by_calendar_days(b.hist, b.price, 30))}, "
            f"3月={_fmt_pct(change_by_calendar_days(b.hist, b.price, 91))}, "
            f"1年={_fmt_pct(change_by_calendar_days(b.hist, b.price, 365))}"
        )
        lines.append(
            f"- [{theme}/{b.market}] {b.name}({b.code}) 价={b.price:.2f} "
            f"阶段={_stage_label(b)} MA30偏离={ma_dev:+.2f}% "
            f"年位={_fmt_position(position)} 距一年高点={_fmt_pct(dd)} | {chg}"
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


_COMMENT_HEADINGS = {
    "一句话结论": ("🔎", "blue"),
    "领涨与强势": ("🔥", "orange"),
    "走弱与异常": ("❄️", "red"),
    "阶段与位置": ("📍", "purple"),
    "行动优先级": ("🎯", "blue"),
}


def _style_comment(comment: str) -> str:
    """Apply consistent visual hierarchy even if the model varies markdown."""
    output: list[str] = []
    for raw in (comment or "").splitlines():
        line = raw.strip()
        matched = False
        for heading, (icon, color) in _COMMENT_HEADINGS.items():
            if heading in line and len(re.sub(r"[*#：:\s]", "", line)) <= len(heading) + 3:
                output.append(f"<font color='{color}'>**{icon} {heading}**</font>")
                matched = True
                break
        if not matched:
            output.append(line)
    return "\n".join(output).strip()


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
            md = _style_comment(comment)
            if dry_run:
                log.info("[dry-run] %s\n%s", title, md)
            else:
                channel = send_alert(title=title, markdown=md)
                log.info("已通过 %s 发送 %s", channel, title)
            sent += 1

    log.info("%s完成：发送 %d 组，全失败市场 %d", REPORT_TITLES[kind], sent, fail_markets)
    return 1 if sent == 0 else 0
