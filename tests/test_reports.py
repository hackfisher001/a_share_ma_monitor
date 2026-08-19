"""Report table content and ranking."""

import pandas as pd

from src.fetch_quotes import QuoteBundle
from src.reports import (
    _build_tables,
    _facts_for_llm,
    _row_daily,
    _stage_label,
    _style_comment,
    _year_position,
)


def _bundle(name: str, code: str, start: float, end: float) -> QuoteBundle:
    dates = pd.date_range("2025-06-20", periods=300, freq="B")
    closes = [start + (end - start) * i / 299 for i in range(300)]
    hist = pd.DataFrame({"date": dates, "close": closes})
    return QuoteBundle(
        code=code,
        name=name,
        market="cn",
        price=end,
        ma30=float(hist["close"].tail(30).mean()),
        high_252=float(hist["close"].tail(252).max()),
        as_of=str(dates[-1].date()),
        hist=hist,
        theme="stock",
    )


def test_daily_row_omits_code_and_summarizes_horizons():
    bundle = _bundle("测试股票", "600000", 80.0, 120.0)
    row = _row_daily(bundle)

    assert "code" not in row
    assert set(row) == {"name", "price", "spark", "recent", "position"}
    assert len(row["spark"]["values"]) >= 252
    assert len(set(row["spark"]["years"])) == 2
    assert "日 " in row["recent"] and "周 " in row["recent"] and "月 " in row["recent"]
    assert "MA30 " in row["position"] and "一年 " in row["position"]
    assert "年位 " in row["position"] and "距高 " in row["position"]


def test_year_position_uses_one_year_range():
    bundle = _bundle("测试股票", "600000", 80.0, 120.0)
    position, drawdown = _year_position(bundle)

    assert position == 100.0
    assert drawdown == 0.0


def test_tables_rank_stronger_recent_performance_first():
    weak = _bundle("偏弱", "600001", 120.0, 90.0)
    strong = _bundle("偏强", "600002", 80.0, 120.0)

    tables = _build_tables("daily", [weak, strong])

    assert [c["name"] for c in tables[0]["columns"]] == [
        "name",
        "price",
        "spark",
        "recent",
        "position",
    ]
    assert tables[0]["rows"][0]["name"] == "**偏强**"
    assert "按近1月强→弱" in tables[0]["title"]


def test_stage_label_distinguishes_direction_changes():
    rising = _bundle("持续上涨", "600001", 80.0, 120.0)
    falling = _bundle("持续下跌", "600002", 120.0, 80.0)

    assert _stage_label(rising) == "持续走强"
    assert _stage_label(falling) == "持续走弱"


def test_llm_facts_include_full_horizons_and_stage():
    facts = _facts_for_llm("daily", [_bundle("测试股票", "600000", 80.0, 120.0)])

    assert "阶段=持续走强" in facts
    assert "1日=" in facts
    assert "1周=" in facts
    assert "1月=" in facts
    assert "3月=" in facts
    assert "1年=" in facts
    assert "年位=" in facts


def test_style_comment_applies_colored_visual_hierarchy():
    raw = (
        "🔎 **一句话结论**\n科技领涨，消费走弱。\n"
        "🔥 **领涨与强势**\n- 芯片近月上涨。\n"
        "❄️ **走弱与异常**\n- 白酒持续偏弱。"
    )
    styled = _style_comment(raw)

    assert "<font color='blue'>**🔎 一句话结论**</font>" in styled
    assert "<font color='orange'>**🔥 领涨与强势**</font>" in styled
    assert "<font color='red'>**❄️ 走弱与异常**</font>" in styled
    assert "- 芯片近月上涨。" in styled
