"""ETF trading premium / discount vs IOPV (实时估值).

Most A-share ETFs hug NAV. QDII wraps of overseas indices often do not —
159509 (纳指科技) recently traded ~25% above IOPV while 510300 stays near flat.
A price dip on a high-premium name can just be the premium compressing, so the
monitor surfaces 溢价率 whenever a watchlist entry opts in with `premium: true`.

East Money's `基金折价率` is signed the opposite of everyday language:
negative 折价率 means the share is trading *above* IOPV (a premium). We flip
the sign and always speak in 溢价率 terms.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import akshare as ak

log = logging.getLogger("ma_monitor")

# Full-market spot pull is expensive; one scan shares the same table.
_CACHE_TTL_SEC = 300.0
_cache_at: float = 0.0
_cache_rows: dict[str, "PremiumQuote"] = {}

# Above this, a price dip is not automatically "cheaper NAV exposure".
HIGH_PREMIUM_PCT = 10.0
EXTREME_PREMIUM_PCT = 20.0


@dataclass(frozen=True)
class PremiumQuote:
    code: str
    name: str
    price: float
    iopv: float
    # (price - iopv) / iopv * 100. Positive = trading above NAV.
    premium_pct: float

    @property
    def elevated(self) -> bool:
        return self.premium_pct >= HIGH_PREMIUM_PCT

    @property
    def extreme(self) -> bool:
        return self.premium_pct >= EXTREME_PREMIUM_PCT

    def markdown_line(self) -> str:
        note = ""
        if self.extreme:
            note = "\n**注意：** 溢价很高，价格急跌多半是溢价回落，不等于净值变便宜"
        elif self.elevated:
            note = "\n**注意：** 溢价偏高，急跌后仍可能贵于净值"
        return (
            f"**溢价：** {self.premium_pct:+.1f}%　"
            f"现价 {self.price:.3f} / IOPV {self.iopv:.3f}"
            f"{note}"
        )

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


def parse_eastmoney_etf_row(row: dict) -> PremiumQuote | None:
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


def lookup_premium(code: str, *, force: bool = False) -> PremiumQuote | None:
    """Return the latest premium for a CN ETF code, or None when unavailable."""
    code = str(code).strip().zfill(6)
    _refresh_cache(force=force)
    return _cache_rows.get(code)


def clear_premium_cache() -> None:
    """Test helper."""
    global _cache_at, _cache_rows
    _cache_at = 0.0
    _cache_rows = {}
