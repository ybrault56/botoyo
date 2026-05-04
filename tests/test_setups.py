"""Tests for setup detection."""

from __future__ import annotations

from app.strategy.setups import detect_setups


def _config() -> dict:
    import yaml

    from app.utils.logging import ROOT_DIR

    return yaml.safe_load((ROOT_DIR / "config" / "bot.yaml").read_text(encoding="utf-8"))


def _base_candles() -> dict:
    config = _config()
    trend_tf = config["timeframes"]["trend"]
    setup_tf = config["timeframes"]["setup"]
    entry_tf = config["timeframes"]["entry"]
    return {
        trend_tf: [
            {"open": 96, "high": 100, "low": 94, "close": 99, "volume": 900},
            {"open": 99, "high": 104, "low": 98, "close": 103, "volume": 940},
            {"open": 103, "high": 108, "low": 101, "close": 107, "volume": 980},
            {"open": 107, "high": 112, "low": 105, "close": 111, "volume": 1020},
        ],
        setup_tf: [
            {"open": 104, "high": 106, "low": 103, "close": 105, "volume": 100},
            {"open": 105, "high": 107, "low": 104, "close": 106, "volume": 100},
            {"open": 106, "high": 108, "low": 105, "close": 107, "volume": 100},
            {"open": 107, "high": 109, "low": 106, "close": 108, "volume": 100},
            {"open": 108, "high": 110, "low": 107, "close": 109, "volume": 100},
            {"open": 109, "high": 111, "low": 108, "close": 110, "volume": 180},
        ],
        entry_tf: [
            {"open": 108.2, "high": 108.4, "low": 107.7, "close": 107.9, "volume": 100},
            {"open": 107.9, "high": 108.0, "low": 107.2, "close": 107.4, "volume": 105},
            {"open": 107.4, "high": 107.8, "low": 107.0, "close": 107.2, "volume": 110},
            {"open": 107.2, "high": 108.5, "low": 107.1, "close": 108.4, "volume": 135},
        ],
    }


def test_trend_continuation_long_detected() -> None:
    config = _config()
    trend_tf = config["timeframes"]["trend"]
    setup_tf = config["timeframes"]["setup"]
    entry_tf = config["timeframes"]["entry"]
    indicators = {
        trend_tf: {"ema20": 120, "ema50": 110, "ema200": 90, "close": 122, "atr": 5, "structure": "bullish"},
        setup_tf: {
            "ema20": 110,
            "ema50": 108,
            "close": 109,
            "atr": 4,
            "structure": "bullish",
            "volume_ratio": 1.1,
            "major_level_touch": True,
            "fib_levels": {"0.382": 109.7, "0.5": 109.0, "0.618": 108.6},
            "bullish_clear_space_atr": 2.8,
        },
        entry_tf: {
            "atr": 1.5,
            "volume_ratio": 1.4,
            "rsi_rebound_long": True,
            "macd_cross": "bullish",
            "structure_break": True,
            "reversal_direction": "long",
            "bullish_divergence": True,
        },
    }

    setups = detect_setups("BTCUSDT", "bull_trend", indicators, _base_candles(), config)
    assert any(setup["type"] == "trend_continuation" and setup["direction"] == "long" for setup in setups)


def test_breakout_detected_on_compression_and_volume() -> None:
    config = _config()
    trend_tf = config["timeframes"]["trend"]
    setup_tf = config["timeframes"]["setup"]
    entry_tf = config["timeframes"]["entry"]
    candles = _base_candles()
    candles[setup_tf] = [
        {"open": 100, "high": 101, "low": 99.8, "close": 100.5, "volume": 100},
        {"open": 100.5, "high": 101, "low": 100, "close": 100.6, "volume": 100},
        {"open": 100.6, "high": 101.1, "low": 100.1, "close": 100.7, "volume": 100},
        {"open": 100.7, "high": 101.2, "low": 100.2, "close": 100.8, "volume": 100},
        {"open": 100.8, "high": 101.3, "low": 100.3, "close": 100.9, "volume": 100},
        {"open": 100.9, "high": 104.0, "low": 100.4, "close": 103.8, "volume": 250},
    ]
    indicators = {
        trend_tf: {"ema20": 110, "ema50": 105, "ema200": 90, "close": 112, "atr": 4, "structure": "bullish"},
        setup_tf: {"atr": 3, "close": 103.8, "volume_ratio": 2.4, "bullish_clear_space_atr": 2.5},
        entry_tf: {
            "atr": 1.5,
            "volume_ratio": 1.4,
            "rsi_rebound_long": True,
            "macd_cross": "bullish",
            "structure_break": True,
            "reversal_direction": "long",
        },
    }

    setups = detect_setups("BTCUSDT", "bull_trend", indicators, candles, config)
    assert any(setup["type"] == "breakout" for setup in setups)


