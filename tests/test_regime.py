"""Tests for market regime classification."""

from __future__ import annotations

import numpy as np

from app.supervisor import _derive_structure
from app.strategy.regime import classify_regime


def _config() -> dict:
    import yaml

    from app.utils.logging import ROOT_DIR

    return yaml.safe_load((ROOT_DIR / "config" / "bot.yaml").read_text(encoding="utf-8"))


def test_bull_trend_on_aligned_indicators() -> None:
    config = _config()
    result = classify_regime(
        {"ema20": 118, "ema50": 100, "ema200": 80, "close": 120, "adx": 24, "structure": "bullish"},
        {"close": 118, "atr": 3, "structure": "hh_hl", "volume_ratio": 1.2, "range_atr_ratio": 1.0, "clear_stop": True},
        config,
    )
    assert result == "bull_trend"


def test_bear_trend_on_inverted_indicators() -> None:
    config = _config()
    result = classify_regime(
        {"ema20": 75, "ema50": 80, "ema200": 100, "close": 70, "adx": 28, "structure": "bearish"},
        {"close": 72, "atr": 3, "structure": "lh_ll", "volume_ratio": 1.1, "range_atr_ratio": 1.0, "clear_stop": True},
        config,
    )
    assert result == "bear_trend"


def test_range_on_low_adx() -> None:
    config = _config()
    result = classify_regime(
        {"ema20": 100, "ema50": 100, "ema200": 99, "close": 100, "adx": 16, "structure": "range"},
        {"close": 100, "atr": 2, "structure": "range", "near_midpoint": True, "volume_ratio": 1.0, "range_atr_ratio": 1.0, "clear_stop": True},
        config,
    )
    assert result == "range"


def test_high_volatility_noise_on_high_atr_ratio() -> None:
    config = _config()
    result = classify_regime(
        {"ema20": 102, "ema50": 100, "ema200": 90, "close": 101, "adx": 25, "structure": "bullish"},
        {"close": 100, "atr": 7, "structure": "range", "wick_ratio": 0.2, "repeated_wick_ratio": 0.2, "volume_ratio": 1.0, "range_atr_ratio": 1.0, "clear_stop": True},
        config,
    )
    assert result == "high_volatility_noise"


def test_low_quality_market_on_weak_volume() -> None:
    config = _config()
    result = classify_regime(
        {"ema20": 100, "ema50": 100, "ema200": 99, "close": 100, "adx": 16, "structure": "range"},
        {"close": 100, "atr": 2, "structure": "range", "near_midpoint": True, "volume_ratio": 0.5, "range_atr_ratio": 1.0, "clear_stop": True},
        config,
    )
    assert result == "low_quality_market"


def test_regime_does_not_fail_on_only_soft_mean_volume() -> None:
    config = _config()
    result = classify_regime(
        {"ema20": 118, "ema50": 100, "ema200": 80, "close": 120, "adx": 24, "structure": "bullish"},
        {
            "close": 118,
            "atr": 3,
            "structure": "hh_hl",
            "volume_ratio": 1.05,
            "mean_volume_ratio": 0.62,
            "range_atr_ratio": 1.0,
            "clear_stop": True,
        },
        config,
    )
    assert result == "bull_trend"


def test_bull_trend_allows_neutral_4h_structure_when_ema_adx_are_aligned() -> None:
    config = _config()
    result = classify_regime(
        {"ema20": 118, "ema50": 100, "ema200": 80, "close": 121, "adx": 24, "structure": "range"},
        {
            "close": 119,
            "atr": 3,
            "structure": "range",
            "volume_ratio": 1.1,
            "mean_volume_ratio": 1.0,
            "range_atr_ratio": 1.1,
            "clear_stop": True,
        },
        config,
    )
    assert result == "bull_trend"


def test_structure_does_not_flip_bearish_on_simple_pullback() -> None:
    closes = np.asarray([100.0, 104.0, 103.0, 102.0, 101.0], dtype=float)
    highs = np.asarray([101.0, 105.0, 104.0, 103.0, 102.0], dtype=float)
    lows = np.asarray([99.0, 103.0, 102.0, 101.0, 100.0], dtype=float)

    assert _derive_structure(closes, highs, lows) == "range"
