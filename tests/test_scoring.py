"""Tests for setup scoring."""

from __future__ import annotations

from app.strategy.scoring import score_setup


def _config() -> dict:
    import yaml

    from app.utils.logging import ROOT_DIR

    return yaml.safe_load((ROOT_DIR / "config" / "bot.yaml").read_text(encoding="utf-8"))


def _indicators() -> dict:
    config = _config()
    trend_tf = config["timeframes"]["trend"]
    setup_tf = config["timeframes"]["setup"]
    entry_tf = config["timeframes"]["entry"]
    return {
        trend_tf: {"adx": 25},
        setup_tf: {"adx": 24},
        entry_tf: {"adx": 23, "rsi": 55, "atr": 4, "macd_cross": "bullish", "macd_histogram": 0.8},
    }


def _setup() -> dict:
    return {
        "direction": "long",
        "entry_zone": (100.0, 100.4),
        "stop": 96.4,
        "validity_hours": 12,
        "invalidation_rule": "structure invalidee",
        "features": {
            "regime_alignment": "perfect",
            "timeframe_alignment": 3,
            "conditions_met": 1.0,
            "location_quality": "major",
            "confluence_count": 4,
            "volume_ratio": 1.6,
            "clear_stop": True,
            "atr": 4.0,
            "reward_distance": 9.0,
        },
    }


def test_score_total_does_not_exceed_100() -> None:
    result = score_setup(_setup(), _indicators(), _config())
    assert 0.0 <= result["score"] <= 100.0


def test_weights_sum_to_100() -> None:
    config = _config()
    assert sum(config["scoring"]["weights"].values()) == 100


def test_reject_on_insufficient_rr() -> None:
    setup = _setup()
    setup["features"]["reward_distance"] = 4.0
    result = score_setup(setup, _indicators(), _config())
    assert result["decision"] == "reject"


def test_reject_on_score_below_65() -> None:
    setup = _setup()
    setup["features"].update(
        {
            "regime_alignment": "compatible",
            "timeframe_alignment": 1,
            "conditions_met": 0.3,
            "location_quality": "poor",
            "confluence_count": 0,
            "volume_ratio": 0.6,
            "clear_stop": True,
            "reward_distance": 7.0,
        }
    )
    indicators = _indicators()
    entry_tf = _config()["timeframes"]["entry"]
    indicators[entry_tf]["adx"] = 10
    indicators[entry_tf]["rsi"] = 75
    indicators[entry_tf]["macd_cross"] = "none"
    indicators[entry_tf]["macd_histogram"] = -0.2
    result = score_setup(setup, indicators, _config())
    assert result["decision"] == "reject"


def test_alert_priority_on_score_ge_83() -> None:
    result = score_setup(_setup(), _indicators(), _config())
    assert result["decision"] == "alert_priority"
