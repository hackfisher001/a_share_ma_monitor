"""Notify helpers (no network)."""

import os

from src import notify


def test_send_alert_requires_feishu(monkeypatch):
    monkeypatch.delenv("FEISHU_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("WECOM_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("DINGTALK_WEBHOOK_URL", raising=False)
    try:
        notify.send_alert("x")
        assert False, "should raise"
    except ValueError as e:
        assert "FEISHU_WEBHOOK_URL" in str(e)


def test_send_alert_prefers_feishu(monkeypatch):
    called = {}

    def fake_feishu(text, title="买入提醒 · MA30", webhook_url=None):
        called["feishu"] = (text, title)

    monkeypatch.setenv("FEISHU_WEBHOOK_URL", "https://example.com/hook")
    monkeypatch.setenv("WECOM_WEBHOOK_URL", "https://example.com/wecom")
    monkeypatch.setattr(notify, "send_feishu", fake_feishu)
    channel = notify.send_alert("hello", title="t")
    assert channel == "feishu"
    assert called["feishu"] == ("hello", "t")
