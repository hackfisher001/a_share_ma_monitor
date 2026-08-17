"""Fetch CN / HK / US daily history and latest price."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta

import akshare as ak
import pandas as pd
import requests

from src.signals import QuoteSnapshot


def _normalize_cn(code: str) -> str:
    return str(code).strip().zfill(6)


def _normalize_hk(code: str) -> str:
    return str(code).strip().zfill(5)


def _normalize_us(code: str) -> str:
    return str(code).strip().upper()


def to_sina_symbol(code: str) -> str:
    """600519 -> sh600519; 000001 -> sz000001; 920000 -> bj920000."""
    code = _normalize_cn(code)
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


def _ensure_ohlc(df: pd.DataFrame) -> pd.DataFrame:
    out = df.rename(columns=str.lower).copy()
    if "date" not in out.columns:
        raise ValueError("缺少 date 列")
    out["date"] = pd.to_datetime(out["date"])
    if getattr(out["date"].dt, "tz", None) is not None:
        out["date"] = out["date"].dt.tz_localize(None)
    return out.sort_values("date").reset_index(drop=True)


def fetch_daily_history_cn(code: str, lookback_days: int = 420) -> pd.DataFrame:
    code = _normalize_cn(code)
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
        return _ensure_ohlc(df)

    def _from_tx():
        df = ak.stock_zh_a_hist_tx(
            symbol=symbol,
            start_date=start_s,
            end_date=end_s,
            adjust="qfq",
        )
        if df is None or df.empty:
            raise ValueError(f"腾讯日线为空: {symbol}")
        return _ensure_ohlc(df)

    errors: list[str] = []
    for loader in (_from_sina, _from_tx):
        try:
            return _retry(loader, attempts=2, delay=1.0)
        except Exception as exc:
            errors.append(f"{loader.__name__}: {exc}")
    raise RuntimeError(f"{code} 日线拉取失败: " + " | ".join(errors))


def fetch_daily_history_hk(code: str, lookback_days: int = 420) -> pd.DataFrame:
    code = _normalize_hk(code)
    end = datetime.now()
    start = end - timedelta(days=lookback_days)
    start_s = start.strftime("%Y%m%d")
    end_s = end.strftime("%Y%m%d")

    def _from_hk_daily():
        df = ak.stock_hk_daily(symbol=code, adjust="qfq")
        if df is None or df.empty:
            raise ValueError(f"港股 daily 为空: {code}")
        out = _ensure_ohlc(df)
        out = out[(out["date"] >= pd.Timestamp(start)) & (out["date"] <= pd.Timestamp(end))]
        if out.empty:
            raise ValueError(f"港股 daily 无近期数据: {code}")
        return out.reset_index(drop=True)

    def _from_em():
        df = ak.stock_hk_hist(
            symbol=code,
            period="daily",
            start_date=start_s,
            end_date=end_s,
            adjust="qfq",
        )
        if df is None or df.empty:
            raise ValueError(f"港股东财日线为空: {code}")
        rename = {
            "日期": "date",
            "开盘": "open",
            "收盘": "close",
            "最高": "high",
            "最低": "low",
            "成交量": "volume",
        }
        return _ensure_ohlc(df.rename(columns=rename))

    def _from_yf():
        import yfinance as yf

        ticker = yf.Ticker(f"{code}.HK")
        df = ticker.history(period="2y", auto_adjust=True)
        if df is None or df.empty:
            raise ValueError(f"yfinance 港股为空: {code}")
        out = df.reset_index().rename(
            columns={
                "Date": "date",
                "Close": "close",
                "Open": "open",
                "High": "high",
                "Low": "low",
                "Volume": "volume",
            }
        )
        return _ensure_ohlc(out)

    errors: list[str] = []
    for loader in (_from_hk_daily, _from_em, _from_yf):
        try:
            return _retry(loader, attempts=2, delay=1.0)
        except Exception as exc:
            errors.append(f"{loader.__name__}: {exc}")
    raise RuntimeError(f"{code} 港股日线拉取失败: " + " | ".join(errors))


def fetch_daily_history_us(code: str, lookback_days: int = 420) -> pd.DataFrame:
    """Prefer akshare US daily (reachable from CN cloud); yfinance as last resort."""
    code = _normalize_us(code)
    end = datetime.now()
    start = end - timedelta(days=lookback_days)

    def _from_ak_daily():
        df = ak.stock_us_daily(symbol=code, adjust="qfq")
        if df is None or df.empty:
            raise ValueError(f"akshare 美股 daily 为空: {code}")
        out = _ensure_ohlc(df)
        filtered = out[(out["date"] >= pd.Timestamp(start)) & (out["date"] <= pd.Timestamp(end))]
        if len(filtered) >= 30:
            return filtered.reset_index(drop=True)
        return out.reset_index(drop=True)

    def _from_yf():
        import yfinance as yf

        ticker = yf.Ticker(code)
        df = ticker.history(period="2y", auto_adjust=True)
        if df is None or df.empty:
            raise ValueError(f"yfinance 美股为空: {code}")
        out = df.reset_index().rename(
            columns={
                "Date": "date",
                "Close": "close",
                "Open": "open",
                "High": "high",
                "Low": "low",
                "Volume": "volume",
            }
        )
        return _ensure_ohlc(out)

    errors: list[str] = []
    for loader, attempts in ((_from_ak_daily, 2), (_from_yf, 1)):
        try:
            return _retry(loader, attempts=attempts, delay=1.0)
        except Exception as exc:
            errors.append(f"{loader.__name__}: {exc}")
    raise RuntimeError(f"{code} 美股日线拉取失败: " + " | ".join(errors))


def lookup_spot_cn(code: str) -> tuple[float | None, str]:
    code = _normalize_cn(code)
    symbol = to_sina_symbol(code)
    url = f"https://hq.sinajs.cn/list={symbol}"
    try:
        resp = requests.get(
            url,
            headers={"Referer": "https://finance.sina.com.cn"},
            timeout=10,
        )
        resp.raise_for_status()
        text = resp.content.decode("gbk", errors="ignore")
        if '=""' in text or '="' not in text:
            return None, ""
        payload = text.split('="', 1)[1].rstrip('";\n')
        parts = payload.split(",")
        if len(parts) < 4:
            return None, ""
        name = parts[0].strip()
        price = float(parts[3])
        if price <= 0:
            return None, name
        return price, name
    except Exception:
        return None, ""


def lookup_spot_hk(code: str) -> tuple[float | None, str]:
    code = _normalize_hk(code)
    symbol = f"hk{code}"
    url = f"https://hq.sinajs.cn/list={symbol}"
    try:
        resp = requests.get(
            url,
            headers={"Referer": "https://finance.sina.com.cn"},
            timeout=10,
        )
        resp.raise_for_status()
        text = resp.content.decode("gbk", errors="ignore")
        if '=""' in text or '="' not in text:
            return None, ""
        payload = text.split('="', 1)[1].rstrip('";\n')
        parts = payload.split(",")
        if len(parts) < 7:
            return None, ""
        name = parts[1].strip() or parts[0].strip()
        price = float(parts[6])
        if price <= 0:
            return None, name
        return price, name
    except Exception:
        return None, ""


def lookup_spot_us(code: str) -> tuple[float | None, str]:
    """CN cloud often cannot reach Yahoo; leave spot empty and use daily close."""
    return None, ""


@dataclass
class QuoteBundle:
    code: str
    name: str
    market: str
    price: float
    ma30: float
    as_of: str
    hist: pd.DataFrame


def fetch_history(code: str, market: str = "cn") -> pd.DataFrame:
    market = (market or "cn").strip().lower()
    if market == "hk":
        return fetch_daily_history_hk(code)
    if market == "us":
        return fetch_daily_history_us(code)
    return fetch_daily_history_cn(code)


def build_bundle(code: str, name: str = "", market: str = "cn") -> QuoteBundle:
    market = (market or "cn").strip().lower()
    if market == "hk":
        display = _normalize_hk(code)
        hist = fetch_daily_history_hk(display)
        spot_price, spot_name = lookup_spot_hk(display)
    elif market == "us":
        display = _normalize_us(code)
        hist = fetch_daily_history_us(display)
        spot_price, spot_name = lookup_spot_us(display)
    else:
        display = _normalize_cn(code)
        hist = fetch_daily_history_cn(display)
        spot_price, spot_name = lookup_spot_cn(display)

    if len(hist) < 30:
        raise ValueError(f"{display} 日线不足 30 根（当前 {len(hist)}）")

    ma30 = float(hist["close"].tail(30).mean())
    price = float(spot_price) if spot_price is not None else float(hist.iloc[-1]["close"])
    display_name = name or spot_name or display
    as_of = hist.iloc[-1]["date"].strftime("%Y-%m-%d")
    return QuoteBundle(
        code=display,
        name=display_name,
        market=market,
        price=price,
        ma30=ma30,
        as_of=as_of,
        hist=hist,
    )


def build_snapshot(code: str, name: str = "", market: str = "cn") -> QuoteSnapshot:
    b = build_bundle(code, name=name, market=market)
    return QuoteSnapshot(
        code=b.code,
        name=b.name,
        price=b.price,
        ma30=b.ma30,
        as_of=b.as_of,
        history_rows=len(b.hist),
    )
