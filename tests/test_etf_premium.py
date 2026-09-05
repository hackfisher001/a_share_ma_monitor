"""ETF premium / discount parsing and labels."""

from src.etf_premium import (
    EXTREME_PREMIUM_PCT,
    HIGH_PREMIUM_PCT,
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
    assert quote.elevated and quote.extreme
    assert "溢价" in quote.short_label()
    assert "溢价很高" in quote.markdown_line()


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
    assert not quote.elevated
    assert "注意" not in quote.markdown_line()


def test_elevated_but_not_extreme_gets_a_milder_note():
    # Just above HIGH_PREMIUM_PCT, below EXTREME.
    mid = (HIGH_PREMIUM_PCT + EXTREME_PREMIUM_PCT) / 2
    iopv = 2.0
    price = iopv * (1 + mid / 100)
    quote = parse_eastmoney_etf_row(
        {
            "代码": "159509",
            "名称": "纳指科技",
            "最新价": price,
            "IOPV实时估值": iopv,
        }
    )

    assert quote is not None
    assert quote.elevated and not quote.extreme
    assert "溢价偏高" in quote.markdown_line()


def test_missing_iopv_returns_none():
    assert (
        parse_eastmoney_etf_row(
            {"代码": "159509", "最新价": 2.8, "IOPV实时估值": "-"}
        )
        is None
    )


def test_premium_quote_dataclass_thresholds():
    q = PremiumQuote("159509", "纳指科技", 2.5, 2.0, 25.0)
    assert q.extreme
    assert q.short_label() == "溢价 +25.0%"
