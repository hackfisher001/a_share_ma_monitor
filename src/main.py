#!/usr/bin/env python3
"""Entry: MA30 touch alerts + daily price digest."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

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
from src.notify import send_alert, send_test_ping
from src.ops import heartbeat_markdown, snapshot_state
from src.payday import PaydayConfig, is_payday, payday_markdown
from src.price_context import (
    RECENT_PERCENTILE,
    build_price_context,
    compose_alert_markdown,
    detect_recent_pullback,
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
    pending_escalate_pct: float = DEFAULT_ESCALATE_PCT
    pending_ttl_days: int = DEFAULT_PENDING_TTL_DAYS


def _intraday_levels(raw: dict | None) -> tuple[float, ...]:
    raw = raw or {}
    if raw.get("enabled") is False:
        return ()
    levels = raw.get("levels") or DEFAULT_INTRADAY_DIP_LEVELS
    return tuple(sorted({abs(float(x)) for x in levels}))


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
) -> str:
    markdown = compose_alert_markdown(headline, ctx, extra)
    if dry_run:
        log.info("[dry-run] %s 将发送:\n%s", title, markdown)
        return "dry-run"
    keys = _sparkline_keys(title, ctx.stage, ctx.spark)
    return send_alert(
        markdown=markdown,
        title=title,
        image_keys=keys or None,
        prefer_images=False,
    )


def drawdown_levels_for(item: dict, config: ScanConfig) -> tuple[float, ...]:
    """Drop bands a T symbol already covers, so one dip is not reported twice.

    A T 低吸 alert fires at exactly the shallow drawdown the observe bands watch,
    but carries the sleeve context, so it supersedes them.
    """
    if not item.get("swing_t"):
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
            log.info(
                "[%s] %s(%s) price=%.2f ma30=%.2f dev=%+.2f%% 距一年高点=%+.2f%% 阶段=%s",
                market.upper(),
                snap.name,
                snap.code,
                snap.price,
                snap.ma30,
                (snap.price - snap.ma30) / snap.ma30 * 100,
                drawdown,
                ctx.stage,
            )

            # A same-session slide is the one thing that must interrupt you, so
            # it runs ahead of everything else and ignores action_only.
            if config.intraday_dip_levels:
                # Keyed on the quote's own session date, not the local day.
                already_dip = (
                    () if force else state.intraday_fired_levels(state_key, snap.as_of)
                )
                for dip in crossed_intraday_dip_levels(
                    snap, config.intraday_dip_levels, already_fired=already_dip
                ):
                    channel = _dispatch_alert(
                        title=dip.title,
                        headline=dip.message,
                        ctx=ctx,
                        dry_run=dry_run,
                    )
                    if not dry_run:
                        log.info(
                            "已通过 %s 发送急跌提醒: %s -%g%%",
                            channel,
                            state_key,
                            dip.threshold_pct,
                        )
                        state.mark_intraday_level(
                            state_key, dip.threshold_pct, snap.as_of
                        )
                    alerts += 1

            # Swing-T is opt-in per symbol; the sleeve is bookkeeping, so --force
            # must not replay it or the recorded average cost would drift.
            if item.get("swing_t"):
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
                    channel = _dispatch_alert(
                        title="走势提示 · MA30",
                        headline=ma_signal.message,
                        ctx=ctx,
                        dry_run=dry_run,
                    )
                    if not dry_run:
                        log.info("已通过 %s 发送 MA30 提醒: %s", channel, state_key)
                        state.mark_alerted(state_key)
                    alerts += 1
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
                for dd_signal in (dd_signals if not config.action_only else []):
                    level = dd_signal.threshold_pct
                    title = (
                        f"回撤观察 · 超过{level:g}%"
                        if level >= 30
                        else f"回撤观察 · {level:g}%"
                    )
                    channel = _dispatch_alert(
                        title=title,
                        headline=dd_signal.message,
                        ctx=ctx,
                        dry_run=dry_run,
                    )
                    if not dry_run:
                        log.info(
                            "已通过 %s 发送回撤提醒: %s -%g%%",
                            channel,
                            state_key,
                            level,
                        )
                        state.mark_drawdown_level(state_key, level)
                    alerts += 1

            if config.recent_pullback:
                hit, reason = detect_recent_pullback(
                    bundle.hist,
                    bundle.price,
                    percentile=config.recent_pullback_percentile,
                )
                cooldown_key = f"recent:{state_key}"
                if hit and not config.action_only and (force or not state.in_cooldown(cooldown_key, config.recent_pullback_cooldown_days)):
                    headline = (
                        f"**{snap.name}({snap.code})** 出现近期异常回撤\n"
                        f"现价 **{snap.price:.2f}**　日线截至 {snap.as_of}"
                    )
                    channel = _dispatch_alert(
                        title="近期异常回撤",
                        headline=headline,
                        ctx=ctx,
                        extra=f"**触发：** {reason}",
                        dry_run=dry_run,
                    )
                    if not dry_run:
                        log.info("已通过 %s 发送近期回撤提醒: %s", channel, state_key)
                        state.mark_cooldown(cooldown_key)
                    alerts += 1
                elif hit:
                    log.info("近期回撤仍在冷却期，跳过 %s", state_key)
        except Exception as exc:
            errors += 1
            log.exception("处理 %s 失败: %s", state_key, exc)

    log.info("完成：触发 %d 条，失败 %d 只", alerts, errors)
    if not dry_run:
        state.record_scan_health(checked=len(stocks), errors=errors, alerts=alerts)
    # Any fetch failure is a real failure: a partially blind scan used to exit 0
    # simply because some other symbol happened to alert.
    return 1 if errors else 0


def run_payday(watchlist_path: Path, dry_run: bool = False) -> int:
    raw = yaml.safe_load(watchlist_path.read_text(encoding="utf-8")) or {}
    config = PaydayConfig.from_dict(raw.get("payday"))
    due, bonus = is_payday(config)
    if not due:
        log.info("今天不是发薪日，跳过")
        return 0

    rows: list[dict] = []
    for item in raw.get("stocks") or []:
        code = str(item.get("code", "")).strip()
        if not code:
            continue
        try:
            bundle = build_bundle(
                code,
                name=str(item.get("name") or "").strip(),
                market=str(item.get("market") or "cn").strip().lower(),
            )
            ctx = build_price_context(bundle.hist, bundle.price, bundle.ma30)
            rows.append(
                {
                    "name": bundle.name,
                    "code": bundle.code,
                    "year_dd": ctx.year_dd,
                    "stage": ctx.stage,
                }
            )
        except Exception as exc:
            log.warning("发薪日提醒跳过 %s: %s", code, exc)
    rows.sort(key=lambda r: (r["year_dd"] is None, r["year_dd"] or 0.0))

    markdown = payday_markdown(rows, bonus=bonus)
    if dry_run:
        log.info("[dry-run] 发薪日提醒:\n%s", markdown)
        return 0
    channel = send_alert(
        markdown=markdown,
        title="发薪日 · 按计划买入",
        prefer_images=False,
    )
    log.info("已通过 %s 发送发薪日提醒", channel)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="股价监控：MA30 + 多档回撤 + 日/周/月报（DeepSeek 点评）"
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
        help="若今天是发薪日则推送买入提醒（非发薪日静默退出）",
    )
    parser.add_argument(
        "--digest",
        action="store_true",
        help="兼容旧参数：等价于 --report daily",
    )
    parser.add_argument(
        "--report",
        choices=["daily", "weekly", "monthly"],
        help="发送持仓报告：daily / weekly / monthly（含 DeepSeek 点评）",
    )
    parser.add_argument(
        "--market",
        default="all",
        help="报告市场过滤：all / cn / hk / us（可逗号分隔）",
    )
    parser.add_argument(
        "--full-report",
        action="store_true",
        help="日报发送完整标的表；默认只发送行动清单与强弱摘要",
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
        load_dotenv(ROOT / ".env")
        raise SystemExit(run_payday(Path(args.config), dry_run=args.dry_run))

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
        raise SystemExit(
            run_report(
                stocks,
                kind=report_kind,
                dry_run=args.dry_run,
                markets=markets,
                compact=(report_kind == "daily" and not args.full_report),
                action_markdown=action_md if report_kind == "daily" else None,
                ledger=ledger,
            )
        )

    raise SystemExit(
        run_ma_scan(Path(args.config), dry_run=args.dry_run, force=args.force)
    )


if __name__ == "__main__":
    main()
