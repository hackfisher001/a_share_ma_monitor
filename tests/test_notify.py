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

    def fake_feishu(
        text="",
        title="买入提醒 · MA30",
        webhook_url=None,
        tables=None,
        markdown=None,
    ):
        called["feishu"] = {
            "text": text,
            "title": title,
            "tables": tables,
            "markdown": markdown,
        }

    monkeypatch.setenv("FEISHU_WEBHOOK_URL", "https://example.com/hook")
    monkeypatch.setenv("WECOM_WEBHOOK_URL", "https://example.com/wecom")
    monkeypatch.setattr(notify, "send_feishu", fake_feishu)
    channel = notify.send_alert("hello", title="t")
    assert channel == "feishu"
    assert called["feishu"]["title"] == "t"
    assert called["feishu"]["text"] == "hello"


def test_markdown_fallback_table():
    text = notify._markdown_fallback_table(
        {
            "title": "科技ETF",
            "columns": [
                {"name": "name", "display_name": "名称"},
                {"name": "d1", "display_name": "1日"},
            ],
            "rows": [{"name": "**芯片**", "d1": "-1.2%"}],
        }
    )
    assert "**科技ETF**" in text
    assert "**名称**" in text
    assert "**芯片**" in text
