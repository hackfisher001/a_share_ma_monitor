from __future__ import annotations

from datetime import date

from src.dividends import (
    DividendPayment,
    build_profile,
    income_markdown,
    parse_fhps_rows,
)

TODAY = date(2026, 9, 3)


def _pay(period_end: str, per_share: float, ex_date: str | None) -> DividendPayment:
    return DividendPayment(
        period_end=date.fromisoformat(period_end),
        per_share=per_share,
        ex_date=date.fromisoformat(ex_date) if ex_date else None,
    )


# 招商银行真实记录：FY2024 一次付清，FY2025 改为中期+年度两次
CMB = [
    _pay("2023-12-31", 1.972, "2024-07-11"),
    _pay("2024-12-31", 2.000, "2025-07-11"),
    _pay("2025-06-30", 1.013, "2026-01-16"),
    _pay("2025-12-31", 1.003, "2026-07-10"),
    _pay("2026-06-30", 0.0, None),
]

# 中国移动真实记录：每年 6 月与 9 月各派一次，9 月那笔紧贴滚动窗口边界
CMCC = [
    _pay("2023-12-31", 2.1849, "2024-06-06"),
    _pay("2024-06-30", 2.3789, "2024-09-02"),
    _pay("2024-12-31", 2.2916, "2025-06-06"),
    _pay("2025-06-30", 2.5025, "2025-09-01"),
    _pay("2025-12-31", 2.2012, "2026-06-05"),
    _pay("2026-06-30", 2.5100, None),  # 董事会决议通过，尚未除权
]


def test_per_ten_shares_is_converted_to_per_share():
    rows = [{"报告期": "2025-12-31", "现金分红-现金分红比例": 10.03, "除权除息日": "2026-07-10"}]
    payments = parse_fhps_rows(rows)
    assert len(payments) == 1
    assert abs(payments[0].per_share - 1.003) < 1e-9
    assert payments[0].fiscal_year == 2025
    assert payments[0].is_annual


def test_declared_but_unexecuted_rows_are_kept_but_marked():
    rows = [{"报告期": "2026-06-30", "现金分红-现金分红比例": 25.1, "除权除息日": None}]
    payments = parse_fhps_rows(rows)
    assert len(payments) == 1 and not payments[0].executed


def test_rows_without_cash_dividend_are_excluded():
    rows = [{"报告期": "2026-06-30", "现金分红-现金分红比例": float("nan"), "除权除息日": None}]
    assert parse_fhps_rows(rows) == []


def test_interim_period_is_not_annual():
    rows = [{"报告期": "2025-06-30", "现金分红-现金分红比例": 10.13, "除权除息日": "2026-01-16"}]
    assert not parse_fhps_rows(rows)[0].is_annual


def test_switch_to_semiannual_is_not_read_as_a_cut():
    """单笔看像 2.000 -> 1.003 腰斩，按财报年度合计其实是 2.016，略增。"""
    profile = build_profile("600036", CMB, as_of=TODAY)

    assert profile.fiscal_year == 2025
    assert abs(profile.latest_per_share - 2.016) < 1e-9
    assert abs(profile.prior_per_share - 2.000) < 1e-9
    assert not profile.cut
    assert profile.change_pct is not None and profile.change_pct > 0


def test_payment_near_the_rolling_window_edge_is_not_read_as_a_cut():
    """滚动 365 天会得出 -54%（一笔 vs 两笔）；按财报年度应为 +0.7%。"""
    profile = build_profile("600941", CMCC, as_of=TODAY)

    assert profile.fiscal_year == 2025
    assert abs(profile.latest_per_share - 4.7037) < 1e-6
    assert abs(profile.prior_per_share - 4.6705) < 1e-6
    assert not profile.cut
    assert profile.change_pct is not None and 0 < profile.change_pct < 2


