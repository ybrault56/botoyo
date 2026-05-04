"""Incremental EMA calculator for BotYo."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass(slots=True)
class EMACalculator:
    """Incrementally compute an exponential moving average."""

    period: int
    value: float | None = None
    _seed_count: int = 0
    _seed_sum: float = 0.0
    _initialized: bool = False
    k: float = field(init=False)

    def __post_init__(self) -> None:
        if self.period <= 0:
            raise ValueError("period must be positive")
        self.k = 2.0 / (self.period + 1.0)

    def warmup(self, closes: np.ndarray) -> float:
        """Seed the EMA with historical closes and return the current value."""

        closes = np.asarray(closes, dtype=float)
        if closes.size == 0:
            raise ValueError("warmup requires at least one close")
        for close in closes:
            self.update(float(close))
        assert self.value is not None
        return self.value

    def update(self, close: float) -> float:
        """Update the EMA with one new close."""

        close = float(close)
        if not self._initialized and self._seed_count < self.period:
            self._seed_sum += close
            self._seed_count += 1
            divisor = self.period if self._seed_count >= self.period else self._seed_count
            self.value = self._seed_sum / divisor
            if self._seed_count >= self.period:
                self._initialized = True
            return self.value

        if self.value is None:
            self.value = close
            self._initialized = True
            return self.value

        self.value = close * self.k + self.value * (1.0 - self.k)
        return self.value
