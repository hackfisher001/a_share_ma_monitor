"""Report table content and ranking."""

import pandas as pd

from src.fetch_quotes import QuoteBundle
from src.reports import (
    CN_ETF_TITLE,
    CN_STOCK_TITLE,
    _build_tables,
    _row_daily,
    _split_cn_bundles,
    _year_position,
    run_report,
)


def _bundle(name: str, code: str, start: float, end: float) -> QuoteBundle:
    dates = pd.date_range("2025-06-20", periods=300, freq="B")
    closes = [start + (end - start) * i / 299 for i in range(300)]
    hist = pd.DataFrame({"date": dates, "close": closes})
    return QuoteBundle(
        code=code,
        name=name,
        market="cn",
        price=end,
        ma30=float(hist["close"].tail(30).mean()),
        high_252=float(hist["close"].tail(252).max()),
        as_of=str(dates[-1].date()),
        hist=hist,
        theme="stock",
    )


def test_daily_row_omits_code_and_summarizes_horizons():
    bundle = _bundle("测试股票", "600000", 80.0, 120.0)
    row = _row_daily(bundle)

    assert "code" not in row
    assert set(row) == {"name", "price", "spark", "recent", "position"}
    assert len(row["spark"]["values"]) >= 252
    assert len(set(row["spark"]["years"])) == 2
    assert "日 " in row["recent"] and "周 " in row["recent"] and "月 " in row["recent"]
    assert "MA30 " in row["position"] and "一年 " in row["position"]
    assert "年位 " in row["position"] and "距高 " in row["position"]


def test_year_position_uses_one_year_range():
    bundle = _bundle("测试股票", "600000", 80.0, 120.0)
    position, drawdown = _year_position(bundle)

    assert position == 100.0
    assert drawdown == 0.0


def test_split_cn_bundles_into_stocks_and_etfs():
    stock = _bundle("招商银行", "600036", 80.0, 120.0)
    etf = _bundle("沪深300ETF", "510300", 80.0, 110.0)
    etf.theme = "sector_etf"
    gold = _bundle("黄金ETF华安", "518880", 80.0, 105.0)
    gold.theme = "macro"

    stocks, etfs = _split_cn_bundles([stock, etf, gold])

    assert [b.code for b in stocks] == ["600036"]
    assert {b.code for b in etfs} == {"510300", "518880"}


def test_build_tables_can_merge_one_cn_group():
    stock_a = _bundle("偏强", "600001", 80.0, 120.0)
    stock_b = _bundle("偏弱", "600002", 120.0, 90.0)

    tables = _build_tables("daily", [stock_a, stock_b], group_label=CN_STOCK_TITLE)

    assert len(tables) == 1
    assert tables[0]["title"].startswith(f"{CN_STOCK_TITLE}｜按近1月强→弱")
    assert tables[0]["rows"][0]["name"] == "**偏强**"


def test_tables_rank_stronger_recent_performance_first():
    weak = _bundle("偏弱", "600001", 120.0, 90.0)
    strong = _bundle("偏强", "600002", 80.0, 120.0)

    tables = _build_tables("daily", [weak, strong])

    assert [c["name"] for c in tables[0]["columns"]] == [
        "name",
        "price",
        "spark",
        "recent",
        "position",
    ]
    assert tables[0]["rows"][0]["name"] == "**偏强**"
    assert "按近1月强→弱" in tables[0]["title"]


def test_daily_row_annotates_premium_under_price():
    from src.etf_premium import PremiumQuote
    from src.reports import _row_daily, _row_for

    bundle = _bundle("纳指科技ETF", "159509", 2.0, 2.8)
    bundle.theme = "nasdaq_cn"
    premiums = {
        "159509": PremiumQuote("159509", "纳指科技ETF", 2.8, 2.2, 27.3)
    }
    row = _row_for("daily", bundle, premiums)
    assert "溢价 +27.3%" in row["price"]
    # Untagged symbols stay plain.
    plain = _row_for("daily", _bundle("沪深300ETF", "510300", 4.0, 4.6), premiums)
    assert "溢价" not in plain["price"]


def test_run_report_handles_multiple_markets(monkeypatch):
    """Regression: the CN branch used to rebind `stocks` to QuoteBundles, which
    crashed the *next* market's collect_bundles with AttributeError. Only shows
    when one report covers more than one market."""
    import src.reports as reports

    def fake_collect(stocks, market_filter=None):
        theme = "stock" if market_filter == "cn" else "nasdaq_us"
        return [_bundle("测试", "X1", 80.0, 120.0)], []

    monkeypatch.setattr(reports, "collect_bundles", fake_collect)
    monkeypatch.setattr(reports, "send_alert", lambda **kw: "feishu")

    watchlist = [
        {"code": "600036", "name": "招商银行", "market": "cn", "theme": "stock"},
        {"code": "QQQM", "name": "纳指100ETF", "market": "us", "theme": "nasdaq_us"},
    ]
    exit_code = run_report(watchlist, "daily", dry_run=True, markets=["cn", "us"])

    assert exit_code == 0
