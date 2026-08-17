#!/usr/bin/env python3
"""Entry: scan watchlist, alert when price touches MA30."""

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


def run(watchlist_path: Path, dry_run: bool = False, force: bool = False) -> int:
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
        if not code:
            continue
        try:
            snap = build_snapshot(code, name=name)
            signal = is_touching_ma30(snap, touch_pct)
            log.info(
                "%s(%s) price=%.2f ma30=%.2f dev=%+.2f%% touch=±%.2f%%",
                snap.name,
                snap.code,
                snap.price,
                snap.ma30,
                (snap.price - snap.ma30) / snap.ma30 * 100,
                touch_pct,
            )
            if signal is None:
                continue
            if not force and state.already_alerted(snap.code):
                log.info("今日已提醒过 %s，跳过", snap.code)
                continue

            text = signal.message
            if dry_run:
                log.info("[dry-run] 将发送:\n%s", text)
            else:
                channel = send_alert(text)
                log.info("已通过 %s 发送提醒: %s", channel, snap.code)
            state.mark_alerted(snap.code)
            alerts += 1
        except Exception as exc:
            errors += 1
            log.exception("处理 %s 失败: %s", code, exc)

    log.info("完成：触发 %d 条，失败 %d 只", alerts, errors)
    return 1 if errors and alerts == 0 else 0


def main() -> None:
    parser = argparse.ArgumentParser(description="A股 30日均线触及监控（飞书通知）")
    parser.add_argument(
        "-c",
        "--config",
        default=str(ROOT / "watchlist.yaml"),
        help="watchlist 路径",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只计算信号，不发 Webhook",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="忽略同日去重，强制提醒",
    )
    parser.add_argument(
        "--notify-test",
        action="store_true",
        help="只发一条飞书连通测试消息，不扫行情",
    )
    args = parser.parse_args()
    if args.notify_test:
        load_dotenv(ROOT / ".env")
        channel = send_test_ping()
        log.info("测试消息已发送（%s）", channel)
        raise SystemExit(0)
    raise SystemExit(run(Path(args.config), dry_run=args.dry_run, force=args.force))


if __name__ == "__main__":
    main()
