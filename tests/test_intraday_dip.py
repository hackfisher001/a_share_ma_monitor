from datetime import date, datetime

from src.action_digest import should_realert
from src.fetch_quotes import parse_tencent_us_quote
from src.ops import heartbeat_markdown, snapshot_state
from src.signals import QuoteSnapshot, crossed_intraday_dip_levels
from src.state import AlertState

LEVELS = (3.0, 5.0, 8.0)


def snap(change_pct, price=100.0, high=120.0, live=True):
    return QuoteSnapshot(
        code="518880",
        name="黄金ETF",
        price=price,
        ma30=105.0,
        as_of="2026-09-02",
        history_rows=300,
        high_252=high,
        change_pct=change_pct,
        live=live,
    )


def test_no_signal_above_first_band():
    assert crossed_intraday_dip_levels(snap(-2.37), LEVELS) == []


def test_gap_down_that_percentile_filters_used_to_miss():
    hits = crossed_intraday_dip_levels(snap(-3.5), LEVELS)
    assert [h.threshold_pct for h in hits] == [3.0]
    assert "急跌提醒 · -3%" == hits[0].title
    assert "-3.50%" in hits[0].message


def test_deeper_slide_fires_every_uncrossed_band():
    hits = crossed_intraday_dip_levels(snap(-8.4), LEVELS)
    assert [h.threshold_pct for h in hits] == [3.0, 5.0, 8.0]


def test_already_fired_bands_are_skipped():
    hits = crossed_intraday_dip_levels(snap(-6.0), LEVELS, already_fired=[3.0])
    assert [h.threshold_pct for h in hits] == [5.0]


def test_missing_change_is_not_a_signal():
    assert crossed_intraday_dip_levels(snap(None), LEVELS) == []


def test_rise_is_not_a_signal():
    assert crossed_intraday_dip_levels(snap(4.0), LEVELS) == []


def test_message_flags_a_stale_close():
    hit = crossed_intraday_dip_levels(snap(-4.0, live=False), LEVELS)[0]
    assert "未取到实时价" in hit.message


def test_intraday_bands_persist_within_one_session(tmp_path):
    path = tmp_path / "state.json"
    AlertState(path).mark_intraday_level("cn:518880", 3.0, "2026-09-03")
    assert AlertState(path).intraday_fired_levels("cn:518880", "2026-09-03") == [3.0]


def test_intraday_bands_clear_on_the_next_session(tmp_path):
    path = tmp_path / "state.json"
    AlertState(path).mark_intraday_level("cn:518880", 3.0, "2026-09-03")
    assert AlertState(path).intraday_fired_levels("cn:518880", "2026-09-04") == []


def test_us_session_spanning_beijing_midnight_does_not_double_alert(tmp_path):
    """22:00 and 00:30 Beijing are one US session; the band must stay fired.

    Resetting on the local calendar day used to re-fire it after midnight.
    """
    path = tmp_path / "state.json"
    AlertState(path).mark_intraday_level("us:TSLA", 3.0, "2026-09-03")
    rolled = path.read_text(encoding="utf-8").replace(
        f'"date": "{date.today().isoformat()}"', '"date": "2026-09-03"', 1
    )
    path.write_text(rolled, encoding="utf-8")
    assert AlertState(path).intraday_fired_levels("us:TSLA", "2026-09-03") == [3.0]


def test_next_us_session_is_not_swallowed_by_the_previous_one(tmp_path):
    path = tmp_path / "state.json"
    AlertState(path).mark_intraday_level("us:TSLA", 3.0, "2026-09-03")
    state = AlertState(path)
    assert state.intraday_fired_levels("us:TSLA", "2026-09-04") == []
    state.mark_intraday_level("us:TSLA", 5.0, "2026-09-04")
    assert state.intraday_fired_levels("us:TSLA", "2026-09-04") == [5.0]


