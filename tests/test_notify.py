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


def test_normalize_width_enforces_min_80px():
    assert notify._normalize_width("70px") == "80px"
    assert notify._normalize_width("120px") == "120px"
    assert notify._normalize_width("auto") == "auto"


def test_table_element_schema():
    el = notify._table_element(
        {
            "columns": [
                {"name": "name", "display_name": "名称", "width": "70px"},
                {"name": "d1", "display_name": "1日", "width": "80px"},
            ],
            "rows": [{"name": "**A**", "d1": "-1%"}],
        }
    )
    assert el["tag"] == "table"
    assert el["columns"][0]["width"] == "80px"
    assert el["columns"][0]["data_type"] == "lark_md"
    assert el["freeze_first_column"] is True
