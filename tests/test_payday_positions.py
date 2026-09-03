from datetime import date

from src.action_digest import positions_markdown
from src.payday import PaydayConfig, is_payday, payday_markdown
from src.trades import TradeLedger


def cfg(**kw):
    return PaydayConfig.from_dict({"enabled": True, "day": 10, **kw})


def test_disabled_never_fires():
    assert is_payday(PaydayConfig.from_dict({}), date(2026, 9, 10)) == (False, False)


def test_salary_day_fires_without_bonus():
    assert is_payday(cfg(), date(2026, 9, 10)) == (True, False)


def test_other_days_are_silent():
    assert is_payday(cfg(), date(2026, 9, 11)) == (False, False)


def test_bonus_month_is_flagged():
    assert is_payday(cfg(bonus_months=[2]), date(2026, 2, 10)) == (True, True)


def test_day_31_clamps_to_a_short_month():
    assert is_payday(cfg(day=31), date(2026, 2, 28)) == (True, False)


def test_markdown_orders_cheapest_first_and_discourages_waiting():
    md = payday_markdown(
        [
            {"name": "黄金ETF", "code": "518880", "year_dd": -23.7, "stage": "深度回撤"},
            {"name": "招商银行", "code": "600036", "year_dd": -0.6, "stage": "接近高位"},
        ],
        bonus=False,
    )
    assert "攒钱等回撤 0 胜" in md
    assert md.index("518880") < md.index("600036")


def test_bonus_markdown_warns_against_splitting():
    assert "不要分批等跌" in payday_markdown([], bonus=True)


def _ledger(tmp_path):
    return TradeLedger(tmp_path / "trades.csv")


def test_positions_average_multiple_buys(tmp_path):
    led = _ledger(tmp_path)
    led.record(market="cn", code="600036", side="BUY", quantity=100, price=40.0)
    led.record(market="cn", code="600036", side="BUY", quantity=100, price=42.0)
    pos = led.positions()["cn:600036"]
    assert pos.quantity == 200
    assert pos.avg_price == 41.0
    assert round(pos.unrealised_pct(45.0), 4) == round((45 / 41 - 1) * 100, 4)


def test_sell_realises_profit_and_keeps_cost_basis(tmp_path):
    led = _ledger(tmp_path)
    led.record(market="cn", code="600036", side="BUY", quantity=200, price=40.0)
    led.record(market="cn", code="600036", side="SELL", quantity=100, price=44.0)
    pos = led.positions()["cn:600036"]
    assert pos.quantity == 100
    assert pos.avg_price == 40.0
    assert pos.realised == 400.0


def test_oversized_sell_does_not_go_negative(tmp_path):
    led = _ledger(tmp_path)
    led.record(market="cn", code="600036", side="BUY", quantity=100, price=40.0)
    led.record(market="cn", code="600036", side="SELL", quantity=500, price=44.0)
    pos = led.positions()["cn:600036"]
    assert pos.quantity == 0 and not pos.holding


def test_positions_markdown_is_empty_without_holdings(tmp_path):
    assert positions_markdown(_ledger(tmp_path), {}) == ""


def test_positions_markdown_totals(tmp_path):
    led = _ledger(tmp_path)
    led.record(market="cn", code="600036", side="BUY", quantity=100, price=40.0)
    md = positions_markdown(led, {"cn:600036": 44.0})
    assert "成本 40.00" in md and "+10.00%" in md
    assert "合计浮盈：** +400.00" in md


def test_recent_still_returns_newest_first(tmp_path):
    led = _ledger(tmp_path)
    led.record(market="cn", code="600036", side="BUY", quantity=1, price=1.0)
    led.record(market="cn", code="600941", side="BUY", quantity=1, price=2.0)
    assert [t.code for t in led.recent(2)] == ["600941", "600036"]
