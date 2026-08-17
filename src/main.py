#!/usr/bin/env python3
"""Entry: MA30 touch alerts + daily price digest."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.digest import run_digest
from src.fetch_quotes import build_snapshot
from src.notify import send_alert, send_test_ping
from src.signals import is_touching_ma30
from src.state import AlertState

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("ma_monitor")


def load_watchlist(path: Path) -> tuple[float, list[dict]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    touch_pct = float(data.get("touch_pct", 0.5))
    stocks = data.get("stocks") or []
    if not stocks:
        raise ValueError(f"watchlist 为空: {path}")
    return touch_pct, stocks


def run_ma_scan(watchlist_path: Path, dry_run: bool = False, force: bool = False) -> int:
    load_dotenv(ROOT / ".env")
    touch_pct, stocks = load_watchlist(watchlist_path)
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
            signal = is_touching_ma30(snap, touch_pct)
            log.info(
                "[%s] %s(%s) price=%.2f ma30=%.2f dev=%+.2f%% touch=±%.2f%%",
                market.upper(),
                snap.name,
                snap.code,
                snap.price,
                snap.ma30,
                (snap.price - snap.ma30) / snap.ma30 * 100,
                touch_pct,
            )
            if signal is None:
                continue
            if not force and state.already_alerted(state_key):
                log.info("今日已提醒过 %s，跳过", state_key)
                continue

            text = signal.message
            if dry_run:
                log.info("[dry-run] 将发送:\n%s", text)
            else:
                channel = send_alert(text)
                log.info("已通过 %s 发送提醒: %s", channel, state_key)
                state.mark_alerted(state_key)
            alerts += 1
        except Exception as exc:
            errors += 1
            log.exception("处理 %s 失败: %s", state_key, exc)

    log.info("完成：触发 %d 条，失败 %d 只", alerts, errors)
    return 1 if errors and alerts == 0 else 0


def main() -> None:
    parser = argparse.ArgumentParser(description="股价监控：MA30 触及 + 每日涨跌日报")
    parser.add_argument("-c", "--config", default=str(ROOT / "watchlist.yaml"))
    parser.add_argument("--dry-run", action="store_true", help="只计算，不发 Webhook")
    parser.add_argument("--force", action="store_true", help="MA30 忽略同日去重")
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
