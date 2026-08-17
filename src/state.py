"""Same-day alert deduplication state."""

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
        self._data: dict = {"date": _today(), "alerted": []}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        if raw.get("date") != _today():
            # New trading day → reset
            self._data = {"date": _today(), "alerted": []}
            return
        self._data = {
            "date": _today(),
            "alerted": list(raw.get("alerted") or []),
        }

    def save(self) -> None:
        self.path.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def already_alerted(self, code: str) -> bool:
        return str(code).zfill(6) in self._data["alerted"]

    def mark_alerted(self, code: str) -> None:
        code = str(code).zfill(6)
        if code not in self._data["alerted"]:
            self._data["alerted"].append(code)
        self._data["date"] = _today()
        self.save()
