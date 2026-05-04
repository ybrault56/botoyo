"""Supervisor, config helpers and lifecycle gates for BotYo."""

from __future__ import annotations

import asyncio
import bisect
import hashlib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import yaml

from app.alerts.telegram import send_alert, send_telegram_message
from app.indicators.adx import ADXCalculator, detect_swing_highs, detect_swing_lows
from app.indicators.atr import ATRCalculator
from app.indicators.ema import EMACalculator
from app.indicators.rsi import RSICalculator
from app.market.kraken_rest import TIMEFRAME_TO_INTERVAL, bootstrap_history
from app.market.kraken_ws import KrakenWebSocket
from app.storage.db import (
    SQLiteWriteQueue,
    get_active_signals,
    get_indicator_state,
    get_oldest_candle,
    get_recent_candles,
    get_recent_signals,
    init_db,
    upsert_signal,
)
from app.strategy.probability import ProbabilityEngine
from app.strategy.regime import REGIMES_INTERDITS_ALERTE, classify_regime, diagnose_regime
from app.strategy.scoring import score_setup
from app.strategy.setups import compute_volume_ratio, detect_setups, diagnose_setups
from app.utils.json import dumps, loads
from app.utils.env import load_env_file
from app.utils.logging import ROOT_DIR, get_logger
from app.whales.service import WhalesModule

LOGGER = get_logger("app.supervisor")

_PROBABILITY_REFRESH_STATUSES = {
    "expired",
    "cancelled",
    "hit_t1",
    "hit_stop",
    "expired_without_entry",
    "expired_after_entry",
    "cancelled_regime_change",
}


def load_config(config_path: str | Path | None = None) -> dict[str, Any]:
    """Load BotYo YAML config from disk."""

    path = resolve_config_path(config_path)
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def save_config(config: dict[str, Any], config_path: str | Path | None = None) -> None:
    """Persist BotYo YAML config to disk."""

    path = resolve_config_path(config_path)
    path.write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=False), encoding="utf-8")


def resolve_config_path(config_path: str | Path | None = None) -> Path:
    path = Path(config_path) if config_path is not None else ROOT_DIR / "config" / "bot.yaml"
    if not path.is_absolute():
        path = ROOT_DIR / path
    return path


def should_alert(signal: dict[str, Any], active_signals: list[dict[str, Any]], config: dict[str, Any]) -> tuple[bool, str]:
    """Apply cooldowns, concurrency limits and quality filters."""

    alerts_cfg = config["alerts"]
    filters_cfg = config["filters"]
    risk_cfg = config["risk"]
    probability_cfg = config["probability"]

    symbol = str(signal["symbol"])
    direction = str(signal["direction"])
    emitted_at = _coerce_timestamp(signal.get("emitted_at", _utc_now_ts()))

    if filters_cfg["reject_if_data_incomplete"] and not signal.get("data_complete", True):
        return False, "data incomplete"
    if filters_cfg["reject_if_no_clear_stop"] and not signal.get("stop"):
        return False, "missing stop"
    if filters_cfg["reject_if_rr_below_min"] and float(signal.get("rr", 0.0)) < float(risk_cfg["min_rr_for_alert"]):
        return False, "rr below minimum"

    delivery_mode = str(signal.get("delivery_mode", signal.get("mode", "shadow"))).strip().lower()
    threshold = float(probability_cfg["probability_threshold_live"])
    if delivery_mode != "live":
        threshold = float(probability_cfg["probability_threshold_shadow"])
    if filters_cfg["reject_if_probability_below_threshold"] and float(signal.get("probability", 0.0)) < threshold:
        return False, "probability below threshold"

    wick_ratio = float(signal.get("confirmation_wick_ratio", 0.0))
    if filters_cfg["reject_if_confirmation_wick_excessive"] and wick_ratio > 0.60:
        return False, "confirmation wick excessive"

    if filters_cfg["reject_if_late_entry"] and bool(signal.get("late_entry", False)):
        return False, "late entry"

    active_only = [item for item in active_signals if item.get("status", "active") in {"active", "entered"}]
    if len(active_only) >= int(alerts_cfg["max_active_alerts_total"]):
        return False, "max active alerts reached"

    for active in active_only:
        if active.get("symbol") == symbol and active.get("direction") == direction:
            return False, "active alert already exists for asset and direction"

    for existing in active_signals:
        if existing.get("symbol") != symbol:
            continue
        existing_direction = str(existing.get("direction"))
        delta_hours = abs(emitted_at - _coerce_timestamp(existing.get("emitted_at", emitted_at))) / 3600.0
        if existing_direction == direction and delta_hours < float(alerts_cfg["cooldown_per_asset_same_direction_hours"]):
            return False, "same direction cooldown active"
        if existing_direction != direction and delta_hours < float(alerts_cfg["cooldown_per_asset_opposite_direction_hours"]):
            return False, "opposite direction cooldown active"

    return True, ""


