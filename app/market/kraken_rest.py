"""Kraken REST helpers for historical OHLC bootstrap."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from app.storage.db import SQLiteWriteQueue, upsert_candle
from app.utils.logging import get_logger

LOGGER = get_logger("app.market.kraken_rest")
REST_URL = "https://api.kraken.com/0/public/OHLC"

SYMBOL_TO_REST_PAIR = {
    "BTCUSDT": "XBTUSDT",
    "XBTUSDT": "XBTUSDT",
    "ETHUSDT": "ETHUSDT",
    "XRPUSDT": "XRPUSDT",
}

INTERVAL_TO_TIMEFRAME = {
    15: "15M",
    60: "1H",
    240: "4H",
    1440: "1D",
}

TIMEFRAME_TO_INTERVAL = {value: key for key, value in INTERVAL_TO_TIMEFRAME.items()}


def normalize_symbol(symbol: str) -> str:
    """Normalize Kraken and project symbols to the project format."""

    symbol = symbol.replace("/", "").upper()
    if symbol == "XBTUSDT":
        return "BTCUSDT"
    return symbol


def normalize_rest_pair(symbol: str) -> str:
    """Map a project symbol to the Kraken REST pair name."""

    try:
        return SYMBOL_TO_REST_PAIR[symbol.replace("/", "").upper()]
    except KeyError as exc:
        raise ValueError(f"unsupported symbol: {symbol}") from exc


async def fetch_ohlc(symbol: str, interval_minutes: int, since: int | None = None) -> list[dict[str, Any]]:
    """Fetch closed OHLC candles from Kraken REST and exclude the current candle."""

    pair = normalize_rest_pair(symbol)
    params: dict[str, Any] = {"pair": pair, "interval": interval_minutes}
    if since is not None:
        params["since"] = since

    timeout = httpx.Timeout(20.0, connect=20.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt in range(5):
            try:
                response = await client.get(REST_URL, params=params)
                if response.status_code == 429:
                    raise httpx.HTTPStatusError("rate limited", request=response.request, response=response)
                response.raise_for_status()
                payload = response.json()
                errors = payload.get("error", [])
                if errors:
                    raise RuntimeError(f"kraken returned errors: {errors}")
                result = payload["result"]
                pair_key = next(key for key in result if key != "last")
                rows = result[pair_key]
                closed_rows = rows[:-1] if len(rows) > 1 else []
                return [_parse_rest_row(row) for row in closed_rows]
            except (httpx.HTTPError, RuntimeError) as exc:
                if attempt == 4:
                    LOGGER.error("kraken REST fetch failed for %s interval=%s: %s", symbol, interval_minutes, exc)
                    raise
                await asyncio.sleep(2**attempt)

    return []


async def bootstrap_history(
    symbol: str,
    timeframe: str,
    lookback_days: int,
    *,
    write_queue: SQLiteWriteQueue | None = None,
    since: int | None = None,
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Bootstrap and store historical closed candles for one symbol and timeframe."""

    interval_minutes = TIMEFRAME_TO_INTERVAL[timeframe]
    cutoff = int((datetime.now(timezone.utc) - timedelta(days=lookback_days)).timestamp())
    all_candles: list[dict[str, Any]] = []
    since_cursor = max(cutoff, int(since)) if since is not None else cutoff

    while True:
        candles = await fetch_ohlc(symbol, interval_minutes, since=since_cursor)
        if not candles:
            break

        for candle in candles:
            if candle["open_time"] < cutoff:
                continue
            stored = {
                "symbol": normalize_symbol(symbol),
                "timeframe": timeframe,
                "open_time": candle["open_time"],
                "open": candle["open"],
                "high": candle["high"],
                "low": candle["low"],
                "close": candle["close"],
                "volume": candle["volume"],
                "closed": 1,
            }
            if write_queue is not None:
                await write_queue.upsert_candle(stored)
            else:
                upsert_candle(stored, db_path=db_path)
            all_candles.append(stored)

        next_since = candles[-1]["open_time"] + interval_minutes * 60
        if next_since <= since_cursor:
            break
        since_cursor = next_since

        if len(candles) < 719:
            break

    return all_candles


def _parse_rest_row(row: list[Any]) -> dict[str, Any]:
    return {
        "open_time": int(row[0]),
        "open": float(row[1]),
        "high": float(row[2]),
        "low": float(row[3]),
        "close": float(row[4]),
        "volume": float(row[6]),
    }
