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

from src.digest import run_digest
from src.fetch_quotes import build_snapshot
from src.notify import send_alert, send_test_ping
from src.signals import is_deep_drawdown, is_touching_ma30
from src.state import AlertState

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("ma_monitor")


@dataclass
class ScanConfig:
    touch_pct: float
    drawdown_pct: float
    drawdown_markets: tuple[str, ...]
    drawdown_cooldown_days: int


def load_watchlist(path: Path) -> tuple[ScanConfig, list[dict]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    markets = data.get("drawdown_markets") or ["cn"]
    config = ScanConfig(
        touch_pct=float(data.get("touch_pct", 0.5)),
        drawdown_pct=float(data.get("drawdown_pct", 30)),
        drawdown_markets=tuple(str(m).strip().lower() for m in markets),
        drawdown_cooldown_days=int(data.get("drawdown_cooldown_days", 30)),
    )
    stocks = data.get("stocks") or []
    if not stocks:
        raise ValueError(f"watchlist 为空: {path}")
    return config, stocks


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
            snap = build_snapshot(code, name=name, market=market)
            drawdown = (
                (snap.price / snap.high_252 - 1) * 100 if snap.high_252 > 0 else 0.0
            )
            log.info(
                "[%s] %s(%s) price=%.2f ma30=%.2f dev=%+.2f%% 距一年高点=%+.2f%%",
                market.upper(),
                snap.name,
                snap.code,
                snap.price,
                snap.ma30,
                (snap.price - snap.ma30) / snap.ma30 * 100,
                drawdown,
            )

            ma_signal = is_touching_ma30(snap, config.touch_pct)
            if ma_signal is not None:
                if force or not state.already_alerted(state_key):
                    if dry_run:
                        log.info("[dry-run] MA30 将发送:\n%s", ma_signal.message)
                    else:
                        channel = send_alert(ma_signal.message, title="加仓提醒 · MA30")
                        log.info("已通过 %s 发送 MA30 提醒: %s", channel, state_key)
                        state.mark_alerted(state_key)
                    alerts += 1
                else:
                    log.info("今日已提醒过 %s，跳过", state_key)

            # Deep drawdown only for markets where the backtest supports it (A股).
            if market in config.drawdown_markets:
                dd_signal = is_deep_drawdown(snap, config.drawdown_pct)
                dd_key = f"dd:{state_key}"
                if dd_signal is not None:
                    if force or not state.in_cooldown(
                        dd_key, config.drawdown_cooldown_days
                    ):
                        if dry_run:
                            log.info("[dry-run] 深度回撤将发送:\n%s", dd_signal.message)
                        else:
                            channel = send_alert(
                                dd_signal.message, title="重仓提醒 · 深度回撤"
                            )
                            log.info("已通过 %s 发送回撤提醒: %s", channel, dd_key)
                            state.mark_cooldown(dd_key)
                        alerts += 1
                    else:
                        log.info(
                            "%s 仍在 %d 天冷却期内，跳过",
                            dd_key,
                            config.drawdown_cooldown_days,
                        )
        except Exception as exc:
            errors += 1
            log.exception("处理 %s 失败: %s", state_key, exc)

    log.info("完成：触发 %d 条，失败 %d 只", alerts, errors)
    return 1 if errors and alerts == 0 else 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="股价监控：MA30 触及 + A股深度回撤 + 每日涨跌日报"
    )
    parser.add_argument("-c", "--config", default=str(ROOT / "watchlist.yaml"))
    parser.add_argument("--dry-run", action="store_true", help="只计算，不发 Webhook")
    parser.add_argument("--force", action="store_true", help="忽略去重与冷却期")
    parser.add_argument("--notify-test", action="store_true", help="飞书连通测试")
    parser.add_argument(
        "--digest",
        action="store_true",
        help="发送每日股价日报（1日/5日/1周/1月/半年/1年）",
    )
    parser.add_argument(
        "--market",
        default="all",
        help="日报市场过滤：all / cn / hk / us（可逗号分隔）",
    )
    args = parser.parse_args()

    if args.notify_test:
        load_dotenv(ROOT / ".env")
        channel = send_test_ping()
        log.info("测试消息已发送（%s）", channel)
        raise SystemExit(0)

    if args.digest:
        load_dotenv(ROOT / ".env")
        _, stocks = load_watchlist(Path(args.config))
        markets = None
        if args.market and args.market.lower() != "all":
            markets = [m.strip().lower() for m in args.market.split(",") if m.strip()]
        raise SystemExit(
            run_digest(stocks, dry_run=args.dry_run, markets=markets)
        )

    raise SystemExit(
        run_ma_scan(Path(args.config), dry_run=args.dry_run, force=args.force)
    )


if __name__ == "__main__":
    main()
