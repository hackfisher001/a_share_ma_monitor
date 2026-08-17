"""Alert deduplication: same-day MA30, per-episode drawdown level tracking."""

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
        self._data: dict = {
            "date": _today(),
            "alerted": [],
            "cooldown": {},
            "drawdown_fired": {},
        }
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        # Cooldown + drawdown episode flags must outlive the daily reset.
        cooldown = {str(k): str(v) for k, v in (raw.get("cooldown") or {}).items()}
        fired_raw = raw.get("drawdown_fired") or {}
        drawdown_fired: dict[str, list[float]] = {}
        for key, levels in fired_raw.items():
            try:
                drawdown_fired[str(key)] = sorted({abs(float(x)) for x in (levels or [])})
            except (TypeError, ValueError):
                continue
        alerted = [] if raw.get("date") != _today() else list(raw.get("alerted") or [])
        self._data = {
            "date": _today(),
            "alerted": alerted,
            "cooldown": cooldown,
            "drawdown_fired": drawdown_fired,
        }

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

    def drawdown_fired_levels(self, key: str) -> list[float]:
        return list(self._data["drawdown_fired"].get(str(key), []))

    def mark_drawdown_level(self, key: str, level: float) -> None:
        key = str(key)
        levels = set(self._data["drawdown_fired"].get(key, []))
        levels.add(abs(float(level)))
        self._data["drawdown_fired"][key] = sorted(levels)
        self._data["date"] = _today()
        self.save()

    def clear_drawdown_levels(self, key: str) -> None:
        key = str(key)
        if key in self._data["drawdown_fired"]:
            del self._data["drawdown_fired"][key]
            self._data["date"] = _today()
            self.save()
