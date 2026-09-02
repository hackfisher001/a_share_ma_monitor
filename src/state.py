"""Alert dedup plus the swing-T sleeve, which must survive across runs.

Unlike the other flags here, the sleeve is not deduplication state: it records
what the rolling half of a position is currently holding, so a 高抛 alert can be
priced against the average cost of earlier 低吸 alerts.
"""

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
            "t_sleeve": {},
            "pending_actions": {},
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
        sleeves = {
            str(k): dict(v)
            for k, v in (raw.get("t_sleeve") or {}).items()
            if isinstance(v, dict)
        }
        pending = {
            str(k): dict(v)
            for k, v in (raw.get("pending_actions") or {}).items()
            if isinstance(v, dict)
        }
        self._data = {
            "date": _today(),
            "alerted": alerted,
            "cooldown": cooldown,
            "drawdown_fired": drawdown_fired,
            "t_sleeve": sleeves,
            "pending_actions": pending,
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

    def t_sleeve(self, key: str) -> dict:
        return dict(self._data["t_sleeve"].get(str(key)) or {})

    def save_t_sleeve(self, key: str, payload: dict) -> None:
        self._data["t_sleeve"][str(key)] = dict(payload)
        self._data["date"] = _today()
        self.save()

    def save_pending_action(self, key: str, payload: dict) -> None:
        """Remember a suggested action until the owner explicitly records it."""
        self._data["pending_actions"][str(key)] = dict(payload)
        self._data["date"] = _today()
        self.save()

    def pending_actions(self) -> list[dict]:
        return [dict(v) for v in self._data["pending_actions"].values()]

    def resolve_pending_actions(self, market: str, code: str, side: str) -> int:
        """Mark matching reminders as executed after an explicit journal entry."""
        market, code, side = market.lower(), code.upper(), side.upper()
        removed = []
        for key, action in self._data["pending_actions"].items():
            if (
                str(action.get("market", "")).lower() == market
                and str(action.get("code", "")).upper() == code
                and str(action.get("side", "")).upper() == side
            ):
                removed.append(key)
        for key in removed:
            del self._data["pending_actions"][key]
        if removed:
            self._data["date"] = _today()
            self.save()
        return len(removed)
