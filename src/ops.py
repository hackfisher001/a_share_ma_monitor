"""Operational safety net: state snapshots and a liveness heartbeat.

Silence from this system is ambiguous — it looks the same whether nothing
happened or the host died. The heartbeat removes that ambiguity by speaking
once a day regardless, and the snapshots make the sleeve/trade ledger
survivable, since they are the only records that cannot be re-derived.
"""

from __future__ import annotations

import shutil
from datetime import date, datetime, timedelta
from pathlib import Path

from src.state import AlertState

SNAPSHOT_KEEP_DAYS = 30
STALE_SCAN_HOURS = 24


def snapshot_state(paths: list[Path], backup_root: Path, *, keep_days: int = SNAPSHOT_KEEP_DAYS) -> list[Path]:
    """Copy today's state files aside and prune old days."""
    target = backup_root / date.today().isoformat()
    target.mkdir(parents=True, exist_ok=True)
    copied: list[Path] = []
    for path in paths:
        if not path.exists():
            continue
        dest = target / path.name
        shutil.copy2(path, dest)
        copied.append(dest)

    cutoff = date.today() - timedelta(days=max(1, keep_days))
    for child in backup_root.iterdir() if backup_root.exists() else []:
        if not child.is_dir():
            continue
        try:
            if date.fromisoformat(child.name) < cutoff:
                shutil.rmtree(child, ignore_errors=True)
        except ValueError:
            continue
    return copied


def heartbeat_markdown(
    state: AlertState,
    *,
    pending_count: int,
    now: datetime | None = None,
    stale_hours: int = STALE_SCAN_HOURS,
) -> tuple[str, bool]:
    """Daily liveness note. Returns (markdown, healthy)."""
    now = now or datetime.now()
    health = state.scan_health()
    raw_at = health.get("last_scan_at")
    last_at: datetime | None = None
    if raw_at:
        try:
            last_at = datetime.fromisoformat(str(raw_at))
        except ValueError:
            last_at = None

    if last_at is None:
        return (
            "**⚠️ 巡检心跳异常**\n从未记录过成功巡检，请检查服务器 cron 与 .env。",
            False,
        )

    age_h = (now - last_at).total_seconds() / 3600.0
    errors = int(health.get("errors") or 0)
    checked = int(health.get("checked") or 0)
    healthy = age_h <= stale_hours and errors == 0

    if age_h > stale_hours:
        head = f"**⚠️ 巡检已停摆 {age_h:.0f} 小时**"
    elif errors:
        head = f"**⚠️ 巡检有 {errors} 只抓取失败**"
    else:
        head = "**✅ 巡检正常**"

    return (
        f"{head}\n"
        f"最近一次：{last_at:%Y-%m-%d %H:%M}（{age_h:.1f} 小时前）\n"
        f"覆盖 {checked} 只，失败 {errors} 只，当次提醒 {int(health.get('alerts') or 0)} 条\n"
        f"待确认动作 {pending_count} 条\n"
        "收不到这条消息，就说明监控本身挂了。",
        healthy,
    )
