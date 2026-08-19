"""Fetch CN / HK / US daily history and latest price."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import akshare as ak
import pandas as pd
import requests

from src.signals import HIGH_WINDOW, QuoteSnapshot


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
    """Prefer CN-reachable sources; yfinance only as last resort."""
    code = _normalize_us(code)
    end = datetime.now()
    start = end - timedelta(days=lookback_days)

    # Aliyun often cannot reach Yahoo. Gold/BTC work via akshare foreign futures.
    foreign_map = {
        "GC=F": "XAU",
        "XAU": "XAU",
        "XAUUSD": "XAU",
        "BTC-USD": "BTC",
        "BTCUSD": "BTC",
        "BTC": "BTC",
    }

    def _from_foreign_hist():
        symbol = foreign_map.get(code)
        if not symbol:
            raise ValueError(f"非外盘标的: {code}")
        df = ak.futures_foreign_hist(symbol=symbol)
        if df is None or df.empty:
            raise ValueError(f"外盘日线为空: {symbol}")
        out = _ensure_ohlc(df)
        filtered = out[(out["date"] >= pd.Timestamp(start)) & (out["date"] <= pd.Timestamp(end))]
        if len(filtered) >= 30:
            return filtered.reset_index(drop=True)
        return out.reset_index(drop=True)

    def _from_ak_daily():
        if code in foreign_map:
            raise ValueError(f"跳过 stock_us_daily: {code}")
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

    loaders = []
    if code in foreign_map:
        loaders.append((_from_foreign_hist, 2))
    loaders.extend([(_from_ak_daily, 2), (_from_yf, 1)])

    errors: list[str] = []
    for loader, attempts in loaders:
        try:
            return _retry(loader, attempts=attempts, delay=1.0)
        except Exception as exc:
            errors.append(f"{loader.__name__}: {exc}")
    raise RuntimeError(f"{code} 美股日线拉取失败: " + " | ".join(errors))


@dataclass
class SpotQuote:
    price: float
    name: str = ""
    prev_close: float | None = None
    as_of: date | None = None


def _parse_sina_date(raw: str) -> date | None:
    raw = (raw or "").strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def lookup_spot_cn(code: str) -> tuple[float | None, str]:
    spot = lookup_spot_cn_detail(code)
    if spot is None:
        return None, ""
    return spot.price, spot.name


def parse_tencent_cn_quote(text: str) -> SpotQuote | None:
    """Parse Tencent qt.gtimg.cn payload: name, price, prev close, session date."""
    if '="' not in text:
        return None
    payload = text.split('="', 1)[1].rstrip('";\n')
    if not payload or payload == "1":
        return None
    parts = payload.split("~")
    if len(parts) < 5:
        return None
    name = parts[1].strip()
    try:
        price = float(parts[3])
    except (TypeError, ValueError):
        return None
    if price <= 0:
        return None
    prev_close = None
    try:
        prev_close = float(parts[4]) if parts[4] else None
    except (TypeError, ValueError):
        prev_close = None
    as_of = None
    if len(parts) > 30 and parts[30]:
        raw = parts[30].strip()[:8]
        try:
            as_of = datetime.strptime(raw, "%Y%m%d").date()
        except ValueError:
            as_of = None
    return SpotQuote(price=price, name=name, prev_close=prev_close, as_of=as_of)


def parse_sina_cn_quote(text: str) -> SpotQuote | None:
    if '=""' in text or '="' not in text:
        return None
    payload = text.split('="', 1)[1].rstrip('";\n')
    parts = payload.split(",")
    if len(parts) < 4:
        return None
    name = parts[0].strip()
    try:
        price = float(parts[3])
    except (TypeError, ValueError):
        return None
    if price <= 0:
        return None
    prev_close = None
    try:
        prev_close = float(parts[2]) if parts[2] else None
    except (TypeError, ValueError):
        prev_close = None
    as_of = _parse_sina_date(parts[30]) if len(parts) > 30 else None
    return SpotQuote(price=price, name=name, prev_close=prev_close, as_of=as_of)


def parse_eastmoney_cn_quote(payload: dict) -> SpotQuote | None:
    data = (payload or {}).get("data") or {}
    raw_price = data.get("f43")
    if raw_price in (None, "-", ""):
        return None
    try:
        price = float(raw_price) / 100.0
    except (TypeError, ValueError):
        return None
    if price <= 0:
        return None
    name = str(data.get("f58") or "").strip()
    prev_close = None
    try:
        if data.get("f60") not in (None, "-", ""):
            prev_close = float(data["f60"]) / 100.0
    except (TypeError, ValueError):
        prev_close = None
    return SpotQuote(price=price, name=name, prev_close=prev_close)


def lookup_spot_cn_detail(code: str) -> SpotQuote | None:
    """Aliyun often gets 403 from Sina hq; Tencent/Eastmoney stay reachable."""
    code = _normalize_cn(code)
    symbol = to_sina_symbol(code)
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"}

    def _from_tencent() -> SpotQuote | None:
        resp = requests.get(
            f"https://qt.gtimg.cn/q={symbol}",
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.qq.com"},
            timeout=8,
        )
        resp.raise_for_status()
        return parse_tencent_cn_quote(resp.content.decode("gbk", errors="ignore"))

    def _from_eastmoney() -> SpotQuote | None:
        market_id = "1" if symbol.startswith("sh") else "0"
        url = (
            "https://push2.eastmoney.com/api/qt/stock/get"
            f"?secid={market_id}.{code}&fields=f43,f57,f58,f60"
        )
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
        resp.raise_for_status()
        return parse_eastmoney_cn_quote(resp.json())

    def _from_sina() -> SpotQuote | None:
        resp = requests.get(
            f"https://hq.sinajs.cn/list={symbol}",
            headers=headers,
            timeout=8,
        )
        resp.raise_for_status()
        return parse_sina_cn_quote(resp.content.decode("gbk", errors="ignore"))

    for loader in (_from_tencent, _from_eastmoney, _from_sina):
        try:
            spot = loader()
        except Exception:
            continue
        if spot is not None:
            return spot
    return None


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
    high_252: float
    as_of: str
    hist: pd.DataFrame
    theme: str = ""


def fetch_history(code: str, market: str = "cn") -> pd.DataFrame:
    market = (market or "cn").strip().lower()
    if market == "hk":
        return fetch_daily_history_hk(code)
    if market == "us":
        return fetch_daily_history_us(code)
    return fetch_daily_history_cn(code)


def _session_date(market: str) -> date:
    if market == "us":
        return datetime.now(ZoneInfo("America/New_York")).date()
    return datetime.now(ZoneInfo("Asia/Shanghai")).date()


def attach_live_close(
    hist: pd.DataFrame,
    price: float,
    session: date,
) -> pd.DataFrame:
    """Put the live price on the current session bar so 1-day change uses 昨收."""
    if hist is None or hist.empty or price <= 0:
        return hist
    out = hist.copy()
    last = pd.Timestamp(out.iloc[-1]["date"]).normalize()
    current = pd.Timestamp(session).normalize()
    if last >= current:
        out.iat[-1, out.columns.get_loc("close")] = float(price)
        return out.reset_index(drop=True)
    row = {col: out.iloc[-1][col] for col in out.columns}
    row["date"] = current
    row["close"] = float(price)
    return pd.concat([out, pd.DataFrame([row])], ignore_index=True)


def build_bundle(code: str, name: str = "", market: str = "cn") -> QuoteBundle:
    market = (market or "cn").strip().lower()
    spot: SpotQuote | None = None
    if market == "hk":
        display = _normalize_hk(code)
        hist = fetch_daily_history_hk(display)
        spot_price, spot_name = lookup_spot_hk(display)
        if spot_price is not None:
            spot = SpotQuote(price=spot_price, name=spot_name, as_of=_session_date("hk"))
    elif market == "us":
        display = _normalize_us(code)
        hist = fetch_daily_history_us(display)
        spot_price, spot_name = lookup_spot_us(display)
        if spot_price is not None:
            spot = SpotQuote(price=spot_price, name=spot_name, as_of=_session_date("us"))
    else:
        display = _normalize_cn(code)
        hist = fetch_daily_history_cn(display)
        spot = lookup_spot_cn_detail(display)
        spot_name = spot.name if spot else ""

    if len(hist) < 30:
        raise ValueError(f"{display} 日线不足 30 根（当前 {len(hist)}）")

    if spot is not None:
        session = spot.as_of or _session_date(market)
        hist = attach_live_close(hist, spot.price, session)
        price = float(spot.price)
        display_name = name or spot.name or display
    else:
        price = float(hist.iloc[-1]["close"])
        display_name = name or display

    ma30 = float(hist["close"].tail(30).mean())
    # Close-based high, matching the rolling high used in the backtest.
    high_252 = float(max(hist["close"].tail(HIGH_WINDOW).max(), price))
    as_of = pd.Timestamp(hist.iloc[-1]["date"]).strftime("%Y-%m-%d")
    return QuoteBundle(
        code=display,
        name=display_name,
        market=market,
        price=price,
        ma30=ma30,
        high_252=high_252,
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
        high_252=b.high_252,
    )
