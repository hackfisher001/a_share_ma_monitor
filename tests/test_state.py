"""Deduplication state: same-day list plus multi-day cooldown."""

import json
from datetime import date, timedelta

from src.state import AlertState


def test_same_day_dedup(tmp_path):
    state = AlertState(tmp_path / "s.json")
    assert not state.already_alerted("cn:600519")
    state.mark_alerted("cn:600519")
    assert AlertState(tmp_path / "s.json").already_alerted("cn:600519")


def test_daily_list_resets_but_cooldown_survives(tmp_path):
    path = tmp_path / "s.json"
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    path.write_text(
        json.dumps(
            {
                "date": yesterday,
                "alerted": ["cn:600519"],
                "cooldown": {"dd:cn:600519": yesterday},
            }
        ),
        encoding="utf-8",
    )
    state = AlertState(path)
    assert not state.already_alerted("cn:600519")
    assert state.in_cooldown("dd:cn:600519", days=30)


def test_cooldown_expires(tmp_path):
    path = tmp_path / "s.json"
    long_ago = (date.today() - timedelta(days=45)).isoformat()
    path.write_text(
        json.dumps({"date": long_ago, "alerted": [], "cooldown": {"dd:cn:600519": long_ago}}),
        encoding="utf-8",
    )
    state = AlertState(path)
    assert not state.in_cooldown("dd:cn:600519", days=30)
    assert state.in_cooldown("dd:cn:600519", days=60)


def test_mark_cooldown_persists(tmp_path):
    path = tmp_path / "s.json"
    AlertState(path).mark_cooldown("dd:cn:600519")
    assert AlertState(path).in_cooldown("dd:cn:600519", days=30)


def test_drawdown_levels_persist_until_cleared(tmp_path):
    path = tmp_path / "s.json"
    state = AlertState(path)
    state.mark_drawdown_level("us:QQQM", 5)
    state.mark_drawdown_level("us:QQQM", 10)
    assert AlertState(path).drawdown_fired_levels("us:QQQM") == [5.0, 10.0]
    AlertState(path).clear_drawdown_levels("us:QQQM")
    assert AlertState(path).drawdown_fired_levels("us:QQQM") == []


def test_drawdown_levels_survive_daily_reset(tmp_path):
    path = tmp_path / "s.json"
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    path.write_text(
        json.dumps(
            {
                "date": yesterday,
                "alerted": ["us:QQQM"],
                "cooldown": {},
                "drawdown_fired": {"us:QQQM": [5, 10, 15]},
            }
        ),
        encoding="utf-8",
    )
    state = AlertState(path)
    assert not state.already_alerted("us:QQQM")
    assert state.drawdown_fired_levels("us:QQQM") == [5.0, 10.0, 15.0]
