"""End-to-end scan behaviour: calibrated bands, one message, honest state."""

from __future__ import annotations

import json

import pandas as pd
import pytest

import src.main as main
from src.fetch_quotes import QuoteBundle


def _calm_hist(wobble: float, n: int = 300, trend: float = 0.0) -> pd.DataFrame:
    """Alternating ±wobble% closes (daily σ ≈ wobble%), drifting `trend`% overall.

    The trend knob separates "fell hard today" from "has been sliding for
    months but is flat today" — the drawdown bands only care about the latter.
    """
    closes = [100.0]
    for i in range(n - 1):
        closes.append(closes[-1] * (1 + (wobble if i % 2 == 0 else -wobble) / 100.0))
    if trend:
        span = max(1, len(closes) - 1)
        closes = [
            c * (1 + trend / 100.0) ** (i / span) for i, c in enumerate(closes)
        ]
    dates = pd.date_range("2024-01-02", periods=len(closes), freq="B")
    return pd.DataFrame({"date": dates, "close": closes})


def _bundle(
    code: str,
    name: str,
    market: str,
    drop_pct: float,
    wobble: float,
    trend: float = 0.0,
) -> QuoteBundle:
    hist = _calm_hist(wobble, trend=trend)
    prev_close = float(hist.iloc[-1]["close"])
    price = prev_close * (1 + drop_pct / 100.0)
    hist = pd.concat(
        [hist, pd.DataFrame([{"date": pd.Timestamp("2025-03-03"), "close": price}])],
        ignore_index=True,
    )
    return QuoteBundle(
        code=code,
        name=name,
        market=market,
        price=price,
        ma30=float(hist["close"].tail(30).mean()),
        high_252=float(hist["close"].tail(252).max()),
        as_of="2025-03-03",
        hist=hist,
        live=True,
        prev_close=prev_close,
    )


@pytest.fixture
def scan(tmp_path, monkeypatch):
    """Run a scan against a synthetic watchlist, capturing what got sent."""
    sent: list[dict] = []
    monkeypatch.setattr(main, "send_alert", lambda **kw: sent.append(kw) or "feishu")
    monkeypatch.setattr(main, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("STATE_FILE", str(tmp_path / "state.json"))

    def run(
        watchlist: str,
        drops: dict[str, float],
        wobbles: dict[str, float] | None = None,
        trends: dict[str, float] | None = None,
    ):
        path = tmp_path / "watchlist.yaml"
        path.write_text(watchlist, encoding="utf-8")
        wobbles, trends = wobbles or {}, trends or {}

        def fake_bundle(code, name="", market="cn"):
            return _bundle(
                code,
                name or code,
                market,
                drops.get(code, 0.0),
                wobbles.get(code, 1.0),
                trends.get(code, 0.0),
            )

        monkeypatch.setattr(main, "build_bundle", fake_bundle)
        sent.clear()
        exit_code = main.run_ma_scan(path)
        state_path = tmp_path / "state.json"
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        return exit_code, sent, state

    return run


BASE = """
touch_pct: 0.5
notifications:
  action_only: true
intraday_dip:
  enabled: true
  mode: sigma
  sigma_levels: [2.0, 3.0, 4.0]
recent_pullback: false
drawdown_levels: [5, 10]
drawdown_markets: [us]
benchmarks:
  us: "QQQM"
stocks:
  - {code: "MU", name: "美光", market: us, theme: stock}
  - {code: "AMD", name: "AMD", market: us, theme: stock}
  - {code: "QQQM", name: "纳指100ETF", market: us, theme: nasdaq_us}
"""


def test_several_symbols_each_get_their_own_card(scan):
    exit_code, sent, _ = scan(BASE, {"MU": -6.0, "AMD": -5.0, "QQQM": -4.0})

    assert exit_code == 0
    assert len(sent) == 3, "每只触发的标的各自一张卡，不再合并"
    titles = {s["title"] for s in sent}
    assert all("急跌" in t for t in titles)
    # Each card carries its own sparkline upload attempt (may be empty without Feishu app).
    assert all("markdown" in s for s in sent)


def test_one_symbol_crossing_several_bands_is_listed_once(scan):
    """-6% on a σ≈1% symbol trips 2σ/3σ/4σ; only the deepest card is spoken aloud."""
    _, sent, state = scan(BASE, {"MU": -6.0})

    assert len(sent) == 1
    assert "美光" in sent[0]["markdown"]
    assert len(state["intraday_fired"]["us:MU"]["levels"]) >= 3


def test_a_lone_alert_still_gets_the_detailed_card(scan):
    _, sent, _ = scan(BASE, {"MU": -6.0})

    body = sent[0]["markdown"]
    assert "阶段：" in body and "位置：" in body and "近期：" in body
    assert "解读" not in body and "档位" not in body
    assert "若手头有机动资金" not in body


def test_quiet_scan_sends_nothing(scan):
    _, sent, _ = scan(BASE, {"MU": -0.4, "AMD": -0.3, "QQQM": -0.2})

    assert sent == []


def test_same_drop_alerts_the_calm_symbol_and_spares_the_volatile_one(scan):
    """The core of the change: -4% is noise for σ=4%, an event for σ=0.8%."""
    _, sent, _ = scan(
        BASE,
        {"MU": -4.0, "AMD": -4.0, "QQQM": 0.0},
        wobbles={"MU": 4.0, "AMD": 0.8, "QQQM": 1.0},
    )

    assert len(sent) == 1
    body = sent[0]["markdown"]
    assert "AMD" in body, "σ=0.8% 的标的跌 4% 是 5σ 事件，必须提醒"
    assert "美光" not in body, "σ=4% 的标的跌 4% 只有 1σ，属日常噪音"


def test_benchmark_attribution_appears_on_the_card(scan):
    _, sent, _ = scan(BASE, {"MU": -6.0, "QQQM": -0.1})

    body = sent[0]["markdown"]
    assert "归因" in body and "纳指100ETF" in body
    assert "基准：" not in body and "β" not in body and "常态残差" not in body


def test_muted_drawdown_bands_are_still_recorded(scan):
    """Guards the landmine: flipping action_only off must not replay history.

    While muted the bands used to go unrecorded, so a symbol already down 22%
    would fire 5/10/15/20 the instant the flag changed.
    """
    # MU has slid 15% over the year but is flat today: drawdown bands only.
    _, sent, state = scan(
        BASE,
        {"MU": -0.2, "AMD": -0.1, "QQQM": -0.1},
        trends={"MU": -15.0},
    )

    # action_only mutes the observe bands, so nothing is pushed ...
    assert sent == []
    # ... but the crossings are on record, so flipping the flag replays nothing.
    assert state["drawdown_fired"]["us:MU"] == [5.0, 10.0]


def test_absolute_mode_still_works(scan):
    """The flat ladder remains available for anyone who wants it."""
    flat = BASE.replace("  mode: sigma", "  mode: absolute").replace(
        "  sigma_levels: [2.0, 3.0, 4.0]", "  levels: [3.0]"
    )
    _, sent, _ = scan(
        flat,
        {"MU": -4.0, "AMD": -4.0, "QQQM": 0.0},
        wobbles={"MU": 4.0, "AMD": 0.8, "QQQM": 1.0},
    )

    # Both breach -3% because volatility is ignored in this mode; each gets a card.
    assert len(sent) == 2
    bodies = "\n".join(s["markdown"] for s in sent)
    assert "美光" in bodies and "AMD" in bodies
