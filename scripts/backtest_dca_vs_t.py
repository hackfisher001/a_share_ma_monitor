#!/usr/bin/env python3
"""Compare deployment rules under a real savings cash flow (salary + bonus)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.dca_backtest import DcaConfig, SymbolReport, default_strategies, simulate
from src.history_quality import load_clean_history, sanity_flags

LOOKBACK_DAYS = 4500  # 取到源头能给的最长历史

UNIVERSE: list[tuple[str, str, str, str, float]] = [
    # code, market, name, bucket, one-way cost %
    ("600036", "cn", "招商银行", "收息", 0.10),
    ("600377", "cn", "宁沪高速", "收息", 0.10),
    ("600900", "cn", "长江电力", "收息", 0.10),
    ("601088", "cn", "中国神华", "收息", 0.10),
    ("600941", "cn", "中国移动", "收息", 0.10),
    ("601899", "cn", "紫金矿业", "成长", 0.10),
    ("TSLA", "us", "特斯拉", "成长", 0.05),
    ("AMD", "us", "AMD", "成长", 0.05),
    ("NVDA", "us", "英伟达", "成长", 0.05),
    ("MU", "us", "美光", "成长", 0.05),
    ("510300", "cn", "沪深300ETF", "指数", 0.05),
    ("159941", "cn", "纳指ETF广发", "指数", 0.05),
    ("518880", "cn", "黄金ETF华安", "指数", 0.05),
    ("QQQM", "us", "纳指100ETF", "指数", 0.05),
]

OUT_JSON = ROOT / "conf" / "dca_vs_t_report.json"
OUT_MD = ROOT / "docs" / "定投与做T回测.md"


def main() -> int:
    cfg = DcaConfig()
    reports: list[tuple[str, SymbolReport]] = []
    failures: list[str] = []
    data_notes: list[str] = []

    for code, market, name, bucket, cost in UNIVERSE:
        try:
            hist, hrep = load_clean_history(code, market, lookback_days=LOOKBACK_DAYS)
        except Exception as exc:
            failures.append(f"{name}({code}): {str(exc)[:100]}")
            continue
        flags = sanity_flags(hist)
        if flags or hrep.splits or hrep.dropped_leading:
            data_notes.append(f"{name}({code}) {hrep.describe()}" + (
                "；⚠ " + "；".join(flags) if flags else ""
            ))
        if flags:
            failures.append(f"{name}({code}): 数据存疑，已剔除 — {'；'.join(flags)}")
            continue
        rep = SymbolReport(name=name, code=code, span_years=round(hrep.span_years, 1))
        for strat in default_strategies(cost):
            try:
                rep.results.append(simulate(hist, cfg, strat))
            except Exception as exc:
                failures.append(f"{name}/{strat.name}: {str(exc)[:80]}")
        if rep.results:
            reports.append((bucket, rep))

    strategy_names = [s.name for s in default_strategies(0.0)]
    lines: list[str] = [
        "# 定投 vs 做T 回测",
        "",
        f"现金流假设：每月工资 {cfg.monthly:g} 份，每年 {cfg.bonus_month} 月年终奖 "
        f"{cfg.bonus:g} 份，闲置现金按 {cfg.cash_yield_pct:g}%/年计息。",
        "所有策略收到完全相同的钱、在完全相同的日期，因此终值可直接比较。",
        "指标 MWR = 年化资金加权收益率；浮亏 = 历史上账面相对已投入本金最差的时刻。",
        "",
        "## 策略定义",
        "",
        "| 代号 | 规则 |",
        "|---|---|",
        "| A | 工资到账立刻全额买入 |",
        "| B | 到账买 60%，其余攒着，距一年高点 -10% 时投入 |",
        "| C | 全部攒着，距一年高点 -10% 时投入 |",
        "| D | 全部攒着，距一年高点 -20% 时投入 |",
        "| E | 到账买 50% 作底仓；50% 机动仓 -8% 买、+8% 卖 |",
        "| F | 到账买 50% 作底仓；50% 机动仓 -8% 买、**不卖** |",
        "| G | 到账买 50% 作底仓；50% 机动仓 -5% 买、+5% 卖 |",
        "",
    ]

    per_strategy_edges: dict[str, list[float]] = {n: [] for n in strategy_names}

    for bucket in ("收息", "成长", "指数"):
        subset = [(b, r) for b, r in reports if b == bucket]
        if not subset:
            continue
        lines.append(f"## {bucket}类")
        lines.append("")
        for _, rep in subset:
            lines.append(f"### {rep.name}({rep.code})　样本 {rep.span_years} 年")
            lines.append("")
            lines.append("| 策略 | 终值/本金 | MWR | 相对A | 最差浮亏 | 平均现金 | 买/卖 |")
            lines.append("|---|---:|---:|---:|---:|---:|---:|")
            for row in rep.rows():
                per_strategy_edges[row["strategy"]].append(row["edge_vs_dca_pct"])
                lines.append(
                    f"| {row['strategy']} | {row['multiple']:.2f}x | "
                    f"{row['mwr_pct']:+.2f}% | {row['edge_vs_dca_pct']:+.2f}% | "
                    f"{row['worst_unrealized_pct']:.1f}% | "
                    f"{row['avg_cash_frac']*100:.0f}% | "
                    f"{row['buys']}/{row['sells']} |"
                )
            lines.append("")

    lines.append("## 汇总：各策略相对「无脑定投」的年化差额")
    lines.append("")
    lines.append("| 策略 | 平均差额 | 中位差额 | 赢过定投的标的数 |")
    lines.append("|---|---:|---:|---:|")
    summary: dict[str, dict[str, float]] = {}
    for nm in strategy_names:
        edges = per_strategy_edges.get(nm) or []
        if not edges:
            continue
        avg = sum(edges) / len(edges)
        srt = sorted(edges)
        mid = len(srt) // 2
        med = srt[mid] if len(srt) % 2 else (srt[mid - 1] + srt[mid]) / 2
        wins = sum(1 for e in edges if e > 0)
        summary[nm] = {
            "avg_edge_pct": round(avg, 2),
            "median_edge_pct": round(med, 2),
            "wins": wins,
            "total": len(edges),
        }
        lines.append(f"| {nm} | {avg:+.2f}% | {med:+.2f}% | {wins}/{len(edges)} |")
    lines.append("")

    if data_notes:
        lines.append("## 数据清洗说明")
        lines.append("")
        for note in data_notes:
            lines.append(f"- {note}")
        lines.append("")

    if failures:
        lines.append("## 未纳入")
        lines.append("")
        for f in failures:
            lines.append(f"- {f}")
        lines.append("")

    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(
        json.dumps(
            {
                "config": {
                    "monthly": cfg.monthly,
                    "bonus": cfg.bonus,
                    "bonus_month": cfg.bonus_month,
                    "cash_yield_pct": cfg.cash_yield_pct,
                },
                "summary": summary,
                "symbols": [
                    {
                        "bucket": b,
                        "name": r.name,
                        "code": r.code,
                        "span_years": r.span_years,
                        "rows": r.rows(),
                    }
                    for b, r in reports
                ],
                "data_notes": data_notes,
                "failures": failures,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print("\n".join(lines[-(len(summary) + 12) :]))
    print(f"\nWrote {OUT_MD}")
    print(f"Wrote {OUT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
