"""SQLite storage layer for BotYo with WAL and serialized writes."""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = ROOT_DIR / "data" / "botyo.db"

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS candles (
      id          INTEGER PRIMARY KEY AUTOINCREMENT,
      symbol      TEXT NOT NULL,
      timeframe   TEXT NOT NULL,
      open_time   INTEGER NOT NULL,
      open        REAL NOT NULL,
      high        REAL NOT NULL,
      low         REAL NOT NULL,
      close       REAL NOT NULL,
      volume      REAL NOT NULL,
      closed      INTEGER NOT NULL DEFAULT 1,
      created_at  INTEGER NOT NULL DEFAULT (unixepoch()),
      UNIQUE(symbol, timeframe, open_time)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS signals (
      id                  TEXT PRIMARY KEY,
      symbol              TEXT NOT NULL,
      direction           TEXT NOT NULL,
      setup_type          TEXT NOT NULL,
      regime              TEXT NOT NULL,
      score               REAL NOT NULL,
      probability         REAL,
      entry_low           REAL NOT NULL,
      entry_high          REAL NOT NULL,
      stop                REAL NOT NULL,
      target1             REAL NOT NULL,
      target2             REAL NOT NULL,
      rr                  REAL NOT NULL,
      validity_hours      REAL NOT NULL,
      invalidation_rule   TEXT NOT NULL,
      features_json       TEXT,
      emitted_at          INTEGER NOT NULL,
      expires_at          INTEGER NOT NULL,
      status              TEXT NOT NULL DEFAULT 'active',
      result_r            REAL,
      closed_at           INTEGER,
      mode                TEXT NOT NULL,
      comment             TEXT
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS metrics (
      id          INTEGER PRIMARY KEY AUTOINCREMENT,
      computed_at INTEGER NOT NULL,
      scope       TEXT NOT NULL,
      key         TEXT NOT NULL,
      value       REAL NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS external_alerts (
      id              TEXT PRIMARY KEY,
      source          TEXT NOT NULL,
      symbol          TEXT NOT NULL,
      signal          TEXT NOT NULL,
      probability     REAL NOT NULL,
      observed_at     INTEGER NOT NULL,
      title           TEXT,
      message         TEXT NOT NULL,
      metadata_json   TEXT,
      delivery_status TEXT NOT NULL DEFAULT 'detected',
      created_at      INTEGER NOT NULL DEFAULT (unixepoch())
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS indicator_states (
      symbol        TEXT NOT NULL,
      timeframe     TEXT NOT NULL,
      last_open_time INTEGER NOT NULL,
      state_json    TEXT NOT NULL,
      snapshot_json TEXT NOT NULL,
      updated_at    INTEGER NOT NULL DEFAULT (unixepoch()),
      PRIMARY KEY(symbol, timeframe)
    );
    """,
)

_UPSERT_CANDLE_SQL = """
INSERT INTO candles (
  symbol,
  timeframe,
  open_time,
  open,
  high,
  low,
  close,
  volume,
  closed
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(symbol, timeframe, open_time) DO UPDATE SET
  open = excluded.open,
  high = excluded.high,
  low = excluded.low,
  close = excluded.close,
  volume = excluded.volume,
  closed = excluded.closed;
"""

_UPSERT_SIGNAL_SQL = """
INSERT INTO signals (
  id,
  symbol,
  direction,
  setup_type,
  regime,
  score,
  probability,
  entry_low,
  entry_high,
  stop,
  target1,
  target2,
  rr,
  validity_hours,
  invalidation_rule,
  features_json,
  emitted_at,
  expires_at,
  status,
  result_r,
  closed_at,
  mode,
  comment
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(id) DO UPDATE SET
  symbol = excluded.symbol,
  direction = excluded.direction,
  setup_type = excluded.setup_type,
  regime = excluded.regime,
  score = excluded.score,
  probability = excluded.probability,
  entry_low = excluded.entry_low,
  entry_high = excluded.entry_high,
  stop = excluded.stop,
  target1 = excluded.target1,
  target2 = excluded.target2,
  rr = excluded.rr,
  validity_hours = excluded.validity_hours,
  invalidation_rule = excluded.invalidation_rule,
  features_json = excluded.features_json,
  emitted_at = excluded.emitted_at,
  expires_at = excluded.expires_at,
  status = excluded.status,
  result_r = excluded.result_r,
  closed_at = excluded.closed_at,
  mode = excluded.mode,
  comment = excluded.comment;
"""

_INSERT_METRIC_SQL = """
INSERT INTO metrics (
  computed_at,
  scope,
  key,
  value
) VALUES (?, ?, ?, ?);
"""

_UPSERT_EXTERNAL_ALERT_SQL = """
INSERT INTO external_alerts (
  id,
  source,
  symbol,
  signal,
  probability,
  observed_at,
  title,
  message,
  metadata_json,
  delivery_status
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(id) DO UPDATE SET
  source = excluded.source,
  symbol = excluded.symbol,
  signal = excluded.signal,
  probability = excluded.probability,
  observed_at = excluded.observed_at,
  title = excluded.title,
  message = excluded.message,
  metadata_json = excluded.metadata_json,
  delivery_status = excluded.delivery_status;
"""

_UPSERT_INDICATOR_STATE_SQL = """
INSERT INTO indicator_states (
  symbol,
  timeframe,
  last_open_time,
  state_json,
  snapshot_json
) VALUES (?, ?, ?, ?, ?)
ON CONFLICT(symbol, timeframe) DO UPDATE SET
  last_open_time = excluded.last_open_time,
  state_json = excluded.state_json,
  snapshot_json = excluded.snapshot_json,
  updated_at = unixepoch();
"""


def resolve_db_path(db_path: str | Path | None = None) -> Path:
    """Resolve the configured database path and ensure its parent exists."""

    candidate = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
    if not candidate.is_absolute():
        candidate = ROOT_DIR / candidate
    candidate.parent.mkdir(parents=True, exist_ok=True)
    return candidate


def get_connection(db_path: str | Path | None = None) -> sqlite3.Connection:
    """Create a SQLite connection configured for BotYo storage."""

    resolved_path = resolve_db_path(db_path)
    connection = sqlite3.connect(str(resolved_path), timeout=30.0, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL;")
    connection.execute("PRAGMA synchronous=NORMAL;")
    return connection


def init_db(db_path: str | Path | None = None) -> Path:
    """Initialize the SQLite database and create the required tables."""

    resolved_path = resolve_db_path(db_path)
    with get_connection(resolved_path) as connection:
        for statement in _SCHEMA_STATEMENTS:
            connection.execute(statement)
        connection.commit()
    return resolved_path


def _fetch_all(query: str, params: Sequence[Any], db_path: str | Path | None = None) -> list[dict[str, Any]]:
    with get_connection(db_path) as connection:
        rows = connection.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def _fetch_one(query: str, params: Sequence[Any], db_path: str | Path | None = None) -> dict[str, Any] | None:
    with get_connection(db_path) as connection:
        row = connection.execute(query, params).fetchone()
    return dict(row) if row is not None else None


def _execute_write(
    query: str,
    params: Sequence[Any],
    db_path: str | Path | None = None,
) -> int:
    with get_connection(db_path) as connection:
        cursor = connection.execute(query, params)
        connection.commit()
        return cursor.lastrowid


def upsert_candle(candle: Mapping[str, Any], db_path: str | Path | None = None) -> int:
    """Insert or update a candle row."""

    params = (
        candle["symbol"],
        candle["timeframe"],
        candle["open_time"],
        candle["open"],
        candle["high"],
        candle["low"],
        candle["close"],
        candle["volume"],
        int(candle.get("closed", 1)),
    )
    return _execute_write(_UPSERT_CANDLE_SQL, params, db_path)


def get_candle(symbol: str, timeframe: str, open_time: int, db_path: str | Path | None = None) -> dict[str, Any] | None:
    """Return a single candle by its unique key."""

    return _fetch_one(
        """
        SELECT * FROM candles
        WHERE symbol = ? AND timeframe = ? AND open_time = ?;
        """,
        (symbol, timeframe, open_time),
        db_path,
    )


def get_recent_candles(
    symbol: str,
    timeframe: str,
    limit: int,
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Return the most recent candles in ascending open_time order."""

    rows = _fetch_all(
        """
        SELECT * FROM candles
        WHERE symbol = ? AND timeframe = ?
        ORDER BY open_time DESC
        LIMIT ?;
        """,
        (symbol, timeframe, limit),
        db_path,
    )
    rows.reverse()
    return rows


def get_oldest_candle(
    symbol: str,
    timeframe: str,
    db_path: str | Path | None = None,
) -> dict[str, Any] | None:
    """Return the oldest stored candle for one symbol/timeframe."""

    return _fetch_one(
        """
        SELECT * FROM candles
        WHERE symbol = ? AND timeframe = ?
        ORDER BY open_time ASC
        LIMIT 1;
        """,
        (symbol, timeframe),
        db_path,
    )


def upsert_signal(signal: Mapping[str, Any], db_path: str | Path | None = None) -> str:
    """Insert or update a signal row."""

    params = (
        signal["id"],
        signal["symbol"],
        signal["direction"],
        signal["setup_type"],
        signal["regime"],
        signal["score"],
        signal.get("probability"),
        signal["entry_low"],
        signal["entry_high"],
        signal["stop"],
        signal["target1"],
        signal["target2"],
        signal["rr"],
        signal["validity_hours"],
        signal["invalidation_rule"],
        signal.get("features_json"),
        signal["emitted_at"],
        signal["expires_at"],
        signal.get("status", "active"),
        signal.get("result_r"),
        signal.get("closed_at"),
        signal["mode"],
        signal.get("comment"),
    )
    _execute_write(_UPSERT_SIGNAL_SQL, params, db_path)
    return str(signal["id"])


def get_signal(signal_id: str, db_path: str | Path | None = None) -> dict[str, Any] | None:
    """Return a single signal by ID."""

    return _fetch_one(
        "SELECT * FROM signals WHERE id = ?;",
        (signal_id,),
        db_path,
    )


def get_recent_signals(limit: int = 50, db_path: str | Path | None = None) -> list[dict[str, Any]]:
    """Return the most recent signals ordered by emission time."""

    return _fetch_all(
        """
        SELECT * FROM signals
        ORDER BY emitted_at DESC
        LIMIT ?;
        """,
        (limit,),
        db_path,
    )


def get_active_signals(
    symbol: str | None = None,
    direction: str | None = None,
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Return open signals, optionally filtered by symbol and direction."""

    clauses = ["status IN ('active', 'entered')"]
    params: list[Any] = []

    if symbol is not None:
        clauses.append("symbol = ?")
        params.append(symbol)
    if direction is not None:
        clauses.append("direction = ?")
        params.append(direction)

    query = "SELECT * FROM signals WHERE " + " AND ".join(clauses) + " ORDER BY emitted_at DESC;"
    return _fetch_all(query, tuple(params), db_path)


def update_signal_status(
    signal_id: str,
    status: str,
    *,
    result_r: float | None = None,
    closed_at: int | None = None,
    comment: str | None = None,
    db_path: str | Path | None = None,
) -> None:
    """Update a signal lifecycle status."""

    _execute_write(
        """
        UPDATE signals
        SET status = ?, result_r = ?, closed_at = ?, comment = ?
        WHERE id = ?;
        """,
        (status, result_r, closed_at, comment, signal_id),
        db_path,
    )


def insert_metric(
    scope: str,
    key: str,
    value: float,
    *,
    computed_at: int,
    db_path: str | Path | None = None,
) -> int:
    """Insert a metric sample."""

    return _execute_write(_INSERT_METRIC_SQL, (computed_at, scope, key, value), db_path)


def get_metrics(
    *,
    scope: str | None = None,
    key: str | None = None,
    limit: int = 100,
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Return metric rows filtered by scope and key."""

    clauses: list[str] = []
    params: list[Any] = []

    if scope is not None:
        clauses.append("scope = ?")
        params.append(scope)
    if key is not None:
        clauses.append("key = ?")
        params.append(key)

    query = "SELECT * FROM metrics"
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY computed_at DESC, id DESC LIMIT ?;"
    params.append(limit)

    return _fetch_all(query, tuple(params), db_path)


def upsert_external_alert(alert: Mapping[str, Any], db_path: str | Path | None = None) -> str:
    """Insert or update one external alert row."""

    params = (
        alert["id"],
        alert["source"],
        alert["symbol"],
        alert["signal"],
        alert["probability"],
        alert["observed_at"],
        alert.get("title"),
        alert["message"],
        alert.get("metadata_json"),
        alert.get("delivery_status", "detected"),
    )
    _execute_write(_UPSERT_EXTERNAL_ALERT_SQL, params, db_path)
    return str(alert["id"])


def get_external_alert(alert_id: str, db_path: str | Path | None = None) -> dict[str, Any] | None:
    """Return one external alert by its unique event ID."""

    return _fetch_one(
        "SELECT * FROM external_alerts WHERE id = ?;",
        (alert_id,),
        db_path,
    )


def get_recent_external_alerts(limit: int = 100, db_path: str | Path | None = None) -> list[dict[str, Any]]:
    """Return recent external alerts ordered by observation time."""

    return _fetch_all(
        """
        SELECT * FROM external_alerts
        ORDER BY observed_at DESC, created_at DESC
        LIMIT ?;
        """,
        (limit,),
        db_path,
    )


def upsert_indicator_state(state: Mapping[str, Any], db_path: str | Path | None = None) -> None:
    """Insert or update one persisted indicator state."""

    _execute_write(
        _UPSERT_INDICATOR_STATE_SQL,
        (
            state["symbol"],
            state["timeframe"],
            state["last_open_time"],
            state["state_json"],
            state["snapshot_json"],
        ),
        db_path,
    )


def get_indicator_state(
    symbol: str,
    timeframe: str,
    db_path: str | Path | None = None,
) -> dict[str, Any] | None:
    """Return one persisted indicator state for a symbol/timeframe."""

    return _fetch_one(
        """
        SELECT * FROM indicator_states
        WHERE symbol = ? AND timeframe = ?;
        """,
        (symbol, timeframe),
        db_path,
    )


@dataclass(slots=True)
class _QueuedWrite:
    query: str
    params: Sequence[Any]
    future: asyncio.Future[Any]
    stop: bool = False


class SQLiteWriteQueue:
    """Serialize SQLite writes through an asyncio queue."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = resolve_db_path(db_path)
        self._queue: asyncio.Queue[_QueuedWrite] = asyncio.Queue()
        self._worker_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Start the background writer task if it is not already running."""

        init_db(self.db_path)
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._worker(), name="botyo-sqlite-write-queue")

    async def stop(self) -> None:
        """Stop the background writer task cleanly."""

        if self._worker_task is None:
            return

        loop = asyncio.get_running_loop()
        future: asyncio.Future[None] = loop.create_future()
        await self._queue.put(_QueuedWrite("", (), future, stop=True))
        await future
        await self._worker_task
        self._worker_task = None

    async def execute(self, query: str, params: Sequence[Any] = ()) -> int:
        """Enqueue a write statement and wait for it to complete."""

        loop = asyncio.get_running_loop()
        future: asyncio.Future[int] = loop.create_future()
        await self._queue.put(_QueuedWrite(query, tuple(params), future))
        return await future

    async def upsert_candle(self, candle: Mapping[str, Any]) -> int:
        """Enqueue a candle upsert."""

        params = (
            candle["symbol"],
            candle["timeframe"],
            candle["open_time"],
            candle["open"],
            candle["high"],
            candle["low"],
            candle["close"],
            candle["volume"],
            int(candle.get("closed", 1)),
        )
        return await self.execute(_UPSERT_CANDLE_SQL, params)

    async def upsert_signal(self, signal: Mapping[str, Any]) -> int:
        """Enqueue a signal upsert."""

        params = (
            signal["id"],
            signal["symbol"],
            signal["direction"],
            signal["setup_type"],
            signal["regime"],
            signal["score"],
            signal.get("probability"),
            signal["entry_low"],
            signal["entry_high"],
            signal["stop"],
            signal["target1"],
            signal["target2"],
            signal["rr"],
            signal["validity_hours"],
            signal["invalidation_rule"],
            signal.get("features_json"),
            signal["emitted_at"],
            signal["expires_at"],
            signal.get("status", "active"),
            signal.get("result_r"),
            signal.get("closed_at"),
            signal["mode"],
            signal.get("comment"),
        )
        return await self.execute(_UPSERT_SIGNAL_SQL, params)

    async def insert_metric(
        self,
        *,
        computed_at: int,
        scope: str,
        key: str,
        value: float,
    ) -> int:
        """Enqueue a metric insert."""

        return await self.execute(_INSERT_METRIC_SQL, (computed_at, scope, key, value))

    async def upsert_external_alert(self, alert: Mapping[str, Any]) -> int:
        """Enqueue an external alert upsert."""

        params = (
            alert["id"],
            alert["source"],
            alert["symbol"],
            alert["signal"],
            alert["probability"],
            alert["observed_at"],
            alert.get("title"),
            alert["message"],
            alert.get("metadata_json"),
            alert.get("delivery_status", "detected"),
        )
        return await self.execute(_UPSERT_EXTERNAL_ALERT_SQL, params)

    async def upsert_indicator_state(self, state: Mapping[str, Any]) -> int:
        """Enqueue a persisted indicator state upsert."""

        return await self.execute(
            _UPSERT_INDICATOR_STATE_SQL,
            (
                state["symbol"],
                state["timeframe"],
                state["last_open_time"],
                state["state_json"],
                state["snapshot_json"],
            ),
        )

    async def _worker(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                if item.stop:
                    item.future.set_result(None)
                    break
                result = await asyncio.to_thread(_execute_write, item.query, item.params, self.db_path)
                item.future.set_result(result)
            except Exception as exc:
                if not item.future.done():
                    item.future.set_exception(exc)
            finally:
                self._queue.task_done()
