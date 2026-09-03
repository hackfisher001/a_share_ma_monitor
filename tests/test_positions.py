from src.action_digest import positions_markdown
from src.trades import TradeLedger


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


def test_market_scoped_report_omits_the_other_market(tmp_path):
    """`--report daily --market cn` has no US prices; showing 0.00 would lie."""
    led = _ledger(tmp_path)
    led.record(market="cn", code="600036", side="BUY", quantity=100, price=40.0)
    led.record(market="us", code="TSLA", side="BUY", quantity=10, price=300.0)
    md = positions_markdown(led, {"cn:600036": 44.0})
    assert "600036" in md and "TSLA" not in md
    assert "0.00　浮盈" not in md
    assert "合计浮盈：** +400.00" in md


def test_all_markets_report_shows_everything(tmp_path):
    led = _ledger(tmp_path)
    led.record(market="cn", code="600036", side="BUY", quantity=100, price=40.0)
    led.record(market="us", code="TSLA", side="BUY", quantity=10, price=300.0)
    md = positions_markdown(led, {"cn:600036": 44.0, "us:TSLA": 330.0})
    assert "600036" in md and "TSLA" in md


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
