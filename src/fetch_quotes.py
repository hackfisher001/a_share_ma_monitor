"""Fetch A-share daily history and latest price via akshare."""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from functools import lru_cache

import akshare as ak
import pandas as pd

from src.signals import QuoteSnapshot


def _normalize_code(code: str) -> str:
    return str(code).strip().zfill(6)


def _retry(fn, *, attempts: int = 3, delay: float = 1.5):
    last_exc: Exception | None = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:  # network / proxy flakiness
            last_exc = exc
            if i < attempts - 1:
                time.sleep(delay * (i + 1))
    assert last_exc is not None
    raise last_exc


@lru_cache(maxsize=1)
def load_spot_board() -> pd.DataFrame:
    """Cache A-share spot board for one process run."""

    def _load():
        spot = ak.stock_zh_a_spot_em()
        spot = spot.copy()
        spot["代码"] = spot["代码"].astype(str).str.zfill(6)
        return spot

    return _retry(_load)


def fetch_daily_history(code: str, lookback_days: int = 120) -> pd.DataFrame:
    """Return recent daily OHLCV for a symbol (enough rows for MA30)."""
    code = _normalize_code(code)
    end = datetime.now()
    start = end - timedelta(days=lookback_days)
    start_s = start.strftime("%Y%m%d")
    end_s = end.strftime("%Y%m%d")

    def _load():
        df = ak.stock_zh_a_hist(
            symbol=code,
            period="daily",
            start_date=start_s,
            end_date=end_s,
            adjust="qfq",
        )
        if df is None or df.empty:
            raise ValueError(f"无日线数据: {code}")
        return df

    df = _retry(_load)
    rename = {
        "日期": "date",
        "开盘": "open",
        "收盘": "close",
        "最高": "high",
        "最低": "low",
        "成交量": "volume",
    }
    df = df.rename(columns=rename)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    return df


def lookup_spot(code: str) -> tuple[float | None, str]:
    """Return (latest_price, name) from spot board if available."""
    code = _normalize_code(code)
    try:
        spot = load_spot_board()
        row = spot.loc[spot["代码"] == code]
        if row.empty:
            return None, ""
        price = float(row.iloc[0]["最新价"])
        name = str(row.iloc[0].get("名称", "") or "")
        if price <= 0:
            return None, name
        return price, name
    except Exception:
        return None, ""


def build_snapshot(code: str, name: str = "") -> QuoteSnapshot:
    code = _normalize_code(code)
    hist = fetch_daily_history(code)
    if len(hist) < 30:
        raise ValueError(f"{code} 日线不足 30 根，无法计算 MA30（当前 {len(hist)}）")

    ma30 = float(hist["close"].tail(30).mean())
    spot_price, spot_name = lookup_spot(code)
    if spot_price is not None:
        price = spot_price
    else:
        price = float(hist.iloc[-1]["close"])

    if not name:
        name = spot_name or code

    as_of = hist.iloc[-1]["date"].strftime("%Y-%m-%d")
    return QuoteSnapshot(
        code=code,
        name=name,
        price=price,
        ma30=ma30,
        as_of=as_of,
        history_rows=len(hist),
    )
