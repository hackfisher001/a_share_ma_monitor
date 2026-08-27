"""Long-horizon history cleaning: split gaps and sign flips."""

import pandas as pd

from src.history_quality import (
    _match_split,
    drop_leading_invalid,
    sanity_flags,
    split_adjust,
)


def _hist(closes: list[float]) -> pd.DataFrame:
    dates = pd.date_range("2014-01-01", periods=len(closes), freq="B")
    return pd.DataFrame({"date": dates, "close": closes})


def test_match_split_recognizes_common_ratios():
    assert _match_split(0.25) == 4
    assert _match_split(0.1) == 10
    assert _match_split(0.2) == 5
    assert _match_split(1.0) is None
    # A real 45% crash must not be mistaken for a 2:1 split.
    assert _match_split(0.55) is None


def test_split_adjust_makes_series_continuous():
    pre = [100.0, 101.0, 102.0]
    post = [25.5, 25.8, 26.0]  # 4:1 split
    df = _hist(pre + post)

    fixed, splits = split_adjust(df)

    assert len(splits) == 1
    assert splits[0][1] == 4
    ratio = fixed["close"].iloc[3] / fixed["close"].iloc[2]
    assert 0.9 < ratio < 1.1


def test_split_adjust_handles_two_splits():
    df = _hist([100.0, 102.0] + [25.5, 26.0] + [2.6, 2.65])  # 4:1 then 10:1

    fixed, splits = split_adjust(df)

    assert [f for _, f in splits] == [4, 10]
    assert fixed["close"].min() > 0
    assert fixed["close"].pct_change().dropna().min() > -0.5


def test_split_adjust_leaves_normal_series_untouched():
    df = _hist([10.0 + 0.1 * i for i in range(50)])

    fixed, splits = split_adjust(df)

    assert splits == []
    assert fixed["close"].equals(df["close"])


def test_drop_leading_invalid_starts_after_last_bad_bar():
    df = _hist([-0.01, 0.0, 1.0, 2.0, 3.0])

    trimmed, dropped = drop_leading_invalid(df)

    assert dropped == 2
    assert trimmed["close"].tolist() == [1.0, 2.0, 3.0]


def test_sanity_flags_reports_negative_and_split_like_moves():
    assert "存在非正价格" in sanity_flags(_hist([1.0, -1.0, 2.0]))[0]
    flags = sanity_flags(_hist([100.0, 100.0, 25.0, 25.0]))
    assert any("拆股" in f for f in flags)
    assert sanity_flags(_hist([10.0, 10.1, 10.2])) == []
