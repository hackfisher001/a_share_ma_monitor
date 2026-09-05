from src.action_digest import action_payload, action_summary_markdown, pending_key
from src.state import AlertState
from src.trades import TradeLedger


def test_pending_action_is_shown_then_resolved_by_recorded_trade(tmp_path):
    state = AlertState(tmp_path / "state.json")
    ledger = TradeLedger(tmp_path / "trades.csv")
    state.save_pending_action(
        pending_key("cn", "600036", "BUY"),
        action_payload(
            market="cn",
            code="600036",
            name="招商银行",
            side="BUY",
            price=42.35,
            text="做T · 低吸",
        ),
    )

    assert "待你确认：1 件" in action_summary_markdown(state, ledger)
    ledger.record(market="cn", code="600036", side="BUY", quantity=100, price=42.35)
    assert state.resolve_pending_actions("cn", "600036", "BUY") == 1
    # Nothing pending must render nothing: a card that says 「无需操作」 daily is noise.
    assert action_summary_markdown(state, ledger) == ""


def test_no_pending_actions_renders_nothing(tmp_path):
    state = AlertState(tmp_path / "state.json")
    ledger = TradeLedger(tmp_path / "trades.csv")
    ledger.record(market="cn", code="600036", side="BUY", quantity=100, price=42.35)

    assert action_summary_markdown(state, ledger) == ""