def test_reversal_detected_on_major_level_and_break_structure() -> None:
    config = _config()
    trend_tf = config["timeframes"]["trend"]
    setup_tf = config["timeframes"]["setup"]
    entry_tf = config["timeframes"]["entry"]
    indicators = {
        trend_tf: {"ema20": 110, "ema50": 108, "ema200": 95, "close": 112, "atr": 4, "structure": "bullish"},
        setup_tf: {"atr": 4, "close": 101, "major_level_touch": True, "extension_atr": 1.8},
        entry_tf: {
            "atr": 2,
            "structure_break": True,
            "reversal_direction": "long",
            "volume_ratio": 1.2,
            "rsi_rebound_long": True,
            "macd_cross": "bullish",
            "bullish_divergence": True,
        },
    }
    candles = _base_candles()

    setups = detect_setups("ETHUSDT", "range", indicators, candles, config)
    reversal = next(setup for setup in setups if setup["type"] == "reversal")
    entry_mid = float(candles[entry_tf][-1]["close"])
    assert abs(entry_mid - float(reversal["stop"])) / float(indicators[entry_tf]["atr"]) <= float(
        config["risk"]["max_stop_distance_atr"]
    )
    assert reversal["features"]["execution_policy"] == "market_on_close"


def test_range_rotation_detected_only_in_range() -> None:
    config = _config()
    trend_tf = config["timeframes"]["trend"]
    setup_tf = config["timeframes"]["setup"]
    entry_tf = config["timeframes"]["entry"]
    indicators = {
        trend_tf: {"ema20": 100, "ema50": 100, "ema200": 99, "close": 100, "atr": 3, "structure": "range"},
        setup_tf: {"atr": 3, "range_low": 95, "range_high": 105, "volume_ratio": 1.0},
        entry_tf: {
            "atr": 1.5,
            "volume_ratio": 1.0,
            "rsi_rebound_long": True,
            "macd_cross": "bullish",
            "structure_break": True,
            "reversal_direction": "long",
        },
    }
    candles = _base_candles()
    candles[setup_tf][-1]["close"] = 95.5
    candles[setup_tf][-1]["low"] = 95.1

    setups = detect_setups("XRPUSDT", "range", indicators, candles, config)
    range_setup = next(setup for setup in setups if setup["type"] == "range_rotation")
    assert range_setup["features"]["execution_policy"] == "market_on_close"


def test_incompatible_regime_is_rejected() -> None:
    config = _config()
    trend_tf = config["timeframes"]["trend"]
    setup_tf = config["timeframes"]["setup"]
    entry_tf = config["timeframes"]["entry"]
    indicators = {
        trend_tf: {"ema20": 120, "ema50": 110, "ema200": 90, "close": 122, "atr": 5, "structure": "bullish"},
        setup_tf: {"ema20": 110, "ema50": 108, "close": 109, "atr": 4, "structure": "bullish", "volume_ratio": 1.1},
        entry_tf: {"atr": 1.5, "volume_ratio": 1.4, "rsi_rebound_long": True, "macd_cross": "bullish"},
    }

    setups = detect_setups("BTCUSDT", "low_quality_market", indicators, _base_candles(), config)
    assert not any(setup["type"] == "trend_continuation" for setup in setups)
