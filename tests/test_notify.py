"""Table image + notify helpers."""

import os

from src import notify, table_image


def test_render_table_png_smoke():
    png = table_image.render_table_png(
        {
            "title": "个股",
            "columns": [
                {"name": "name", "display_name": "名称"},
                {"name": "code", "display_name": "代码"},
                {"name": "d1", "display_name": "1日"},
            ],
            "rows": [
                {"name": "**招商银行**", "code": "600036", "d1": "-0.68%"},
                {"name": "**长电科技**", "code": "600584", "d1": "+1.20%"},
            ],
        }
    )
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(png) > 500


def test_render_sparkline_table_png():
    png = table_image.render_table_png(
        {
            "title": "走势",
            "columns": [
                {"name": "name", "display_name": "名称", "width": "120px"},
                {
                    "name": "spark",
                    "display_name": "近一年走势",
                    "width": "240px",
                    "data_type": "sparkline",
                },
            ],
            "rows": [
                {
                    "name": "**黄金**",
                    "spark": {
                        "values": [100, 130, 160, 150, 120, 125, 138],
                        "years": [2025, 2025, 2025, 2025, 2026, 2026, 2026],
                    },
                }
            ],
        }
    )
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(png) > 1000


def test_mobile_stack_table():
    text = notify._mobile_stack_table(
        {
            "title": "科技ETF",
            "columns": [
                {"name": "name", "display_name": "名称"},
                {"name": "code", "display_name": "代码"},
                {"name": "d1", "display_name": "1日"},
            ],
            "rows": [{"name": "**芯片**", "code": "512480", "d1": "-1.2%"}],
        }
    )
    assert "**科技ETF**" in text
    assert "**芯片**" in text
    assert "`512480`" in text
    assert "1日 -1.2%" in text


def test_mobile_stack_hides_raw_sparkline_data():
    text = notify._mobile_stack_table(
        {
            "columns": [
                {"name": "name", "display_name": "名称"},
                {"name": "spark", "display_name": "走势", "data_type": "sparkline"},
            ],
            "rows": [{"name": "**黄金**", "spark": {"values": [1, 2, 3]}}],
        }
    )
    assert "走势 见图片" in text
    assert "[1, 2, 3]" not in text


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
        image_keys=None,
        prefer_images=True,
    ):
        called["feishu"] = {
            "text": text,
            "title": title,
            "tables": tables,
            "markdown": markdown,
            "image_keys": image_keys,
        }

    monkeypatch.setenv("FEISHU_WEBHOOK_URL", "https://example.com/hook")
    monkeypatch.setenv("WECOM_WEBHOOK_URL", "https://example.com/wecom")
    monkeypatch.setattr(notify, "send_feishu", fake_feishu)
    channel = notify.send_alert("hello", title="t")
    assert channel == "feishu"
    assert called["feishu"]["title"] == "t"
    assert called["feishu"]["text"] == "hello"


def test_normalize_width_enforces_min_80px():
    assert notify._normalize_width("70px") == "80px"
    assert notify._normalize_width("120px") == "120px"
    assert notify._normalize_width("auto") == "auto"
