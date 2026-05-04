"""Kraken WebSocket v2 client for closed OHLC candles."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any

import orjson
import websockets
from websockets.asyncio.client import ClientConnection

from app.market.kraken_rest import INTERVAL_TO_TIMEFRAME, normalize_symbol
from app.storage.db import SQLiteWriteQueue
from app.utils.logging import get_logger

LOGGER = get_logger("app.market.kraken_ws")
WS_URL = "wss://ws.kraken.com/v2"
_SEEN_CACHE_LIMIT = 4096

CandleHandler = Callable[[str, str, dict[str, Any]], Awaitable[None] | None]


class KrakenWebSocket:
    """Subscribe to Kraken OHLC channels and emit only closed candles."""

    def __init__(
        self,
        *,
        symbols: list[str],
        intervals: list[int],
        write_queue: SQLiteWriteQueue | None = None,
        on_candle_close: CandleHandler | None = None,
    ) -> None:
        self.symbols = [project_to_ws_symbol(symbol) for symbol in symbols]
        self.intervals = intervals
        self.write_queue = write_queue
        self.on_candle_close = on_candle_close
        self._stop_event = asyncio.Event()
        self._seen: set[tuple[str, str, int]] = set()
        self._seen_order: deque[tuple[str, str, int]] = deque()
        self._connections: set[ClientConnection] = set()

    async def run(self) -> None:
        """Run the public WebSocket client until stopped."""

        tasks = [asyncio.create_task(self._run_interval(interval), name=f"kraken-ws-{interval}") for interval in self.intervals]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            for task in tasks:
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    async def stop(self) -> None:
        """Request the client loop to stop."""

        self._stop_event.set()
        for websocket in list(self._connections):
            await websocket.close()

    async def _run_interval(self, interval: int) -> None:
        """Maintain one dedicated Kraken connection per OHLC interval."""

        backoff_seconds = 1
        while not self._stop_event.is_set():
            try:
                async with websockets.connect(WS_URL, ping_interval=20, ping_timeout=20) as websocket:
                    self._connections.add(websocket)
                    try:
                        await self._subscribe(websocket, interval)
                        backoff_seconds = 1
                        await self._consume(websocket, interval)
                    finally:
                        self._connections.discard(websocket)
            except Exception as exc:
                if self._stop_event.is_set():
                    break
                LOGGER.error("kraken websocket error on interval %s: %s", interval, exc)
                await asyncio.sleep(backoff_seconds)
                backoff_seconds = min(backoff_seconds * 2, 30)

    async def _subscribe(self, websocket: ClientConnection, interval: int) -> None:
        payload = {
            "method": "subscribe",
            "params": {
                "channel": "ohlc",
                "symbol": self.symbols,
                "interval": interval,
                "snapshot": True,
            },
        }
        await websocket.send(orjson.dumps(payload).decode("utf-8"))

    async def _consume(self, websocket: ClientConnection, interval: int) -> None:
        silence_timeout = _silence_timeout_seconds(interval)
        while not self._stop_event.is_set():
            try:
                message = await asyncio.wait_for(websocket.recv(), timeout=silence_timeout)
            except asyncio.TimeoutError as exc:
                raise TimeoutError(
                    f"stale websocket on interval {interval}: no messages for {silence_timeout}s"
                ) from exc
            await self.handle_message(message)
            if self._stop_event.is_set():
                break

    async def handle_message(self, message: str | bytes) -> list[dict[str, Any]]:
        """Process one WebSocket message and return stored closed candles."""

        payload = orjson.loads(message)
        if payload.get("channel") != "ohlc":
            return []

        event_type = payload.get("type")
        rows = payload.get("data", [])
        closed_candles: list[dict[str, Any]] = []

        for row in rows:
            candle = _parse_ws_row(row)
            if candle is None:
                continue
            cache_key = (candle["symbol"], candle["timeframe"], candle["open_time"])
            if cache_key in self._seen:
                continue
            self._remember(cache_key)
            closed_candles.append(candle)
            if self.write_queue is not None:
                await self.write_queue.upsert_candle(candle)
            if self.on_candle_close is not None:
                outcome = self.on_candle_close(candle["symbol"], candle["timeframe"], candle)
                if asyncio.iscoroutine(outcome):
                    await outcome

        if closed_candles and event_type in {"snapshot", "update"}:
            LOGGER.info("received %s closed candles from websocket", len(closed_candles))

        return closed_candles

    def _remember(self, cache_key: tuple[str, str, int]) -> None:
        self._seen.add(cache_key)
        self._seen_order.append(cache_key)
        while len(self._seen_order) > _SEEN_CACHE_LIMIT:
            expired = self._seen_order.popleft()
            self._seen.discard(expired)


def project_to_ws_symbol(symbol: str) -> str:
    normalized = normalize_symbol(symbol)
    base = normalized[:-4]
    quote = normalized[-4:]
    if base == "BTC":
        base = "BTC"
    return f"{base}/{quote}"


def _parse_ws_row(row: dict[str, Any]) -> dict[str, Any] | None:
    interval = int(row["interval"])
    timeframe = INTERVAL_TO_TIMEFRAME.get(interval)
    if timeframe is None:
        return None

    close_time = _row_close_time(row)
    if close_time > datetime.now(timezone.utc):
        return None

    open_time = _parse_utc_timestamp(row["interval_begin"])
    return {
        "symbol": normalize_symbol(str(row["symbol"])),
        "timeframe": timeframe,
        "open_time": int(open_time.timestamp()),
        "open": float(row["open"]),
        "high": float(row["high"]),
        "low": float(row["low"]),
        "close": float(row["close"]),
        "volume": float(row["volume"]),
        "closed": 1,
    }


def _row_close_time(row: dict[str, Any]) -> datetime:
    timestamp = row.get("timestamp")
    if isinstance(timestamp, str) and timestamp:
        return _parse_utc_timestamp(timestamp)
    open_time = _parse_utc_timestamp(row["interval_begin"])
    return open_time + timedelta(minutes=int(row["interval"]))


def _parse_utc_timestamp(value: str) -> datetime:
    normalized = value
    if normalized.endswith("Z"):
        normalized = normalized[:-1]
    if "." in normalized:
        head, tail = normalized.split(".", 1)
        tail = tail[:6].ljust(6, "0")
        normalized = f"{head}.{tail}"
    return datetime.fromisoformat(normalized).replace(tzinfo=timezone.utc)


def _silence_timeout_seconds(interval: int) -> int:
    return max((int(interval) * 60) + 180, 300)
