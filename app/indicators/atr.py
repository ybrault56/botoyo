"""Incremental ATR calculator for BotYo."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass(slots=True)
class ATRCalculator:
    """Incrementally compute ATR using Wilder smoothing."""

    period: int = 14
    value: float | None = None
    prev_close: float | None = None
    _tr_buffer: list[float] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.period <= 0:
            raise ValueError("period must be positive")

    def warmup(self, highs: np.ndarray, lows: np.ndarray, closes: np.ndarray) -> float:
        """Warm up the ATR state with historical OHLC data."""

        highs = np.asarray(highs, dtype=float)
        lows = np.asarray(lows, dtype=float)
        closes = np.asarray(closes, dtype=float)
        if not (highs.size == lows.size == closes.size):
            raise ValueError("highs, lows and closes must have the same length")
        if highs.size == 0:
            raise ValueError("warmup requires at least one candle")
        for high, low, close in zip(highs, lows, closes, strict=True):
            self.update(float(high), float(low), float(close))
        assert self.value is not None
        return self.value

    def update(self, high: float, low: float, close: float) -> float:
        """Update the ATR with one new candle."""

        high = float(high)
        low = float(low)
        close = float(close)

        if self.prev_close is None:
            tr = high - low
        else:
            tr = max(high - low, abs(high - self.prev_close), abs(low - self.prev_close))

        if self.value is None:
            self._tr_buffer.append(tr)
            self.value = sum(self._tr_buffer) / len(self._tr_buffer)
            if len(self._tr_buffer) >= self.period:
                self.value = sum(self._tr_buffer[-self.period :]) / self.period
                self._tr_buffer = self._tr_buffer[-self.period :]
        else:
            self.value = ((self.value * (self.period - 1)) + tr) / self.period

        self.prev_close = close
        return self.value

