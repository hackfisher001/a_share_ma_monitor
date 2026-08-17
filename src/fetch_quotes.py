"""Fetch A-share daily history and latest price via akshare.

Prefer Sina / Tencent endpoints — Eastmoney is often blocked on cloud IPs.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta

import akshare as ak
import pandas as pd
import requests

from src.signals import QuoteSnapshot


def _normalize_code(code: str) -> str:
    return str(code).strip().zfill(6)


def to_sina_symbol(code: str) -> str:
    """600519 -> sh600519; 000001 -> sz000001; 920000 -> bj920000."""
    code = _normalize_code(code)
    if code.startswith(("5", "6", "9")):
        return f"sh{code}"
    if code.startswith(("4", "8")):
        return f"bj{code}"
    return f"sz{code}"


def _retry(fn, *, attempts: int = 3, delay: float = 1.5):
    last_exc: Exception | None = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            if i < attempts - 1:
                time.sleep(delay * (i + 1))
    assert last_exc is not None
    raise last_exc


def fetch_daily_history(code: str, lookback_days: int = 120) -> pd.DataFrame:
    """Return recent daily OHLCV (enough rows for MA30)."""
    code = _normalize_code(code)
    symbol = to_sina_symbol(code)
    end = datetime.now()
    start = end - timedelta(days=lookback_days)
    start_s = start.strftime("%Y%m%d")
    end_s = end.strftime("%Y%m%d")

    def _from_sina():
        df = ak.stock_zh_a_daily(
            symbol=symbol,
            start_date=start_s,
            end_date=end_s,
            adjust="qfq",
        )
        if df is None or df.empty:
            raise ValueError(f"新浪日线为空: {symbol}")
        # columns: date, open, high, low, close, volume, ...
        out = df.rename(columns=str.lower).copy()
        out["date"] = pd.to_datetime(out["date"])
        return out.sort_values("date").reset_index(drop=True)

    def _from_tx():
        df = ak.stock_zh_a_hist_tx(
            symbol=symbol,
            start_date=start_s,
            end_date=end_s,
            adjust="qfq",
        )
        if df is None or df.empty:
            raise ValueError(f"腾讯日线为空: {symbol}")
        out = df.rename(columns=str.lower).copy()
        out["date"] = pd.to_datetime(out["date"])
        return out.sort_values("date").reset_index(drop=True)

    def _from_em():
        df = ak.stock_zh_a_hist(
            symbol=code,
            period="daily",
            start_date=start_s,
            end_date=end_s,
            adjust="qfq",
        )
        if df is None or df.empty:
            raise ValueError(f"东财日线为空: {code}")
        rename = {
            "日期": "date",
            "开盘": "open",
            "收盘": "close",
            "最高": "high",
            "最低": "low",
            "成交量": "volume",
        }
        out = df.rename(columns=rename)
        out["date"] = pd.to_datetime(out["date"])
        return out.sort_values("date").reset_index(drop=True)

    errors: list[str] = []
    for loader in (_from_sina, _from_tx, _from_em):
        try:
            return _retry(loader, attempts=2, delay=1.0)
        except Exception as exc:
            errors.append(f"{loader.__name__}: {exc}")
    raise RuntimeError(f"{code} 日线拉取失败: " + " | ".join(errors))


def lookup_spot(code: str) -> tuple[float | None, str]:
    """
    Latest price via Sina hq API (lightweight, no full-market board).
    Returns (price, name).
    """
    code = _normalize_code(code)
    symbol = to_sina_symbol(code)
    url = f"https://hq.sinajs.cn/list={symbol}"
    try:
        resp = requests.get(
            url,
            headers={"Referer": "https://finance.sina.com.cn"},
            timeout=10,
        )
        resp.raise_for_status()
        # var hq_str_sh600519="贵州茅台,open,...,price,...";
        text = resp.content.decode("gbk", errors="ignore")
        if '=""' in text or '="' not in text:
            return None, ""
        payload = text.split('="', 1)[1].rstrip('";\n')
        parts = payload.split(",")
        if len(parts) < 4:
            return None, ""
        name = parts[0].strip()
        # field 3 is current price on sina hq
        price = float(parts[3])
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
