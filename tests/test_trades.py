from src.trades import TradeLedger


def test_trade_ledger_records_and_returns_latest_first(tmp_path):
    ledger = TradeLedger(tmp_path / "trades.csv")
    ledger.record(market="cn", code="600036", side="buy", quantity=100, price=42.35)
    ledger.record(market="us", code="tsla", side="sell", quantity=2, price=315.2)

    recent = ledger.recent()

    assert [trade.code for trade in recent] == ["TSLA", "600036"]
    assert recent[0].side == "SELL"
    assert recent[1].price == 42.35


def test_trade_ledger_rejects_invalid_trade(tmp_path):
    ledger = TradeLedger(tmp_path / "trades.csv")
    try:
        ledger.record(market="cn", code="600036", side="HOLD", quantity=1, price=1)
    except ValueError as exc:
        assert "BUY" in str(exc)
    else:
        raise AssertionError("invalid side should fail")
