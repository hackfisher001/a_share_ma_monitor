#!/usr/bin/env python3
"""Walk-forward calibration of swing-T thresholds (costs + forced exits included)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.fetch_quotes import fetch_daily_history_cn, fetch_daily_history_us
from src.t_backtest import (
    CALIBRATION_UNIVERSE,
    ROUND_TRIP_COST_PCT,
    dip_statistics,
    recommend_profile_params,
    walk_forward,
)

LOOKBACK_DAYS = 2200  # ~6 calendar years
OUT_YAML = ROOT / "conf" / "t_profiles.yaml"
OUT_JSON = ROOT / "conf" / "t_backtest_report.json"


def load_hist(code: str, market: str):
    if market == "us":
        return fetch_daily_history_us(code, lookback_days=LOOKBACK_DAYS)
    return fetch_daily_history_cn(code, lookback_days=LOOKBACK_DAYS)


def main() -> int:
    report: dict[str, dict] = {}
    lines: list[str] = []

    for profile, symbols in CALIBRATION_UNIVERSE.items():
        lines.append(f"\n=== {profile} ===")
        collected: list = []
        for code, market, name, cost_key in symbols:
            cost = ROUND_TRIP_COST_PCT[cost_key]
            try:
                hist = load_hist(code, market)
            except Exception as exc:
                lines.append(f"  {name}({code}): 拉取失败 {exc}")
                continue

            stats = dip_statistics(hist, profile)
            wf = walk_forward(hist, profile, name, cost_pct=cost)
            span_years = (hist["date"].iloc[-1] - hist["date"].iloc[0]).days / 365.25
            collected.append((wf, stats))

            if not wf.ok:
                lines.append(
                    f"  {name}({code}): 样本 {len(hist)} 根 / {span_years:.1f} 年 — {wf.note}"
                )
                continue

            s = wf.summary()
            lines.append(
                f"  {name}({code}): 样本 {span_years:.1f}年 成本 {cost:.2f}% | "
                f"样本外 {s['trades']} 笔 ({s['trades_per_year']}/年) "
                f"胜率 {s['win_rate']}% 均笔 {s['avg_net_pct']:+.2f}% "
                f"最差 {s['worst_net_pct']:+.2f}%"
            )
            lines.append(
                f"      年化T {s['ann_net_pct']:+.2f}% (占用 {s['exposure']*100:.0f}%，"
                f"折算满仓 {s['ann_net_per_exposure_pct']:+.2f}%) vs 持有 "
                f"{s['buy_hold_ann_pct']:+.2f}%"
            )
            lines.append(
                f"      半仓底+半仓T 组合 {s['combo_ann_pct']:+.2f}% → "
                f"较全仓持有 {s['combo_edge_pct']:+.2f}% | "
                f"T累计回撤 {s['max_drawdown_pct']:.2f}% 均持 {s['avg_hold_days']}天 | "
                f"退出 {s['exit_reasons']}"
            )

        if collected:
            rec = recommend_profile_params(collected, profile)
            report[profile] = {
                "recommended": rec,
                "symbols": [w.summary() if w.ok else {"name": w.name, "note": w.note} for w, _ in collected],
                "dip_stats": {w.name: s for w, s in collected},
            }
            lines.append(
                f"  >> {rec['symbols_with_edge']}/{rec['symbols_tested']} 只做T后组合优于全仓持有"
                f"（{', '.join(rec['symbols_with_edge_names']) or '无'}）"
            )
            lines.append(
                f"  >> 参考参数: 低吸 -{rec['buy_dd_pct']}% / 高抛 +{rec['sell_bounce_pct']}% "
                f"/ 止损 {rec['stop_loss_pct']}% / 最长持有 {rec['max_hold_days']}天"
            )

    OUT_YAML.parent.mkdir(parents=True, exist_ok=True)
    yaml_lines = [
        "# Walk-forward calibrated swing-T references (scripts/backtest_t_params.py)",
        "# 样本外结果，已扣交易成本，含止损/最长持有/期末平仓。",
        "# 仅作为观察提醒阈值，不构成买卖指令。",
        "profiles:",
    ]
    for profile in ("short", "mid", "slow"):
        if profile not in report:
            continue
        rec = report[profile]["recommended"]
        yaml_lines.append(f"  {profile}:")
        yaml_lines.append(f"    lookback_high: {rec['lookback_high']}")
        yaml_lines.append(f"    buy_dd_pct: {rec['buy_dd_pct']}")
        yaml_lines.append(f"    sell_bounce_pct: {rec['sell_bounce_pct']}")
        stop = rec["stop_loss_pct"]
        yaml_lines.append(f"    stop_loss_pct: {'null' if stop is None else stop}")
        yaml_lines.append(f"    max_hold_days: {rec['max_hold_days']}")
        yaml_lines.append(f"    empirical_dd_p25: {rec['empirical_dd_p25']}")
        yaml_lines.append(f"    empirical_bounce_p50: {rec['empirical_bounce_p50']}")
        yaml_lines.append(
            f"    # 样本外跑赢持有: {rec['symbols_with_edge']}/{rec['symbols_tested']}"
        )
    OUT_YAML.write_text("\n".join(yaml_lines) + "\n", encoding="utf-8")
    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n".join(lines))
    print(f"\nWrote {OUT_YAML}")
    print(f"Wrote {OUT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