def test_legacy_list_shaped_intraday_state_is_ignored(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(
        '{"date": "2026-09-03", "intraday_fired": {"cn:518880": [3.0]}}',
        encoding="utf-8",
    )
    assert AlertState(path).intraday_fired_levels("cn:518880", "2026-09-03") == []


def test_drawdown_bands_survive_the_day_rollover(tmp_path):
    """Year-drawdown episodes are not per-session and must not reset with it."""
    path = tmp_path / "state.json"
    state = AlertState(path)
    state.mark_drawdown_level("cn:518880", 10.0)
    stale = path.read_text(encoding="utf-8").replace(date.today().isoformat(), "2020-01-01", 1)
    path.write_text(stale, encoding="utf-8")
    assert AlertState(path).drawdown_fired_levels("cn:518880") == [10.0]


def test_first_signal_always_alerts():
    assert should_realert({}, price=10.0, side="BUY")[0] is True


def test_repeat_is_suppressed_while_price_is_unchanged():
    pending = {"price": 10.0, "first_seen": date.today().isoformat(), "last_alerted": date.today().isoformat()}
    assert should_realert(pending, price=9.9, side="BUY")[0] is False


def test_buy_realerts_when_it_keeps_falling():
    pending = {"price": 10.0, "first_seen": date.today().isoformat(), "last_alerted": date.today().isoformat()}
    ok, why = should_realert(pending, price=9.6, side="BUY")
    assert ok and "又跌了" in why


def test_sell_realerts_when_it_keeps_rising():
    pending = {"price": 10.0, "first_seen": date.today().isoformat(), "last_alerted": date.today().isoformat()}
    ok, why = should_realert(pending, price=10.4, side="SELL")
    assert ok and "又涨了" in why


def test_stale_pending_speaks_up_again():
    pending = {"price": 10.0, "first_seen": "2026-08-01", "last_alerted": "2026-08-20"}
    ok, why = should_realert(pending, price=10.0, side="BUY", today=date(2026, 9, 3))
    assert ok and "未记录成交" in why


def test_pending_keeps_its_original_first_seen(tmp_path):
    state = AlertState(tmp_path / "state.json")
    state.save_pending_action("k", {"price": 10.0, "first_seen": "2026-08-01"})
    state.save_pending_action("k", {"price": 9.0})
    assert state.pending_action("k")["first_seen"] == "2026-08-01"
    assert state.pending_action("k")["last_alerted"] == date.today().isoformat()


def test_tencent_us_quote_gives_price_and_prev_close():
    raw = 'v_usTSLA="200~特斯拉~TSLA.OQ~357.01~356.09~360.41~33951909~0~0~356.71~40~0~0~0~0~0~0~0~0~356.85~160~0~0~0~0~0~0~0~0~~2026-09-02 16:00:01~0.92~0.26~360.62~349.92~USD~";'
    spot = parse_tencent_us_quote(raw)
    assert spot is not None
    assert spot.price == 357.01
    assert spot.prev_close == 356.09
    assert spot.as_of == date(2026, 9, 2)


def test_tencent_us_quote_rejects_unknown_symbol():
    assert parse_tencent_us_quote('v_usZZZZ="";') is None


def test_heartbeat_flags_a_stalled_scan(tmp_path):
    state = AlertState(tmp_path / "state.json")
    state.record_scan_health(checked=20, errors=0, alerts=1)
    md, healthy = heartbeat_markdown(
        state, pending_count=0, now=datetime.now().replace(year=datetime.now().year + 1)
    )
    assert not healthy and "停摆" in md


def test_heartbeat_is_green_after_a_clean_scan(tmp_path):
    state = AlertState(tmp_path / "state.json")
    state.record_scan_health(checked=20, errors=0, alerts=0)
    md, healthy = heartbeat_markdown(state, pending_count=2)
    assert healthy and "巡检正常" in md and "待确认动作 2 条" in md


def test_stray_fetch_failure_is_reported_but_not_alarming(tmp_path):
    state = AlertState(tmp_path / "state.json")
    state.record_scan_health(checked=20, errors=1, alerts=0)
    md, healthy = heartbeat_markdown(state, pending_count=0)
    assert healthy and "1 只偶发失败" in md


def test_mostly_blind_scan_escalates(tmp_path):
    state = AlertState(tmp_path / "state.json")
    state.record_scan_health(checked=20, errors=12, alerts=0)
    md, healthy = heartbeat_markdown(state, pending_count=0)
    assert not healthy and "12/20" in md


def test_heartbeat_without_any_scan(tmp_path):
    md, healthy = heartbeat_markdown(AlertState(tmp_path / "state.json"), pending_count=0)
    assert not healthy and "从未记录过成功巡检" in md


def test_snapshot_copies_and_prunes(tmp_path):
    src = tmp_path / "alert_state.json"
    src.write_text("{}", encoding="utf-8")
    backups = tmp_path / "backups"
    (backups / "2020-01-01").mkdir(parents=True)
    copied = snapshot_state([src, tmp_path / "missing.csv"], backups)
    assert len(copied) == 1
    assert (backups / date.today().isoformat() / "alert_state.json").exists()
    assert not (backups / "2020-01-01").exists()
