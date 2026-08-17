"""Daily price digest for Feishu."""

from __future__ import annotations

import logging
from datetime import date

from src.fetch_quotes import QuoteBundle, build_bundle
from src.notify import send_alert
from src.perf import compute_period_changes

log = logging.getLogger("ma_monitor")

MARKET_TITLE = {
    "cn": "A股日报",
    "hk": "港股日报",
    "us": "美股日报",
}


def _fmt_stock_block(bundle: QuoteBundle) -> str:
    changes = compute_period_changes(bundle.hist, bundle.price)
    line_chg = "　".join(c.fmt() for c in changes)
    ma_dev = (bundle.price - bundle.ma30) / bundle.ma30 * 100
    return (
        f"**{bundle.name}({bundle.code})**  {bundle.price:.2f}\n"
        f"MA30 {bundle.ma30:.2f}（{ma_dev:+.2f}%）｜截至 {bundle.as_of}\n"
        f"{line_chg}"
    )


def build_digest_text(bundles: list[QuoteBundle], market: str) -> str:
    today = date.today().isoformat()
    header = f"共 {len(bundles)} 只｜{today}"
    blocks = [_fmt_stock_block(b) for b in bundles]
    return header + "\n\n" + "\n\n".join(blocks)


def collect_bundles(stocks: list[dict], market_filter: str | None = None) -> tuple[list[QuoteBundle], list[str]]:
    bundles: list[QuoteBundle] = []
    errors: list[str] = []
    for item in stocks:
        code = str(item.get("code", "")).strip()
        name = str(item.get("name") or "").strip()
        market = str(item.get("market") or "cn").strip().lower()
        if not code:
            continue
        if market_filter and market != market_filter:
            continue
        try:
            b = build_bundle(code, name=name, market=market)
            bundles.append(b)
            log.info(
                "[digest/%s] %s(%s) %.2f",
                market.upper(),
                b.name,
                b.code,
                b.price,
            )
        except Exception as exc:
            key = f"{market}:{code}"
            errors.append(key)
            log.exception("日报拉取失败 %s: %s", key, exc)
    return bundles, errors


def run_digest(
    stocks: list[dict],
    *,
    dry_run: bool = False,
    markets: list[str] | None = None,
) -> int:
    """
    Send one Feishu card per market to avoid message size limits.
    markets default: cn, hk, us (only those present in watchlist).
    """
    present = sorted({str(s.get("market") or "cn").lower() for s in stocks})
    targets = markets or [m for m in ("cn", "hk", "us") if m in present]

    sent = 0
    fail_markets = 0
    for market in targets:
        bundles, errors = collect_bundles(stocks, market_filter=market)
        if not bundles and not errors:
            continue
        if not bundles:
            fail_markets += 1
            continue
        text = build_digest_text(bundles, market)
        if errors:
            text += "\n\n拉取失败: " + ", ".join(errors)
        title = f"{MARKET_TITLE.get(market, market.upper())} · 涨跌一览"
        if dry_run:
            log.info("[dry-run] %s\n%s", title, text)
        else:
            channel = send_alert(text, title=title)
            log.info("已通过 %s 发送 %s（%d 只）", channel, title, len(bundles))
        sent += 1

    log.info("日报完成：发送 %d 组，全失败市场 %d", sent, fail_markets)
    return 1 if sent == 0 else 0
