#!/usr/bin/env python3
"""Entry: MA30 touch alerts + daily price digest."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.fetch_quotes import QuoteBundle, build_bundle
from src.action_digest import (
    DEFAULT_ESCALATE_PCT,
    DEFAULT_PENDING_TTL_DAYS,
    action_payload,
    action_summary_markdown,
    pending_key,
    should_realert,
)
from src.alert_batch import ScanAlert, dedupe_for_display, sort_alerts
from src.dip_bands import (
    DEFAULT_MAX_ABS_PCT,
    DEFAULT_MIN_ABS_PCT,
    DEFAULT_SIGMA_LEVELS,
    calibrated_dip_levels,
    daily_sigma,
    describe_band,
    sigma_multiple,
)
from src.notify import send_alert, send_test_ping
from src.etf_premium import lookup_premium
from src.relative import DEFAULT_BENCHMARKS, compare_to_benchmark
from src.ops import heartbeat_markdown, snapshot_state
from src.price_context import (
    RECENT_PERCENTILE,
    build_price_context,
    compose_alert_markdown,
    detect_recent_pullback,
    should_realert_pullback,
)
from src.reports import run_report
from src.signals import (
    DEFAULT_DRAWDOWN_LEVELS,
    DEFAULT_INTRADAY_DIP_LEVELS,
    QuoteSnapshot,
    crossed_drawdown_levels,
    crossed_intraday_dip_levels,
    is_touching_ma30,
)
from src.state import AlertState
from src.t_signals import TConfig, TSleeve, evaluate_t, sleeve_status_line
from src.trades import TradeLedger

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("ma_monitor")


@dataclass
class ScanConfig:
    touch_pct: float
    drawdown_levels: tuple[float, ...]
    drawdown_markets: tuple[str, ...]
    # When drawdown recovers above -reset_pct, clear fired bands for a new episode.
    drawdown_reset_pct: float
    recent_pullback: bool
    recent_pullback_percentile: float
    recent_pullback_cooldown_days: int
    swing_t: TConfig
    action_only: bool = True
    # Same-session slide bands. Deliberately exempt from action_only: they are
    # only useful while you can still trade on them.
    intraday_dip_levels: tuple[float, ...] = DEFAULT_INTRADAY_DIP_LEVELS
    # "sigma" sizes the bands off each symbol's own volatility; "absolute" keeps
    # the flat ladder. See src/dip_bands.py for why sigma is the default.
    intraday_dip_mode: str = "sigma"
    intraday_sigma_levels: tuple[float, ...] = DEFAULT_SIGMA_LEVELS
    intraday_min_abs_pct: float = DEFAULT_MIN_ABS_PCT
    intraday_max_abs_pct: float = DEFAULT_MAX_ABS_PCT
    benchmarks: dict[str, str] = field(
        default_factory=lambda: dict(DEFAULT_BENCHMARKS)
    )
    pending_escalate_pct: float = DEFAULT_ESCALATE_PCT
    pending_ttl_days: int = DEFAULT_PENDING_TTL_DAYS


def _intraday_levels(raw: dict | None) -> tuple[float, ...]:
    raw = raw or {}
    if raw.get("enabled") is False:
        return ()
    levels = raw.get("levels") or DEFAULT_INTRADAY_DIP_LEVELS
    return tuple(sorted({abs(float(x)) for x in levels}))


def _sigma_levels(raw: dict | None) -> tuple[float, ...]:
    raw = raw or {}
    levels = raw.get("sigma_levels") or DEFAULT_SIGMA_LEVELS
    return tuple(sorted({abs(float(x)) for x in levels}))


def _benchmarks(raw: dict | None) -> dict[str, str]:
    if not raw:
        return dict(DEFAULT_BENCHMARKS)
    return {
        str(k).strip().lower(): str(v).strip().upper()
        for k, v in raw.items()
        if str(v or "").strip()
    }


def load_watchlist(path: Path) -> tuple[ScanConfig, list[dict]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    markets = data.get("drawdown_markets") or ["cn", "us"]
    raw_levels = data.get("drawdown_levels")
    if raw_levels:
        levels = tuple(sorted({abs(float(x)) for x in raw_levels}))
    elif data.get("drawdown_pct") is not None:
        # Backward compatible with the old single-threshold config.
        levels = (abs(float(data["drawdown_pct"])),)
    else:
        levels = DEFAULT_DRAWDOWN_LEVELS
    config = ScanConfig(
        touch_pct=float(data.get("touch_pct", 0.5)),
        drawdown_levels=levels,
        drawdown_markets=tuple(str(m).strip().lower() for m in markets),
        drawdown_reset_pct=abs(float(data.get("drawdown_reset_pct", 3))),
        recent_pullback=bool(data.get("recent_pullback", True)),
        recent_pullback_percentile=float(
            data.get("recent_pullback_percentile", RECENT_PERCENTILE)
        ),
        recent_pullback_cooldown_days=int(data.get("recent_pullback_cooldown_days", 7)),
        swing_t=TConfig.from_dict(data.get("swing_t")),
        action_only=bool((data.get("notifications") or {}).get("action_only", True)),
        intraday_dip_levels=_intraday_levels(data.get("intraday_dip")),
        intraday_dip_mode=str(
            (data.get("intraday_dip") or {}).get("mode", "sigma")
        ).strip().lower(),
        intraday_sigma_levels=_sigma_levels(data.get("intraday_dip")),
        intraday_min_abs_pct=abs(
            float((data.get("intraday_dip") or {}).get("min_abs_pct", DEFAULT_MIN_ABS_PCT))
        ),
        intraday_max_abs_pct=abs(
            float((data.get("intraday_dip") or {}).get("max_abs_pct", DEFAULT_MAX_ABS_PCT))
        ),
        benchmarks=_benchmarks(data.get("benchmarks")),
        pending_escalate_pct=abs(
            float((data.get("swing_t") or {}).get("escalate_pct", DEFAULT_ESCALATE_PCT))
        ),
        pending_ttl_days=int(
            (data.get("swing_t") or {}).get("pending_ttl_days", DEFAULT_PENDING_TTL_DAYS)
        ),
    )
    stocks = data.get("stocks") or []
    if not stocks:
        raise ValueError(f"watchlist 为空: {path}")
    return config, stocks


def _fmt_signed(value: float | None) -> str:
    return "—" if value is None else f"{value:+.2f}%"


def _snapshot_from_bundle(bundle: QuoteBundle) -> QuoteSnapshot:
    return QuoteSnapshot(
        code=bundle.code,
        name=bundle.name,
        price=bundle.price,
        ma30=bundle.ma30,
        as_of=bundle.as_of,
        history_rows=len(bundle.hist),
        high_252=bundle.high_252,
        live=bundle.live,
    )


def _sparkline_keys(title: str, subtitle: str, spark: dict) -> list[str]:
    if not spark.get("values"):
        return []
    try:
        from src.feishu_media import feishu_app_configured, upload_image_png
        from src.table_image import render_sparkline_card_png

        if not feishu_app_configured():
            return []
        png = render_sparkline_card_png(title=title, subtitle=subtitle, spark=spark)
        return [upload_image_png(png)]
    except Exception as exc:
        log.warning("走势图上传失败: %s", exc)
        return []


def _dispatch_alert(
    *,
    title: str,
    headline: str,
    ctx,
    extra: str = "",
    dry_run: bool,
    chart_title: str | None = None,
) -> str:
    markdown = compose_alert_markdown(headline, ctx, extra)
    if dry_run:
        log.info("[dry-run] %s 将发送:\n%s", title, markdown)
        return "dry-run"
    # Chart caption is the stock name; the alert title is too long for the PNG.
    keys = _sparkline_keys(chart_title or title, ctx.stage, ctx.spark)
    return send_alert(
        markdown=markdown,
        title=title,
        image_keys=keys or None,
        prefer_images=False,
    )


def dip_levels_for(
    item: dict,
    config: ScanConfig,
    hist: pd.DataFrame,
) -> tuple[tuple[float, ...], str]:
    """Slide bands for one symbol, plus a short label of where they came from.

    Priority: an explicit per-symbol override, then σ calibration, then the flat
    ladder. The fallback matters for a freshly listed symbol whose history is
    too short to measure σ on — it stays covered instead of going silent.
    """
    if not config.intraday_dip_levels and config.intraday_dip_mode != "sigma":
        return (), "关闭"
    override = item.get("dip_levels")
    if override:
        return tuple(sorted({abs(float(x)) for x in override})), "自定义"
    if config.intraday_dip_mode == "sigma":
        bands = calibrated_dip_levels(
            hist,
            sigma_levels=config.intraday_sigma_levels,
            min_abs_pct=config.intraday_min_abs_pct,
            max_abs_pct=config.intraday_max_abs_pct,
        )
        if bands:
            return bands, "σ校准"
    return config.intraday_dip_levels, "固定档"


def _flush_scan_alerts(
    alerts: list[ScanAlert],
    *,
    peers: dict[str, tuple[str, float | None, pd.DataFrame | None]],
    benchmarks: dict[str, str],
    dry_run: bool,
    premium_codes: set[str] | None = None,
) -> int:
    """Send each alert as its own card, sparkline included.

    Same-session slides used to be collapsed into one aggregate card. That
    saved noise on a market-wide day, but buried the one-year path chart that
    actually answers "high-level dump or flat then dump?" — so each symbol now
    speaks alone. Crossed bands on the *same* symbol are still collapsed to the
    deepest one so a -5σ day does not produce three nearly identical cards.
    """
    if not alerts:
        return 0

    premium_codes = premium_codes or set()
    for alert in alerts:
        entry = peers.get(alert.key)
        alert.relative = compare_to_benchmark(
            market=alert.market,
            code=alert.code,
            change_pct=alert.change_pct,
            symbol_hist=entry[2] if entry else None,
            peers=peers,
            benchmarks=benchmarks,
        )

    display = dedupe_for_display(alerts)
    for alert in sort_alerts(display):
        parts = [p for p in (alert.extra,) if p]
        if alert.relative is not None:
            parts.append(alert.relative.markdown_line())
        if alert.code.zfill(6) in premium_codes:
            quote = lookup_premium(alert.code)
            if quote is not None:
                parts.append(quote.markdown_line())
        channel = _dispatch_alert(
            title=alert.title,
            headline=alert.headline,
            ctx=alert.ctx,
            extra="\n".join(parts),
            dry_run=dry_run,
            chart_title=alert.name,
        )
        if not dry_run:
            log.info("已通过 %s 发送 %s: %s", channel, alert.title, alert.key)

    # Every crossed band is committed — including ones hidden by the display
    # dedupe — so they cannot re-fire later in the same session.
    if not dry_run:
        for alert in alerts:
            if alert.commit is not None:
                alert.commit()
    return len(alerts)


def drawdown_levels_for(item: dict, config: ScanConfig) -> tuple[float, ...]:
    """Drop bands a T symbol already covers, so one dip is not reported twice.

    A T 低吸 alert fires at exactly the shallow drawdown the observe bands watch,
    but carries the sleeve context, so it supersedes them.
    """
    if not (config.swing_t.enabled and item.get("swing_t")):
        return config.drawdown_levels
    floor = config.swing_t.buy_drawdown_pct
    return tuple(level for level in config.drawdown_levels if level > floor)


def _run_swing_t(
    *,
    item: dict,
    snap: QuoteSnapshot,
    ctx,
    config: ScanConfig,
    state: AlertState,
    state_key: str,
    dry_run: bool,
) -> int:
    """Advance one symbol's T sleeve, alert on transitions, persist the result."""
    sleeve_key = f"t:{state_key}"
    sleeve = TSleeve.from_dict(state.t_sleeve(sleeve_key))
    before = sleeve.to_dict()

    signal = evaluate_t(snap, sleeve, config.swing_t, commit=False)
    sent = 0
    if signal is not None:
        side = "BUY" if signal.title.endswith("低吸") else "SELL"
        market = str(item.get("market") or "cn").lower()
        key = pending_key(market, snap.code, side)
        repeat, why = should_realert(
            state.pending_action(key),
            price=snap.price,
            side=side,
            escalate_pct=config.pending_escalate_pct,
            ttl_days=config.pending_ttl_days,
        )
        if not repeat:
            log.info("  已有待确认%s，跳过重复提醒", "买入" if side == "BUY" else "卖出")
            return 0
        escalation = f"\n\n⚠️ **重复提醒：** {why}" if why else ""
        prompt = (
            f"{escalation}"
            f"\n\n👉 **现在要做：{'买入' if side == 'BUY' else '卖出'}机动仓**"
            f"（底仓不动）。执行后记录："
            f"`python -m src.main --record-trade {side} {snap.code} 数量 成交价 --market {market}`\n"
            "不记录就算没做，系统会继续把它放在待办里。"
        )
        channel = _dispatch_alert(
            title=signal.title,
            headline=signal.message + prompt,
            ctx=ctx,
            dry_run=dry_run,
        )
        if not dry_run:
            log.info("已通过 %s 发送%s: %s", channel, signal.title, state_key)
        sent = 1
        if not dry_run:
            state.save_pending_action(
                key,
                action_payload(
                    market=market,
                    code=snap.code,
                    name=snap.name,
                    side=side,
                    price=snap.price,
                    text=signal.title,
                    kind="t_buy" if side == "BUY" else "t_sell",
                ),
            )

    after = sleeve.to_dict()
    if after != before:
        # Re-arming happens without an alert, so persist on any state change.
        if dry_run:
            log.info("[dry-run] 机动仓状态将更新为 %s", after)
        else:
            state.save_t_sleeve(sleeve_key, after)
    log.info("  机动仓 %s", sleeve_status_line(snap.name, snap.code, sleeve, snap.price))
    return sent


