"""Fetch CN / HK daily history and latest price via akshare."""

from __future__ import annotations

import time
from datetime import datetime, timedelta

import akshare as ak
import pandas as pd
import requests

from src.signals import QuoteSnapshot


def _normalize_cn(code: str) -> str:
    return str(code).strip().zfill(6)


def _normalize_hk(code: str) -> str:
    return str(code).strip().zfill(5)


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


def fetch_daily_history_cn(code: str, lookback_days: int = 120) -> pd.DataFrame:
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

    errors: list[str] = []
    for loader in (_from_sina, _from_tx):
        try:
            return _retry(loader, attempts=2, delay=1.0)
        except Exception as exc:
            errors.append(f"{loader.__name__}: {exc}")
    raise RuntimeError(f"{code} 日线拉取失败: " + " | ".join(errors))


def fetch_daily_history_hk(code: str, lookback_days: int = 180) -> pd.DataFrame:
    code = _normalize_hk(code)
    end = datetime.now()
    start = end - timedelta(days=lookback_days)
    start_s = start.strftime("%Y%m%d")
    end_s = end.strftime("%Y%m%d")

    def _from_hk_daily():
        # Sina-backed; more reliable than Eastmoney on some networks
        df = ak.stock_hk_daily(symbol=code, adjust="qfq")
        if df is None or df.empty:
            raise ValueError(f"港股 daily 为空: {code}")
        out = df.rename(columns=str.lower).copy()
        out["date"] = pd.to_datetime(out["date"])
        out = out[(out["date"] >= pd.Timestamp(start)) & (out["date"] <= pd.Timestamp(end))]
        if out.empty:
            raise ValueError(f"港股 daily 无近期数据: {code}")
        return out.sort_values("date").reset_index(drop=True)

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
        out = df.rename(columns=rename)
        out["date"] = pd.to_datetime(out["date"])
        return out.sort_values("date").reset_index(drop=True)

    def _from_yf():
        import yfinance as yf

        ticker = yf.Ticker(f"{code}.HK")
        df = ticker.history(period="6mo", auto_adjust=True)
        if df is None or df.empty:
            raise ValueError(f"yfinance 港股为空: {code}")
        out = df.reset_index()
        out = out.rename(columns={"Date": "date", "Close": "close", "Open": "open", "High": "high", "Low": "low", "Volume": "volume"})
        out["date"] = pd.to_datetime(out["date"]).dt.tz_localize(None)
        return out.sort_values("date").reset_index(drop=True)

    errors: list[str] = []
    for loader in (_from_hk_daily, _from_em, _from_yf):
        try:
            return _retry(loader, attempts=2, delay=1.0)
        except Exception as exc:
            errors.append(f"{loader.__name__}: {exc}")
    raise RuntimeError(f"{code} 港股日线拉取失败: " + " | ".join(errors))



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
    """Sina HK quote: hk00700."""
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
        # hk format: name, ..., price often at index 6
        if len(parts) < 7:
            return None, ""
        name = parts[1].strip() or parts[0].strip()
        price = float(parts[6])
        if price <= 0:
            return None, name
        return price, name
    except Exception:
        return None, ""


def build_snapshot(code: str, name: str = "", market: str = "cn") -> QuoteSnapshot:
    market = (market or "cn").strip().lower()
    if market == "hk":
        code = _normalize_hk(code)
        hist = fetch_daily_history_hk(code)
        spot_price, spot_name = lookup_spot_hk(code)
        display_code = code
    else:
        code = _normalize_cn(code)
        hist = fetch_daily_history_cn(code)
        spot_price, spot_name = lookup_spot_cn(code)
        display_code = code

    if len(hist) < 30:
        raise ValueError(f"{display_code} 日线不足 30 根，无法计算 MA30（当前 {len(hist)}）")

    ma30 = float(hist["close"].tail(30).mean())
    if spot_price is not None:
        price = spot_price
    else:
        price = float(hist.iloc[-1]["close"])

    if not name:
        name = spot_name or display_code

    as_of = hist.iloc[-1]["date"].strftime("%Y-%m-%d")
    return QuoteSnapshot(
        code=display_code,
        name=name,
        price=price,
        ma30=ma30,
        as_of=as_of,
        history_rows=len(hist),
    )
