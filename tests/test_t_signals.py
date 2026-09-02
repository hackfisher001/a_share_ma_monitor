"""Swing-T sleeve: arming, adds, take-profit, and persistence round-trips."""

from src.signals import QuoteSnapshot
from src.state import AlertState
from src.t_signals import (
    TBuySignal,
    TConfig,
    TSellSignal,
    TSleeve,
    evaluate_t,
    sleeve_status_line,
)


def _snap(price: float, high: float = 100.0, rows: int = 300) -> QuoteSnapshot:
    return QuoteSnapshot(
        code="600036",
        name="招商银行",
        price=price,
        ma30=price,
        as_of="2026-08-27",
        history_rows=rows,
        high_252=high,
    )


CFG = TConfig()


def test_config_defaults_match_the_validated_variant():
    assert CFG.buy_drawdown_pct == 5.0
    assert CFG.sell_bounce_pct == 5.0
    assert CFG.high_window == 252


def test_config_from_dict_ignores_sign():
    cfg = TConfig.from_dict({"buy_drawdown_pct": -8, "sell_bounce_pct": 8})

    assert cfg.buy_drawdown_pct == 8.0
    assert cfg.sell_bounce_pct == 8.0


def test_no_signal_while_price_is_near_the_high():
    sleeve = TSleeve()

    assert evaluate_t(_snap(98.0), sleeve, CFG) is None
    assert not sleeve.holding


def test_dip_below_threshold_fires_a_buy_and_disarms():
    sleeve = TSleeve()

    signal = evaluate_t(_snap(94.0), sleeve, CFG)

    assert isinstance(signal, TBuySignal)
    assert signal.add_index == 1
    assert sleeve.holding
    assert not sleeve.armed
    assert abs(sleeve.avg_price - 94.0) < 1e-9


def test_preview_signal_does_not_assume_trade_was_executed():
    sleeve = TSleeve()

    signal = evaluate_t(_snap(94.0), sleeve, CFG, commit=False)

    assert isinstance(signal, TBuySignal)
    assert sleeve.holding is False
    assert sleeve.adds == 0


def test_further_slide_does_not_fire_again_until_price_recovers():
    sleeve = TSleeve()
    evaluate_t(_snap(94.0), sleeve, CFG)

    assert evaluate_t(_snap(90.0), sleeve, CFG) is None
    assert evaluate_t(_snap(85.0), sleeve, CFG) is None
    assert sleeve.adds == 1


def test_recovery_rearms_and_allows_a_second_add():
    sleeve = TSleeve()
    evaluate_t(_snap(94.0), sleeve, CFG)

    # Back inside -4% of the high re-arms without alerting.
    assert evaluate_t(_snap(97.0), sleeve, CFG) is None
    assert sleeve.armed

    second = evaluate_t(_snap(94.5), sleeve, CFG)

    assert isinstance(second, TBuySignal)
    assert second.add_index == 2
    assert second.prev_avg_price == 94.0


def test_average_cost_spans_multiple_adds():
    sleeve = TSleeve()
    evaluate_t(_snap(95.0), sleeve, CFG)
    evaluate_t(_snap(97.0), sleeve, CFG)  # re-arm
    evaluate_t(_snap(90.0), sleeve, CFG)

    assert sleeve.adds == 2
    # Equal money per add, so the average is harmonic and sits below the midpoint.
    assert 92.0 < sleeve.avg_price < 92.6


def test_max_adds_caps_a_runaway_slide():
    cfg = TConfig(max_adds=2)
    sleeve = TSleeve()
    for price in (95.0, 97.0, 94.0, 97.0, 93.0):
        evaluate_t(_snap(price), sleeve, cfg)

    assert sleeve.adds == 2


def test_sell_fires_once_the_sleeve_is_up_by_the_threshold():
    sleeve = TSleeve()
    evaluate_t(_snap(94.0), sleeve, CFG)

    assert evaluate_t(_snap(97.0), sleeve, CFG) is None  # only +3.2%, just re-arms

    signal = evaluate_t(_snap(99.0), sleeve, CFG)

    assert isinstance(signal, TSellSignal)
    assert signal.adds == 1
    assert signal.gain_pct > 5.0
    assert not sleeve.holding


def test_a_fresh_add_cannot_also_sell_on_the_same_bar():
    sleeve = TSleeve()
    signal = evaluate_t(_snap(94.0), sleeve, CFG)

    assert isinstance(signal, TBuySignal)
    assert sleeve.gain_pct(94.0) == 0.0


def test_short_history_and_bad_prices_are_ignored():
    assert evaluate_t(_snap(94.0, rows=50), TSleeve(), CFG) is None
    assert evaluate_t(_snap(94.0, high=0.0), TSleeve(), CFG) is None
    assert evaluate_t(_snap(0.0), TSleeve(), CFG) is None


def test_sleeve_survives_a_dict_round_trip():
    sleeve = TSleeve()
    evaluate_t(_snap(94.0), sleeve, CFG)

    restored = TSleeve.from_dict(sleeve.to_dict())

    assert restored.adds == sleeve.adds
    assert restored.armed is sleeve.armed
    assert abs(restored.avg_price - sleeve.avg_price) < 1e-9


def test_sleeve_from_corrupt_dict_falls_back_to_empty():
    sleeve = TSleeve.from_dict({"shares": "oops", "cost": None})

    assert not sleeve.holding
    assert sleeve.armed


def test_state_persists_the_sleeve_across_instances(tmp_path):
    path = tmp_path / "state.json"
    sleeve = TSleeve()
    evaluate_t(_snap(94.0), sleeve, CFG)

    AlertState(path).save_t_sleeve("t:cn:600036", sleeve.to_dict())
    reloaded = TSleeve.from_dict(AlertState(path).t_sleeve("t:cn:600036"))

    assert reloaded.adds == 1
    assert not reloaded.armed
    assert abs(reloaded.avg_price - 94.0) < 1e-9


def test_state_returns_empty_sleeve_for_unknown_key(tmp_path):
    assert AlertState(tmp_path / "s.json").t_sleeve("t:cn:000001") == {}


def test_signal_messages_name_the_sleeve_and_leave_the_core_alone():
    sleeve = TSleeve()
    buy = evaluate_t(_snap(94.0), sleeve, CFG)
    evaluate_t(_snap(97.0), sleeve, CFG)
    sell = evaluate_t(_snap(99.5), sleeve, CFG)

    assert "低吸" in buy.title and "机动仓" in buy.message
    assert "底仓不动" in buy.message
    assert "高抛" in sell.title and "底仓不动" in sell.message


def test_status_line_reports_holding_and_empty_states():
    sleeve = TSleeve()
    assert "空仓" in sleeve_status_line("招商银行", "600036", sleeve, 94.0)

    evaluate_t(_snap(94.0), sleeve, CFG)
    line = sleeve_status_line("招商银行", "600036", sleeve, 96.0)

    assert "1 笔" in line and "均价 94.00" in line
