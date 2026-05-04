"""Incremental ADX calculator and structure helpers for BotYo."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


def detect_swing_highs(highs: np.ndarray, window: int = 5) -> list[int]:
    """Return pivot-high indices using a symmetric window."""

    highs = np.asarray(highs, dtype=float)
    if window <= 0:
        raise ValueError("window must be positive")
    if highs.size < (window * 2) + 1:
        return []

    indices: list[int] = []
    for index in range(window, highs.size - window):
        center = highs[index]
        left = highs[index - window : index]
        right = highs[index + 1 : index + window + 1]
        if center > float(np.max(left)) and center > float(np.max(right)):
            indices.append(index)
    return indices


def detect_swing_lows(lows: np.ndarray, window: int = 5) -> list[int]:
    """Return pivot-low indices using a symmetric window."""

    lows = np.asarray(lows, dtype=float)
    if window <= 0:
        raise ValueError("window must be positive")
    if lows.size < (window * 2) + 1:
        return []

    indices: list[int] = []
    for index in range(window, lows.size - window):
        center = lows[index]
        left = lows[index - window : index]
        right = lows[index + 1 : index + window + 1]
        if center < float(np.min(left)) and center < float(np.min(right)):
            indices.append(index)
    return indices


def compute_volume_ma(volumes: np.ndarray, period: int = 20) -> float:
    """Return the mean of the most recent volume window."""

    volumes = np.asarray(volumes, dtype=float)
    if volumes.size == 0:
        raise ValueError("volumes must not be empty")
    if period <= 0:
        raise ValueError("period must be positive")
    if volumes.size < period:
        return float(np.mean(volumes))
    return float(np.mean(volumes[-period:]))


@dataclass(slots=True)
class ADXCalculator:
    """Incrementally compute +DI, -DI and ADX using Wilder smoothing."""

    period: int = 14
    prev_high: float | None = None
    prev_low: float | None = None
    prev_close: float | None = None
    adx: float = 0.0
    plus_di: float = 0.0
    minus_di: float = 0.0
    _smoothed_tr: float | None = None
    _smoothed_plus_dm: float | None = None
    _smoothed_minus_dm: float | None = None
    _tr_values: list[float] = field(default_factory=list)
    _plus_dm_values: list[float] = field(default_factory=list)
    _minus_dm_values: list[float] = field(default_factory=list)
    _dx_values: list[float] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.period <= 0:
            raise ValueError("period must be positive")

    def warmup(self, highs: np.ndarray, lows: np.ndarray, closes: np.ndarray) -> dict[str, float]:
        """Warm up the ADX state with historical OHLC data."""

        highs = np.asarray(highs, dtype=float)
        lows = np.asarray(lows, dtype=float)
        closes = np.asarray(closes, dtype=float)
        if not (highs.size == lows.size == closes.size):
            raise ValueError("highs, lows and closes must have the same length")
        if highs.size == 0:
            raise ValueError("warmup requires at least one candle")
        result = {"adx": 0.0, "plus_di": 0.0, "minus_di": 0.0}
        for high, low, close in zip(highs, lows, closes, strict=True):
            result = self.update(float(high), float(low), float(close))
        return result

    def update(self, high: float, low: float, close: float) -> dict[str, float]:
        """Update the ADX state with one new candle."""

        high = float(high)
        low = float(low)
        close = float(close)

        if self.prev_high is None or self.prev_low is None or self.prev_close is None:
            self.prev_high = high
            self.prev_low = low
            self.prev_close = close
            return self.snapshot()

        up_move = high - self.prev_high
        down_move = self.prev_low - low
        plus_dm = up_move if up_move > down_move and up_move > 0.0 else 0.0
        minus_dm = down_move if down_move > up_move and down_move > 0.0 else 0.0
        tr = max(high - low, abs(high - self.prev_close), abs(low - self.prev_close))

        if self._smoothed_tr is None:
            self._tr_values.append(tr)
            self._plus_dm_values.append(plus_dm)
            self._minus_dm_values.append(minus_dm)

            if len(self._tr_values) >= self.period:
                self._smoothed_tr = sum(self._tr_values[-self.period :])
                self._smoothed_plus_dm = sum(self._plus_dm_values[-self.period :])
                self._smoothed_minus_dm = sum(self._minus_dm_values[-self.period :])
                self._refresh_di()
                self._update_adx_from_dx()
        else:
            assert self._smoothed_plus_dm is not None
            assert self._smoothed_minus_dm is not None
            self._smoothed_tr = self._smoothed_tr - (self._smoothed_tr / self.period) + tr
            self._smoothed_plus_dm = self._smoothed_plus_dm - (self._smoothed_plus_dm / self.period) + plus_dm
            self._smoothed_minus_dm = self._smoothed_minus_dm - (self._smoothed_minus_dm / self.period) + minus_dm
            self._refresh_di()
            self._update_adx_from_dx()

        self.prev_high = high
        self.prev_low = low
        self.prev_close = close
        return self.snapshot()

    def _refresh_di(self) -> None:
        assert self._smoothed_tr is not None
        assert self._smoothed_plus_dm is not None
        assert self._smoothed_minus_dm is not None

        if self._smoothed_tr == 0.0:
            self.plus_di = 0.0
            self.minus_di = 0.0
            return

        self.plus_di = 100.0 * (self._smoothed_plus_dm / self._smoothed_tr)
        self.minus_di = 100.0 * (self._smoothed_minus_dm / self._smoothed_tr)

    def _update_adx_from_dx(self) -> None:
        denominator = self.plus_di + self.minus_di
        dx = 0.0 if denominator == 0.0 else 100.0 * abs(self.plus_di - self.minus_di) / denominator

        if len(self._dx_values) < self.period:
            self._dx_values.append(dx)
            if len(self._dx_values) == self.period:
                self.adx = float(np.mean(np.asarray(self._dx_values, dtype=float)))
            else:
                self.adx = float(np.mean(np.asarray(self._dx_values, dtype=float)))
            return

        self.adx = ((self.adx * (self.period - 1)) + dx) / self.period

    def snapshot(self) -> dict[str, float]:
        """Return the current indicator snapshot."""

        return {
            "adx": float(max(0.0, min(100.0, self.adx))),
            "plus_di": float(max(0.0, min(100.0, self.plus_di))),
            "minus_di": float(max(0.0, min(100.0, self.minus_di))),
        }

