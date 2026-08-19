"""Price path context for alerts: recent moves, stage, and relative pullbacks."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from src.perf import change_by_calendar_days, change_by_trading_days

MIN_HISTORY_FOR_RECENT = 80
RECENT_PERCENTILE = 10.0
MIN_3D_DROP_PCT = -2.0
MIN_20D_DROP_PCT = -5.0


def _fmt_pct(v: float | None) -> str:
    return "—" if v is None else f"{v:+.2f}%"


def _fmt_pos(v: float | None) -> str:
    return "—" if v is None else f"{v:.0f}%"


def _closes(hist: pd.DataFrame) -> pd.Series:
    return pd.to_numeric(hist["close"], errors="coerce").dropna()


def sparkline_from_hist(hist: pd.DataFrame, price: float) -> dict:
    tail = hist.tail(252)
    if tail.empty:
        return {"values": [], "years": []}
    values = [float(v) for v in tail["close"]]
    years = [int(pd.Timestamp(v).year) for v in tail["date"]]
    if values and abs(values[-1] - price) > 1e-9:
        values.append(float(price))
        years.append(years[-1])
    return {"values": values, "years": years}


def _ma_rising(closes: pd.Series) -> bool | None:
    if len(closes) < 35:
        return None
    now = float(closes.iloc[-30:].mean())
    prev = float(closes.iloc[-35:-5].mean())
    return now >= prev


def _drawdown_from_window(closes: pd.Series, window: int) -> float | None:
    if len(closes) < max(5, window // 4):
        return None
    high = float(closes.tail(window).max())
    price = float(closes.iloc[-1])
    if high <= 0:
        return None
    return (price / high - 1.0) * 100.0


def _year_position(closes: pd.Series, price: float) -> tuple[float | None, float | None]:
    window = closes.tail(252)
    if window.empty:
        return None, None
    low = float(min(window.min(), price))
    high = float(max(window.max(), price))
    if high <= 0:
        return None, None
    drawdown = (price / high - 1.0) * 100
    percentile = 100.0 if high == low else (price - low) / (high - low) * 100
    return percentile, drawdown


def stage_label(
    *,
    week: float | None,
    month: float | None,
    dd20: float | None,
    year_dd: float | None,
    ma_dev: float,
    ma_up: bool | None,
) -> str:
    """One-glance path description, not a buy/sell call."""
    if week is None or month is None:
        return "数据不足"
    near_high = year_dd is not None and year_dd > -8
    if near_high:
        if week < 0:
            return "接近一年高位、近周回落"
        return "接近一年高位震荡"
    if month >= 0 and week >= 0:
        if dd20 is not None and dd20 <= -5:
            return "上涨趋势中回调"
        return "持续走强"
    if month >= 0 and week < 0:
        return "上涨后开始走弱"
    if month < 0 and week >= 0:
        if year_dd is not None and year_dd <= -15:
            return "深度回撤后反弹"
        return "月内偏弱、近周反弹"
    if ma_dev < 0 or ma_up is False:
        return "持续下跌"
    return "回调但仍在MA30上"


@dataclass
class PriceContext:
    day1: float | None
    day3: float | None
    week: float | None
    month: float | None
    dd20: float | None
    year_dd: float | None
    year_pos: float | None
    ma_dev: float
    ma_up: bool | None
    stage: str
    spark: dict

    def markdown_block(self) -> str:
        ma_dir = "上行" if self.ma_up else ("下行" if self.ma_up is False else "方向不明")
        observe = "趋势中的回撤，可观察" if self.watch_dip else "偏弱回撤，先观察不急加"
        return (
            f"**阶段：** {self.stage}\n"
            f"**近期：** 日 {_fmt_pct(self.day1)}　3日 {_fmt_pct(self.day3)}　"
            f"周 {_fmt_pct(self.week)}　月 {_fmt_pct(self.month)}\n"
            f"**位置：** 距20日高 {_fmt_pct(self.dd20)}　"
            f"距一年高 {_fmt_pct(self.year_dd)}　年位 {_fmt_pos(self.year_pos)}\n"
            f"**均线：** MA30 {_fmt_pct(self.ma_dev)}（{ma_dir}）\n"
            f"**解读：** {observe}"
        )

    @property
    def watch_dip(self) -> bool:
        """True when a dip happens inside a still-constructive trend."""
        return self.ma_up is True and self.ma_dev >= -1.5 and (
            self.month is None or self.month >= 0
        )


def build_price_context(hist: pd.DataFrame, price: float, ma30: float) -> PriceContext:
    closes = _closes(hist)
    day1 = change_by_trading_days(hist, price, 1)
    day3 = change_by_trading_days(hist, price, 3)
    week = change_by_calendar_days(hist, price, 7)
    month = change_by_calendar_days(hist, price, 30)
    dd20 = _drawdown_from_window(closes, 20)
    year_pos, year_dd = _year_position(closes, price)
    ma_dev = (price - ma30) / ma30 * 100 if ma30 > 0 else 0.0
    ma_up = _ma_rising(closes)
    return PriceContext(
        day1=day1,
        day3=day3,
        week=week,
        month=month,
        dd20=dd20,
        year_dd=year_dd,
        year_pos=year_pos,
        ma_dev=ma_dev,
        ma_up=ma_up,
        stage=stage_label(
            week=week,
            month=month,
            dd20=dd20,
            year_dd=year_dd,
            ma_dev=ma_dev,
            ma_up=ma_up,
        ),
        spark=sparkline_from_hist(hist, price),
    )


def compose_alert_markdown(headline: str, ctx: PriceContext, extra: str = "") -> str:
    parts = [headline.strip(), ctx.markdown_block()]
    if extra.strip():
        parts.append(extra.strip())
    return "\n".join(parts)


def _percentile_rank(sample: pd.Series, value: float) -> float | None:
    clean = pd.to_numeric(sample, errors="coerce").dropna()
    if len(clean) < 40:
        return None
    return float((clean <= value).mean() * 100.0)


def detect_recent_pullback(
    hist: pd.DataFrame,
    price: float,
    *,
    percentile: float = RECENT_PERCENTILE,
) -> tuple[bool, str]:
    """
    Trigger when the recent dip is unusually large for *this* symbol.

    Uses the symbol's own 1-year distribution:
    - 3-day return in the worst `percentile`
    - or 20-day high drawdown in the worst `percentile`
    Plus a small absolute floor so quiet markets don't spam.
    """
    closes = _closes(hist)
    if len(closes) < MIN_HISTORY_FOR_RECENT:
        return False, ""

    r3 = closes.pct_change(3) * 100.0
    current_r3 = float(r3.iloc[-1]) if pd.notna(r3.iloc[-1]) else None
    hist_r3 = r3.dropna().iloc[:-1].tail(252)

    roll_high = closes.rolling(20).max()
    dd20 = (closes / roll_high - 1.0) * 100.0
    current_dd20 = float(dd20.iloc[-1]) if pd.notna(dd20.iloc[-1]) else None
    hist_dd20 = dd20.dropna().iloc[:-1].tail(252)

    reasons: list[str] = []
    if current_r3 is not None:
        rank = _percentile_rank(hist_r3, current_r3)
        if (
            rank is not None
            and rank <= percentile
            and current_r3 <= MIN_3D_DROP_PCT
        ):
            reasons.append(
                f"近3日 {_fmt_pct(current_r3)}，落在该标的近一年最差 {percentile:.0f}% 区间"
            )
    if current_dd20 is not None:
        rank = _percentile_rank(hist_dd20, current_dd20)
        if (
            rank is not None
            and rank <= percentile
            and current_dd20 <= MIN_20D_DROP_PCT
        ):
            reasons.append(
                f"距20日高点 {_fmt_pct(current_dd20)}，属于该标的近一年较深回撤"
            )
    if not reasons:
        return False, ""
    return True, "；".join(reasons)
