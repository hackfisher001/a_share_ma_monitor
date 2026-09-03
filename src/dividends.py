"""股息率与分红变化：收息仓真正的估值锚。

为什么需要这个：对招行、长江电力、宁沪高速、中国神华这类标的，收益的很大
一部分来自分红，而「距一年高点 -X%」只看价格，天然低估了它们的真实回报，
也说不出「现在到底便宜不便宜」。股息率是绝对锚，价格回撤是相对锚——
一只跌 10% 但股息率 6% 的银行，和跌 10% 但股息率 3% 的银行完全不同。

更重要的是：**分红被砍是收息股唯一应该让你重新考虑持有的信号**，而价格类
指标完全看不到它。

关键实现约束——必须按「报告期」归集财报年度，不能用滚动 365 天窗口，也不能
比较单笔分红。两个真实的坑：

1. 单笔比较会误报。招行 FY2024 一次派 2.000 元/股，FY2025 拆成中期 1.013 +
   年度 1.003。单看最后一笔像腰斩，按年度合计 2.016 反而略增。

2. 滚动 365 天窗口同样会误报。中国移动每年 6 月与 9 月各派一次，2025-09-01
   那笔离窗口起点只差两天就被排除，而当年 9 月那笔尚未派发，结果拿「一笔」
   比上期「两笔」，凭空得出 -54%。按财报年度归集后是 +0.7%。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

log = logging.getLogger(__name__)

# 分红一年只变几次，盘中巡检没必要重复拉取
CACHE_TTL_DAYS = 7
# 相对上一财报年度跌破该比例才算「被砍」，容忍正常波动
CUT_THRESHOLD_PCT = -20.0


@dataclass(frozen=True)
class DividendPayment:
    """一笔分红方案。`ex_date` 为空表示已公告但尚未除权。"""

    period_end: date
    per_share: float
    ex_date: date | None = None
    note: str = ""

    @property
    def fiscal_year(self) -> int:
        return self.period_end.year

    @property
    def is_annual(self) -> bool:
        return self.period_end.month == 12

    @property
    def executed(self) -> bool:
        return self.ex_date is not None


@dataclass(frozen=True)
class DividendProfile:
    code: str
    fiscal_year: int | None = None
    latest_per_share: float = 0.0
    prior_per_share: float = 0.0
    # 已公告但还没除权的部分，用来说明「今年还有一笔在路上」
    pending_per_share: float = 0.0
    payments: tuple[DividendPayment, ...] = ()
    next_ex_date: date | None = None

    def yield_pct(self, price: float) -> float | None:
        """按最近一个完整财报年度的分红计算，这是国内通行的年度股息率口径。"""
        if price <= 0 or self.latest_per_share <= 0:
            return None
        return self.latest_per_share / price * 100.0

    @property
    def change_pct(self) -> float | None:
        if self.prior_per_share <= 0 or self.latest_per_share <= 0:
            return None
        return (self.latest_per_share / self.prior_per_share - 1.0) * 100.0

    @property
    def cut(self) -> bool:
        change = self.change_pct
        return change is not None and change <= CUT_THRESHOLD_PCT

    def summary(self, price: float) -> str:
        y = self.yield_pct(price)
        if y is None:
            return "无可用的完整年度分红记录"
        parts = [f"股息率 {y:.2f}%（FY{self.fiscal_year} 每股 {self.latest_per_share:.3f} 元）"]
        change = self.change_pct
        if change is not None:
            parts.append(f"同比 {change:+.1f}%")
        if self.pending_per_share > 0:
            parts.append(f"另有 {self.pending_per_share:.3f} 元已公告待除权")
        if self.cut:
            parts.append("⚠️ 分红显著下降，收息逻辑需重新确认")
        return "　".join(parts)


def _to_date(raw: object) -> date | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text or text.lower() in {"nan", "nat", "none", "-"}:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    return None


def _to_float(raw: object) -> float | None:
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if value != value:  # NaN
        return None
    return value


def parse_fhps_rows(rows: list[dict]) -> list[DividendPayment]:
    """Read akshare's 分红送配 table.

    `现金分红-现金分红比例` is quoted per 10 shares (10派X元), so it is divided
    by 10. 报告期 is required because it — not the ex-dividend date — decides
    which fiscal year a payment belongs to.
    """
    out: list[DividendPayment] = []
    for row in rows:
        period_end = _to_date(row.get("报告期"))
        per_ten = _to_float(row.get("现金分红-现金分红比例"))
        if period_end is None or per_ten is None or per_ten <= 0:
            continue
        out.append(
            DividendPayment(
                period_end=period_end,
                per_share=per_ten / 10.0,
                ex_date=_to_date(row.get("除权除息日")),
                note=str(row.get("现金分红-现金分红比例描述") or "").strip(),
            )
        )
    out.sort(key=lambda p: p.period_end)
    return out


def build_profile(
    code: str,
    payments: list[DividendPayment],
    *,
    as_of: date | None = None,
) -> DividendProfile:
    """Aggregate by fiscal year, comparing only *complete* years.

    A fiscal year counts as complete once its annual (报告期 = 12-31) dividend
    has actually gone ex. Comparing a half-finished year against a finished one
    is what produces phantom cuts.
    """
    as_of = as_of or date.today()
    complete = {
        p.fiscal_year for p in payments if p.is_annual and p.executed and p.ex_date <= as_of
    }
    if not complete:
        return DividendProfile(code=code, payments=tuple(payments))

    year = max(complete)

    def total(y: int) -> float:
        return sum(
            p.per_share
            for p in payments
            if p.fiscal_year == y and p.executed and p.ex_date <= as_of
        )

    pending = sum(
        p.per_share
        for p in payments
        if p.fiscal_year > year and (not p.executed or p.ex_date > as_of)
    )
    upcoming = [p.ex_date for p in payments if p.ex_date and p.ex_date > as_of]

    return DividendProfile(
        code=code,
        fiscal_year=year,
        latest_per_share=float(total(year)),
        prior_per_share=float(total(year - 1)),
        pending_per_share=float(pending),
        payments=tuple(payments),
        next_ex_date=min(upcoming) if upcoming else None,
    )


def _cache_read(path: Path, code: str) -> list[DividendPayment] | None:
    if not path.exists():
        return None
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    entry = (blob or {}).get(code)
    if not entry:
        return None
    fetched = _to_date(entry.get("fetched_at"))
    if fetched is None or (date.today() - fetched).days > CACHE_TTL_DAYS:
        return None
    out = []
    for item in entry.get("payments") or []:
        period_end = _to_date(item.get("period_end"))
        per_share = _to_float(item.get("per_share"))
        if period_end and per_share:
            out.append(
                DividendPayment(
                    period_end=period_end,
                    per_share=per_share,
                    ex_date=_to_date(item.get("ex_date")),
                    note=str(item.get("note") or ""),
                )
            )
    return out or None


def _cache_write(path: Path, code: str, payments: list[DividendPayment]) -> None:
    blob: dict = {}
    if path.exists():
        try:
            blob = json.loads(path.read_text(encoding="utf-8")) or {}
        except (json.JSONDecodeError, OSError):
            blob = {}
    blob[code] = {
        "fetched_at": date.today().isoformat(),
        "payments": [
            {
                "period_end": p.period_end.isoformat(),
                "per_share": p.per_share,
                "ex_date": p.ex_date.isoformat() if p.ex_date else None,
                "note": p.note,
            }
            for p in payments
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(blob, ensure_ascii=False, indent=1), encoding="utf-8")


def fetch_dividends(code: str, *, cache_path: Path | None = None) -> list[DividendPayment]:
    """A 股分红历史；失败时返回空列表而不是抛出，日报不该因此挂掉。"""
    if cache_path is not None:
        cached = _cache_read(cache_path, code)
        if cached is not None:
            return cached
    try:
        import akshare as ak

        df = ak.stock_fhps_detail_em(symbol=code)
        payments = parse_fhps_rows(df.to_dict("records")) if df is not None else []
    except Exception as exc:
        log.warning("分红数据拉取失败 %s: %s", code, exc)
        return []
    if cache_path is not None and payments:
        _cache_write(cache_path, code, payments)
    return payments


def load_profile(code: str, *, cache_path: Path | None = None) -> DividendProfile:
    return build_profile(code, fetch_dividends(code, cache_path=cache_path))


def income_markdown(rows: list[tuple[str, str, DividendProfile, float]]) -> str:
    """`rows` 为 (name, code, profile, price)，按股息率从高到低。"""
    scored = [
        (p.yield_pct(price) or -1.0, name, code, p) for name, code, p, price in rows
    ]
    scored = [r for r in scored if r[0] > 0]
    if not scored:
        return ""
    scored.sort(key=lambda r: -r[0])
    lines = [
        "**收息仓 · 股息率**",
        "",
        "股息率是这些标的的绝对估值锚，比「距一年高点」更能说明贵贱。",
        "口径为最近一个完整财报年度的现金分红。",
        "",
    ]
    for y, name, code, profile in scored:
        change = profile.change_pct
        chg = "" if change is None else f"　同比 {change:+.1f}%"
        flag = "　⚠️ 分红下降" if profile.cut else ""
        lines.append(f"- {name}({code})　**{y:.2f}%**（FY{profile.fiscal_year}）{chg}{flag}")
    if any(r[3].cut for r in scored):
        lines.append("")
        lines.append("**分红被砍是收息股唯一该让你重新考虑持有的信号**，建议查一下原因。")
    return "\n".join(lines)
