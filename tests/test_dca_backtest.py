"""Cash-flow backtest: contributions, deployment rules, and comparability."""

import pandas as pd

from src.dca_backtest import (
    DcaConfig,
    Strategy,
    contribution_schedule,
    money_weighted_return,
    simulate,
)


def _hist(closes: list[float], start: str = "2015-01-01") -> pd.DataFrame:
    dates = pd.date_range(start, periods=len(closes), freq="B")
    return pd.DataFrame({"date": dates, "close": closes})


def test_contributions_land_on_first_trading_bar_of_each_month():
    hist = _hist([10.0] * 300)
    cfg = DcaConfig(monthly=1.0, bonus=3.0, bonus_month=2)

    schedule = contribution_schedule(hist["date"], cfg)
    months = hist["date"].dt.to_period("M").nunique()

    assert len(schedule) == months
    assert max(schedule.values()) == 4.0  # 工资 + 年终奖


def test_all_strategies_receive_identical_contributions():
    hist = _hist([10.0 + 0.01 * i for i in range(600)])
    cfg = DcaConfig()

    dca = simulate(hist, cfg, Strategy(name="A 无脑定投", core_frac=1.0))
    wait = simulate(hist, cfg, Strategy(name="C 等回撤", core_frac=0.0, dip_pct=10.0))

    assert abs(dca.contributed - wait.contributed) < 1e-9


def test_rising_market_favours_immediate_buying():
    hist = _hist([10.0 * (1.0008**i) for i in range(1200)])
    cfg = DcaConfig()

    dca = simulate(hist, cfg, Strategy(name="A 无脑定投", core_frac=1.0))
    wait = simulate(hist, cfg, Strategy(name="C 等回撤", core_frac=0.0, dip_pct=15.0))

    assert dca.final_value > wait.final_value
    assert wait.avg_cash_frac > dca.avg_cash_frac


def test_dip_rule_deploys_reserve_and_rearms_after_recovery():
    # Two distinct dips separated by a recovery above the re-arm level.
    leg = [10.0] * 60 + [8.0] * 20 + [10.5] * 60
    hist = _hist(leg + leg)
    cfg = DcaConfig(bonus=0.0)
    strat = Strategy(name="dip", core_frac=0.0, dip_pct=10.0, dip_window=40, rearm_pct=4.0)

    res = simulate(hist, cfg, strat)

    assert res.buys >= 2


def test_sleeve_sells_only_when_gain_target_met():
    hist = _hist([10.0] * 60 + [8.0] * 30 + [12.0] * 60, start="2016-01-01")
    cfg = DcaConfig(bonus=0.0)
    common = dict(core_frac=0.5, dip_pct=10.0, dip_window=40)

    holder = simulate(hist, cfg, Strategy(name="hold", **common))
    seller = simulate(hist, cfg, Strategy(name="sell", sell_gain_pct=8.0, **common))

    assert holder.sells == 0
    assert seller.sells >= 1


def test_money_weighted_return_recovers_known_rate():
    flows = [(pd.Timestamp("2020-01-01"), 100.0)]
    mwr = money_weighted_return(flows, 121.0, pd.Timestamp("2022-01-01"))

    assert abs(mwr - 10.0) < 0.5


def test_cash_yield_credits_idle_reserve():
    flat = _hist([10.0] * 800)
    cfg = DcaConfig(bonus=0.0, cash_yield_pct=3.0)
    # Dip never triggers on a flat series, so everything stays in cash.
    res = simulate(flat, cfg, Strategy(name="idle", core_frac=0.0, dip_pct=10.0))

    assert res.final_value > res.contributed
    assert res.buys == 0


def test_trading_cost_reduces_shares():
    hist = _hist([10.0] * 400)
    cfg = DcaConfig(bonus=0.0)

    free = simulate(hist, cfg, Strategy(name="free", core_frac=1.0, cost_pct=0.0))
    paid = simulate(hist, cfg, Strategy(name="paid", core_frac=1.0, cost_pct=0.5))

    assert paid.core_shares < free.core_shares
