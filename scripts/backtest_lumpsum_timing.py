"""一次性大额资金：立刻买入 vs 等回撤再买 vs 分批建仓。

这回答的是和 `backtest_dca_vs_t.py` 不同的问题。那个测的是「每月工资到账，
立刻买还是攒着等」；这个测的是「手上已经有一大笔钱（年终奖、卖房款），
现在全买还是等跌」。两者数学不同：定投的现金流被动到达，入场时点不可选；
一次性资金的入场时点确实可选，且一次押注的方差大得多。

方法上刻意避开事后视角：不看「某一次等回撤是否更好」，而是遍历历史上
**每一个可能的入场日**，统计等待规则的胜率与平均差额。单个起点是轶事，
全部起点才是证据。

等待期的现金按 `--cash-apr` 计息（默认 1.5%，接近货币基金），否则等于
默认现金零收益，对等待策略不公平。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.history_quality import load_clean_history  # noqa: E402

TRADING_DAYS_YEAR = 252


def rolling_high(closes: np.ndarray, window: int) -> np.ndarray:
    """Trailing max, mirroring the monitor's 距一年高点."""
    return pd.Series(closes).rolling(window, min_periods=1).max().to_numpy()


def next_true_index(flags: np.ndarray) -> np.ndarray:
    """`out[i]` = smallest j >= i with flags[j], else len(flags)."""
    n = len(flags)
    out = np.empty(n + 1, dtype=np.int64)
    out[n] = n
    for i in range(n - 1, -1, -1):
        out[i] = i if flags[i] else out[i + 1]
    return out


def wait_for_dip(
    closes: np.ndarray,
    starts: np.ndarray,
    horizon: int,
    dip_pct: float,
    patience: int,
    cash_daily: float,
    high_window: int = TRADING_DAYS_YEAR,
) -> tuple[np.ndarray, np.ndarray, float]:
    """Hold cash until price is `dip_pct` below its trailing high, then buy all.

    Patience is finite on purpose: a real person capitulates rather than sitting
    in cash forever, and pretending otherwise would flatter the strategy.
    Returns (terminal value per 1 unit, waited days, share that never triggered).
    """
    highs = rolling_high(closes, high_window)
    triggered = closes <= highs * (1.0 - dip_pct / 100.0)
    nxt = next_true_index(triggered)

    ends = starts + horizon
    limits = np.minimum(starts + patience, ends)
    hit = nxt[starts]
    timed_out = hit >= limits
    buy_at = np.where(timed_out, limits, hit)
    waited = buy_at - starts
    # Cash compounds while waiting, then the whole pot buys in.
    value = (1.0 + cash_daily) ** waited * (closes[ends] / closes[buy_at])
    return value, waited, float(timed_out.mean())


def buy_in_tranches(
    closes: np.ndarray,
    starts: np.ndarray,
    horizon: int,
    months: int,
    cash_daily: float,
) -> np.ndarray:
    """Split the pot into `months` equal buys, one per ~21 trading days."""
    step = 21
    ends = starts + horizon
    value = np.zeros(len(starts), dtype=float)
    for k in range(months):
        offset = k * step
        buy_at = np.minimum(starts + offset, ends)
        slice_value = (1.0 + cash_daily) ** (buy_at - starts) * (
            closes[ends] / closes[buy_at]
        )
        value += slice_value / months
    return value


def run(
    code: str,
    name: str,
    market: str,
    years_list: list[float],
    dips: list[float],
    tranches: list[int],
    cash_apr: float,
) -> None:
    hist, report = load_clean_history(code, market)
    closes = pd.to_numeric(hist["close"], errors="coerce").to_numpy(dtype=float)
    dates = pd.to_datetime(hist["date"])
    cash_daily = (1.0 + cash_apr / 100.0) ** (1.0 / TRADING_DAYS_YEAR) - 1.0

    print(f"\n## {name}({code})　{len(closes)} 根　{dates.iloc[0].date()} ~ {dates.iloc[-1].date()}")

    for years in years_list:
        horizon = int(years * TRADING_DAYS_YEAR)
        if len(closes) < horizon + TRADING_DAYS_YEAR + 10:
            print(f"\n### 持有 {years:g} 年：样本不足，跳过")
            continue
        starts = np.arange(TRADING_DAYS_YEAR, len(closes) - horizon)
        now = closes[starts + horizon] / closes[starts]

        print(f"\n### 持有 {years:g} 年（{len(starts)} 个入场日，现金按 {cash_apr:g}% 计息）")
        print("| 策略 | 平均终值 | 中位终值 | 跑赢立刻买 | 最差终值 | 备注 |")
        print("|---|---:|---:|---:|---:|---|")
        print(
            f"| **立刻全买** | **{now.mean():.3f}x** | {np.median(now):.3f}x "
            f"| — | {now.min():.3f}x | 基准 |"
        )
        for dip in dips:
            val, waited, missed = wait_for_dip(
                closes, starts, horizon, dip, TRADING_DAYS_YEAR, cash_daily
            )
            win = float((val > now).mean() * 100.0)
            print(
                f"| 等回撤 -{dip:g}% | {val.mean():.3f}x | {np.median(val):.3f}x "
                f"| {win:.0f}% | {val.min():.3f}x "
                f"| 平均等 {waited.mean():.0f} 天，{missed * 100:.0f}% 等不到 |"
            )
        for months in tranches:
            val = buy_in_tranches(closes, starts, horizon, months, cash_daily)
            win = float((val > now).mean() * 100.0)
            print(
                f"| 分 {months} 个月建仓 | {val.mean():.3f}x | {np.median(val):.3f}x "
                f"| {win:.0f}% | {val.min():.3f}x | 无需判断时点 |"
            )


def main() -> None:
    ap = argparse.ArgumentParser(description="一次性资金：立刻买 vs 等回撤 vs 分批")
    ap.add_argument("--dips", default="10,15,20")
    ap.add_argument("--years", default="1,3,5")
    ap.add_argument("--tranches", default="3,6,12")
    ap.add_argument("--cash-apr", type=float, default=1.5)
    args = ap.parse_args()

    targets = [
        ("159941", "纳指ETF广发", "cn"),
        ("QQQM", "纳指100ETF", "us"),
        ("NVDA", "英伟达", "us"),
        ("600036", "招商银行", "cn"),
        ("510300", "沪深300ETF", "cn"),
        ("518880", "黄金ETF华安", "cn"),
    ]
    years = [float(x) for x in args.years.split(",")]
    dips = [float(x) for x in args.dips.split(",")]
    tranches = [int(x) for x in args.tranches.split(",")]

    print("# 一次性大额资金的入场时点回测")
    print()
    print("终值 = 期末资产 / 投入本金。遍历历史上每个可能的入场日，")
    print("避免用「今年正好跌了」这种事后视角下结论。")

    for code, name, market in targets:
        try:
            run(code, name, market, years, dips, tranches, args.cash_apr)
        except Exception as exc:
            print(f"\n## {name}({code}) 跳过：{exc}")


if __name__ == "__main__":
    main()
