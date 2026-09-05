"""ETF trading premium / discount vs IOPV (实时估值).

Most A-share ETFs hug NAV. QDII wraps of overseas indices often do not —
159509 (纳指科技) recently sat around +17% median premium for a full year.
A fixed 10%/20% warning would therefore fire on almost every card and become
noise. We always show the number; a "偏高" note only appears when today's
premium is rich *relative to this symbol's own* trailing year (top decile).

East Money's `基金折价率` is signed the opposite of everyday language:
negative 折价率 means the share is trading *above* IOPV (a premium). We flip
the sign and always speak in 溢价率 terms.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import akshare as ak
import pandas as pd

log = logging.getLogger("ma_monitor")

# Full-market spot pull is expensive; one scan shares the same table.
_CACHE_TTL_SEC = 300.0
_cache_at: float = 0.0
_cache_rows: dict[str, "PremiumQuote"] = {}

# Historical (price vs published NAV) baseline — slow, refresh rarely.
_HIST_TTL_SEC = 6 * 3600.0
_hist_at: dict[str, float] = {}
_hist_rows: dict[str, "PremiumBaseline"] = {}

HIST_WINDOW = 250
# Warn only when current premium is in this symbol's own top decile.
UNUSUAL_PERCENTILE = 90.0


@dataclass(frozen=True)
class PremiumBaseline:
    """Trailing distribution of (收盘价 / 单位净值 - 1), in percent."""

    median_pct: float
    p90_pct: float
    samples: int


@dataclass(frozen=True)
class PremiumQuote:
    code: str
    name: str
    price: float
    iopv: float
    # (price - iopv) / iopv * 100. Positive = trading above NAV.
    premium_pct: float
    baseline: PremiumBaseline | None = None

    @property
    def unusual(self) -> bool:
        """True when rich vs this ETF's own recent history, not a global cutoff."""
        if self.baseline is None or self.baseline.samples < 60:
            return False
        return self.premium_pct >= self.baseline.p90_pct

    def markdown_line(self) -> str:
        line = (
            f"**溢价：** {self.premium_pct:+.1f}%　"
            f"现价 {self.price:.3f} / IOPV {self.iopv:.3f}"
        )
        if self.baseline is not None and self.baseline.samples >= 60:
            line += f"　近一年中位 {self.baseline.median_pct:+.1f}%"
        if self.unusual:
            line += (
                "\n**注意：** 相对自身历史偏高"
                f"（≥近一年 {UNUSUAL_PERCENTILE:.0f} 分位 "
                f"{self.baseline.p90_pct:+.1f}%），"
                "急跌更可能是溢价回落"
            )
        return line

    def short_label(self) -> str:
        return f"溢价 {self.premium_pct:+.1f}%"


def _to_float(raw) -> float | None:
    if raw is None:
        return None
    try:
        text = str(raw).strip().replace("%", "")
        if text in {"", "-", "--", "None", "nan"}:
            return None
        return float(text)
    except (TypeError, ValueError):
        return None


def parse_eastmoney_etf_row(
    row: dict,
    *,
    baseline: PremiumBaseline | None = None,
) -> PremiumQuote | None:
    """Build a PremiumQuote from one fund_etf_spot_em row (dict-like)."""
    code = str(row.get("代码") or "").strip().zfill(6)
    price = _to_float(row.get("最新价"))
    iopv = _to_float(row.get("IOPV实时估值"))
    if not code or price is None or iopv is None or iopv <= 0 or price <= 0:
        return None
    # Prefer computing from price/IOPV so the sign is unambiguous.
    premium = (price / iopv - 1.0) * 100.0
    # Cross-check against 基金折价率 when present: ours should ≈ -折价率.
    listed = _to_float(row.get("基金折价率"))
    if listed is not None and abs(premium + listed) > 1.5:
        log.warning(
            "ETF %s 溢价口径异常: 现价/IOPV=%+.2f%% 与 -折价率=%+.2f%% 不一致",
            code,
            premium,
            -listed,
        )
    return PremiumQuote(
        code=code,
        name=str(row.get("名称") or "").strip(),
        price=price,
        iopv=iopv,
        premium_pct=premium,
        baseline=baseline,
    )


def _refresh_cache(*, force: bool = False) -> None:
    global _cache_at, _cache_rows
    now = time.monotonic()
    if not force and _cache_rows and (now - _cache_at) < _CACHE_TTL_SEC:
        return
    try:
        frame = ak.fund_etf_spot_em()
    except Exception as exc:
        log.warning("拉取 ETF 溢价失败: %s", exc)
        return
    if frame is None or frame.empty:
        return
    rows: dict[str, PremiumQuote] = {}
    for raw in frame.to_dict(orient="records"):
        quote = parse_eastmoney_etf_row(raw)
        if quote is not None:
            rows[quote.code] = quote
    if rows:
        _cache_rows = rows
        _cache_at = now
        log.info("ETF 溢价缓存刷新：%d 只", len(rows))


def load_premium_baseline(code: str, *, force: bool = False) -> PremiumBaseline | None:
    """Trailing-year premium distribution from 收盘价 / 公布单位净值.

    QDII NAV is often a day behind the share price, so the level is approximate;
    the *rank* of today's premium within that series is still the right question
    ("is this rich for *this* fund?").
    """
    code = str(code).strip().zfill(6)
    now = time.monotonic()
    if (
        not force
        and code in _hist_rows
        and (now - _hist_at.get(code, 0.0)) < _HIST_TTL_SEC
    ):
        return _hist_rows[code]

    try:
        from src.fetch_quotes import fetch_history

        px = fetch_history(code, "cn").copy()
        px["date"] = pd.to_datetime(px["date"]).dt.normalize()
        price = px.set_index("date")["close"].astype(float)

        nav = ak.fund_open_fund_info_em(symbol=code, indicator="单位净值走势")
        nav["净值日期"] = pd.to_datetime(nav["净值日期"]).dt.normalize()
        unit = nav.set_index("净值日期")["单位净值"].astype(float)

        joined = pd.DataFrame({"price": price, "nav": unit}).dropna().tail(HIST_WINDOW)
        if len(joined) < 60:
            return None
        prem = (joined["price"] / joined["nav"] - 1.0) * 100.0
        baseline = PremiumBaseline(
            median_pct=float(prem.median()),
            p90_pct=float(prem.quantile(UNUSUAL_PERCENTILE / 100.0)),
            samples=int(len(prem)),
        )
    except Exception as exc:
        log.warning("拉取 %s 历史溢价失败: %s", code, exc)
        return _hist_rows.get(code)

    _hist_rows[code] = baseline
    _hist_at[code] = now
    return baseline


def lookup_premium(code: str, *, force: bool = False) -> PremiumQuote | None:
    """Return the latest premium for a CN ETF code, or None when unavailable."""
    code = str(code).strip().zfill(6)
    _refresh_cache(force=force)
    quote = _cache_rows.get(code)
    if quote is None:
        return None
    baseline = load_premium_baseline(code, force=force)
    if baseline is None:
        return quote
    return PremiumQuote(
        code=quote.code,
        name=quote.name,
        price=quote.price,
        iopv=quote.iopv,
        premium_pct=quote.premium_pct,
        baseline=baseline,
    )


def clear_premium_cache() -> None:
    """Test helper."""
    global _cache_at, _cache_rows, _hist_at, _hist_rows
    _cache_at = 0.0
    _cache_rows = {}
    _hist_at = {}
    _hist_rows = {}
