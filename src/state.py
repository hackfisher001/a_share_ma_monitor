"""Alert deduplication: same-day for MA30, multi-day cooldown for drawdowns."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path


def _today() -> str:
    return date.today().isoformat()


class AlertState:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data: dict = {"date": _today(), "alerted": [], "cooldown": {}}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        # Cooldown timestamps must outlive the daily reset.
        cooldown = {str(k): str(v) for k, v in (raw.get("cooldown") or {}).items()}
        alerted = [] if raw.get("date") != _today() else list(raw.get("alerted") or [])
        self._data = {"date": _today(), "alerted": alerted, "cooldown": cooldown}

    def save(self) -> None:
        self.path.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def already_alerted(self, code: str) -> bool:
        return str(code) in self._data["alerted"]

    def mark_alerted(self, code: str) -> None:
        code = str(code)
        if code not in self._data["alerted"]:
            self._data["alerted"].append(code)
        self._data["date"] = _today()
        self.save()

    def in_cooldown(self, key: str, days: int) -> bool:
        last = self._data["cooldown"].get(str(key))
        if not last:
            return False
        try:
            last_day = date.fromisoformat(last)
        except ValueError:
            return False
        return (date.today() - last_day).days < max(0, int(days))

    def mark_cooldown(self, key: str) -> None:
        self._data["cooldown"][str(key)] = _today()
        self._data["date"] = _today()
        self.save()
