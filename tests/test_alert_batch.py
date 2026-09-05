"""One scan's alerts collapse into a single message when several fire."""

from src.alert_batch import (
    BatchContext,
    ScanAlert,
    batch_markdown,
    batch_title,
    sort_alerts,
)
from src.price_context import PriceContext
from src.relative import RelativeMove


def _ctx(stage: str = "上涨后开始走弱", ma_dev: float = -4.0) -> PriceContext:
    return PriceContext(
        day1=-3.0,
        day3=-5.0,
        week=-6.0,
        month=2.0,
        dd20=-7.0,
        year_dd=-12.0,
        year_pos=48.0,
        ma_dev=ma_dev,
        ma_up=False,
        stage=stage,
        spark={"values": [], "years": []},
    )


def _alert(name: str, code: str, severity: float, **kw) -> ScanAlert:
    return ScanAlert(
        kind=kw.pop("kind", "dip"),
        market=kw.pop("market", "us"),
        code=code,
        name=name,
        title="急跌提醒",
        headline=f"**{name}({code})** 盘中急跌",
        ctx=kw.pop("ctx", _ctx()),
        summary_head=kw.pop("summary_head", "当日 -6.00%"),
        severity=severity,
        **kw,
    )


def test_ranking_is_by_sigma_not_by_raw_percent():
    """A small move on a calm symbol can be the rarer, more informative event."""
    calm = _alert("长江电力", "600900", severity=2.9, summary_head="当日 -2.50%")
    wild = _alert("美光", "MU", severity=1.2, summary_head="当日 -6.00%")

    assert [a.code for a in sort_alerts([wild, calm])] == ["600900", "MU"]


def test_batch_card_lists_every_symbol_once_with_backdrop():
    alerts = [
        _alert("美光", "MU", severity=2.4),
        _alert("AMD", "AMD", severity=2.1),
        _alert("紫金矿业", "601899", severity=1.8, market="cn"),
    ]
    ctx = BatchContext([("纳指100ETF", -1.1), ("沪深300ETF", -3.2)])

    md = batch_markdown(alerts, ctx)

    assert batch_title(alerts) == "盘中提醒 · 3 只触发"
    assert "3 只标的同时触发" in md
    assert "纳指100ETF -1.10%" in md and "沪深300ETF -3.20%" in md
    assert md.count("**美光(MU)**") == 1
    assert "急跌 3 只" in md
    # Ordered by severity.
    assert md.index("美光") < md.index("AMD") < md.index("紫金矿业")


def test_batch_card_flags_idiosyncratic_moves_only():
    own = _alert("美光", "MU", severity=2.4)
    own.relative = RelativeMove(
        benchmark_name="纳指100ETF", benchmark_change=0.1, beta=2.0, excess_pct=-6.2
    )
    beta_driven = _alert("AMD", "AMD", severity=2.1)
    beta_driven.relative = RelativeMove(
        benchmark_name="纳指100ETF", benchmark_change=-3.0, beta=2.0, excess_pct=-0.2
    )

    md = batch_markdown([own, beta_driven], BatchContext())

    assert md.count("⚠️") == 1
    assert "个股独有" in md
    assert "超额" in md
    assert "β" not in md


def test_mixed_kinds_are_counted_separately():
    md = batch_markdown(
        [
            _alert("美光", "MU", severity=2.4),
            _alert("招商银行", "600036", severity=1.5, kind="pullback", market="cn"),
        ],
        BatchContext(),
    )

    assert "急跌 1 只" in md and "近期异常回撤 1 只" in md