def expire_active_signals(
    active_signals: list[dict[str, Any]],
    *,
    current_time: int,
    current_regimes: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Return expired signal updates based on validity and regime changes."""

    expired: list[dict[str, Any]] = []
    current_regimes = current_regimes or {}

    for signal in active_signals:
        if signal.get("status", "active") != "active":
            continue
        reason = ""
        if current_time >= int(signal.get("expires_at", current_time + 1)):
            reason = "validity expired"
        elif bool(signal.get("target1_hit_without_execution", False)):
            reason = "target1 hit without execution"
        elif bool(signal.get("stop_incoherent", False)):
            reason = "stop incoherent"
        elif current_regimes.get(str(signal["symbol"])) not in {None, signal.get("regime")}:
            reason = "regime changed"

        if reason:
            updated = dict(signal)
            updated["status"] = "expired"
            updated["comment"] = reason
            expired.append(updated)

    return expired


@dataclass(slots=True)
class BotYoSupervisor:
    """Async orchestration for data intake, analysis and alert journaling."""

    config_path: Path = field(default_factory=resolve_config_path)
    db_path: Path = field(default_factory=lambda: ROOT_DIR / "data" / "botyo.db")
    config: dict[str, Any] = field(init=False)
    write_queue: SQLiteWriteQueue = field(init=False)
    probability_engine: ProbabilityEngine = field(init=False)
    suspended_symbols: set[str] = field(default_factory=set)
    latest_candle_times: dict[str, dict[str, int]] = field(default_factory=dict)
    latest_regimes: dict[str, str] = field(default_factory=dict)
    live_activation_status: dict[str, Any] = field(default_factory=dict)
    started_at: datetime | None = None
    last_startup_sync_at: datetime | None = None
    last_market_sync_at: datetime | None = None
    last_recalibrated_at: datetime | None = None
    live_eligibility_notified_at: datetime | None = None
    last_startup_sync_summary: dict[str, Any] = field(default_factory=dict)
    history_coverage: dict[str, Any] = field(default_factory=dict)
    ws_client: KrakenWebSocket | None = None
    ws_task: asyncio.Task[None] | None = None
    market_sync_task: asyncio.Task[None] | None = None
    recalibration_task: asyncio.Task[None] | None = None
    whales_module: WhalesModule | None = None
    indicator_context_states: dict[str, dict[str, Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.config = load_config(self.config_path)
        load_env_file(self.config.get("whales", {}).get("env_file"))
        self.write_queue = SQLiteWriteQueue(self.db_path)
        self.probability_engine = ProbabilityEngine(self.config)
        self.live_activation_status = self.probability_engine.get_live_activation_status()
        self.whales_module = WhalesModule(
            config=self.config,
            db_path=self.db_path,
            write_queue=self.write_queue,
            price_resolver=self.get_latest_reference_price,
            technical_context_resolver=self.get_whale_technical_context,
        )

    async def start(self) -> None:
        """Initialize DB, load config and start background tasks."""

        init_db(self.db_path)
        await self.write_queue.start()
        self.started_at = datetime.now(timezone.utc)
        self.probability_engine = ProbabilityEngine(self.config)
        self.indicator_context_states.clear()

        if self.config["bot"]["environment"] in {"shadow_live", "live_alert"}:
            await self.bootstrap_all_history()
            await self.recalibrate_probability(notify=True)
            if self.whales_module is not None:
                await self.whales_module.reconfigure(self.config)
            intervals = [TIMEFRAME_TO_INTERVAL[timeframe] for timeframe in _configured_timeframe_keys(self.config)]
            self.ws_client = KrakenWebSocket(
                symbols=list(self.config["markets"]["symbols"]),
                intervals=intervals,
                write_queue=self.write_queue,
                on_candle_close=self.on_candle_close,
            )
            self.ws_task = asyncio.create_task(self.ws_client.run(), name="botyo-kraken-ws")
            self._restart_market_sync_task()
            self._restart_recalibration_task()
            LOGGER.info("supervisor started in %s", self.config["bot"]["environment"])
            return

        if self.config["bot"]["environment"] == "backtest":
            await self.recalibrate_probability(notify=True)
            await self.run_backtest()

    async def stop(self) -> None:
        """Stop background tasks and flush pending writes."""

        LOGGER.info("supervisor stopping")
        if self.ws_client is not None:
            await self.ws_client.stop()
        if self.ws_task is not None:
            self.ws_task.cancel()
            try:
                await self.ws_task
            except asyncio.CancelledError:
                pass
        if self.market_sync_task is not None:
            self.market_sync_task.cancel()
            try:
                await self.market_sync_task
            except asyncio.CancelledError:
                pass
        if self.recalibration_task is not None:
            self.recalibration_task.cancel()
            try:
                await self.recalibration_task
            except asyncio.CancelledError:
                pass
        if self.whales_module is not None:
            await self.whales_module.stop()
        await self.write_queue.stop()
        LOGGER.info("supervisor stopped")

    async def reload_config(self) -> dict[str, Any]:
        """Reload config from disk without restarting the process."""

        self.config = load_config(self.config_path)
        load_env_file(self.config.get("whales", {}).get("env_file"))
        self.probability_engine = ProbabilityEngine(self.config)
        self.indicator_context_states.clear()
        await self.recalibrate_probability(notify=True)
        if self.whales_module is not None:
            await self.whales_module.reconfigure(self.config)
        if self.config["bot"]["environment"] in {"shadow_live", "live_alert"}:
            self._restart_market_sync_task()
            self._restart_recalibration_task()
        LOGGER.info("configuration reloaded")
        return self.config

    async def recalibrate_probability(self, *, notify: bool) -> dict[str, Any]:
        """Rebuild probability calibration and refresh live activation readiness."""

        history = get_recent_signals(limit=5000, db_path=self.db_path)
        previously_eligible = bool(self.live_activation_status.get("eligible", False))
        self.probability_engine.recalibrate(history)
        self.last_recalibrated_at = datetime.now(timezone.utc)
        self.live_activation_status = self.probability_engine.get_live_activation_status()

        if not self.live_activation_status["eligible"]:
            self.live_eligibility_notified_at = None
            return self.live_activation_status

        if (
            notify
            and self.config["bot"]["environment"] == "shadow_live"
            and (not previously_eligible or self.live_eligibility_notified_at is None)
        ):
            sent = await send_telegram_message(
                _format_live_activation_message(self.live_activation_status),
                self.config,
                force=True,
            )
            if sent:
                self.live_eligibility_notified_at = self.last_recalibrated_at

        return self.live_activation_status

    async def _recalibration_loop(self) -> None:
        """Run periodic recalibration according to the configured cadence."""

        interval_seconds = _recalibration_interval_seconds(self.config["probability"]["recalibration_frequency"])
        while True:
            try:
                await asyncio.sleep(interval_seconds)
                await self.recalibrate_probability(notify=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOGGER.error("periodic recalibration failed: %s", exc)

    async def _market_sync_loop(self) -> None:
        """Keep closed candles refreshed while the bot is running."""

        interval_seconds = max(30, int(self.config["data"].get("runtime_sync_seconds", 60)))
        while True:
            try:
                await asyncio.sleep(interval_seconds)
                await self.runtime_sync_history()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOGGER.error("periodic market sync failed: %s", exc)

    def _restart_market_sync_task(self) -> None:
        if self.market_sync_task is not None and not self.market_sync_task.done():
            self.market_sync_task.cancel()
        self.market_sync_task = asyncio.create_task(
            self._market_sync_loop(),
            name="botyo-market-runtime-sync",
        )

    def _restart_recalibration_task(self) -> None:
        if self.recalibration_task is not None and not self.recalibration_task.done():
            self.recalibration_task.cancel()
        self.recalibration_task = asyncio.create_task(
            self._recalibration_loop(),
            name="botyo-probability-recalibration",
        )

    async def bootstrap_all_history(self) -> None:
        """Synchronize candles from REST at startup and refresh the runtime state."""

        lookback_days = int(self.config["data"]["historical_lookback_days"])
        summary: dict[str, Any] = {"symbols": {}, "analyzed": {}}

        for symbol in self.config["markets"]["symbols"]:
            symbol_summary: dict[str, Any] = {}
            for timeframe in _configured_timeframe_keys(self.config):
                interval_seconds = TIMEFRAME_TO_INTERVAL[timeframe] * 60
                existing = get_recent_candles(symbol, timeframe, 1, db_path=self.db_path)
                recoverable_gap = True
                sync_since = None
                if existing:
                    recoverable_gap = (_utc_now_ts() - int(existing[-1]["open_time"])) <= (interval_seconds * 719)
                    if not recoverable_gap:
                        LOGGER.warning(
                            "startup gap for %s %s exceeds Kraken REST recoverable window; missing candles may remain",
                            symbol,
                            timeframe,
                        )
                    sync_since = max(0, int(existing[-1]["open_time"]) - (interval_seconds * 2))
                try:
                    synced = await bootstrap_history(
                        symbol,
                        timeframe,
                        lookback_days,
                        write_queue=self.write_queue,
                        since=sync_since,
                        db_path=self.db_path,
                    )
                except Exception as exc:
                    LOGGER.error("startup sync failed for %s %s: %s", symbol, timeframe, exc)
                    self.suspend_symbol(symbol)
                    symbol_summary[timeframe] = {
                        "synced": 0,
                        "latest_open_time": existing[-1]["open_time"] if existing else None,
                        "error": str(exc),
                    }
                    continue

                latest_open_time: int | None = None
                if synced:
                    latest_open_time = int(synced[-1]["open_time"])
                elif existing:
                    latest_open_time = int(existing[-1]["open_time"])
                if latest_open_time is not None:
                    self.latest_candle_times.setdefault(symbol, {})[timeframe] = latest_open_time
                coverage = _history_coverage_snapshot(
                    symbol=symbol,
                    timeframe=timeframe,
                    lookback_days=lookback_days,
                    db_path=self.db_path,
                )
                symbol_summary[timeframe] = {
                    "synced": len(synced),
                    "latest_open_time": latest_open_time,
                    "since": sync_since,
                    "recoverable_gap": recoverable_gap,
                    **coverage,
                }

            summary["symbols"][symbol] = symbol_summary

        for symbol in self.config["markets"]["symbols"]:
            if symbol in self.suspended_symbols:
                continue
            try:
                analyzed_bars = await self._startup_backfill_symbol(symbol)
                summary["analyzed"][symbol] = analyzed_bars
            except Exception as exc:
                LOGGER.error("startup analysis failed for %s: %s", symbol, exc)
                self.suspend_symbol(symbol)
                summary["analyzed"][symbol] = "error"

        self.last_startup_sync_at = datetime.now(timezone.utc)
        self.last_startup_sync_summary = summary
        self.history_coverage = _summarize_history_coverage(summary["symbols"])

    async def runtime_sync_history(self) -> dict[str, Any]:
        """Refresh recent closed candles while the bot is running."""

        lookback_days = int(self.config["data"]["historical_lookback_days"])
        entry_tf = str(self.config["timeframes"]["entry"])
        synced_summary: dict[str, Any] = {"symbols": {}, "replayed": {}}

        for symbol in self.config["markets"]["symbols"]:
            if symbol in self.suspended_symbols:
                continue

            symbol_summary: dict[str, Any] = {}
            previous_entry_latest = self.latest_candle_times.get(symbol, {}).get(entry_tf)
            current_entry_latest = previous_entry_latest

            for timeframe in _configured_timeframe_keys(self.config):
                interval_seconds = TIMEFRAME_TO_INTERVAL[timeframe] * 60
                existing = get_recent_candles(symbol, timeframe, 1, db_path=self.db_path)
                previous_latest = int(existing[-1]["open_time"]) if existing else None
                sync_since = (
                    max(0, previous_latest - (interval_seconds * 2))
                    if previous_latest is not None
                    else None
                )
                synced = await bootstrap_history(
                    symbol,
                    timeframe,
                    lookback_days,
                    write_queue=self.write_queue,
                    since=sync_since,
                    db_path=self.db_path,
                )

                latest_open_time = previous_latest
                if synced:
                    latest_open_time = int(synced[-1]["open_time"])
                if latest_open_time is not None:
                    self.latest_candle_times.setdefault(symbol, {})[timeframe] = latest_open_time
                symbol_summary[timeframe] = {
                    "synced": len(synced),
                    "latest_open_time": latest_open_time,
                    "since": sync_since,
                }
                if timeframe == entry_tf:
                    current_entry_latest = latest_open_time
                    if len(synced) > 0:
                        LOGGER.info(
                            "runtime market sync refreshed %s %s with %s closed candles",
                            symbol,
                            timeframe,
                            len(synced),
                        )

            replayed = 0
            if current_entry_latest is not None and (
                previous_entry_latest is None or current_entry_latest > previous_entry_latest
            ):
                replayed = await self._replay_entry_candle_range(
                    symbol,
                    after_open_time=previous_entry_latest,
                    up_to_open_time=current_entry_latest,
                )

            synced_summary["symbols"][symbol] = symbol_summary
            synced_summary["replayed"][symbol] = replayed

        self.last_market_sync_at = datetime.now(timezone.utc)
        return synced_summary

    async def on_candle_close(self, symbol: str, timeframe: str, candle: dict[str, Any]) -> None:
        """Handle one closed candle from the WebSocket stream."""

        self.latest_candle_times.setdefault(symbol, {})[timeframe] = int(candle["open_time"])
        if symbol in self.suspended_symbols:
            return
        try:
            if timeframe == str(self.config["timeframes"]["entry"]):
                await self.analyze_symbol(symbol)
        except Exception as exc:
            LOGGER.error("analysis failed for %s: %s", symbol, exc)
            self.suspend_symbol(symbol)

    async def analyze_symbol(
        self,
        symbol: str,
        *,
        emitted_at: int | None = None,
        mode_override: str | None = None,
        analysis_context: str | None = None,
    ) -> list[dict[str, Any]]:
        """Run the full analysis pipeline for one symbol."""

        candles_by_tf = self._load_candle_windows(symbol)
        if not all(candles_by_tf.values()):
            return []

        return await self._analyze_candles(
            symbol,
            candles_by_tf,
            emitted_at=emitted_at,
            mode_override=mode_override,
            analysis_context=analysis_context,
        )

    async def _analyze_candles(
        self,
        symbol: str,
        candles_by_tf: dict[str, list[dict[str, Any]]],
        *,
        emitted_at: int | None,
        mode_override: str | None = None,
        analysis_context: str | None = None,
    ) -> list[dict[str, Any]]:
        """Run the strategy pipeline on explicit candle windows."""

        trend_tf = str(self.config["timeframes"]["trend"])
        setup_tf = str(self.config["timeframes"]["setup"])
        effective_emitted_at = _analysis_emitted_at(candles_by_tf, self.config, emitted_at)
        indicators_by_tf = {}
        for timeframe, candles in candles_by_tf.items():
            indicators_by_tf[timeframe] = await self._indicator_snapshot(
                symbol,
                timeframe,
                candles,
                context_id=analysis_context,
                persist_state=self._should_persist_indicator_state(symbol, timeframe, candles, mode_override),
            )
        regime = classify_regime(indicators_by_tf[trend_tf], indicators_by_tf[setup_tf], self.config)
        self.latest_regimes[symbol] = regime
        runtime_mode = mode_override or self.config["bot"]["environment"]

        lifecycle_summary = self._reconcile_signal_lifecycle(
            symbol=symbol,
            regime=regime,
            candles_by_tf=candles_by_tf,
            current_time=effective_emitted_at,
        )
        if lifecycle_summary["refresh_probability"] and runtime_mode in {"shadow_live", "live_alert"}:
            await self.recalibrate_probability(notify=True)

        setups = detect_setups(symbol, regime, indicators_by_tf, candles_by_tf, self.config)
        emitted: list[dict[str, Any]] = []

        for setup in setups:
            scored = score_setup(setup, indicators_by_tf, self.config)
            probability = self.probability_engine.estimate(
                scored["score"],
                {"symbol": symbol, **setup["features"]},
                setup["type"],
                setup["direction"],
            )
            signal = _build_signal(
                symbol,
                regime,
                setup,
                scored,
                probability,
                effective_emitted_at,
                runtime_mode,
                indicators_by_tf,
                candles_by_tf,
                self.config,
            )
            active_signals = get_active_signals(db_path=self.db_path)
            allowed, reason = should_alert(signal, active_signals, self.config)

            if regime in REGIMES_INTERDITS_ALERTE or scored["decision"] == "reject":
                signal["status"] = "rejected"
                signal["comment"] = "regime or score rejected"
            elif runtime_mode == "live_alert" and signal["delivery_mode"] != "live":
                signal["status"] = "rejected"
                signal["comment"] = "live gating blocked segment or probability"
            elif not allowed:
                signal["status"] = "rejected"
                signal["comment"] = reason
            else:
                if bool(signal["features"].get("executed", False)):
                    signal["status"] = "entered"
                    signal["comment"] = "entry assumed at signal close"
                else:
                    signal["status"] = "active"
                    signal["comment"] = ""

            upsert_signal(
                {
                    **signal,
                    "features_json": dumps(signal["features"]),
                },
                db_path=self.db_path,
            )
            emitted.append(signal)

            if signal["status"] in {"active", "entered"} and runtime_mode == "live_alert" and signal["delivery_mode"] == "live":
                await send_alert(signal, self.config)

        if emitted and runtime_mode in {"shadow_live", "live_alert"}:
            await self.recalibrate_probability(notify=True)

        return emitted

    async def run_backtest(self) -> list[dict[str, Any]]:
        """Run a lightweight backtest sweep over currently stored candles."""

        emitted: list[dict[str, Any]] = []
        indicators_cfg = self.config["data"]["indicators"]
        min_entry_bars = max(
            20,
            int(indicators_cfg["volume_ma_period"]),
            int(indicators_cfg["atr_period"]) + 1,
            int(indicators_cfg["rsi_period"]) + 1,
            int(indicators_cfg["adx_period"]) + 1,
            (int(indicators_cfg["swing_window"]) * 2) + 1,
        )
        trend_tf = str(self.config["timeframes"]["trend"])
        setup_tf = str(self.config["timeframes"]["setup"])
        entry_tf = str(self.config["timeframes"]["entry"])
        for symbol in self.config["markets"]["symbols"]:
            context_id = f"backtest:{symbol}"
            bars = self.config["data"]["warmup_bars"]
            candles_trend = get_recent_candles(symbol, trend_tf, int(bars[trend_tf]), db_path=self.db_path)
            candles_setup = get_recent_candles(symbol, setup_tf, int(bars[setup_tf]), db_path=self.db_path)
            candles_entry = get_recent_candles(symbol, entry_tf, int(bars[entry_tf]), db_path=self.db_path)
            if not candles_trend or not candles_setup or not candles_entry:
                continue

            start_index = min(min_entry_bars, max(len(candles_entry) - 1, 0))

            try:
                for entry_index in range(start_index, len(candles_entry)):
                    current_end = int(candles_entry[entry_index]["open_time"])
                    window = {
                        trend_tf: [candle for candle in candles_trend if int(candle["open_time"]) <= current_end],
                        setup_tf: [candle for candle in candles_setup if int(candle["open_time"]) <= current_end],
                        entry_tf: candles_entry[: entry_index + 1],
                    }
                    if len(window[trend_tf]) < 20 or len(window[setup_tf]) < 20 or len(window[entry_tf]) < 20:
                        continue
                    emitted.extend(
                        await self._analyze_candles(
                            symbol,
                            window,
                            emitted_at=current_end + (TIMEFRAME_TO_INTERVAL[entry_tf] * 60),
                            mode_override="backtest",
                            analysis_context=context_id,
                        )
                    )
            finally:
                self._clear_indicator_context(context_id)
        return emitted

    def suspend_symbol(self, symbol: str) -> None:
        self.suspended_symbols.add(symbol)

    def get_latest_reference_price(self, symbol: str) -> float | None:
        """Return the latest closed price known in SQLite for one market symbol."""

        candles = get_recent_candles(symbol, str(self.config["timeframes"]["entry"]), 1, db_path=self.db_path)
        if not candles:
            return None
        return float(candles[-1]["close"])

    def status_snapshot(self) -> dict[str, Any]:
        """Return the current runtime status for the API."""

        uptime = 0.0
        if self.started_at is not None:
            uptime = (datetime.now(timezone.utc) - self.started_at).total_seconds()
        return {
            "mode": self.config["bot"]["environment"],
            "uptime_seconds": round(uptime, 2),
            "suspended_symbols": sorted(self.suspended_symbols),
            "latest_candle_times": self.latest_candle_times,
            "last_startup_sync_at": (
                self.last_startup_sync_at.strftime("%Y-%m-%d %H:%M:%S UTC")
                if self.last_startup_sync_at is not None
                else None
            ),
            "startup_sync_summary": self.last_startup_sync_summary,
            "last_market_sync_at": (
                self.last_market_sync_at.strftime("%Y-%m-%d %H:%M:%S UTC")
                if self.last_market_sync_at is not None
                else None
            ),
            "last_recalibrated_at": (
                self.last_recalibrated_at.strftime("%Y-%m-%d %H:%M:%S UTC")
                if self.last_recalibrated_at is not None
                else None
            ),
            "live_activation": self.live_activation_status,
            "history_coverage": self.history_coverage,
            "whales": self.whales_module.status_snapshot() if self.whales_module is not None else {},
        }

    def get_whale_technical_context(self, asset_symbol: str) -> dict[str, Any] | None:
        """Return a compact multi-timeframe technical context for whales alerts."""

        market_symbol = f"{str(asset_symbol).upper()}USDT"
        candles_by_tf = self._load_candle_windows(market_symbol)
        if not all(candles_by_tf.values()):
            return None

        trend_tf = str(self.config["timeframes"]["trend"])
        setup_tf = str(self.config["timeframes"]["setup"])
        entry_tf = str(self.config["timeframes"]["entry"])
        indicators_by_tf = {
            timeframe: self._indicator_snapshot_for_diagnostics(market_symbol, timeframe, candles)
            for timeframe, candles in candles_by_tf.items()
        }
        regime_details = diagnose_regime(indicators_by_tf[trend_tf], indicators_by_tf[setup_tf], self.config)
        entry_indicators = indicators_by_tf[entry_tf]
        bias = _technical_bias_from_regime(regime_details, entry_indicators)
        probability = _technical_confirmation_probability(regime_details, entry_indicators, bias)
        summary = _technical_confirmation_summary(
            regime_details=regime_details,
            entry_indicators=entry_indicators,
            bias=bias,
        )
        return {
            "market_symbol": market_symbol,
            "regime": regime_details["regime"],
            "bias": bias,
            "probability": probability,
            "summary": summary,
            "trend_structure": regime_details.get("trend_structure", "neutral"),
            "setup_structure": regime_details.get("setup_structure", "neutral"),
            "entry_structure": entry_indicators.get("structure", "neutral"),
            "entry_rsi": round(float(entry_indicators.get("rsi", 50.0)), 1),
            "entry_macd_cross": str(entry_indicators.get("macd_cross", "none")),
            "entry_volume_ratio": round(float(entry_indicators.get("volume_ratio", 1.0)), 2),
        }

    def build_symbol_diagnostics(self) -> list[dict[str, Any]]:
        """Return one diagnostic snapshot per configured symbol."""

        diagnostics: list[dict[str, Any]] = []
        trend_tf = str(self.config["timeframes"]["trend"])
        setup_tf = str(self.config["timeframes"]["setup"])
        entry_tf = str(self.config["timeframes"]["entry"])

        for symbol in self.config["markets"]["symbols"]:
            candles_by_tf = self._load_candle_windows(symbol)
            if not all(candles_by_tf.values()):
                diagnostics.append(
                    {
                        "symbol": symbol,
                        "regime": "insufficient_data",
                        "summary": "Donnees insuffisantes",
                        "bias": "neutral",
                        "blockers": ["historique insuffisant sur un ou plusieurs timeframes"],
                        "trend_structure": "neutral",
                        "setup_structure": "neutral",
                        "trend_adx": 0.0,
                        "volume_ratio": 0.0,
                        "mean_volume_ratio": 0.0,
                        "range_atr_ratio": 0.0,
                        "entry_rsi": 0.0,
                        "entry_macd_cross": "none",
                        "setups": [],
                    }
                )
                continue

            indicators_by_tf = {
                timeframe: self._indicator_snapshot_for_diagnostics(symbol, timeframe, candles)
                for timeframe, candles in candles_by_tf.items()
            }
            regime_details = diagnose_regime(indicators_by_tf[trend_tf], indicators_by_tf[setup_tf], self.config)
            entry_indicators = indicators_by_tf[entry_tf]
            diagnostics.append(
                {
                    "symbol": symbol,
                    **regime_details,
                    "entry_rsi": round(float(entry_indicators.get("rsi", 50.0)), 1),
                    "entry_macd_cross": str(entry_indicators.get("macd_cross", "none")),
                    "setups": diagnose_setups(symbol, regime_details["regime"], indicators_by_tf, candles_by_tf, self.config),
                }
            )

        return diagnostics

    def _load_candle_windows(self, symbol: str) -> dict[str, list[dict[str, Any]]]:
        bars = self.config["data"]["warmup_bars"]
        return {
            timeframe: get_recent_candles(symbol, timeframe, int(bars[timeframe]), db_path=self.db_path)
            for timeframe in _configured_timeframe_keys(self.config)
        }

    async def _indicator_snapshot(
        self,
        symbol: str,
        timeframe: str,
        candles: list[dict[str, Any]],
        *,
        context_id: str | None,
        persist_state: bool,
    ) -> dict[str, Any]:
        cache_key = self._indicator_cache_key(context_id, symbol, timeframe)
        latest_open_time = int(candles[-1]["open_time"])
        bundle = self.indicator_context_states.get(cache_key)
        if bundle is not None and int(bundle["last_open_time"]) == latest_open_time:
            return dict(bundle["snapshot"])

        if bundle is None and persist_state:
            persisted = get_indicator_state(symbol, timeframe, db_path=self.db_path)
            if persisted is not None:
                loaded = _load_indicator_bundle(persisted)
                if loaded is not None and latest_open_time >= int(loaded["last_open_time"]):
                    bundle = loaded

        if bundle is None or _indicator_bundle_requires_rebuild(bundle, candles, timeframe):
            bundle = _build_indicator_bundle(candles, self.config)
        else:
            bundle = _advance_indicator_bundle(bundle, candles, timeframe, self.config)

        self.indicator_context_states[cache_key] = bundle
        if persist_state:
            await self.write_queue.upsert_indicator_state(
                _indicator_state_record(symbol, timeframe, bundle)
            )
        return dict(bundle["snapshot"])

    def _indicator_snapshot_for_diagnostics(
        self,
        symbol: str,
        timeframe: str,
        candles: list[dict[str, Any]],
    ) -> dict[str, Any]:
        latest_open_time = int(candles[-1]["open_time"])
        live_bundle = self.indicator_context_states.get(self._indicator_cache_key(None, symbol, timeframe))
        if live_bundle is not None and int(live_bundle["last_open_time"]) == latest_open_time:
            return dict(live_bundle["snapshot"])

        persisted = get_indicator_state(symbol, timeframe, db_path=self.db_path)
        if persisted is not None and int(persisted.get("last_open_time", 0) or 0) == latest_open_time:
            try:
                snapshot = loads(str(persisted["snapshot_json"]))
            except Exception:
                snapshot = None
            if isinstance(snapshot, Mapping):
                return dict(snapshot)
        return _compute_indicator_snapshot(candles, self.config)

    def _should_persist_indicator_state(
        self,
        symbol: str,
        timeframe: str,
        candles: list[dict[str, Any]],
        mode_override: str | None,
    ) -> bool:
        runtime_mode = mode_override or self.config["bot"]["environment"]
        if runtime_mode not in {"shadow_live", "live_alert"}:
            return False
        latest_known = self.latest_candle_times.get(symbol, {}).get(timeframe)
        return latest_known is not None and int(candles[-1]["open_time"]) == int(latest_known)

    def _clear_indicator_context(self, context_id: str) -> None:
        prefix = f"{context_id}:"
        for key in [key for key in self.indicator_context_states if key.startswith(prefix)]:
            self.indicator_context_states.pop(key, None)

    @staticmethod
    def _indicator_cache_key(context_id: str | None, symbol: str, timeframe: str) -> str:
        return f"{context_id or 'live'}:{symbol}:{timeframe}"

    async def _startup_backfill_symbol(self, symbol: str) -> int:
        """Replay the recent entry-timeframe closes missed while the bot was stopped."""

        entry_tf = str(self.config["timeframes"]["entry"])
        context_id = f"startup-backfill:{symbol}"
        backfill_bars = int(self.config["data"].get("startup_signal_backfill_bars", 0))
        if backfill_bars <= 0:
            try:
                await self.analyze_symbol(symbol, analysis_context=context_id)
            finally:
                self._clear_indicator_context(context_id)
            return 1

        warmup_bars = self.config["data"]["warmup_bars"]
        candle_batches: dict[str, list[dict[str, Any]]] = {}
        open_times: dict[str, list[int]] = {}
        for timeframe in _configured_timeframe_keys(self.config):
            limit = int(warmup_bars[timeframe]) + backfill_bars + 4
            candle_batches[timeframe] = get_recent_candles(symbol, timeframe, limit, db_path=self.db_path)
            open_times[timeframe] = [int(candle["open_time"]) for candle in candle_batches[timeframe]]

        entry_candles = candle_batches.get(entry_tf, [])
        if not entry_candles:
            return 0

        interval_seconds = TIMEFRAME_TO_INTERVAL[entry_tf] * 60
        analyzed = 0
        try:
            for entry_candle in entry_candles[-backfill_bars:]:
                entry_open_time = int(entry_candle["open_time"])
                windows: dict[str, list[dict[str, Any]]] = {}
                for timeframe, candles in candle_batches.items():
                    idx = bisect.bisect_right(open_times[timeframe], entry_open_time)
                    windows[timeframe] = candles[:idx]
                if not all(windows.values()):
                    continue
                await self._analyze_candles(
                    symbol,
                    windows,
                    emitted_at=entry_open_time + interval_seconds,
                    mode_override=self.config["bot"]["environment"],
                    analysis_context=context_id,
                )
                analyzed += 1
            return analyzed
        finally:
            self._clear_indicator_context(context_id)

    async def _replay_entry_candle_range(
        self,
        symbol: str,
        *,
        after_open_time: int | None,
        up_to_open_time: int,
    ) -> int:
        """Replay only the entry candles newly recovered after runtime sync."""

        entry_tf = str(self.config["timeframes"]["entry"])
        context_id = f"runtime-replay:{symbol}"
        warmup_bars = self.config["data"]["warmup_bars"]
        entry_interval_seconds = TIMEFRAME_TO_INTERVAL[entry_tf] * 60
        replay_limit = int(warmup_bars[entry_tf]) + 128

        candle_batches: dict[str, list[dict[str, Any]]] = {}
        open_times: dict[str, list[int]] = {}
        for timeframe in _configured_timeframe_keys(self.config):
            limit = int(warmup_bars[timeframe]) + 128
            if timeframe == entry_tf:
                limit = replay_limit
            candle_batches[timeframe] = get_recent_candles(symbol, timeframe, limit, db_path=self.db_path)
            open_times[timeframe] = [int(candle["open_time"]) for candle in candle_batches[timeframe]]

        entry_candles = [
            candle
            for candle in candle_batches.get(entry_tf, [])
            if int(candle["open_time"]) <= int(up_to_open_time)
            and (after_open_time is None or int(candle["open_time"]) > int(after_open_time))
        ]
        if not entry_candles:
            return 0

        replayed = 0
        try:
            for entry_candle in entry_candles:
                entry_open_time = int(entry_candle["open_time"])
                windows: dict[str, list[dict[str, Any]]] = {}
                for timeframe, candles in candle_batches.items():
                    idx = bisect.bisect_right(open_times[timeframe], entry_open_time)
                    windows[timeframe] = candles[:idx]
                if not all(windows.values()):
                    continue
                await self._analyze_candles(
                    symbol,
                    windows,
                    emitted_at=entry_open_time + entry_interval_seconds,
                    mode_override=self.config["bot"]["environment"],
                    analysis_context=context_id,
                )
                replayed += 1
            return replayed
        finally:
            self._clear_indicator_context(context_id)

    def _reconcile_signal_lifecycle(
        self,
        *,
        symbol: str,
        regime: str,
        candles_by_tf: dict[str, list[dict[str, Any]]],
        current_time: int,
    ) -> dict[str, int | bool]:
        entry_tf = str(self.config["timeframes"]["entry"])
        entry_candles = candles_by_tf[entry_tf]
        open_signals = get_active_signals(symbol=symbol, db_path=self.db_path)
        updated_count = 0
        resolved_updates = 0
        for signal in open_signals:
            updated = _evaluate_signal_lifecycle(
                signal,
                entry_candles=entry_candles,
                current_regime=regime,
                current_time=current_time,
                config=self.config,
            )
            if updated is not None:
                upsert_signal(updated, db_path=self.db_path)
                updated_count += 1
                if str(updated.get("status", "")) in _PROBABILITY_REFRESH_STATUSES:
                    resolved_updates += 1
        return {
            "updated": updated_count,
            "resolved": resolved_updates,
            "refresh_probability": resolved_updates > 0,
        }


def _build_signal(
    symbol: str,
    regime: str,
    setup: dict[str, Any],
    scored: dict[str, Any],
    probability: dict[str, Any],
    emitted_at: int,
    runtime_mode: str,
    indicators_by_tf: dict[str, dict[str, Any]],
    candles_by_tf: dict[str, list[dict[str, Any]]],
    config: dict[str, Any],
) -> dict[str, Any]:
    trend_tf = str(config["timeframes"]["trend"])
    setup_tf = str(config["timeframes"]["setup"])
    entry_tf = str(config["timeframes"]["entry"])
    trend_indicators = indicators_by_tf[trend_tf]
    setup_indicators = indicators_by_tf[setup_tf]
    entry_indicators = indicators_by_tf[entry_tf]
    entry_interval_seconds = TIMEFRAME_TO_INTERVAL[entry_tf] * 60
    signal_open_time = int(candles_by_tf[entry_tf][-1]["open_time"])
    signal_close_time = signal_open_time + entry_interval_seconds
    tracking_start_at = signal_close_time
    entry_low = float(scored["entry_low"])
    entry_high = float(scored["entry_high"])
    entry_mid = (entry_low + entry_high) / 2.0
    execution_policy = str(setup["features"].get("execution_policy", "market_on_close"))
    immediate_execution = execution_policy == "market_on_close"
    enriched_features = {
        **setup["features"],
        "trend_structure": str(trend_indicators.get("structure", "range")),
        "setup_structure": str(setup_indicators.get("structure", "range")),
        "entry_structure": str(entry_indicators.get("structure", "range")),
        "entry_rsi": float(entry_indicators.get("rsi", 50.0)),
        "entry_macd_cross": str(entry_indicators.get("macd_cross", "none")),
        "entry_macd_histogram": float(entry_indicators.get("macd_histogram", 0.0)),
        "entry_volume_ratio": float(entry_indicators.get("volume_ratio", setup["features"].get("volume_ratio", 1.0))),
        "entry_bullish_divergence": bool(entry_indicators.get("bullish_divergence", False)),
        "entry_bearish_divergence": bool(entry_indicators.get("bearish_divergence", False)),
        "entry_rsi_rebound_long": bool(entry_indicators.get("rsi_rebound_long", False)),
        "entry_rsi_rebound_short": bool(entry_indicators.get("rsi_rebound_short", False)),
        "score_breakdown": dict(scored["breakdown"]),
        "score_decision": str(scored["decision"]),
        "probability_mode": probability["mode"],
        "strict_live_ready": bool(probability.get("strict_live_ready", False)),
        "segment_live_eligible": bool(probability.get("segment_live_eligible", False)),
        "confirmation_wick_ratio": float(entry_indicators.get("wick_ratio", 0.0)),
        "lifecycle_start_at": tracking_start_at,
        "signal_open_time": signal_open_time,
        "signal_close_time": signal_close_time,
        "entry_mid": entry_mid,
        "entry_interval_seconds": entry_interval_seconds,
        "execution_policy": execution_policy,
        "entry_touched": immediate_execution,
        "executed": immediate_execution,
        "entered_at": signal_close_time if immediate_execution else None,
        "entry_assumed_price": entry_mid if immediate_execution else None,
    }
    payload = {
        "id": _signal_id(symbol, setup["type"], setup["direction"], signal_close_time),
        "symbol": symbol,
        "direction": setup["direction"],
        "setup_type": setup["type"],
        "regime": regime,
        "score": scored["score"],
        "probability": probability["probability"],
        "entry_low": scored["entry_low"],
        "entry_high": scored["entry_high"],
        "stop": scored["stop"],
        "target1": scored["target1"],
        "target2": scored["target2"],
        "rr": scored["rr"],
        "validity_hours": scored["validity_hours"],
        "invalidation_rule": scored["invalidation_rule"],
        "emitted_at": signal_close_time,
        "expires_at": signal_close_time + int(scored["validity_hours"] * 3600),
        "mode": runtime_mode,
        "delivery_mode": str(probability["mode"]),
        "features": enriched_features,
        "data_complete": True,
        "late_entry": False,
        "confirmation_wick_ratio": float(entry_indicators.get("wick_ratio", 0.0)),
    }
    return payload


def _signal_id(symbol: str, setup_type: str, direction: str, emitted_at: int) -> str:
    raw = f"{symbol}:{setup_type}:{direction}:{emitted_at}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:16]


def _configured_timeframe_keys(config: dict[str, Any]) -> list[str]:
    timeframes = [
        str(config["timeframes"]["trend"]),
        str(config["timeframes"]["setup"]),
        str(config["timeframes"]["entry"]),
    ]
    return list(dict.fromkeys(timeframes))


def _technical_bias_from_regime(regime_details: Mapping[str, Any], entry_indicators: Mapping[str, Any]) -> str:
    regime = str(regime_details.get("regime", "unknown"))
    if regime == "bull_trend":
        return "long"
    if regime == "bear_trend":
        return "short"
    trend_structure = str(regime_details.get("trend_structure", "neutral"))
    setup_structure = str(regime_details.get("setup_structure", "neutral"))
    entry_structure = str(entry_indicators.get("structure", "neutral"))
    bullish_votes = sum(value == "bullish" for value in (trend_structure, setup_structure, entry_structure))
    bearish_votes = sum(value == "bearish" for value in (trend_structure, setup_structure, entry_structure))
    if bullish_votes >= 2 and bullish_votes > bearish_votes:
        return "long"
    if bearish_votes >= 2 and bearish_votes > bullish_votes:
        return "short"
    return "neutral"


def _technical_confirmation_probability(
    regime_details: Mapping[str, Any],
    entry_indicators: Mapping[str, Any],
    bias: str,
) -> int:
    regime = str(regime_details.get("regime", "unknown"))
    if regime == "bull_trend":
        score = 62.0
    elif regime == "bear_trend":
        score = 62.0
    elif regime == "range":
        score = 52.0
    else:
        score = 42.0

    trend_structure = str(regime_details.get("trend_structure", "neutral"))
    setup_structure = str(regime_details.get("setup_structure", "neutral"))
    entry_structure = str(entry_indicators.get("structure", "neutral"))
    target_structure = "bullish" if bias == "long" else "bearish" if bias == "short" else "neutral"
    for weight, structure in ((8.0, trend_structure), (6.0, setup_structure), (4.0, entry_structure)):
        if target_structure == "neutral":
            continue
        if structure == target_structure:
            score += weight
        elif structure in {"bullish", "bearish"}:
            score -= weight

    macd_cross = str(entry_indicators.get("macd_cross", "none"))
    if bias == "long":
        if macd_cross == "bullish":
            score += 5.0
        elif macd_cross == "bearish":
            score -= 5.0
    elif bias == "short":
        if macd_cross == "bearish":
            score += 5.0
        elif macd_cross == "bullish":
            score -= 5.0

    volume_ratio = float(entry_indicators.get("volume_ratio", 1.0))
    if volume_ratio >= 1.2:
        score += 3.0
    elif volume_ratio <= 0.8:
        score -= 3.0

    adx = float(regime_details.get("trend_adx", 0.0))
    if adx >= 25.0:
        score += 3.0
    elif adx < 18.0:
        score -= 3.0

    rsi = float(entry_indicators.get("rsi", 50.0))
    if bias == "long":
        if 48.0 <= rsi <= 68.0:
            score += 4.0
        elif rsi < 35.0:
            score -= 6.0
        elif rsi > 75.0:
            score -= 4.0
    elif bias == "short":
        if 32.0 <= rsi <= 52.0:
            score += 4.0
        elif rsi > 65.0:
            score -= 6.0
        elif rsi < 25.0:
            score -= 4.0

    if regime in REGIMES_INTERDITS_ALERTE:
        score -= 8.0

    return int(round(max(35.0, min(90.0, score)) / 5.0) * 5)


def _technical_confirmation_summary(
    *,
    regime_details: Mapping[str, Any],
    entry_indicators: Mapping[str, Any],
    bias: str,
) -> str:
    bias_label = {"long": "haussier", "short": "baissier", "neutral": "neutre"}.get(bias, "neutre")
    trend_structure = str(regime_details.get("trend_structure", "neutral"))
    setup_structure = str(regime_details.get("setup_structure", "neutral"))
    rsi = round(float(entry_indicators.get("rsi", 50.0)), 1)
    macd_cross = str(entry_indicators.get("macd_cross", "none"))
    volume_ratio = round(float(entry_indicators.get("volume_ratio", 1.0)), 2)
    return (
        f"biais {bias_label}, structure 4H {trend_structure}, structure 1H {setup_structure}, "
        f"RSI 15M {rsi}, MACD {macd_cross}, volume x{volume_ratio}"
    )


def _analysis_emitted_at(
    candles_by_tf: dict[str, list[dict[str, Any]]],
    config: dict[str, Any],
    emitted_at: int | None,
) -> int:
    _ = emitted_at
    entry_tf = str(config["timeframes"]["entry"])
    entry_interval_seconds = TIMEFRAME_TO_INTERVAL[entry_tf] * 60
    return int(candles_by_tf[entry_tf][-1]["open_time"]) + entry_interval_seconds


def _history_coverage_snapshot(
    *,
    symbol: str,
    timeframe: str,
    lookback_days: int,
    db_path: str | Path,
) -> dict[str, Any]:
    target_start = int(datetime.now(timezone.utc).timestamp()) - (int(lookback_days) * 86_400)
    oldest = get_oldest_candle(symbol, timeframe, db_path=db_path)
    if oldest is None:
        return {
            "coverage_start": None,
            "coverage_days": 0.0,
            "lookback_target_met": False,
            "archive_preserved": False,
            "source_limited": True,
            "target_start": target_start,
        }
    coverage_start = int(oldest["open_time"])
    coverage_days = round(max(0, int(datetime.now(timezone.utc).timestamp()) - coverage_start) / 86_400.0, 2)
    target_met = coverage_start <= target_start
    return {
        "coverage_start": coverage_start,
        "coverage_days": coverage_days,
        "lookback_target_met": target_met,
        "archive_preserved": True,
        "source_limited": not target_met,
        "target_start": target_start,
    }


def _summarize_history_coverage(symbols_summary: dict[str, dict[str, Any]]) -> dict[str, Any]:
    partial_targets: list[str] = []
    for symbol, timeframe_summary in symbols_summary.items():
        for timeframe, details in timeframe_summary.items():
            if not bool(details.get("lookback_target_met", False)):
                partial_targets.append(f"{symbol} {timeframe}")
    return {
        "all_targets_met": len(partial_targets) == 0,
        "partial_targets": partial_targets,
    }


def _indicator_bundle_requires_rebuild(
    bundle: dict[str, Any],
    candles: list[dict[str, Any]],
    timeframe: str,
) -> bool:
    if not candles:
        return True
    latest_open_time = int(candles[-1]["open_time"])
    last_open_time = int(bundle.get("last_open_time", 0) or 0)
    if latest_open_time < last_open_time:
        return True
    if latest_open_time == last_open_time:
        return False
    missing = [candle for candle in candles if int(candle["open_time"]) > last_open_time]
    if not missing:
        return True
    expected_next = last_open_time + (TIMEFRAME_TO_INTERVAL[timeframe] * 60)
    return int(missing[0]["open_time"]) != expected_next


def _build_indicator_bundle(candles: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    state = _new_indicator_runtime_state(config)
    for candle in candles:
        _update_indicator_runtime_state(state, candle)
    snapshot = _snapshot_from_indicator_runtime_state(state, config)
    return {
        "last_open_time": int(candles[-1]["open_time"]),
        "state": state,
        "snapshot": snapshot,
    }


def _advance_indicator_bundle(
    bundle: dict[str, Any],
    candles: list[dict[str, Any]],
    timeframe: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    state = bundle["state"]
    last_open_time = int(bundle["last_open_time"])
    missing = [candle for candle in candles if int(candle["open_time"]) > last_open_time]
    if not missing:
        return bundle
    expected_next = last_open_time + (TIMEFRAME_TO_INTERVAL[timeframe] * 60)
    if int(missing[0]["open_time"]) != expected_next:
        return _build_indicator_bundle(candles, config)
    for candle in missing:
        _update_indicator_runtime_state(state, candle)
    snapshot = _snapshot_from_indicator_runtime_state(state, config)
    return {
        "last_open_time": int(candles[-1]["open_time"]),
        "state": state,
        "snapshot": snapshot,
    }


def _indicator_state_record(symbol: str, timeframe: str, bundle: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "last_open_time": int(bundle["last_open_time"]),
        "state_json": dumps(_serialize_indicator_runtime_state(bundle["state"])),
        "snapshot_json": dumps(bundle["snapshot"]),
    }


def _load_indicator_bundle(row: Mapping[str, Any]) -> dict[str, Any] | None:
    try:
        state_payload = loads(str(row["state_json"]))
        snapshot_payload = loads(str(row["snapshot_json"]))
    except Exception:
        return None
    if not isinstance(state_payload, Mapping) or not isinstance(snapshot_payload, Mapping):
        return None
    return {
        "last_open_time": int(row["last_open_time"]),
        "state": _hydrate_indicator_runtime_state(state_payload),
        "snapshot": dict(snapshot_payload),
    }


def _new_indicator_runtime_state(config: dict[str, Any]) -> dict[str, Any]:
    indicators_cfg = config["data"]["indicators"]
    return {
        "history_limit": _indicator_history_limit(config),
        "candles": [],
        "ema20": EMACalculator(int(indicators_cfg["ema_fast"])),
        "ema50": EMACalculator(int(indicators_cfg["ema_mid"])),
        "ema200": EMACalculator(int(indicators_cfg["ema_slow"])),
        "atr": ATRCalculator(int(indicators_cfg["atr_period"])),
        "rsi": RSICalculator(int(indicators_cfg["rsi_period"])),
        "adx": ADXCalculator(int(indicators_cfg["adx_period"])),
        "macd": {
            "fast": int(indicators_cfg.get("macd_fast", 12)),
            "slow": int(indicators_cfg.get("macd_slow", 26)),
            "signal": int(indicators_cfg.get("macd_signal", 9)),
            "fast_ema": None,
            "slow_ema": None,
            "signal_ema": None,
            "macd": 0.0,
            "signal_value": 0.0,
            "histogram": 0.0,
            "cross": "none",
            "histogram_contracting": False,
        },
    }


def _update_indicator_runtime_state(state: dict[str, Any], candle: Mapping[str, Any]) -> None:
    compact = _compact_candle(candle)
    close = float(compact["close"])
    high = float(compact["high"])
    low = float(compact["low"])
    state["ema20"].update(close)
    state["ema50"].update(close)
    state["ema200"].update(close)
    state["atr"].update(high, low, close)
    state["rsi"].update(close)
    state["adx"].update(high, low, close)
    _update_macd_state(state["macd"], close)
    candles = state["candles"]
    candles.append(compact)
    while len(candles) > int(state["history_limit"]):
        candles.pop(0)


def _snapshot_from_indicator_runtime_state(state: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    candles = list(state["candles"])
    snapshot = _compute_indicator_snapshot(candles, config)
    macd_state = state["macd"]
    snapshot.update(
        {
            "ema20": float(state["ema20"].value or snapshot.get("ema20", 0.0)),
            "ema50": float(state["ema50"].value or snapshot.get("ema50", 0.0)),
            "ema200": float(state["ema200"].value or snapshot.get("ema200", 0.0)),
            "atr": float(state["atr"].value or snapshot.get("atr", 0.0)),
            "rsi": float(state["rsi"].value),
            "adx": float(state["adx"].adx),
            "plus_di": float(state["adx"].plus_di),
            "minus_di": float(state["adx"].minus_di),
            "macd": float(macd_state["macd"]),
            "macd_signal": float(macd_state["signal_value"]),
            "macd_histogram": float(macd_state["histogram"]),
            "macd_cross": str(macd_state["cross"]),
            "macd_histogram_contracting": bool(macd_state["histogram_contracting"]),
        }
    )
    return snapshot


def _serialize_indicator_runtime_state(state: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "history_limit": int(state["history_limit"]),
        "candles": [dict(candle) for candle in state["candles"]],
        "ema20": asdict(state["ema20"]),
        "ema50": asdict(state["ema50"]),
        "ema200": asdict(state["ema200"]),
        "atr": asdict(state["atr"]),
        "rsi": asdict(state["rsi"]),
        "adx": asdict(state["adx"]),
        "macd": dict(state["macd"]),
    }


def _hydrate_indicator_runtime_state(payload: Mapping[str, Any]) -> dict[str, Any]:
    state = {
        "history_limit": int(payload.get("history_limit", 240)),
        "candles": [_compact_candle(candle) for candle in payload.get("candles", [])],
        "ema20": _restore_dataclass_state(EMACalculator, payload.get("ema20", {"period": 20})),
        "ema50": _restore_dataclass_state(EMACalculator, payload.get("ema50", {"period": 50})),
        "ema200": _restore_dataclass_state(EMACalculator, payload.get("ema200", {"period": 200})),
        "atr": _restore_dataclass_state(ATRCalculator, payload.get("atr", {"period": 14})),
        "rsi": _restore_dataclass_state(RSICalculator, payload.get("rsi", {"period": 14})),
        "adx": _restore_dataclass_state(ADXCalculator, payload.get("adx", {"period": 14})),
        "macd": dict(payload.get("macd", {})),
    }
    if "signal_value" not in state["macd"]:
        state["macd"]["signal_value"] = float(state["macd"].get("signal", 0.0) or 0.0)
    return state


def _restore_dataclass_state(factory: type[Any], payload: Any) -> Any:
    data = dict(payload) if isinstance(payload, Mapping) else {}
    period = int(data.get("period", 1))
    instance = factory(period)
    for key, value in data.items():
        if hasattr(instance, key):
            setattr(instance, key, value)
    return instance


def _update_macd_state(state: dict[str, Any], close: float) -> None:
    fast_period = max(1, int(state.get("fast", 12)))
    slow_period = max(1, int(state.get("slow", 26)))
    signal_period = max(1, int(state.get("signal", 9)))
    fast_k = 2.0 / (fast_period + 1.0)
    slow_k = 2.0 / (slow_period + 1.0)
    signal_k = 2.0 / (signal_period + 1.0)

    prev_macd = float(state.get("macd", 0.0))
    prev_signal = float(state.get("signal_value", 0.0))
    prev_histogram = float(state.get("histogram", 0.0))
    fast_ema = float(state["fast_ema"]) if state.get("fast_ema") is not None else float(close)
    slow_ema = float(state["slow_ema"]) if state.get("slow_ema") is not None else float(close)
    fast_ema = (float(close) * fast_k) + (fast_ema * (1.0 - fast_k))
    slow_ema = (float(close) * slow_k) + (slow_ema * (1.0 - slow_k))
    macd_line = fast_ema - slow_ema
    signal_ema = float(state["signal_ema"]) if state.get("signal_ema") is not None else macd_line
    signal_ema = (macd_line * signal_k) + (signal_ema * (1.0 - signal_k))
    histogram = macd_line - signal_ema

    cross = "none"
    if prev_macd <= prev_signal and macd_line > signal_ema:
        cross = "bullish"
    elif prev_macd >= prev_signal and macd_line < signal_ema:
        cross = "bearish"

    state.update(
        {
            "fast_ema": fast_ema,
            "slow_ema": slow_ema,
            "signal_ema": signal_ema,
            "macd": macd_line,
            "signal_value": signal_ema,
            "histogram": histogram,
            "cross": cross,
            "histogram_contracting": abs(histogram) < abs(prev_histogram),
        }
    )


def _indicator_history_limit(config: dict[str, Any]) -> int:
    indicators_cfg = config["data"]["indicators"]
    fib_lookback = int(indicators_cfg.get("fib_lookback_bars", 120))
    volume_period = int(indicators_cfg.get("volume_ma_period", 20))
    return max(240, fib_lookback + 24, volume_period * 3)


def _compact_candle(candle: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "open_time": int(candle["open_time"]),
        "open": float(candle["open"]),
        "high": float(candle["high"]),
        "low": float(candle["low"]),
        "close": float(candle["close"]),
        "volume": float(candle["volume"]),
        "closed": int(candle.get("closed", 1)),
    }


def _compute_indicator_snapshot(candles: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    indicators_cfg = config["data"]["indicators"]
    closes = np.asarray([float(candle["close"]) for candle in candles], dtype=float)
    highs = np.asarray([float(candle["high"]) for candle in candles], dtype=float)
    lows = np.asarray([float(candle["low"]) for candle in candles], dtype=float)
    volumes = np.asarray([float(candle["volume"]) for candle in candles], dtype=float)

    ema20 = EMACalculator(int(indicators_cfg["ema_fast"])).warmup(closes)
    ema50 = EMACalculator(int(indicators_cfg["ema_mid"])).warmup(closes)
    ema200 = EMACalculator(int(indicators_cfg["ema_slow"])).warmup(closes)
    atr = ATRCalculator(int(indicators_cfg["atr_period"])).warmup(highs, lows, closes)
    rsi = RSICalculator(int(indicators_cfg["rsi_period"])).warmup(closes)
    adx_snapshot = ADXCalculator(int(indicators_cfg["adx_period"])).warmup(highs, lows, closes)
    macd_snapshot = _macd_snapshot(closes, indicators_cfg)
    rsi_series = _rsi_series(closes, int(indicators_cfg["rsi_period"]))

    reference_highs = highs[:-1] if highs.size > 1 else highs
    reference_lows = lows[:-1] if lows.size > 1 else lows
    recent_window = max(1, min(12, len(reference_highs)))
    recent_high = float(np.max(reference_highs[-recent_window:]))
    recent_low = float(np.min(reference_lows[-recent_window:]))
    recent_range = recent_high - recent_low
    midpoint = recent_low + (recent_range / 2.0)
    latest = candles[-1]
    latest_high = float(latest["high"])
    latest_low = float(latest["low"])
    latest_close = float(latest["close"])

    swing_highs = detect_swing_highs(highs, window=1)
    swing_lows = detect_swing_lows(lows, window=1)
    reference_swing_highs = detect_swing_highs(reference_highs, window=1)
    reference_swing_lows = detect_swing_lows(reference_lows, window=1)
    structure = _derive_structure(closes, highs, lows, swing_window=int(indicators_cfg.get("swing_window", 3)))
    wick_ratio = _wick_ratio(latest)
    structure_break, reversal_direction = _detect_structure_break(reference_highs, reference_lows, closes)
    divergence = _detect_rsi_divergence(reference_highs, reference_lows, rsi_series[:-1] if rsi_series.size > 1 else rsi_series)
    fib_levels = _fibonacci_snapshot(highs, lows, int(indicators_cfg.get("fib_lookback_bars", 120)))
    oversold = float(indicators_cfg.get("rsi_oversold", 30.0))
    overbought = float(indicators_cfg.get("rsi_overbought", 70.0))
    previous_rsi = float(rsi_series[-2]) if rsi_series.size > 1 else float(rsi)
    rsi_rebound_long = previous_rsi <= oversold and float(rsi) > previous_rsi
    rsi_rebound_short = previous_rsi >= overbought and float(rsi) < previous_rsi

    extension_atr = 0.0
    if atr > 0.0:
        upside_extension = max(0.0, latest_high - recent_high)
        downside_extension = max(0.0, recent_low - latest_low)
        extension_atr = max(upside_extension, downside_extension) / atr

    recent_volumes = volumes[-20:] if volumes.size >= 20 else volumes
    baseline_volumes = volumes[-40:-20] if volumes.size >= 40 else recent_volumes
    mean_volume_ratio = float(np.mean(recent_volumes) / max(float(np.mean(baseline_volumes)), 1e-9))
    last_three_wicks = [_wick_ratio(candle) for candle in candles[-3:]]
    repeated_wick_ratio = max(last_three_wicks) if sum(value > 0.60 for value in last_three_wicks) >= 2 else 0.0
    swing_high_values = sorted(
        float(reference_highs[index]) for index in reference_swing_highs if float(reference_highs[index]) > latest_close
    )
    swing_low_values = sorted(
        (float(reference_lows[index]) for index in reference_swing_lows if float(reference_lows[index]) < latest_close),
        reverse=True,
    )
    bullish_clear_space_atr = 99.0
    bearish_clear_space_atr = 99.0
    if atr > 0.0:
        if swing_high_values:
            bullish_clear_space_atr = max(0.0, (swing_high_values[0] - latest_close) / atr)
        if swing_low_values:
            bearish_clear_space_atr = max(0.0, (latest_close - swing_low_values[0]) / atr)

    return {
        "close": float(closes[-1]),
        "ema20": float(ema20),
        "ema50": float(ema50),
        "ema200": float(ema200),
        "atr": float(atr),
        "rsi": float(rsi),
        "adx": float(adx_snapshot["adx"]),
        "plus_di": float(adx_snapshot["plus_di"]),
        "minus_di": float(adx_snapshot["minus_di"]),
        "macd": float(macd_snapshot["macd"]),
        "macd_signal": float(macd_snapshot["signal"]),
        "macd_histogram": float(macd_snapshot["histogram"]),
        "macd_cross": macd_snapshot["cross"],
        "macd_histogram_contracting": bool(macd_snapshot["histogram_contracting"]),
        "volume_ratio": float(compute_volume_ratio(candles, period=int(indicators_cfg["volume_ma_period"]))),
        "mean_volume_ratio": mean_volume_ratio,
        "structure": structure,
        "range_high": recent_high,
        "range_low": recent_low,
        "range_atr_ratio": (recent_range / atr) if atr > 0 else 0.0,
        "near_midpoint": abs(closes[-1] - midpoint) <= max(recent_range * 0.25, 0.0),
        "clear_stop": bool(swing_highs or swing_lows),
        "major_level_touch": atr > 0.0 and (
            latest_high >= recent_high - (atr * 0.25) or latest_low <= recent_low + (atr * 0.25)
        ),
        "extension_atr": extension_atr,
        "wick_ratio": wick_ratio,
        "repeated_wick_ratio": repeated_wick_ratio,
        "clear_space_atr": max(bullish_clear_space_atr, bearish_clear_space_atr),
        "bullish_clear_space_atr": bullish_clear_space_atr,
        "bearish_clear_space_atr": bearish_clear_space_atr,
        "structure_break": structure_break,
        "reversal_direction": reversal_direction,
        "bullish_divergence": bool(divergence["bullish"]),
        "bearish_divergence": bool(divergence["bearish"]),
        "rsi_rebound_long": bool(rsi_rebound_long),
        "rsi_rebound_short": bool(rsi_rebound_short),
        "fib_levels": fib_levels,
    }


def _derive_structure(closes: np.ndarray, highs: np.ndarray, lows: np.ndarray, *, swing_window: int = 3) -> str:
    if closes.size < 5:
        return "range"
    pivot_window = max(1, int(swing_window))
    swing_highs = detect_swing_highs(highs, window=pivot_window)
    swing_lows = detect_swing_lows(lows, window=pivot_window)
    if len(swing_highs) >= 2 and len(swing_lows) >= 2:
        last_high = float(highs[swing_highs[-1]])
        prev_high = float(highs[swing_highs[-2]])
        last_low = float(lows[swing_lows[-1]])
        prev_low = float(lows[swing_lows[-2]])
        if last_high > prev_high and last_low > prev_low:
            return "bullish"
        if last_high < prev_high and last_low < prev_low:
            return "bearish"
        return "range"

    recent_closes = closes[-5:]
    if np.all(np.diff(recent_closes) > 0):
        return "bullish"
    if np.all(np.diff(recent_closes) < 0):
        return "bearish"
    return "range"


def _detect_structure_break(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray) -> tuple[bool, str]:
    if closes.size < 2:
        return False, "long"
    swing_highs = detect_swing_highs(highs, window=1) if highs.size >= 3 else []
    swing_lows = detect_swing_lows(lows, window=1) if lows.size >= 3 else []
    last_close = float(closes[-1])
    if swing_highs and last_close > float(highs[swing_highs[-1]]):
        return True, "long"
    if swing_lows and last_close < float(lows[swing_lows[-1]]):
        return True, "short"
    return False, "long"


def _ema_series(values: np.ndarray, period: int) -> np.ndarray:
    if values.size == 0:
        return np.asarray([], dtype=float)
    series = np.asarray(values, dtype=float).copy()
    k = 2.0 / (float(period) + 1.0)
    ema = series[0]
    output = np.empty_like(series)
    output[0] = ema
    for index in range(1, series.size):
        ema = (series[index] * k) + (ema * (1.0 - k))
        output[index] = ema
    return output


def _macd_snapshot(closes: np.ndarray, indicators_cfg: dict[str, Any]) -> dict[str, Any]:
    fast = int(indicators_cfg.get("macd_fast", 12))
    slow = int(indicators_cfg.get("macd_slow", 26))
    signal = int(indicators_cfg.get("macd_signal", 9))
    fast_ema = _ema_series(closes, fast)
    slow_ema = _ema_series(closes, slow)
    macd_line = fast_ema - slow_ema
    signal_line = _ema_series(macd_line, signal)
    histogram = macd_line - signal_line

    cross = "none"
    contracting = False
    if histogram.size >= 2:
        prev_macd = float(macd_line[-2])
        prev_signal = float(signal_line[-2])
        curr_macd = float(macd_line[-1])
        curr_signal = float(signal_line[-1])
        if prev_macd <= prev_signal and curr_macd > curr_signal:
            cross = "bullish"
        elif prev_macd >= prev_signal and curr_macd < curr_signal:
            cross = "bearish"
        contracting = abs(float(histogram[-1])) < abs(float(histogram[-2]))

    return {
        "macd": float(macd_line[-1]),
        "signal": float(signal_line[-1]),
        "histogram": float(histogram[-1]),
        "cross": cross,
        "histogram_contracting": contracting,
    }


def _rsi_series(closes: np.ndarray, period: int) -> np.ndarray:
    if closes.size == 0:
        return np.asarray([], dtype=float)
    output = np.full(closes.shape, 50.0, dtype=float)
    if closes.size == 1:
        return output

    deltas = np.diff(closes)
    gains = np.maximum(deltas, 0.0)
    losses = np.maximum(-deltas, 0.0)
    avg_gain = float(np.mean(gains[:period])) if gains.size >= period else float(np.mean(gains)) if gains.size else 0.0
    avg_loss = float(np.mean(losses[:period])) if losses.size >= period else float(np.mean(losses)) if losses.size else 0.0

    start_index = min(period, deltas.size)
    for index in range(start_index, deltas.size):
        avg_gain = ((avg_gain * (period - 1)) + gains[index]) / float(period)
        avg_loss = ((avg_loss * (period - 1)) + losses[index]) / float(period)
        rs = avg_gain / avg_loss if avg_loss > 0 else np.inf
        output[index + 1] = 100.0 - (100.0 / (1.0 + rs))

    if start_index > 0:
        rs = avg_gain / avg_loss if avg_loss > 0 else np.inf
        output[start_index] = 100.0 - (100.0 / (1.0 + rs))

    output[np.isnan(output)] = 50.0
    return output


def _detect_rsi_divergence(highs: np.ndarray, lows: np.ndarray, rsi_series: np.ndarray) -> dict[str, bool]:
    bullish = False
    bearish = False

    swing_lows = detect_swing_lows(lows, window=1) if lows.size >= 3 else []
    if len(swing_lows) >= 2:
        last_low = swing_lows[-1]
        prev_low = swing_lows[-2]
        bullish = float(lows[last_low]) < float(lows[prev_low]) and float(rsi_series[last_low]) > float(rsi_series[prev_low])

    swing_highs = detect_swing_highs(highs, window=1) if highs.size >= 3 else []
    if len(swing_highs) >= 2:
        last_high = swing_highs[-1]
        prev_high = swing_highs[-2]
        bearish = float(highs[last_high]) > float(highs[prev_high]) and float(rsi_series[last_high]) < float(rsi_series[prev_high])

    return {"bullish": bullish, "bearish": bearish}


def _fibonacci_snapshot(highs: np.ndarray, lows: np.ndarray, lookback_bars: int) -> dict[str, float] | None:
    if highs.size < 3 or lows.size < 3:
        return None
    lookback = min(int(lookback_bars), highs.size)
    local_highs = highs[-lookback:]
    local_lows = lows[-lookback:]
    high_index = int(np.argmax(local_highs))
    low_index = int(np.argmin(local_lows))
    high_value = float(local_highs[high_index])
    low_value = float(local_lows[low_index])
    price_range = high_value - low_value
    if price_range <= 0.0:
        return None

    if low_index < high_index:
        return {
            "direction": "bullish",
            "0.382": high_value - (price_range * 0.382),
            "0.5": high_value - (price_range * 0.5),
            "0.618": high_value - (price_range * 0.618),
            "high": high_value,
            "low": low_value,
        }
    return {
        "direction": "bearish",
        "0.382": low_value + (price_range * 0.382),
        "0.5": low_value + (price_range * 0.5),
        "0.618": low_value + (price_range * 0.618),
        "high": high_value,
        "low": low_value,
    }


def _wick_ratio(candle: dict[str, Any]) -> float:
    high = float(candle["high"])
    low = float(candle["low"])
    open_ = float(candle.get("open", candle["close"]))
    close = float(candle["close"])
    candle_range = max(high - low, 1e-9)
    upper = high - max(open_, close)
    lower = min(open_, close) - low
    return max(upper, lower) / candle_range


def _coerce_timestamp(value: Any) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(datetime.strptime(value, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            return _utc_now_ts()
    return _utc_now_ts()


def _utc_now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def _recalibration_interval_seconds(frequency: Any) -> int:
    normalized = str(frequency).strip().lower()
    mapping = {
        "hourly": 3600,
        "daily": 86400,
        "weekly": 604800,
    }
    if normalized in mapping:
        return mapping[normalized]
    LOGGER.warning("unknown recalibration frequency %s, defaulting to weekly", frequency)
    return mapping["weekly"]


def _evaluate_signal_lifecycle(
    signal: dict[str, Any],
    *,
    entry_candles: list[dict[str, Any]],
    current_regime: str,
    current_time: int,
    config: dict[str, Any],
) -> dict[str, Any] | None:
    if not entry_candles:
        return None

    features = _decode_signal_features(signal)
    updated = dict(signal)
    direction = str(signal["direction"])
    entry_low = float(signal["entry_low"])
    entry_high = float(signal["entry_high"])
    entry_mid = float(features.get("entry_mid", (entry_low + entry_high) / 2.0))
    stop = float(signal["stop"])
    target1 = float(signal["target1"])
    tracking_start_at = int(features.get("lifecycle_start_at", signal["emitted_at"]))
    interval_seconds = int(features.get("entry_interval_seconds", TIMEFRAME_TO_INTERVAL[str(config["timeframes"]["entry"])] * 60))
    relevant_candles = [candle for candle in entry_candles if int(candle["open_time"]) >= tracking_start_at]
    entry_touched = bool(features.get("entry_touched", False)) or str(signal.get("status", "")) in {"entered", "hit_t1", "hit_stop"}
    changed = False

    for candle in relevant_candles:
        candle_closed_at = int(candle["open_time"]) + interval_seconds
        if not entry_touched and _candle_overlaps_entry(candle, entry_low, entry_high):
            entry_touched = True
            changed = True
            updated["status"] = "entered"
            updated["comment"] = "entry touched"
            features["entry_touched"] = True
            features["executed"] = True
            features["entered_at"] = candle_closed_at
            features["entry_assumed_price"] = entry_mid

        if not entry_touched and _level_hit(direction, candle, target1):
            changed = True
            updated["status"] = "expired_without_entry"
            updated["closed_at"] = candle_closed_at
            updated["result_r"] = None
            updated["comment"] = "target1 reached before execution"
            features["entry_touched"] = False
            features["executed"] = False
            features["resolved_reason"] = "target1 before entry"
            return _finalize_lifecycle_update(updated, features)

        if not entry_touched:
            continue

        stop_hit = _level_hit(direction, candle, stop)
        target_hit = _level_hit(direction, candle, target1)

        if target_hit and stop_hit:
            changed = True
            updated["status"] = "hit_stop"
            updated["closed_at"] = candle_closed_at
            updated["result_r"] = -1.0
            updated["comment"] = "ambiguous candle resolved conservatively to stop"
            features["resolved_reason"] = "ambiguous_same_candle_stop"
            return _finalize_lifecycle_update(updated, features)
        if stop_hit:
            changed = True
            updated["status"] = "hit_stop"
            updated["closed_at"] = candle_closed_at
            updated["result_r"] = -1.0
            updated["comment"] = "stop hit"
            features["resolved_reason"] = "stop_hit"
            return _finalize_lifecycle_update(updated, features)
        if target_hit:
            changed = True
            updated["status"] = "hit_t1"
            updated["closed_at"] = candle_closed_at
            updated["result_r"] = float(config["risk"]["targets"]["t1_r"])
            updated["comment"] = "target1 hit"
            features["resolved_reason"] = "target1_hit"
            return _finalize_lifecycle_update(updated, features)

    last_close = float(entry_candles[-1]["close"])
    current_status = str(updated.get("status", signal.get("status", "active")))

    if entry_touched:
        features["entry_touched"] = True
        features["executed"] = True
        if current_regime != str(signal.get("regime", "")):
            changed = True
            updated["status"] = "cancelled_regime_change"
            updated["closed_at"] = current_time
            updated["result_r"] = _mark_to_market_r(last_close, entry_mid, stop, direction)
            updated["comment"] = "regime changed after entry"
            features["resolved_reason"] = "regime_changed_after_entry"
            return _finalize_lifecycle_update(updated, features)
        if current_time >= int(signal["expires_at"]):
            changed = True
            updated["status"] = "expired_after_entry"
            updated["closed_at"] = current_time
            updated["result_r"] = _mark_to_market_r(last_close, entry_mid, stop, direction)
            updated["comment"] = "validity expired after entry"
            features["resolved_reason"] = "expired_after_entry"
            return _finalize_lifecycle_update(updated, features)
        if current_status != "entered":
            changed = True
            updated["status"] = "entered"
            updated["comment"] = "entry touched"
    else:
        features["entry_touched"] = False
        features["executed"] = False
        if current_regime != str(signal.get("regime", "")):
            changed = True
            updated["status"] = "cancelled_regime_change"
            updated["closed_at"] = current_time
            updated["result_r"] = None
            updated["comment"] = "regime changed before entry"
            features["resolved_reason"] = "regime_changed_before_entry"
            return _finalize_lifecycle_update(updated, features)
        if current_time >= int(signal["expires_at"]):
            changed = True
            updated["status"] = "expired_without_entry"
            updated["closed_at"] = current_time
            updated["result_r"] = None
            updated["comment"] = "validity expired without entry"
            features["resolved_reason"] = "expired_without_entry"
            return _finalize_lifecycle_update(updated, features)

    if not changed:
        return None
    return _finalize_lifecycle_update(updated, features)


def _decode_signal_features(signal: Mapping[str, Any]) -> dict[str, Any]:
    raw = signal.get("features")
    if isinstance(raw, dict):
        return dict(raw)
    raw_json = signal.get("features_json")
    if not raw_json:
        return {}
    try:
        decoded = loads(raw_json)
    except Exception:
        return {}
    return dict(decoded) if isinstance(decoded, dict) else {}


def _finalize_lifecycle_update(signal: dict[str, Any], features: dict[str, Any]) -> dict[str, Any]:
    payload = dict(signal)
    payload["features_json"] = dumps(features)
    return payload


def _candle_overlaps_entry(candle: Mapping[str, Any], entry_low: float, entry_high: float) -> bool:
    low = float(candle["low"])
    high = float(candle["high"])
    return low <= entry_high and high >= entry_low


def _level_hit(direction: str, candle: Mapping[str, Any], level: float) -> bool:
    low = float(candle["low"])
    high = float(candle["high"])
    _ = direction
    return low <= level <= high


def _mark_to_market_r(price: float, entry_mid: float, stop: float, direction: str) -> float:
    risk = max(abs(entry_mid - stop), 1e-9)
    if direction == "long":
        return round((price - entry_mid) / risk, 4)
    return round((entry_mid - price) / risk, 4)


def _format_live_activation_message(status: dict[str, Any]) -> str:
    current = status["current"]
    required = status["required"]
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return "\n".join(
        [
            "BotYo",
            "Calibration live atteinte",
            "Le bot est eligible au mode live_alert.",
            "Activation manuelle requise via /admin.",
            f"Echantillons totaux : {current['total']}/{required['total']}",
            (
                "Meilleur setup/direction : "
                f"{current['best_setup_direction']['key']} "
                f"({current['best_setup_direction']['count']}/{required['setup_direction']})"
            ),
            (
                "Meilleur actif/setup/direction : "
                f"{current['best_asset_setup_direction']['key']} "
                f"({current['best_asset_setup_direction']['count']}/{required['asset_setup_direction']})"
            ),
            f"Verifie puis active live_alert manuellement. {timestamp}",
        ]
    )
