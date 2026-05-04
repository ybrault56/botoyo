"""Database tests for candle persistence."""

from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import time
from pathlib import Path

from app.market.kraken_rest import bootstrap_history
from app.storage.db import (
    get_connection,
    get_external_alert,
    get_indicator_state,
    get_recent_candles,
    init_db,
    upsert_candle,
    upsert_external_alert,
    upsert_indicator_state,
)


def _db_path() -> Path:
    return Path(tempfile.mkdtemp()) / "botyo-test.db"


def test_insert_candle() -> None:
    db_path = _db_path()
    init_db(db_path)

    upsert_candle(
        {
            "symbol": "BTCUSDT",
            "timeframe": "1H",
            "open_time": 1_700_000_000,
            "open": 100.0,
            "high": 110.0,
            "low": 95.0,
            "close": 108.0,
            "volume": 12.0,
            "closed": 1,
        },
        db_path,
    )

    with get_connection(db_path) as connection:
        stored = connection.execute("SELECT symbol, timeframe, close FROM candles").fetchone()

    assert dict(stored) == {"symbol": "BTCUSDT", "timeframe": "1H", "close": 108.0}


def test_get_recent_candles_returns_latest_in_order() -> None:
    db_path = _db_path()
    init_db(db_path)

    for open_time in range(3):
        upsert_candle(
            {
                "symbol": "ETHUSDT",
                "timeframe": "4H",
                "open_time": open_time,
                "open": 10.0 + open_time,
                "high": 11.0 + open_time,
                "low": 9.0 + open_time,
                "close": 10.5 + open_time,
                "volume": 100.0 + open_time,
                "closed": 1,
            },
            db_path,
        )

    candles = get_recent_candles("ETHUSDT", "4H", limit=2, db_path=db_path)

    assert [candle["open_time"] for candle in candles] == [1, 2]


def test_candle_uniqueness_constraint() -> None:
    db_path = _db_path()
    init_db(db_path)

    candle = {
        "symbol": "XRPUSDT",
        "timeframe": "1D",
        "open_time": 10,
        "open": 1.0,
        "high": 2.0,
        "low": 0.8,
        "close": 1.5,
        "volume": 999.0,
        "closed": 1,
    }

    upsert_candle(candle, db_path)
    updated = dict(candle)
    updated["close"] = 1.6
    upsert_candle(updated, db_path)

    with sqlite3.connect(db_path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM candles").fetchone()[0]
        close = connection.execute("SELECT close FROM candles").fetchone()[0]

    assert count == 1
    assert close == 1.6


def test_bootstrap_history_respects_since_for_startup_resync(monkeypatch) -> None:
    db_path = _db_path()
    init_db(db_path)
    base_open_time = int(time.time()) - 7200
    calls: list[int | None] = []

    async def fake_fetch(symbol: str, interval_minutes: int, since: int | None = None) -> list[dict[str, float | int]]:
        calls.append(since)
        if len(calls) == 1:
            return [
                {
                    "open_time": base_open_time,
                    "open": 100.0,
                    "high": 101.0,
                    "low": 99.0,
                    "close": 100.5,
                    "volume": 10.0,
                },
                {
                    "open_time": base_open_time + 3600,
                    "open": 100.5,
                    "high": 102.0,
                    "low": 100.0,
                    "close": 101.5,
                    "volume": 12.0,
                },
            ]
        return []

    monkeypatch.setattr("app.market.kraken_rest.fetch_ohlc", fake_fetch)

    candles = asyncio.run(
        bootstrap_history(
            "BTCUSDT",
            "1H",
            lookback_days=30,
            since=base_open_time,
            db_path=db_path,
        )
    )

    stored = get_recent_candles("BTCUSDT", "1H", limit=5, db_path=db_path)

    assert calls[0] == base_open_time
    assert len(candles) == 2
    assert [candle["open_time"] for candle in stored] == [base_open_time, base_open_time + 3600]


def test_insert_external_alert() -> None:
    db_path = _db_path()
    init_db(db_path)

    upsert_external_alert(
        {
            "id": "x:12345",
            "source": "x_post",
            "symbol": "BTC",
            "signal": "PUMP",
            "probability": 75.0,
            "observed_at": 1_700_000_000,
            "title": "X post elonmusk",
            "message": "sample",
            "metadata_json": '{"post_id":"12345"}',
            "delivery_status": "shadow",
        },
        db_path,
    )

    stored = get_external_alert("x:12345", db_path=db_path)

    assert stored is not None
    assert stored["source"] == "x_post"
    assert stored["signal"] == "PUMP"


def test_upsert_indicator_state_roundtrip() -> None:
    db_path = _db_path()
    init_db(db_path)

    upsert_indicator_state(
        {
            "symbol": "BTCUSDT",
            "timeframe": "15M",
            "last_open_time": 1_700_000_000,
            "state_json": '{"candles":[]}',
            "snapshot_json": '{"close":100.5}',
        },
        db_path,
    )

    stored = get_indicator_state("BTCUSDT", "15M", db_path=db_path)

    assert stored is not None
    assert stored["last_open_time"] == 1_700_000_000
