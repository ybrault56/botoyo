"""Targeted tests for Kraken WebSocket resilience."""

from __future__ import annotations

import asyncio

import pytest

from app.market.kraken_ws import KrakenWebSocket, _silence_timeout_seconds


class _SilentWebSocket:
    async def recv(self) -> str:
        await asyncio.sleep(0.05)
        return ""


def test_silence_timeout_scales_with_interval() -> None:
    assert _silence_timeout_seconds(15) == 1080
    assert _silence_timeout_seconds(60) == 3780
    assert _silence_timeout_seconds(240) == 14580


def test_consume_times_out_when_interval_goes_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.market.kraken_ws._silence_timeout_seconds", lambda interval: 0.01)
    client = KrakenWebSocket(symbols=["BTCUSDT"], intervals=[15])

    with pytest.raises(TimeoutError, match="stale websocket on interval 15"):
        asyncio.run(client._consume(_SilentWebSocket(), 15))
