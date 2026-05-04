"""Tests for the probability engine."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from app.strategy.probability import ProbabilityEngine
from app.storage.db import init_db, upsert_candle, upsert_signal
from app.supervisor import BotYoSupervisor, _build_signal, _evaluate_signal_lifecycle
from app.utils.json import dumps, loads
from app.utils.logging import ROOT_DIR


def _config() -> dict:
    import yaml

    return yaml.safe_load((ROOT_DIR / "config" / "bot.yaml").read_text(encoding="utf-8"))


def _temp_config_path() -> Path:
    source = ROOT_DIR / "config" / "bot.yaml"
    temp_dir = Path(tempfile.mkdtemp())
    target = temp_dir / "bot.yaml"
    target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    return target


def _eligible_history() -> list[dict[str, object]]:
    history: list[dict[str, object]] = []
    emitted_at = 1_700_000_000
    for index in range(200):
        is_win = index % 4 != 0
        history.append(
            {
                "symbol": "BTCUSDT" if index < 120 else "ETHUSDT",
                "setup_type": "trend_continuation",
                "direction": "long",
                "score": 89 if is_win else 58,
                "status": "hit_t1" if is_win else "hit_stop",
                "emitted_at": emitted_at + (index * 3600),
                "result_r": 1.5 if is_win else -1.0,
                "features_json": dumps({"executed": True}),
            }
        )
    return history


def test_shadow_mode_if_insufficient_samples() -> None:
    engine = ProbabilityEngine(_config())
    result = engine.estimate(72.0, {"symbol": "BTCUSDT"}, "trend_continuation", "long")
    assert result["mode"] == "shadow"
    assert result["calibrated"] is False


def test_probability_between_zero_and_one() -> None:
    engine = ProbabilityEngine(_config())
    result = engine.estimate(83.0, {"symbol": "BTCUSDT"}, "breakout", "long")
    assert 0.0 <= result["probability"] <= 1.0


def test_live_activation_status_reports_remaining_thresholds() -> None:
    engine = ProbabilityEngine(_config())
    status = engine.get_live_activation_status()

    assert status["eligible"] is False
    assert status["remaining"]["total"] == 75
    assert status["remaining"]["setup_direction"] == 20
    assert status["remaining"]["asset_setup_direction"] == 10


def test_recalibration_on_synthetic_history() -> None:
    engine = ProbabilityEngine(_config())
    history = _eligible_history()
    engine.recalibrate(history)
    counts = engine.get_sample_counts()
    result = engine.estimate(82.0, {"symbol": "BTCUSDT"}, "trend_continuation", "long")
    live_status = engine.get_live_activation_status()

    assert counts["total"] == 200
    assert counts["by_setup_direction"]["trend_continuation:long"] == 200
    assert counts["by_asset_setup_direction"]["BTCUSDT:trend_continuation:long"] == 120
    assert 0.0 <= result["probability"] <= 1.0
    assert result["calibrated"] is True
    assert live_status["eligible"] is True
    assert live_status["recommended_segments"][0]["eligible"] is True


def test_walk_forward_uses_configurable_smaller_windows_for_low_frequency_bot() -> None:
    config = _config()
    config["probability"]["min_total_samples_for_live"] = 45
    config["probability"]["min_samples_per_setup_direction"] = 20
    config["probability"]["min_samples_per_asset_setup_direction"] = 10
    config["probability"]["live_requirements"]["walk_forward_min_records"] = 45
    config["probability"]["live_requirements"]["walk_forward_min_train_records"] = 30
    config["probability"]["live_requirements"]["walk_forward_min_test_records_per_fold"] = 5
    engine = ProbabilityEngine(config)

    engine.recalibrate(_eligible_history()[:45])
    walk_forward = engine.get_live_activation_status()["walk_forward"]

    assert walk_forward["ready"] is True
    assert walk_forward["folds"] >= 1


def test_recalibration_waits_for_minimum_samples_and_two_outcomes() -> None:
    engine = ProbabilityEngine(_config())
    engine.recalibrate(
        [
            {
                "symbol": "BTCUSDT",
                "setup_type": "range_rotation",
                "direction": "long",
                "score": 87,
                "status": "hit_stop",
                "emitted_at": 1_700_000_000,
                "result_r": -1.0,
                "features_json": dumps({"executed": True}),
            }
        ]
    )

    result = engine.estimate(83.0, {"symbol": "BTCUSDT"}, "range_rotation", "long")

    assert result["calibrated"] is False
    assert result["probability"] >= engine.config["probability"]["probability_threshold_shadow"]
    assert result["probability"] < 0.83


def test_pre_live_probability_blends_calibration_without_enabling_live() -> None:
    engine = ProbabilityEngine(_config())
    history: list[dict[str, object]] = []
    emitted_at = 1_700_000_000
    for index in range(30):
        history.append(
            {
                "symbol": "BTCUSDT",
                "setup_type": "range_rotation",
                "direction": "long",
                "score": 92 if index % 2 == 0 else 55,
                "status": "hit_stop" if index % 2 == 0 else "hit_t1",
                "emitted_at": emitted_at + (index * 900),
                "result_r": -1.0 if index % 2 == 0 else 1.5,
                "features_json": dumps({"executed": True}),
            }
        )

    engine.recalibrate(history)
    result = engine.estimate(90.0, {"symbol": "BTCUSDT"}, "range_rotation", "long")

    assert result["mode"] == "shadow"
    assert result["calibrated"] is True
    assert result["strict_live_ready"] is False
    assert 0.0 < result["calibration_weight"] < 1.0
    assert result["probability"] >= engine.config["probability"]["probability_threshold_shadow"]
    assert result["probability"] < 0.9


def test_recalibration_ignores_unresolved_and_non_executed_signals() -> None:
    engine = ProbabilityEngine(_config())
    history = _eligible_history()
    history.extend(
        [
            {
                "symbol": "XRPUSDT",
                "setup_type": "breakout",
                "direction": "long",
                "score": 88,
                "status": "active",
                "emitted_at": 1_900_000_000,
            },
            {
                "symbol": "XRPUSDT",
                "setup_type": "breakout",
                "direction": "long",
                "score": 77,
                "status": "expired_without_entry",
                "emitted_at": 1_900_000_900,
                "features_json": dumps({"executed": False}),
            },
        ]
    )

    engine.recalibrate(history)
    counts = engine.get_sample_counts()
    live_status = engine.get_live_activation_status()

    assert counts["total"] == 200
    assert live_status["performance"]["resolved_signals"] == 201
    assert live_status["performance"]["resolved_executed"] == 200
    assert live_status["performance"]["non_execution_rate"] > 0.0


def test_recent_window_uses_latest_chronological_records() -> None:
    engine = ProbabilityEngine(_config())
    history: list[dict[str, object]] = []
    emitted_at = 1_700_000_000
    for index in range(60):
        is_recent = index >= 30
        history.append(
            {
                "symbol": "BTCUSDT",
                "setup_type": "trend_continuation",
                "direction": "long",
                "score": 85 if not is_recent else 60,
                "status": "hit_stop" if is_recent else "hit_t1",
                "emitted_at": emitted_at + (index * 900),
                "result_r": -1.0 if is_recent else 1.5,
                "features_json": dumps({"executed": True}),
            }
        )

    engine.recalibrate(history)
    performance = engine.get_live_activation_status()["performance"]

    assert performance["win_rate"] == 50.0
    assert performance["recent_win_rate"] == 0.0
    assert performance["recent_expectancy"] == -1.0


def test_recalibration_dedupes_legacy_wall_clock_drift_on_same_entry_close() -> None:
    engine = ProbabilityEngine(_config())
    history = [
        {
            "id": "sig-close",
            "symbol": "ETHUSDT",
            "setup_type": "trend_continuation",
            "direction": "long",
            "score": 84,
            "status": "hit_t1",
            "emitted_at": 1_776_328_200,
            "result_r": 1.5,
            "features_json": dumps({"executed": True, "entry_interval_seconds": 900}),
        },
        {
            "id": "sig-drift",
            "symbol": "ETHUSDT",
            "setup_type": "trend_continuation",
            "direction": "long",
            "score": 84,
            "status": "hit_stop",
            "emitted_at": 1_776_328_264,
            "result_r": -1.0,
            "features_json": dumps({"executed": True, "entry_interval_seconds": 900}),
        },
    ]

    engine.recalibrate(history)
    counts = engine.get_live_activation_status()["performance"]

    assert counts["resolved_executed"] == 1
    assert counts["wins"] == 1


def test_signal_lifecycle_resolves_hit_t1_after_entry() -> None:
    config = _config()
    signal = {
        "id": "sig-life-001",
        "symbol": "BTCUSDT",
        "direction": "long",
        "setup_type": "trend_continuation",
        "regime": "bull_trend",
        "score": 84.0,
        "probability": 0.81,
        "entry_low": 100.0,
        "entry_high": 101.0,
        "stop": 95.0,
        "target1": 108.0,
        "target2": 112.0,
        "rr": 2.4,
        "validity_hours": 12.0,
        "invalidation_rule": "test",
        "features_json": dumps(
            {
                "lifecycle_start_at": 900,
                "entry_interval_seconds": 900,
                "entry_mid": 100.5,
                "entry_touched": False,
                "executed": False,
            }
        ),
        "emitted_at": 0,
        "expires_at": 7200,
        "status": "active",
        "mode": "backtest",
        "comment": "",
    }
    entry_candles = [
        {"open_time": 900, "open": 100.8, "high": 101.0, "low": 100.2, "close": 100.9},
        {"open_time": 1800, "open": 101.2, "high": 108.4, "low": 100.7, "close": 108.1},
    ]

    updated = _evaluate_signal_lifecycle(
        signal,
        entry_candles=entry_candles,
        current_regime="bull_trend",
        current_time=2700,
        config=config,
    )

    assert updated is not None
    assert updated["status"] == "hit_t1"
    assert updated["result_r"] == 1.5
    features = loads(updated["features_json"])
    assert features["executed"] is True
    assert features["resolved_reason"] == "target1_hit"


def test_build_signal_seeds_market_on_close_execution() -> None:
    config = _config()
    signal = _build_signal(
        symbol="BTCUSDT",
        regime="range",
        setup={
            "type": "range_rotation",
            "direction": "long",
            "features": {
                "execution_policy": "market_on_close",
                "atr": 1.5,
                "volume_ratio": 1.0,
            },
        },
        scored={
            "score": 80.0,
            "breakdown": {},
            "decision": "shadow",
            "entry_low": 100.0,
            "entry_high": 101.0,
            "stop": 95.0,
            "target1": 108.0,
            "target2": 112.0,
            "rr": 2.2,
            "validity_hours": 12,
            "invalidation_rule": "test",
        },
        probability={
            "probability": 0.8,
            "mode": "shadow",
            "strict_live_ready": False,
            "segment_live_eligible": False,
        },
        emitted_at=1_700_000_900,
        runtime_mode="shadow_live",
        indicators_by_tf={
            "4H": {"structure": "range"},
            "1H": {"structure": "range"},
            "15M": {
                "structure": "bullish",
                "rsi": 42.0,
                "macd_cross": "bullish",
                "macd_histogram": 0.2,
                "volume_ratio": 1.1,
                "wick_ratio": 0.1,
                "bullish_divergence": False,
                "bearish_divergence": False,
                "rsi_rebound_long": True,
                "rsi_rebound_short": False,
            },
        },
        candles_by_tf={
            "4H": [{"open_time": 1_699_996_400, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}],
            "1H": [{"open_time": 1_700_000_000, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}],
            "15M": [
                {
                    "open_time": 1_700_000_000,
                    "open": 100.0,
                    "high": 101.0,
                    "low": 99.5,
                    "close": 100.5,
                    "volume": 1,
                }
            ],
        },
        config=config,
    )

    assert signal["features"]["execution_policy"] == "market_on_close"
    assert signal["features"]["executed"] is True
    assert signal["features"]["entry_touched"] is True
    assert signal["features"]["entered_at"] == 1_700_000_900
    assert signal["features"]["entry_assumed_price"] == pytest.approx(100.5)


def test_build_signal_id_is_deterministic_per_closed_entry_candle() -> None:
    config = _config()
    payload_kwargs = {
        "symbol": "BTCUSDT",
        "regime": "range",
        "setup": {
            "type": "range_rotation",
            "direction": "long",
            "features": {"execution_policy": "market_on_close", "atr": 1.5, "volume_ratio": 1.0},
        },
        "scored": {
            "score": 80.0,
            "breakdown": {},
            "decision": "shadow",
            "entry_low": 100.0,
            "entry_high": 101.0,
            "stop": 95.0,
            "target1": 108.0,
            "target2": 112.0,
            "rr": 2.2,
            "validity_hours": 12,
            "invalidation_rule": "test",
        },
        "probability": {
            "probability": 0.8,
            "mode": "shadow",
            "strict_live_ready": False,
            "segment_live_eligible": False,
        },
        "runtime_mode": "shadow_live",
        "indicators_by_tf": {
            "4H": {"structure": "range"},
            "1H": {"structure": "range"},
            "15M": {
                "structure": "bullish",
                "rsi": 42.0,
                "macd_cross": "bullish",
                "macd_histogram": 0.2,
                "volume_ratio": 1.1,
                "wick_ratio": 0.1,
                "bullish_divergence": False,
                "bearish_divergence": False,
                "rsi_rebound_long": True,
                "rsi_rebound_short": False,
            },
        },
        "candles_by_tf": {
            "4H": [{"open_time": 1_699_996_400, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}],
            "1H": [{"open_time": 1_700_000_000, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}],
            "15M": [
                {
                    "open_time": 1_700_000_000,
                    "open": 100.0,
                    "high": 101.0,
                    "low": 99.5,
                    "close": 100.5,
                    "volume": 1,
                }
            ],
        },
        "config": config,
    }

    first = _build_signal(emitted_at=1_700_000_900, **payload_kwargs)
    second = _build_signal(emitted_at=1_700_001_500, **payload_kwargs)

    assert first["id"] == second["id"]
    assert first["emitted_at"] == 1_700_000_900
    assert second["emitted_at"] == 1_700_000_900


def test_reconcile_signal_lifecycle_requests_recalibration_for_resolved_execution() -> None:
    config_path = _temp_config_path()
    db_path = config_path.parent / "botyo.db"
    init_db(db_path)
    supervisor = BotYoSupervisor(config_path=config_path, db_path=db_path)
    signal = {
        "id": "sig-life-refresh-001",
        "symbol": "BTCUSDT",
        "direction": "long",
        "setup_type": "trend_continuation",
        "regime": "bull_trend",
        "score": 84.0,
        "probability": 0.81,
        "entry_low": 100.0,
        "entry_high": 101.0,
        "stop": 95.0,
        "target1": 108.0,
        "target2": 112.0,
        "rr": 2.4,
        "validity_hours": 12.0,
        "invalidation_rule": "test",
        "features_json": dumps(
            {
                "lifecycle_start_at": 900,
                "entry_interval_seconds": 900,
                "entry_mid": 100.5,
                "entry_touched": False,
                "executed": False,
            }
        ),
        "emitted_at": 0,
        "expires_at": 7200,
        "status": "active",
        "mode": "shadow_live",
        "comment": "",
    }
    upsert_signal(signal, db_path=db_path)
    summary = supervisor._reconcile_signal_lifecycle(
        symbol="BTCUSDT",
        regime="bull_trend",
        candles_by_tf={
            "15M": [
                {"open_time": 900, "open": 100.8, "high": 101.0, "low": 100.2, "close": 100.9},
                {"open_time": 1800, "open": 101.2, "high": 108.4, "low": 100.7, "close": 108.1},
            ]
        },
        current_time=2700,
    )

    assert summary == {"updated": 1, "resolved": 1, "refresh_probability": True}


def test_analyze_candles_recalibrates_when_new_signal_is_emitted(monkeypatch) -> None:
    config_path = _temp_config_path()
    supervisor = BotYoSupervisor(config_path=config_path, db_path=config_path.parent / "botyo.db")
    recalibration_calls: list[bool] = []

    async def fake_recalibrate_probability(self: BotYoSupervisor, *, notify: bool) -> dict[str, object]:
        _ = self
        recalibration_calls.append(notify)
        return {"eligible": False}

    monkeypatch.setattr(BotYoSupervisor, "recalibrate_probability", fake_recalibrate_probability)
    monkeypatch.setattr(
        BotYoSupervisor,
        "_reconcile_signal_lifecycle",
        lambda self, **kwargs: {"updated": 0, "resolved": 0, "refresh_probability": False},
    )
    monkeypatch.setattr("app.supervisor._compute_indicator_snapshot", lambda candles, config: {"structure": "range"})
    monkeypatch.setattr("app.supervisor.classify_regime", lambda trend, setup, config: "range")
    monkeypatch.setattr(
        "app.supervisor.detect_setups",
        lambda symbol, regime, indicators_by_tf, candles_by_tf, config: [
            {"type": "range_rotation", "direction": "long", "features": {}}
        ],
    )
    monkeypatch.setattr("app.supervisor.score_setup", lambda setup, indicators_by_tf, config: {"score": 80.0, "breakdown": {}, "decision": "alert"})
    monkeypatch.setattr(
        supervisor.probability_engine,
        "estimate",
        lambda score, features, setup_type, direction: {
            "probability": 0.8,
            "mode": "shadow",
            "calibrated": False,
            "segment_live_eligible": False,
            "strict_live_ready": False,
        },
    )
    monkeypatch.setattr(
        "app.supervisor._build_signal",
        lambda *args, **kwargs: {
            "id": "sig-dynamic-001",
            "symbol": "BTCUSDT",
            "direction": "long",
            "setup_type": "range_rotation",
            "regime": "range",
            "score": 80.0,
            "probability": 0.8,
            "entry_low": 100.0,
            "entry_high": 101.0,
            "stop": 95.0,
            "target1": 108.0,
            "target2": 112.0,
            "rr": 2.2,
            "validity_hours": 12.0,
            "invalidation_rule": "test",
            "emitted_at": 1_700_000_000,
            "expires_at": 1_700_043_200,
            "delivery_mode": "shadow",
            "features": {},
        },
    )
    monkeypatch.setattr("app.supervisor.get_active_signals", lambda **kwargs: [])
    monkeypatch.setattr("app.supervisor.should_alert", lambda signal, active_signals, config: (True, ""))
    monkeypatch.setattr("app.supervisor.upsert_signal", lambda payload, db_path: None)

    emitted = asyncio.run(
        supervisor._analyze_candles(
            "BTCUSDT",
            candles_by_tf={
                "4H": [{"open_time": 1, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}],
                "1H": [{"open_time": 1, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}],
                "15M": [{"open_time": 1, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}],
            },
            emitted_at=1_700_000_900,
        )
    )

    assert len(emitted) == 1
    assert recalibration_calls == [True]


def test_startup_sync_bootstraps_history_and_updates_runtime_state(monkeypatch) -> None:
    config_path = _temp_config_path()
    supervisor = BotYoSupervisor(config_path=config_path, db_path=config_path.parent / "botyo.db")
    init_db(supervisor.db_path)
    bootstrapped: list[tuple[str, str, int | None]] = []
    analyzed: list[str] = []

    async def fake_bootstrap_history(
        symbol: str,
        timeframe: str,
        lookback_days: int,
        *,
        write_queue: object | None = None,
        since: int | None = None,
        db_path: object | None = None,
    ) -> list[dict[str, object]]:
        _ = lookback_days, write_queue, db_path
        bootstrapped.append((symbol, timeframe, since))
        open_time = {
            "4H": 1_700_000_000,
            "1H": 1_700_010_000,
            "15M": 1_700_020_000,
        }[timeframe]
        return [
            {
                "symbol": symbol,
                "timeframe": timeframe,
                "open_time": open_time,
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.5,
                "volume": 10.0,
                "closed": 1,
            }
        ]

    async def fake_startup_backfill_symbol(self: BotYoSupervisor, symbol: str) -> int:
        _ = self
        analyzed.append(symbol)
        self.latest_regimes[symbol] = "bull_trend"
        return 4

    monkeypatch.setattr("app.supervisor.bootstrap_history", fake_bootstrap_history)
    monkeypatch.setattr(BotYoSupervisor, "_startup_backfill_symbol", fake_startup_backfill_symbol)

    asyncio.run(supervisor.bootstrap_all_history())

    assert len(bootstrapped) == 9
    assert analyzed == ["BTCUSDT", "ETHUSDT", "XRPUSDT"]
    assert supervisor.latest_candle_times["BTCUSDT"]["15M"] == 1_700_020_000
    assert supervisor.last_startup_sync_at is not None
    assert supervisor.last_startup_sync_summary["symbols"]["BTCUSDT"]["15M"]["synced"] == 1
    assert supervisor.last_startup_sync_summary["symbols"]["BTCUSDT"]["15M"]["recoverable_gap"] is True
    assert supervisor.last_startup_sync_summary["analyzed"]["BTCUSDT"] == 4


def test_runtime_sync_history_refreshes_candles_and_replays_new_entry_range(monkeypatch) -> None:
    config_path = _temp_config_path()
    db_path = config_path.parent / "botyo.db"
    init_db(db_path)
    supervisor = BotYoSupervisor(config_path=config_path, db_path=db_path)

    for symbol in supervisor.config["markets"]["symbols"]:
        upsert_candle(
            {
                "symbol": symbol,
                "timeframe": "4H",
                "open_time": 1_700_000_000,
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.5,
                "volume": 10.0,
                "closed": 1,
            },
            db_path=db_path,
        )
        supervisor.latest_candle_times[symbol] = {
            "4H": 1_700_000_000,
            "1H": 1_700_010_000,
            "15M": 1_700_020_000,
        }
        upsert_candle(
            {
                "symbol": symbol,
                "timeframe": "1H",
                "open_time": 1_700_010_000,
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.5,
                "volume": 10.0,
                "closed": 1,
            },
            db_path=db_path,
        )
        upsert_candle(
            {
                "symbol": symbol,
                "timeframe": "15M",
                "open_time": 1_700_020_000,
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.5,
                "volume": 10.0,
                "closed": 1,
            },
            db_path=db_path,
        )

    bootstrapped: list[tuple[str, str, int | None]] = []
    replayed: list[tuple[str, int | None, int]] = []

    async def fake_bootstrap_history(
        symbol: str,
        timeframe: str,
        lookback_days: int,
        *,
        write_queue: object | None = None,
        since: int | None = None,
        db_path: object | None = None,
    ) -> list[dict[str, object]]:
        _ = lookback_days, write_queue
        bootstrapped.append((symbol, timeframe, since))
        open_time = {
            "4H": 1_700_014_400,
            "1H": 1_700_013_600,
            "15M": 1_700_020_900,
        }[timeframe]
        candle = {
            "symbol": symbol,
            "timeframe": timeframe,
            "open_time": open_time,
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
            "volume": 10.0,
            "closed": 1,
        }
        if db_path is not None:
            upsert_candle(candle, db_path=db_path)
        return [candle]

    async def fake_replay_entry_candle_range(
        self: BotYoSupervisor,
        symbol: str,
        *,
        after_open_time: int | None,
        up_to_open_time: int,
    ) -> int:
        _ = self
        replayed.append((symbol, after_open_time, up_to_open_time))
        return 2

    monkeypatch.setattr("app.supervisor.bootstrap_history", fake_bootstrap_history)
    monkeypatch.setattr(BotYoSupervisor, "_replay_entry_candle_range", fake_replay_entry_candle_range)

    summary = asyncio.run(supervisor.runtime_sync_history())

    assert len(bootstrapped) == 9
    assert supervisor.latest_candle_times["BTCUSDT"]["15M"] == 1_700_020_900
    assert supervisor.last_market_sync_at is not None
    assert summary["symbols"]["BTCUSDT"]["15M"]["synced"] == 1
    assert summary["replayed"]["BTCUSDT"] == 2
    assert replayed[0] == ("BTCUSDT", 1_700_020_000, 1_700_020_900)


def test_supervisor_notifies_when_live_activation_becomes_eligible(monkeypatch) -> None:
    config_path = _temp_config_path()
    supervisor = BotYoSupervisor(config_path=config_path, db_path=config_path.parent / "botyo.db")
    history = _eligible_history()

    monkeypatch.setattr("app.supervisor.get_recent_signals", lambda limit, db_path: history)
    captured: dict[str, object] = {}

    async def fake_send_telegram_message(text: str, config: dict, *, force: bool = False) -> bool:
        captured["text"] = text
        captured["force"] = force
        return True

    monkeypatch.setattr("app.supervisor.send_telegram_message", fake_send_telegram_message)

    status = asyncio.run(supervisor.recalibrate_probability(notify=True))

    assert status["eligible"] is True
    assert captured["force"] is True
    assert "Activation manuelle requise via /admin." in str(captured["text"])
