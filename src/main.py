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
from src.notify import send_alert, send_test_ping
from src.price_context import (
    RECENT_PERCENTILE,
    build_price_context,
    compose_alert_markdown,
    detect_recent_pullback,
)
from src.reports import run_report
from src.signals import (
    DEFAULT_DRAWDOWN_LEVELS,
    QuoteSnapshot,
    crossed_drawdown_levels,
    is_touching_ma30,
)
from src.state import AlertState

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

            ma_signal = is_touching_ma30(snap, config.touch_pct)
            if ma_signal is not None:
                if force or not state.already_alerted(state_key):
                    channel = _dispatch_alert(
                        title="加仓提醒 · MA30",
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
                    snap, config.drawdown_levels, already_fired=already
                )
                for dd_signal in dd_signals:
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
                if hit and (force or not state.in_cooldown(cooldown_key, config.recent_pullback_cooldown_days)):
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
    return 1 if errors and alerts == 0 else 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="股价监控：MA30 + 多档回撤 + 日/周/月报（DeepSeek 点评）"
    )
    parser.add_argument("-c", "--config", default=str(ROOT / "watchlist.yaml"))
    parser.add_argument("--dry-run", action="store_true", help="只计算，不发 Webhook")
    parser.add_argument("--force", action="store_true", help="忽略去重与冷却期")
    parser.add_argument("--notify-test", action="store_true", help="飞书连通测试")
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
    args = parser.parse_args()

    if args.notify_test:
        load_dotenv(ROOT / ".env")
        channel = send_test_ping()
        log.info("测试消息已发送（%s）", channel)
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
        raise SystemExit(
            run_report(
                stocks,
                kind=report_kind,
                dry_run=args.dry_run,
                markets=markets,
            )
        )

    raise SystemExit(
        run_ma_scan(Path(args.config), dry_run=args.dry_run, force=args.force)
    )


if __name__ == "__main__":
    main()
