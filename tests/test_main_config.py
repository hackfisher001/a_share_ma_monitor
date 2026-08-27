"""Watchlist loading and the T / drawdown overlap rule."""

import textwrap

import pytest

from src.main import ScanConfig, drawdown_levels_for, load_watchlist


def _write(tmp_path, body: str):
    path = tmp_path / "watchlist.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def test_swing_t_config_is_read_from_the_watchlist(tmp_path):
    path = _write(
        tmp_path,
        """
        touch_pct: 0.5
        swing_t:
          buy_drawdown_pct: 5.0
          sell_bounce_pct: 5.0
          rearm_pct: 4.0
          max_adds: 4
        stocks:
          - code: "600036"
            name: "招商银行"
            market: cn
            swing_t: true
        """,
    )

    config, stocks = load_watchlist(path)

    assert config.swing_t.buy_drawdown_pct == 5.0
    assert config.swing_t.sell_bounce_pct == 5.0
    assert stocks[0]["swing_t"] is True


def test_swing_t_defaults_apply_when_the_block_is_absent(tmp_path):
    path = _write(
        tmp_path,
        """
        touch_pct: 0.5
        stocks:
          - code: "600036"
            name: "招商银行"
            market: cn
        """,
    )

    config, _ = load_watchlist(path)

    assert config.swing_t.buy_drawdown_pct == 5.0


def test_empty_watchlist_is_rejected(tmp_path):
    path = _write(tmp_path, "touch_pct: 0.5\nstocks: []\n")

    with pytest.raises(ValueError):
        load_watchlist(path)


def _config(levels: tuple[float, ...]) -> ScanConfig:
    from src.t_signals import TConfig

    return ScanConfig(
        touch_pct=0.5,
        drawdown_levels=levels,
        drawdown_markets=("cn", "us"),
        drawdown_reset_pct=3.0,
        recent_pullback=True,
        recent_pullback_percentile=10.0,
        recent_pullback_cooldown_days=7,
        swing_t=TConfig(),
    )


def test_t_symbols_skip_bands_the_dip_alert_already_covers():
    config = _config((5, 10, 15, 20, 30))

    assert drawdown_levels_for({"swing_t": True}, config) == (10, 15, 20, 30)


def test_non_t_symbols_keep_every_band():
    config = _config((5, 10, 15, 20, 30))

    assert drawdown_levels_for({}, config) == (5, 10, 15, 20, 30)
