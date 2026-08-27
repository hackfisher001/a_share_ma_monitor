"""Clean long-horizon price history for research backtests.

akshare's US feed is unusable at 12-year spans in both modes: `qfq` applies an
additive dividend adjustment that drives NVDA's 2014 close below zero, while the
unadjusted series keeps raw split gaps (NVDA 4:1 in 2021, 10:1 in 2024). We take
the unadjusted series and undo splits multiplicatively, which never changes sign.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

# Ratios seen in real corporate actions; a genuine crash rarely lands this close.
SPLIT_FACTORS = (2, 3, 4, 5, 6, 7, 8, 10, 15, 20, 1.5)
SPLIT_TOLERANCE = 0.02
SPLIT_DROP_TRIGGER = 0.62
REVERSE_SPLIT_TRIGGER = 1.62


@dataclass
class HistoryReport:
    code: str
    rows: int
    start: str
    end: str
    span_years: float
    splits: list[tuple[str, float]] = field(default_factory=list)
    dropped_leading: int = 0
    source: str = ""

    def describe(self) -> str:
        parts = [f"{self.rows} 根 {self.start}~{self.end} ({self.span_years:.1f}年)"]
        if self.splits:
            got = ", ".join(f"{d} 1:{f:g}" for d, f in self.splits)
            parts.append(f"还原拆股 {len(self.splits)} 次[{got}]")
        if self.dropped_leading:
            parts.append(f"丢弃前段异常 {self.dropped_leading} 根")
        return "；".join(parts)


def _match_split(ratio: float) -> float | None:
    """Return the split factor if `ratio` looks like a corporate action."""
    if ratio <= 0:
        return None
    if ratio < SPLIT_DROP_TRIGGER:
        target = 1.0 / ratio
    elif ratio > REVERSE_SPLIT_TRIGGER:
        target = ratio
    else:
        return None
    for factor in SPLIT_FACTORS:
        if abs(target - factor) / factor <= SPLIT_TOLERANCE:
            return factor if ratio < 1 else -factor
    return None


def split_adjust(df: pd.DataFrame) -> tuple[pd.DataFrame, list[tuple[str, float]]]:
    """Undo split gaps so the series is continuous and strictly positive."""
    out = df.copy().reset_index(drop=True)
    closes = out["close"].to_numpy(dtype=float).copy()
    detected: list[tuple[str, float]] = []

    for i in range(len(closes) - 1, 0, -1):
        prev = closes[i - 1]
        if prev <= 0:
            continue
        ratio = closes[i] / prev
        factor = _match_split(ratio)
        if factor is None:
            continue
        if factor > 0:
            closes[:i] = closes[:i] / factor
            detected.append((str(out["date"].iloc[i].date()), factor))
        else:
            closes[:i] = closes[:i] * (-factor)
            detected.append((str(out["date"].iloc[i].date()), 1.0 / (-factor)))

    out["close"] = closes
    detected.reverse()
    return out, detected


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    out = df.rename(columns=str.lower).copy()
    if "date" not in out.columns or "close" not in out.columns:
        raise ValueError("缺少 date/close 列")
    out["date"] = pd.to_datetime(out["date"])
    if getattr(out["date"].dt, "tz", None) is not None:
        out["date"] = out["date"].dt.tz_localize(None)
    out["close"] = pd.to_numeric(out["close"], errors="coerce")
    out = out.dropna(subset=["close"])
    return out.sort_values("date").reset_index(drop=True)[["date", "close"]]


def drop_leading_invalid(df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Start after the last non-positive close so no bar can flip sign."""
    bad = df.index[df["close"] <= 0]
    if len(bad) == 0:
        return df, 0
    cut = int(bad[-1]) + 1
    return df.iloc[cut:].reset_index(drop=True), cut


def load_clean_history(
    code: str,
    market: str,
    *,
    lookback_days: int = 4500,
) -> tuple[pd.DataFrame, HistoryReport]:
    """Longest trustworthy daily series available for `code`."""
    import akshare as ak

    from src.fetch_quotes import (
        _normalize_cn,
        _normalize_us,
        fetch_daily_history_cn,
        to_sina_symbol,
    )

    if market == "us":
        symbol = _normalize_us(code)
        raw = ak.stock_us_daily(symbol=symbol, adjust="")
        if raw is None or len(raw) == 0:
            raise ValueError(f"美股历史为空: {symbol}")
        df = _normalize(raw)
        source = "sina_us_unadjusted+split_fix"
    else:
        symbol = _normalize_cn(code)
        try:
            raw = ak.stock_zh_a_daily(
                symbol=to_sina_symbol(symbol),
                start_date="20100101",
                end_date=pd.Timestamp.today().strftime("%Y%m%d"),
                adjust="qfq",
            )
            df = _normalize(raw)
            source = "sina_cn_qfq"
        except Exception:
            df = _normalize(fetch_daily_history_cn(symbol, lookback_days=lookback_days))
            source = "fallback_cn"

    cutoff = pd.Timestamp.today() - pd.Timedelta(days=lookback_days)
    trimmed = df[df["date"] >= cutoff]
    if len(trimmed) >= 250:
        df = trimmed.reset_index(drop=True)

    df, splits = split_adjust(df)
    df, dropped = drop_leading_invalid(df)

    if df.empty or len(df) < 250:
        raise ValueError(f"{code} 清洗后仅剩 {len(df)} 根，不足以回测")
    if (df["close"] <= 0).any():
        raise ValueError(f"{code} 仍存在非正价格")

    span = (df["date"].iloc[-1] - df["date"].iloc[0]).days / 365.25
    report = HistoryReport(
        code=code,
        rows=len(df),
        start=str(df["date"].iloc[0].date()),
        end=str(df["date"].iloc[-1].date()),
        span_years=span,
        splits=splits,
        dropped_leading=dropped,
        source=source,
    )
    return df, report


def sanity_flags(df: pd.DataFrame) -> list[str]:
    """Cheap red flags worth printing next to any backtest result."""
    flags: list[str] = []
    closes = pd.to_numeric(df["close"], errors="coerce")
    if (closes <= 0).any():
        flags.append("存在非正价格")
    ret = closes.pct_change()
    if (ret < -0.5).any():
        worst = df.loc[ret.idxmin(), "date"]
        flags.append(f"单日跌幅超 50%（{pd.Timestamp(worst).date()}），可能是未还原的拆股")
    if (ret > 1.0).any():
        flags.append("单日涨幅超 100%，可能是数据错误")
    return flags
