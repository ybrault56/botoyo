"""Incremental RSI calculator for BotYo."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass(slots=True)
class RSICalculator:
    """Incrementally compute RSI using Wilder smoothing."""

    period: int = 14
    value: float = 50.0
    prev_close: float | None = None
    avg_gain: float | None = None
    avg_loss: float | None = None
    _gains: list[float] = field(default_factory=list)
    _losses: list[float] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.period <= 0:
            raise ValueError("period must be positive")

    def warmup(self, closes: np.ndarray) -> float:
        """Warm up the RSI state from historical closes."""

        closes = np.asarray(closes, dtype=float)
        if closes.size == 0:
            raise ValueError("warmup requires at least one close")
        for close in closes:
            self.update(float(close))
        return self.value

    def update(self, close: float) -> float:
        """Update the RSI with one new close."""

        close = float(close)
        if self.prev_close is None:
            self.prev_close = close
            return self.value

        change = close - self.prev_close
        gain = max(change, 0.0)
        loss = max(-change, 0.0)

        if self.avg_gain is None or self.avg_loss is None:
            self._gains.append(gain)
            self._losses.append(loss)
            self.avg_gain = sum(self._gains) / len(self._gains)
            self.avg_loss = sum(self._losses) / len(self._losses)
            if len(self._gains) >= self.period:
                self.avg_gain = sum(self._gains[-self.period :]) / self.period
                self.avg_loss = sum(self._losses[-self.period :]) / self.period
                self._gains = self._gains[-self.period :]
                self._losses = self._losses[-self.period :]
        else:
            self.avg_gain = ((self.avg_gain * (self.period - 1)) + gain) / self.period
            self.avg_loss = ((self.avg_loss * (self.period - 1)) + loss) / self.period

        self.prev_close = close
        self.value = _compute_rsi(self.avg_gain, self.avg_loss)
        return self.value


def _compute_rsi(avg_gain: float | None, avg_loss: float | None) -> float:
    if avg_gain is None or avg_loss is None:
        return 50.0
    if avg_gain == 0.0 and avg_loss == 0.0:
        return 50.0
    if avg_loss == 0.0:
        return 100.0
    if avg_gain == 0.0:
        return 0.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))

