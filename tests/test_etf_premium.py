"""ETF premium / discount parsing and labels."""

from src.etf_premium import (
    PremiumBaseline,
    PremiumQuote,
    parse_eastmoney_etf_row,
)


def test_premium_is_price_above_iopv():
    """East Money's 折价率 is negative when the share trades above IOPV."""
    quote = parse_eastmoney_etf_row(
        {
            "代码": "159509",
            "名称": "纳指科技ETF景顺",
            "最新价": 2.833,
            "IOPV实时估值": 2.2712,
            "基金折价率": -24.74,
        }
    )

    assert quote is not None
    assert abs(quote.premium_pct - (2.833 / 2.2712 - 1) * 100) < 0.05
    # Chronically high premiums must NOT sermonize without a personal baseline.
    assert "注意" not in quote.markdown_line()
    assert "溢价" in quote.short_label()


def test_near_nav_is_not_flagged():
    quote = parse_eastmoney_etf_row(
        {
            "代码": "510300",
            "名称": "沪深300ETF",
            "最新价": 4.620,
            "IOPV实时估值": 4.618,
            "基金折价率": -0.04,
        }
    )

    assert quote is not None
    assert abs(quote.premium_pct) < 0.2
    assert "注意" not in quote.markdown_line()


def test_warning_only_when_rich_vs_own_history():
    """159509's median premium is ~17%; a flat 10%/20% cutoff would always fire."""
    baseline = PremiumBaseline(median_pct=17.3, p90_pct=23.0, samples=250)

    normal = parse_eastmoney_etf_row(
        {"代码": "159509", "名称": "纳指科技", "最新价": 2.35, "IOPV实时估值": 2.0},
        baseline=baseline,
    )
    rich = parse_eastmoney_etf_row(
        {"代码": "159509", "名称": "纳指科技", "最新价": 2.50, "IOPV实时估值": 2.0},
        baseline=baseline,
    )

    assert normal is not None and rich is not None
    # +17.5% ≈ median → show the number + median, no sermon.
    assert not normal.unusual
    assert "近一年中位" in normal.markdown_line()
    assert "注意" not in normal.markdown_line()
    # +25% ≥ P90 → one relative note.
    assert rich.unusual
    assert "相对自身历史偏高" in rich.markdown_line()
    assert "90 分位" in rich.markdown_line()


def test_missing_iopv_returns_none():
    assert (
        parse_eastmoney_etf_row(
            {"代码": "159509", "最新价": 2.8, "IOPV实时估值": "-"}
        )
        is None
    )


def test_short_label_stays_compact():
    q = PremiumQuote("159509", "纳指科技", 2.5, 2.0, 25.0)
    assert q.short_label() == "溢价 +25.0%"
    assert q.unusual is False  # no baseline → never unusual
