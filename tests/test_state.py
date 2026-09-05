"""Deduplication state: same-day list, pullback marks, drawdown episodes."""

import json
from datetime import date, timedelta

from src.state import AlertState


def test_same_day_dedup(tmp_path):
    state = AlertState(tmp_path / "s.json")
    assert not state.already_alerted("cn:600519")
    state.mark_alerted("cn:600519")
    assert AlertState(tmp_path / "s.json").already_alerted("cn:600519")


def test_daily_list_resets_but_pullback_mark_survives(tmp_path):
    path = tmp_path / "s.json"
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    path.write_text(
        json.dumps(
            {
                "date": yesterday,
                "alerted": ["cn:600519"],
                "pullback_marks": {"cn:600519": {"date": yesterday, "price": 41.2}},
            }
        ),
        encoding="utf-8",
    )
    state = AlertState(path)
    assert not state.already_alerted("cn:600519")
    assert state.pullback_mark("cn:600519") == {"date": yesterday, "price": 41.2}


def test_mark_pullback_persists_date_and_price(tmp_path):
    path = tmp_path / "s.json"
    AlertState(path).mark_pullback("cn:601899", 32.43)
    mark = AlertState(path).pullback_mark("cn:601899")
    assert mark["date"] == date.today().isoformat()
    assert mark["price"] == 32.43


def test_legacy_cooldown_section_is_migrated(tmp_path):
    """Old state files stored a date-only cooldown; dropping it would replay
    one stale alert per symbol on upgrade."""
    path = tmp_path / "s.json"
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    path.write_text(
        json.dumps({"date": yesterday, "cooldown": {"recent:cn:601899": yesterday}}),
        encoding="utf-8",
    )
    mark = AlertState(path).pullback_mark("recent:cn:601899")
    assert mark["date"] == yesterday
    assert mark["price"] == 0.0


def test_unknown_symbol_has_no_mark(tmp_path):
    assert AlertState(tmp_path / "s.json").pullback_mark("cn:000001") == {}


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
                "pullback_marks": {},
                "drawdown_fired": {"us:QQQM": [5, 10, 15]},
            }
        ),
        encoding="utf-8",
    )
    state = AlertState(path)
    assert not state.already_alerted("us:QQQM")
    assert state.drawdown_fired_levels("us:QQQM") == [5.0, 10.0, 15.0]
