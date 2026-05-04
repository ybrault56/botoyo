"""Tests for incremental technical indicators."""

from __future__ import annotations

import math

import numpy as np

from app.indicators.adx import ADXCalculator, compute_volume_ma, detect_swing_highs, detect_swing_lows
from app.indicators.atr import ATRCalculator
from app.indicators.ema import EMACalculator
from app.indicators.rsi import RSICalculator


def test_ema_matches_manual_series() -> None:
    closes = np.asarray([10.0, 11.0, 12.0, 13.0, 14.0], dtype=float)
    calculator = EMACalculator(period=3)

    first = calculator.warmup(closes[:3])
    second = calculator.update(closes[3])
    third = calculator.update(closes[4])

    assert math.isclose(first, 11.0, rel_tol=1e-9)
    assert math.isclose(second, 12.0, rel_tol=1e-9)
    assert math.isclose(third, 13.0, rel_tol=1e-9)


def test_atr_on_three_candles() -> None:
    highs = np.asarray([10.0, 11.0, 12.0], dtype=float)
    lows = np.asarray([8.0, 9.0, 10.0], dtype=float)
    closes = np.asarray([9.0, 10.0, 11.0], dtype=float)

    calculator = ATRCalculator(period=14)
    atr = calculator.warmup(highs, lows, closes)

    expected = (2.0 + 2.0 + 2.0) / 3.0
    assert math.isclose(atr, expected, rel_tol=1e-9)
    assert atr > 0.0


def test_rsi_flat_series_is_neutral() -> None:
    closes = np.asarray([100.0] * 20, dtype=float)
    calculator = RSICalculator(period=14)

    value = calculator.warmup(closes)

    assert math.isclose(value, 50.0, rel_tol=1e-9)


def test_adx_low_on_range_and_high_on_trend() -> None:
    trend_closes = np.linspace(100.0, 170.0, 80)
    trend_highs = trend_closes + 2.0
    trend_lows = trend_closes - 2.0

    range_angles = np.linspace(0.0, 10.0 * np.pi, 80)
    range_closes = 100.0 + np.sin(range_angles)
    range_highs = range_closes + 1.0
    range_lows = range_closes - 1.0

    trend_adx = ADXCalculator(period=14).warmup(trend_highs, trend_lows, trend_closes)
    range_adx = ADXCalculator(period=14).warmup(range_highs, range_lows, range_closes)

    assert 0.0 <= trend_adx["adx"] <= 100.0
    assert 0.0 <= range_adx["adx"] <= 100.0
    assert trend_adx["adx"] > range_adx["adx"]
    assert trend_adx["adx"] >= 20.0


def test_detect_swings_and_volume_ma() -> None:
    highs = np.asarray([1.0, 3.0, 2.0, 5.0, 2.0, 4.0, 1.0], dtype=float)
    lows = np.asarray([4.0, 2.0, 3.0, 1.0, 3.0, 2.0, 4.0], dtype=float)
    volumes = np.asarray([10.0, 20.0, 30.0, 40.0], dtype=float)

    assert detect_swing_highs(highs, window=1) == [1, 3, 5]
    assert detect_swing_lows(lows, window=1) == [1, 3, 5]
    assert math.isclose(compute_volume_ma(volumes, period=3), 30.0, rel_tol=1e-9)