def run_ma_scan(watchlist_path: Path, dry_run: bool = False, force: bool = False) -> int:
    load_dotenv(ROOT / ".env")
    config, stocks = load_watchlist(watchlist_path)
    state_file = os.getenv("STATE_FILE", "data/alert_state.json")
    state_path = Path(state_file)
    if not state_path.is_absolute():
        state_path = ROOT / state_path
    state = AlertState(state_path)

    alerts = 0
    errors = 0
    # Alerts are held until the scan ends so one market-wide selloff arrives as
    # one message, and so the benchmark comparison can use symbols fetched later
    # in the same pass.
    pending: list[ScanAlert] = []
    peers: dict[str, tuple[str, float | None, pd.DataFrame | None]] = {}
    premium_codes = {
        str(s.get("code", "")).strip().zfill(6)
        for s in stocks
        if s.get("premium") and str(s.get("market") or "cn").lower() == "cn"
    }

    for item in stocks:
        code = str(item.get("code", "")).strip()
        name = str(item.get("name") or "").strip()
        market = str(item.get("market") or "cn").strip().lower()
        if not code:
            continue
        state_key = f"{market}:{code}"
        try:
            bundle = build_bundle(code, name=name, market=market)
            snap = _snapshot_from_bundle(bundle)
            ctx = build_price_context(bundle.hist, bundle.price, bundle.ma30)
            # The live feed's own 昨收 beats deriving it from history, which can
            # have gaps that would fake a slide.
            snap.change_pct = (
                (bundle.price / bundle.prev_close - 1.0) * 100.0
                if bundle.prev_close and bundle.prev_close > 0
                else ctx.day1
            )
            drawdown = (
                (snap.price / snap.high_252 - 1) * 100 if snap.high_252 > 0 else 0.0
            )
            sigma = daily_sigma(bundle.hist)
            peers[state_key] = (snap.name, snap.change_pct, bundle.hist)
            premium = (
                lookup_premium(snap.code) if snap.code.zfill(6) in premium_codes else None
            )
            log.info(
                "[%s] %s(%s) price=%.2f ma30=%.2f dev=%+.2f%% 距一年高点=%+.2f%% σ=%s%s 阶段=%s",
                market.upper(),
                snap.name,
                snap.code,
                snap.price,
                snap.ma30,
                (snap.price - snap.ma30) / snap.ma30 * 100,
                drawdown,
                f"{sigma:.2f}%" if sigma is not None else "—",
                f" 溢价={premium.premium_pct:+.1f}%" if premium else "",
                ctx.stage,
            )

            # A same-session slide is the one thing that must interrupt you, so
            # it runs ahead of everything else and ignores action_only.
            dip_levels, _ = dip_levels_for(item, config, bundle.hist)
            if dip_levels:
                # Keyed on the quote's own session date, not the local day.
                already_dip = (
                    () if force else state.intraday_fired_levels(state_key, snap.as_of)
                )
                for dip in crossed_intraday_dip_levels(
                    snap, dip_levels, already_fired=already_dip
                ):
                    band = describe_band(dip.threshold_pct, sigma)
                    multiple = sigma_multiple(snap.change_pct, sigma)
                    pending.append(
                        ScanAlert(
                            kind="dip",
                            market=market,
                            code=snap.code,
                            name=snap.name,
                            title=f"急跌提醒 · {band}",
                            headline=dip.compact_headline(multiple),
                            ctx=ctx,
                            summary_head=(
                                f"{snap.change_pct:+.2f}%"
                                + (f"（{multiple:.1f}σ）" if multiple else "")
                            ),
                            change_pct=snap.change_pct,
                            sigma=sigma,
                            band_pct=dip.threshold_pct,
                            severity=multiple or abs(snap.change_pct or 0.0),
                            commit=(
                                lambda k=state_key,
                                lv=dip.threshold_pct,
                                s=snap.as_of: state.mark_intraday_level(k, lv, s)
                            ),
                        )
                    )

            # Swing-T is off globally (see watchlist.yaml header) but the code
            # path stays so an existing sleeve can still be read and closed out.
            # The sleeve is bookkeeping, so --force must not replay it or the
            # recorded average cost would drift.
            if config.swing_t.enabled and item.get("swing_t"):
                alerts += _run_swing_t(
                    item=item,
                    snap=snap,
                    ctx=ctx,
                    config=config,
                    state=state,
                    state_key=state_key,
                    dry_run=dry_run,
                )

            ma_signal = is_touching_ma30(snap, config.touch_pct)
            if ma_signal is not None and not config.action_only:
                if force or not state.already_alerted(state_key):
                    pending.append(
                        ScanAlert(
                            kind="ma30",
                            market=market,
                            code=snap.code,
                            name=snap.name,
                            title="走势提示 · MA30",
                            headline=ma_signal.message,
                            ctx=ctx,
                            summary_head=(
                                f"贴近 MA30（偏离 {ma_signal.deviation_pct:+.2f}%）"
                            ),
                            change_pct=snap.change_pct,
                            sigma=sigma,
                            severity=sigma_multiple(snap.change_pct, sigma) or 0.0,
                            commit=(
                                lambda k=state_key: state.mark_alerted(k)
                            ),
                        )
                    )
                else:
                    log.info("今日已提醒过 %s，跳过", state_key)

            # Multi-level 1-year drawdown observe alerts (each band once per episode).
            if market in config.drawdown_markets:
                if drawdown > -config.drawdown_reset_pct:
                    state.clear_drawdown_levels(state_key)
                already = () if force else state.drawdown_fired_levels(state_key)
                dd_signals = crossed_drawdown_levels(
                    snap, drawdown_levels_for(item, config), already_fired=already
                )
                for dd_signal in dd_signals:
                    level = dd_signal.threshold_pct
                    if config.action_only:
                        # Record the crossing even while muted. Otherwise the
                        # episode state stays empty and turning action_only off
                        # would replay every band already breached — a symbol
                        # down 22% would fire 5/10/15/20 at once.
                        if not dry_run:
                            state.mark_drawdown_level(state_key, level)
                        continue
                    title = (
                        f"回撤观察 · 超过{level:g}%"
                        if level >= 30
                        else f"回撤观察 · {level:g}%"
                    )
                    pending.append(
                        ScanAlert(
                            kind="drawdown",
                            market=market,
                            code=snap.code,
                            name=snap.name,
                            title=title,
                            headline=dd_signal.message,
                            ctx=ctx,
                            summary_head=(
                                f"距一年高 {dd_signal.drawdown_pct:+.2f}%"
                                f"（跨过 -{level:g}% 档）"
                            ),
                            change_pct=snap.change_pct,
                            sigma=sigma,
                            severity=sigma_multiple(snap.change_pct, sigma) or 0.0,
                            commit=(
                                lambda k=state_key, lv=level: state.mark_drawdown_level(
                                    k, lv
                                )
                            ),
                        )
                    )

            # Deliberately outside action_only: a dip is only worth telling you
            # about while you can still place the order, so folding it into the
            # evening digest is the same as dropping it.
            if config.recent_pullback:
                hit, reason = detect_recent_pullback(
                    bundle.hist,
                    bundle.price,
                    percentile=config.recent_pullback_percentile,
                )
                if hit:
                    mark = {} if force else state.pullback_mark(state_key)
                    speak, escalation = should_realert_pullback(
                        mark,
                        price=snap.price,
                        cooldown_days=config.recent_pullback_cooldown_days,
                    )
                    if speak:
                        headline = (
                            f"**{snap.name}({snap.code})** 出现近期异常回撤\n"
                            f"现价 **{snap.price:.2f}**　日线截至 {snap.as_of}"
                        )
                        extra = f"**触发：** {reason}"
                        if escalation:
                            extra += f"\n**升级：** {escalation}"
                        pending.append(
                            ScanAlert(
                                kind="pullback",
                                market=market,
                                code=snap.code,
                                name=snap.name,
                                title="近期异常回撤",
                                headline=headline,
                                ctx=ctx,
                                summary_head=(
                                    f"近3日 {_fmt_signed(ctx.day3)}"
                                    f"　距20日高 {_fmt_signed(ctx.dd20)}"
                                ),
                                extra=extra,
                                change_pct=snap.change_pct,
                                sigma=sigma,
                                severity=sigma_multiple(snap.change_pct, sigma) or 0.0,
                                commit=(
                                    lambda k=state_key, p=snap.price: state.mark_pullback(
                                        k, p
                                    )
                                ),
                            )
                        )
                    else:
                        log.info("近期回撤已提醒且未继续走弱，跳过 %s", state_key)
        except Exception as exc:
            errors += 1
            log.exception("处理 %s 失败: %s", state_key, exc)

    alerts += _flush_scan_alerts(
        pending,
        peers=peers,
        benchmarks=config.benchmarks,
        dry_run=dry_run,
        premium_codes=premium_codes,
    )

    log.info("完成：触发 %d 条，失败 %d 只", alerts, errors)
    if not dry_run:
        state.record_scan_health(checked=len(stocks), errors=errors, alerts=alerts)
    # Any fetch failure is a real failure: a partially blind scan used to exit 0
    # simply because some other symbol happened to alert.
    return 1 if errors else 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="股价监控：MA30 + 多档回撤 + 日/周/月报"
    )
    parser.add_argument("-c", "--config", default=str(ROOT / "watchlist.yaml"))
    parser.add_argument("--dry-run", action="store_true", help="只计算，不发 Webhook")
    parser.add_argument("--force", action="store_true", help="忽略去重与冷却期")
    parser.add_argument("--notify-test", action="store_true", help="飞书连通测试")
    parser.add_argument(
        "--heartbeat",
        action="store_true",
        help="发送每日巡检心跳，并备份状态文件",
    )
    parser.add_argument(
        "--payday",
        action="store_true",
        help=argparse.SUPPRESS,  # 已移除，保留参数以免旧 crontab 报错
    )
    parser.add_argument(
        "--digest",
        action="store_true",
        help="兼容旧参数：等价于 --report daily",
    )
    parser.add_argument(
        "--report",
        choices=["daily", "weekly", "monthly"],
        help="发送持仓报告：daily / weekly / monthly",
    )
    parser.add_argument(
        "--market",
        default="all",
        help="报告市场过滤：all / cn / hk / us（可逗号分隔）",
    )
    parser.add_argument(
        "--record-trade",
        nargs=4,
        metavar=("BUY|SELL", "CODE", "QUANTITY", "PRICE"),
        help="记录一笔已执行交易，并关闭对应待确认提醒",
    )
    args = parser.parse_args()

    if args.notify_test:
        load_dotenv(ROOT / ".env")
        channel = send_test_ping()
        log.info("测试消息已发送（%s）", channel)
        raise SystemExit(0)

    if args.payday:
        log.info("发薪日提醒已移除，忽略 --payday")
        raise SystemExit(0)

    if args.heartbeat:
        load_dotenv(ROOT / ".env")
        state_path = ROOT / os.getenv("STATE_FILE", "data/alert_state.json")
        trades_path = ROOT / "data" / "trades.csv"
        copied = snapshot_state([state_path, trades_path], ROOT / "data" / "backups")
        log.info("已备份 %d 个状态文件", len(copied))
        state = AlertState(state_path)
        markdown, healthy = heartbeat_markdown(
            state, pending_count=len(state.pending_actions())
        )
        if args.dry_run:
            log.info("[dry-run] 心跳内容:\n%s", markdown)
        else:
            send_alert(markdown=markdown, title="每日巡检心跳", prefer_images=False)
        raise SystemExit(0 if healthy else 1)

    if args.record_trade:
        side, code, quantity_raw, price_raw = args.record_trade
        market = args.market.split(",", 1)[0].strip().lower()
        if market in {"", "all"}:
            market = "cn" if str(code).strip().isdigit() else "us"
        ledger = TradeLedger(ROOT / "data" / "trades.csv")
        trade = ledger.record(
            market=market,
            code=code,
            side=side,
            quantity=float(quantity_raw),
            price=float(price_raw),
        )
        state = AlertState(ROOT / os.getenv("STATE_FILE", "data/alert_state.json"))
        matching = [
            action
            for action in state.pending_actions()
            if str(action.get("market", "")).lower() == trade.market
            and str(action.get("code", "")).upper() == trade.code
            and str(action.get("side", "")).upper() == trade.side
        ]
        # The sleeve changes only here, after an explicit execution record.
        for action in matching:
            sleeve_key = f"t:{trade.market}:{trade.code}"
            sleeve = TSleeve.from_dict(state.t_sleeve(sleeve_key))
            if action.get("kind") == "t_buy":
                sleeve.add(trade.price, as_of=trade.date)
                state.save_t_sleeve(sleeve_key, sleeve.to_dict())
            elif action.get("kind") == "t_sell":
                sleeve.close(as_of=trade.date)
                state.save_t_sleeve(sleeve_key, sleeve.to_dict())
        resolved = state.resolve_pending_actions(trade.market, trade.code, trade.side)
        log.info("已记录 %s %s %s @ %.4f；关闭 %d 条待确认提醒", trade.side, trade.code, trade.quantity, trade.price, resolved)
        raise SystemExit(0)

    report_kind = args.report
    if args.digest and not report_kind:
        report_kind = "daily"

    if report_kind:
        load_dotenv(ROOT / ".env")
        _, stocks = load_watchlist(Path(args.config))
        markets = None
        if args.market and args.market.lower() != "all":
            markets = [m.strip().lower() for m in args.market.split(",") if m.strip()]
        state_path = ROOT / os.getenv("STATE_FILE", "data/alert_state.json")
        ledger = TradeLedger(ROOT / "data" / "trades.csv")
        action_md = action_summary_markdown(AlertState(state_path), ledger)
        income_codes = {
            str(s.get("code", "")).strip() for s in stocks if s.get("income")
        }
        premium_codes = {
            str(s.get("code", "")).strip().zfill(6)
            for s in stocks
            if s.get("premium") and str(s.get("market") or "cn").lower() == "cn"
        }
        premiums = {}
        for code in premium_codes:
            quote = lookup_premium(code)
            if quote is not None:
                premiums[code] = quote
        # 行动清单挂在 A 股日报上；美股单独推送时不再重复发一遍。
        include_actions = report_kind == "daily" and (
            markets is None or "cn" in markets
        )
        raise SystemExit(
            run_report(
                stocks,
                kind=report_kind,
                dry_run=args.dry_run,
                markets=markets,
                action_markdown=action_md if include_actions else None,
                ledger=ledger,
                income_codes=income_codes,
                premiums=premiums,
            )
        )

    raise SystemExit(
        run_ma_scan(Path(args.config), dry_run=args.dry_run, force=args.force)
    )


if __name__ == "__main__":
    main()