def test_unexecuted_future_payment_is_reported_separately():
    profile = build_profile("600941", CMCC, as_of=TODAY)
    assert abs(profile.pending_per_share - 2.51) < 1e-9
    # 未除权的部分不能计入当年股息率
    assert abs(profile.latest_per_share - 4.7037) < 1e-6


def test_a_real_cut_is_detected():
    payments = [
        _pay("2024-12-31", 2.000, "2025-07-11"),
        _pay("2025-12-31", 1.000, "2026-07-10"),
    ]
    profile = build_profile("600036", payments, as_of=TODAY)

    assert profile.cut
    assert profile.change_pct is not None and abs(profile.change_pct + 50.0) < 1e-9


def test_a_mild_decline_is_not_a_cut():
    """中国神华 FY2024 2.26 -> FY2025 2.01 属正常波动，不该报警。"""
    payments = [
        _pay("2024-12-31", 2.26, "2025-07-07"),
        _pay("2025-06-30", 0.98, "2025-11-10"),
        _pay("2025-12-31", 1.03, "2026-07-13"),
    ]
    profile = build_profile("601088", payments, as_of=TODAY)

    assert abs(profile.latest_per_share - 2.01) < 1e-9
    assert not profile.cut
    assert profile.change_pct is not None and -12 < profile.change_pct < -10


def test_incomplete_year_is_never_used_as_the_latest():
    """只发了中期的年度不能拿来和完整年度比，否则必然误报腰斩。"""
    payments = [
        _pay("2024-12-31", 2.000, "2025-07-11"),
        _pay("2025-06-30", 1.013, "2026-01-16"),  # FY2025 年度还没除权
    ]
    profile = build_profile("600036", payments, as_of=TODAY)

    assert profile.fiscal_year == 2024
    assert abs(profile.latest_per_share - 2.000) < 1e-9
    assert not profile.cut


def test_future_ex_date_is_not_counted_yet():
    payments = [
        _pay("2024-12-31", 2.000, "2025-07-11"),
        _pay("2025-12-31", 2.100, "2026-12-01"),  # 晚于 as_of
    ]
    profile = build_profile("600036", payments, as_of=TODAY)
    assert profile.fiscal_year == 2024
    assert profile.next_ex_date == date(2026, 12, 1)


def test_yield_uses_the_latest_complete_year():
    y = build_profile("600036", CMB, as_of=TODAY).yield_pct(41.22)
    assert y is not None and abs(y - 4.891) < 0.01


def test_yield_is_none_without_dividends():
    assert build_profile("QQQM", [], as_of=TODAY).yield_pct(100.0) is None


def test_change_is_none_when_prior_year_had_none():
    payments = [_pay("2025-12-31", 1.0, "2026-07-10")]
    profile = build_profile("600036", payments, as_of=TODAY)
    assert profile.change_pct is None and not profile.cut


def test_summary_mentions_a_cut_and_pending():
    payments = [
        _pay("2024-12-31", 2.0, "2025-07-11"),
        _pay("2025-12-31", 1.0, "2026-07-10"),
        _pay("2026-06-30", 0.5, None),
    ]
    text = build_profile("600036", payments, as_of=TODAY).summary(41.22)
    assert "股息率" in text and "分红显著下降" in text and "待除权" in text


def test_markdown_sorts_by_yield_and_flags_cuts():
    high = build_profile("600036", CMB, as_of=TODAY)
    cut = build_profile(
        "601088",
        [_pay("2024-12-31", 4.0, "2025-07-07"), _pay("2025-12-31", 1.0, "2026-07-13")],
        as_of=TODAY,
    )
    md = income_markdown([("中国神华", "601088", cut, 48.36), ("招商银行", "600036", high, 41.22)])

    assert md.index("600036") < md.index("601088")
    assert "⚠️ 分红下降" in md
    assert "唯一该让你重新考虑持有的信号" in md


def test_markdown_is_empty_without_any_yield():
    empty = build_profile("QQQM", [], as_of=TODAY)
    assert income_markdown([("纳指100ETF", "QQQM", empty, 292.0)]) == ""
