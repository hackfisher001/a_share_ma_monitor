"""Payday reminder: the one action the backtest actually endorsed.

Across 12.3 years and 13 symbols, saving cash to wait for a drawdown lost to
buying on payday every single time. The rest of this system talks about dips,
which risks reinforcing the behaviour the data rejected — this module exists so
the winning behaviour also gets a voice, on a schedule that ignores price.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class PaydayConfig:
    enabled: bool = False
    # Salary lands on the Nth working day (Mon-Fri) of the month.
    workday: int = 1
    # Months where an annual bonus also arrives (1-12).
    bonus_months: tuple[int, ...] = ()
    bonus_workday: int = 1

    @classmethod
    def from_dict(cls, raw: dict | None) -> "PaydayConfig":
        raw = raw or {}
        months = raw.get("bonus_months") or ()
        return cls(
            enabled=bool(raw.get("enabled", False)),
            workday=max(1, min(23, int(raw.get("workday", 1)))),
            bonus_months=tuple(sorted({int(m) for m in months if 1 <= int(m) <= 12})),
            bonus_workday=max(1, min(23, int(raw.get("bonus_workday", raw.get("workday", 1))))),
        )


def nth_workday(year: int, month: int, n: int) -> date:
    """The Nth Mon-Fri of the month; clamps to the month's last working day."""
    days_in_month = calendar.monthrange(year, month)[1]
    seen = 0
    last_workday = date(year, month, 1)
    for day in range(1, days_in_month + 1):
        d = date(year, month, day)
        if d.weekday() < 5:
            seen += 1
            last_workday = d
            if seen == n:
                return d
    return last_workday


def is_payday(config: PaydayConfig, today: date | None = None) -> tuple[bool, bool]:
    """Returns (is_payday, is_bonus_day) for `today`."""
    if not config.enabled:
        return False, False
    today = today or date.today()
    salary = today == nth_workday(today.year, today.month, config.workday)
    bonus = (
        today.month in config.bonus_months
        and today == nth_workday(today.year, today.month, config.bonus_workday)
    )
    return salary or bonus, bonus


def payday_markdown(rows: list[dict], *, bonus: bool) -> str:
    """`rows` are dicts with name/code/year_dd/stage, cheapest first."""
    head = "**发薪日 + 年终奖到账**" if bonus else "**发薪日到账**"
    lines = [
        head,
        "",
        "按计划买入，**不要因为现在贵就推迟**——回测里 13 个标的上，"
        "攒钱等回撤 0 胜，等得越久越差。",
        "",
    ]
    if rows:
        lines.append("当前位置由低到高（只作分配参考，不作是否买入的依据）：")
        for row in rows[:12]:
            dd = row.get("year_dd")
            dd_txt = "—" if dd is None else f"{dd:+.1f}%"
            lines.append(
                f"- {row.get('name')}({row.get('code')})　距一年高 {dd_txt}　{row.get('stage', '')}"
            )
    if bonus:
        lines.append("")
        lines.append("年终奖属于一次性大额：同样一次买入，不要分批等跌。")
    return "\n".join(lines).strip()
