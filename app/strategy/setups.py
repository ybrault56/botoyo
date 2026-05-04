"""Setup detection for BotYo strategy phase."""

from __future__ import annotations

from typing import Any

import numpy as np

from app.indicators.adx import compute_volume_ma, detect_swing_highs, detect_swing_lows


def detect_setups(
    symbol: str,
    regime: str,
    indicators_by_tf: dict[str, dict[str, Any]],
    candles_by_tf: dict[str, list[dict[str, float]]],
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    """Detect all supported V1 setups on closed candles only."""

    results: list[dict[str, Any]] = []

    for detector in (
        _detect_trend_continuation,
        _detect_breakout,
        _detect_reversal,
        _detect_range_rotation,
    ):
        detected = detector(symbol, regime, indicators_by_tf, candles_by_tf, config)
        if detected is not None:
            results.append(detected)

    return results


def diagnose_setups(
    symbol: str,
    regime: str,
    indicators_by_tf: dict[str, dict[str, Any]],
    candles_by_tf: dict[str, list[dict[str, float]]],
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    """Expose current setup blockers for dashboard diagnostics."""

    diagnostics = [
        _diagnose_trend_continuation(symbol, regime, indicators_by_tf, candles_by_tf, config),
        _diagnose_breakout(symbol, regime, indicators_by_tf, candles_by_tf, config),
        _diagnose_reversal(symbol, regime, indicators_by_tf, candles_by_tf, config),
        _diagnose_range_rotation(symbol, regime, indicators_by_tf, candles_by_tf, config),
    ]
    return diagnostics


def _diagnose_trend_continuation(
    symbol: str,
    regime: str,
    indicators_by_tf: dict[str, dict[str, Any]],
    candles_by_tf: dict[str, list[dict[str, float]]],
    config: dict[str, Any],
) -> dict[str, Any]:
    setup_cfg = config["setups"]["trend_continuation"]
    trend_tf, setup_tf, entry_tf = _configured_timeframes(config)
    trend = indicators_by_tf[trend_tf]
    setup = indicators_by_tf[setup_tf]
    entry = indicators_by_tf[entry_tf]
    asset_cfg = _asset_config(config, symbol)
    direction = "long" if regime == "bull_trend" else "short" if regime == "bear_trend" else _trend_bias_direction(trend)
    blockers: list[str] = []

    if not setup_cfg["enabled"]:
        blockers.append("setup desactive")
    if regime not in setup_cfg["allowed_regimes"]:
        blockers.append(f"regime {regime} non autorise")
    if direction is None:
        blockers.append("direction de tendance non lisible")
    else:
        if not _trend_alignment(trend, direction):
            blockers.append("tendance 4H non alignee")
        if not _structure_supports_direction(setup, direction):
            blockers.append("structure 1H opposee")
        location = _location_confluence(
            direction=direction,
            price=float(setup["close"]),
            setup_indicators=setup,
            atr=float(setup["atr"]),
            symbol=symbol,
            config=config,
        )
        if location["count"] < 2:
            blockers.append(f"confluence zone insuffisante ({location['count']}/2)")
        confirmation = _entry_confirmation(
            direction=direction,
            entry_indicators=entry,
            candles_entry=candles_by_tf[entry_tf],
            min_volume_ratio=max(float(asset_cfg["min_volume_ratio"]), 1.0),
        )
        required_confirmation = int(setup_cfg["entry_confirmation_min_confluence"])
        if confirmation["count"] < required_confirmation:
            blockers.append(f"confirmation 15M insuffisante ({confirmation['count']}/{required_confirmation})")

    return _setup_diagnostic_payload("trend_continuation", direction, blockers)


def _diagnose_breakout(
    symbol: str,
    regime: str,
    indicators_by_tf: dict[str, dict[str, Any]],
    candles_by_tf: dict[str, list[dict[str, float]]],
    config: dict[str, Any],
) -> dict[str, Any]:
    setup_cfg = config["setups"]["breakout"]
    _, setup_tf, entry_tf = _configured_timeframes(config)
    setup = indicators_by_tf[setup_tf]
    entry = indicators_by_tf[entry_tf]
    candles_setup = candles_by_tf[setup_tf]
    candles_entry = candles_by_tf[entry_tf]
    asset_cfg = _asset_config(config, symbol)
    blockers: list[str] = []
    direction: str | None = None

    if not setup_cfg["enabled"]:
        blockers.append("setup desactive")
    if regime not in setup_cfg["allowed_regimes"]:
        blockers.append(f"regime {regime} non autorise")
    compression_bars = int(setup_cfg["min_compression_bars"])
    if len(candles_setup) < compression_bars:
        blockers.append("historique compression insuffisant")
    else:
        recent = candles_setup[-(compression_bars + 1) : -1] if len(candles_setup) > compression_bars else candles_setup[:-1]
        highs = np.asarray([float(candle["high"]) for candle in recent], dtype=float)
        lows = np.asarray([float(candle["low"]) for candle in recent], dtype=float)
        compression_range = float(np.max(highs) - np.min(lows))
        atr = float(setup["atr"])
        if compression_range > atr * 1.2:
            blockers.append("pas de compression propre")
        current_close = float(candles_setup[-1]["close"])
        breakout_high = float(np.max(highs))
        breakout_low = float(np.min(lows))
        if current_close > breakout_high:
            direction = "long"
        elif current_close < breakout_low:
            direction = "short"
        else:
            blockers.append("cassure 1H non confirmee")

        if direction is not None:
            volume_ratio = max(float(setup.get("volume_ratio", 1.0)), float(entry.get("volume_ratio", 1.0)))
            required_volume_ratio = max(float(setup_cfg["min_volume_ratio_breakout"]), float(asset_cfg["breakout_volume_ratio"]))
            if volume_ratio < required_volume_ratio:
                blockers.append(f"volume breakout trop faible ({volume_ratio:.2f}/{required_volume_ratio:.2f})")
            clear_space_key = "bullish_clear_space_atr" if direction == "long" else "bearish_clear_space_atr"
            clear_space_atr = float(setup.get(clear_space_key, setup.get("clear_space_atr", 0.0)))
            if clear_space_atr < float(setup_cfg["min_clear_space_atr"]):
                blockers.append("espace libre insuffisant apres cassure")
            confirmation = _entry_confirmation(
                direction=direction,
                entry_indicators=entry,
                candles_entry=candles_entry,
                min_volume_ratio=float(asset_cfg["min_volume_ratio"]),
            )
            required_confirmation = int(setup_cfg["entry_confirmation_min_confluence"])
            if confirmation["count"] < required_confirmation:
                blockers.append(f"confirmation 15M insuffisante ({confirmation['count']}/{required_confirmation})")

    return _setup_diagnostic_payload("breakout", direction, blockers)


def _diagnose_reversal(
    symbol: str,
    regime: str,
    indicators_by_tf: dict[str, dict[str, Any]],
    candles_by_tf: dict[str, list[dict[str, float]]],
    config: dict[str, Any],
) -> dict[str, Any]:
    setup_cfg = config["setups"]["reversal"]
    _, setup_tf, entry_tf = _configured_timeframes(config)
    setup = indicators_by_tf[setup_tf]
    entry = indicators_by_tf[entry_tf]
    asset_cfg = _asset_config(config, symbol)
    direction = str(entry.get("reversal_direction", "long"))
    blockers: list[str] = []

    if not setup_cfg["enabled"]:
        blockers.append("setup desactive")
    if regime not in setup_cfg["allowed_regimes"]:
        blockers.append(f"regime {regime} non autorise")
    if not bool(setup.get("major_level_touch", False)):
        blockers.append("pas de niveau majeur touche")
    if not bool(entry.get("structure_break", False)):
        blockers.append("pas de break de structure 15M")
    if float(setup.get("extension_atr", 0.0)) < float(setup_cfg["min_extension_atr"]):
        blockers.append("extension ATR trop faible")

    confirmation = _entry_confirmation(
        direction=direction,
        entry_indicators=entry,
        candles_entry=candles_by_tf[entry_tf],
        min_volume_ratio=float(asset_cfg["min_volume_ratio"]),
    )
    divergence_key = "bullish_divergence" if direction == "long" else "bearish_divergence"
    if bool(setup_cfg.get("prefer_rsi_divergence", False)) and not (
        bool(entry.get(divergence_key, False)) or confirmation["macd_confirmed"]
    ):
        blockers.append("pas de divergence RSI ni croisement MACD utile")
    required_confirmation = int(setup_cfg["entry_confirmation_min_confluence"])
    if confirmation["count"] < required_confirmation:
        blockers.append(f"confirmation 15M insuffisante ({confirmation['count']}/{required_confirmation})")

    return _setup_diagnostic_payload("reversal", direction, blockers)


def _diagnose_range_rotation(
    symbol: str,
    regime: str,
    indicators_by_tf: dict[str, dict[str, Any]],
    candles_by_tf: dict[str, list[dict[str, float]]],
    config: dict[str, Any],
) -> dict[str, Any]:
    setup_cfg = config["setups"]["range_rotation"]
    _, setup_tf, entry_tf = _configured_timeframes(config)
    setup = indicators_by_tf[setup_tf]
    entry = indicators_by_tf[entry_tf]
    candles_setup = candles_by_tf[setup_tf]
    direction: str | None = None
    blockers: list[str] = []

    if not setup_cfg["enabled"]:
        blockers.append("setup desactive")
    if regime not in setup_cfg["allowed_regimes"]:
        blockers.append(f"regime {regime} non autorise")
    if "range_low" not in setup or "range_high" not in setup:
        blockers.append("range 1H non defini")
    else:
        atr = float(setup["atr"])
        lower = float(setup["range_low"])
        upper = float(setup["range_high"])
        close = float(candles_setup[-1]["close"])
        latest = candles_setup[-1]
        max_distance = atr * float(setup_cfg["max_distance_from_boundary_atr"])
        if float(latest["low"]) <= lower + max_distance and close > lower:
            direction = "long"
        elif float(latest["high"]) >= upper - max_distance and close < upper:
            direction = "short"
        else:
            blockers.append("prix 1H trop loin d'une borne de range")

        if direction is not None:
            confirmation = _entry_confirmation(
                direction=direction,
                entry_indicators=entry,
                candles_entry=candles_by_tf[entry_tf],
                min_volume_ratio=0.8,
            )
            if bool(setup_cfg.get("require_rsi_reversal", False)) and not confirmation["rsi_rebound"]:
                blockers.append("RSI 15M sans rebond de range")
            required_confirmation = int(setup_cfg.get("entry_confirmation_min_confluence", 2))
            if confirmation["count"] < required_confirmation:
                blockers.append(f"confirmation 15M insuffisante ({confirmation['count']}/{required_confirmation})")

    return _setup_diagnostic_payload("range_rotation", direction, blockers)


def _setup_diagnostic_payload(name: str, direction: str | None, blockers: list[str]) -> dict[str, Any]:
    label = name.replace("_", " ").title()
    eligible = not blockers
    summary = "Pret" if eligible else " | ".join(blockers[:2])
    return {
        "name": name,
        "label": label,
        "direction": direction,
        "eligible": eligible,
        "summary": summary,
        "blockers": blockers[:4],
    }


def _detect_trend_continuation(
    symbol: str,
    regime: str,
    indicators_by_tf: dict[str, dict[str, Any]],
    candles_by_tf: dict[str, list[dict[str, float]]],
    config: dict[str, Any],
) -> dict[str, Any] | None:
    setup_cfg = config["setups"]["trend_continuation"]
    if not setup_cfg["enabled"] or regime not in setup_cfg["allowed_regimes"]:
        return None

    trend_tf, setup_tf, entry_tf = _configured_timeframes(config)
    trend = indicators_by_tf[trend_tf]
    setup = indicators_by_tf[setup_tf]
    entry = indicators_by_tf[entry_tf]
    candles_setup = candles_by_tf[setup_tf]
    candles_entry = candles_by_tf[entry_tf]
    asset_cfg = _asset_config(config, symbol)

    direction = "long" if regime == "bull_trend" else "short"
    if not _trend_alignment(trend, direction):
        return None

    if not _structure_supports_direction(setup, direction):
        return None

    location = _location_confluence(
        direction=direction,
        price=float(setup["close"]),
        setup_indicators=setup,
        atr=float(setup["atr"]),
        symbol=symbol,
        config=config,
    )
    if location["count"] < 2:
        return None

    confirmation = _entry_confirmation(
        direction=direction,
        entry_indicators=entry,
        candles_entry=candles_entry,
        min_volume_ratio=max(float(asset_cfg["min_volume_ratio"]), 1.0),
    )
    if confirmation["count"] < int(setup_cfg["entry_confirmation_min_confluence"]):
        return None

    stop = _swing_stop(candles_entry, direction, fallback_atr=float(entry["atr"]))
    entry_mid = float(candles_entry[-1]["close"])
    entry_zone = _entry_zone(
        entry_mid,
        float(entry["atr"]),
        width_factor=float(setup_cfg.get("entry_zone_half_width_atr", 0.05)),
    )
    stop_distance = abs(entry_mid - stop)
    clear_space_key = "bullish_clear_space_atr" if direction == "long" else "bearish_clear_space_atr"
    clear_space_atr = float(setup.get(clear_space_key, setup.get("clear_space_atr", 2.5)))
    reward_distance = max(stop_distance * 2.0, clear_space_atr * float(setup["atr"]))

    return {
        "symbol": symbol,
        "type": "trend_continuation",
        "direction": direction,
        "regime": regime,
        "entry_zone": entry_zone,
        "stop": stop,
        "validity_hours": int(setup_cfg["validity_hours"]),
        "invalidation_rule": f"cloture {setup_tf} contre la structure dominante",
        "features": {
            "regime_alignment": "perfect",
            "timeframe_alignment": 3,
            "conditions_met": _conditions_ratio(location["count"], confirmation["count"], target_total=6),
            "location_quality": location["quality"],
            "confluence_count": location["count"] + confirmation["count"],
            "volume_ratio": float(entry.get("volume_ratio", 1.0)),
            "clear_stop": True,
            "setup_quality": "complete" if confirmation["count"] >= 2 and location["count"] >= 2 else "partial",
            "atr": float(entry["atr"]),
            "reward_distance": reward_distance,
            "execution_policy": str(setup_cfg.get("entry_execution_policy", "market_on_close")),
            "fib_confluence": location["fib"],
            "round_level_confluence": location["round_level"],
            "major_level_touch": location["major_level"],
            "ema_zone": location["ema_zone"],
            "momentum_bonus": bool(entry.get("bullish_divergence" if direction == "long" else "bearish_divergence", False)),
            **_confirmation_features(confirmation),
        },
    }


def _detect_breakout(
    symbol: str,
    regime: str,
    indicators_by_tf: dict[str, dict[str, Any]],
    candles_by_tf: dict[str, list[dict[str, float]]],
    config: dict[str, Any],
) -> dict[str, Any] | None:
    setup_cfg = config["setups"]["breakout"]
    if not setup_cfg["enabled"] or regime not in setup_cfg["allowed_regimes"]:
        return None

    _, setup_tf, entry_tf = _configured_timeframes(config)
    setup = indicators_by_tf[setup_tf]
    entry = indicators_by_tf[entry_tf]
    candles_setup = candles_by_tf[setup_tf]
    candles_entry = candles_by_tf[entry_tf]
    asset_cfg = _asset_config(config, symbol)
    compression_bars = int(setup_cfg["min_compression_bars"])
    if len(candles_setup) < compression_bars:
        return None

    if len(candles_setup) == compression_bars:
        recent = candles_setup[:-1]
    else:
        recent = candles_setup[-(compression_bars + 1) : -1]
    highs = np.asarray([float(candle["high"]) for candle in recent], dtype=float)
    lows = np.asarray([float(candle["low"]) for candle in recent], dtype=float)
    compression_range = float(np.max(highs) - np.min(lows))
    atr = float(setup["atr"])
    if compression_range > atr * 1.2:
        return None

    current_close = float(candles_setup[-1]["close"])
    breakout_high = float(np.max(highs))
    breakout_low = float(np.min(lows))
    direction: str | None = None

    if current_close > breakout_high:
        direction = "long"
        stop = breakout_low
    elif current_close < breakout_low:
        direction = "short"
        stop = breakout_high
    else:
        return None

    volume_ratio = max(float(setup.get("volume_ratio", 1.0)), float(entry.get("volume_ratio", 1.0)))
    required_volume_ratio = max(float(setup_cfg["min_volume_ratio_breakout"]), float(asset_cfg["breakout_volume_ratio"]))
    clear_space_key = "bullish_clear_space_atr" if direction == "long" else "bearish_clear_space_atr"
    clear_space_atr = float(setup.get(clear_space_key, setup.get("clear_space_atr", 0.0)))
    if volume_ratio < required_volume_ratio or clear_space_atr < float(setup_cfg["min_clear_space_atr"]):
        return None

    confirmation = _entry_confirmation(
        direction=direction,
        entry_indicators=entry,
        candles_entry=candles_entry,
        min_volume_ratio=float(asset_cfg["min_volume_ratio"]),
    )
    if confirmation["count"] < int(setup_cfg["entry_confirmation_min_confluence"]):
        return None

    entry_zone = _entry_zone(
        current_close,
        atr,
        width_factor=float(setup_cfg.get("entry_zone_half_width_atr", 0.05)),
    )
    stop_distance = abs(current_close - float(stop))
    reward_distance = max(stop_distance * 2.0, clear_space_atr * atr)
    return {
        "symbol": symbol,
        "type": "breakout",
        "direction": direction,
        "regime": regime,
        "entry_zone": entry_zone,
        "stop": float(stop),
        "validity_hours": int(setup_cfg["validity_hours"]),
        "invalidation_rule": f"reintre dans la compression {setup_tf}",
        "features": {
            "regime_alignment": "perfect" if regime != "range" else "compatible",
            "timeframe_alignment": 3,
            "conditions_met": _conditions_ratio(3, confirmation["count"], target_total=5),
            "location_quality": "major",
            "confluence_count": 2 + confirmation["count"],
            "volume_ratio": volume_ratio,
            "clear_stop": True,
            "setup_quality": "complete",
            "atr": atr,
            "reward_distance": reward_distance,
            "execution_policy": str(setup_cfg.get("entry_execution_policy", "market_on_close")),
            "major_level_touch": False,
            "ema_zone": False,
            "fib_confluence": False,
            "round_level_confluence": False,
            **_confirmation_features(confirmation),
        },
    }


def _detect_reversal(
    symbol: str,
    regime: str,
    indicators_by_tf: dict[str, dict[str, Any]],
    candles_by_tf: dict[str, list[dict[str, float]]],
    config: dict[str, Any],
) -> dict[str, Any] | None:
    setup_cfg = config["setups"]["reversal"]
    if not setup_cfg["enabled"] or regime not in setup_cfg["allowed_regimes"]:
        return None

    _, setup_tf, entry_tf = _configured_timeframes(config)
    setup = indicators_by_tf[setup_tf]
    entry = indicators_by_tf[entry_tf]
    candles_entry = candles_by_tf[entry_tf]
    asset_cfg = _asset_config(config, symbol)
    atr = float(entry["atr"])

    if not bool(setup.get("major_level_touch", False)):
        return None
    if not bool(entry.get("structure_break", False)):
        return None
    if float(setup.get("extension_atr", 0.0)) < float(setup_cfg["min_extension_atr"]):
        return None

    direction = str(entry.get("reversal_direction", "long"))
    confirmation = _entry_confirmation(
        direction=direction,
        entry_indicators=entry,
        candles_entry=candles_entry,
        min_volume_ratio=float(asset_cfg["min_volume_ratio"]),
    )
    divergence_key = "bullish_divergence" if direction == "long" else "bearish_divergence"
    if bool(setup_cfg.get("prefer_rsi_divergence", False)) and not (
        bool(entry.get(divergence_key, False)) or confirmation["macd_confirmed"]
    ):
        return None
    if confirmation["count"] < int(setup_cfg["entry_confirmation_min_confluence"]):
        return None

    stop = _reversal_stop(candles_entry, direction, fallback_atr=float(entry["atr"]))
    current_close = float(candles_entry[-1]["close"])
    stop_distance = abs(current_close - stop)
    reward_distance = max(stop_distance * float(setup_cfg["min_rr"]), float(setup.get("atr", atr)) * 2.0)

    return {
        "symbol": symbol,
        "type": "reversal",
        "direction": direction,
        "regime": regime,
        "entry_zone": _entry_zone(
            current_close,
            atr,
            width_factor=float(setup_cfg.get("entry_zone_half_width_atr", 0.06)),
        ),
        "stop": stop,
        "validity_hours": int(setup_cfg["validity_hours"]),
        "invalidation_rule": "casse a nouveau l'exces",
        "features": {
            "regime_alignment": "compatible",
            "timeframe_alignment": 3,
            "conditions_met": _conditions_ratio(3, confirmation["count"], target_total=5),
            "location_quality": "major",
            "confluence_count": 2 + confirmation["count"],
            "volume_ratio": float(entry.get("volume_ratio", 1.0)),
            "clear_stop": True,
            "setup_quality": "complete",
            "momentum_bonus": bool(entry.get(divergence_key, False)),
            "atr": atr,
            "reward_distance": reward_distance,
            "execution_policy": str(setup_cfg.get("entry_execution_policy", "market_on_close")),
            "major_level_touch": bool(setup.get("major_level_touch", False)),
            "ema_zone": False,
            "fib_confluence": False,
            "round_level_confluence": False,
            **_confirmation_features(confirmation),
        },
    }


def _detect_range_rotation(
    symbol: str,
    regime: str,
    indicators_by_tf: dict[str, dict[str, Any]],
    candles_by_tf: dict[str, list[dict[str, float]]],
    config: dict[str, Any],
) -> dict[str, Any] | None:
    setup_cfg = config["setups"]["range_rotation"]
    if not setup_cfg["enabled"] or regime not in setup_cfg["allowed_regimes"]:
        return None

    _, setup_tf, entry_tf = _configured_timeframes(config)
    setup = indicators_by_tf[setup_tf]
    entry = indicators_by_tf[entry_tf]
    candles_setup = candles_by_tf[setup_tf]
    candles_entry = candles_by_tf[entry_tf]
    atr = float(setup["atr"])
    if "range_low" not in setup or "range_high" not in setup:
        return None
    lower = float(setup["range_low"])
    upper = float(setup["range_high"])
    close = float(candles_setup[-1]["close"])
    latest = candles_setup[-1]

    max_distance = atr * float(setup_cfg["max_distance_from_boundary_atr"])
    direction: str | None = None
    stop: float

    if float(latest["low"]) <= lower + max_distance and close > lower:
        direction = "long"
        stop = lower - (atr * 0.1)
    elif float(latest["high"]) >= upper - max_distance and close < upper:
        direction = "short"
        stop = upper + (atr * 0.1)
    else:
        return None

    confirmation = _entry_confirmation(
        direction=direction,
        entry_indicators=entry,
        candles_entry=candles_entry,
        min_volume_ratio=0.8,
    )
    if bool(setup_cfg.get("require_rsi_reversal", False)) and not confirmation["rsi_rebound"]:
        return None
    required_confirmation = int(setup_cfg.get("entry_confirmation_min_confluence", 2))
    if confirmation["count"] < required_confirmation:
        return None

    entry_mid = float(candles_entry[-1]["close"])
    stop_distance = abs(entry_mid - stop)
    if direction == "long":
        reward_distance = max(stop_distance * 2.0, ((upper - entry_mid) * 0.8))
    else:
        reward_distance = max(stop_distance * 2.0, ((entry_mid - lower) * 0.8))

    return {
        "symbol": symbol,
        "type": "range_rotation",
        "direction": direction,
        "regime": regime,
        "entry_zone": _entry_zone(
            entry_mid,
            float(entry["atr"]),
            width_factor=float(setup_cfg.get("entry_zone_half_width_atr", 0.05)),
        ),
        "stop": stop,
        "validity_hours": int(setup_cfg["validity_hours"]),
        "invalidation_rule": "cloture hors borne du range",
        "features": {
            "regime_alignment": "perfect",
            "timeframe_alignment": 3,
            "conditions_met": _conditions_ratio(2, confirmation["count"], target_total=4),
            "location_quality": "major",
            "confluence_count": 2 + confirmation["count"],
            "volume_ratio": max(float(setup.get("volume_ratio", 1.0)), float(entry.get("volume_ratio", 1.0))),
            "clear_stop": True,
            "setup_quality": "complete",
            "atr": float(entry["atr"]),
            "range_mid": (lower + upper) / 2.0,
            "range_high": upper,
            "range_low": lower,
            "reward_distance": reward_distance,
            "execution_policy": str(setup_cfg.get("entry_execution_policy", "market_on_close")),
            "major_level_touch": True,
            "ema_zone": False,
            "fib_confluence": False,
            "round_level_confluence": False,
            **_confirmation_features(confirmation),
        },
    }


def _entry_zone(entry_mid: float, atr: float, *, width_factor: float) -> tuple[float, float]:
    width = atr * width_factor
    return (entry_mid - width, entry_mid + width)


def _entry_confirmation(
    *,
    direction: str,
    entry_indicators: dict[str, Any],
    candles_entry: list[dict[str, float]],
    min_volume_ratio: float,
) -> dict[str, Any]:
    reversal = _is_reversal_candle(direction, candles_entry[-1])
    rsi_rebound = bool(entry_indicators.get(f"rsi_rebound_{direction}", False))
    macd_confirmed = str(entry_indicators.get("macd_cross", "none")) == ("bullish" if direction == "long" else "bearish")
    structure_confirmed = bool(entry_indicators.get("structure_break", False)) and str(
        entry_indicators.get("reversal_direction", direction)
    ) == direction
    volume_confirmed = float(entry_indicators.get("volume_ratio", 1.0)) >= min_volume_ratio

    count = sum([reversal, rsi_rebound, macd_confirmed, structure_confirmed, volume_confirmed])
    return {
        "count": int(count),
        "reversal": reversal,
        "rsi_rebound": rsi_rebound,
        "macd_confirmed": macd_confirmed,
        "structure_confirmed": structure_confirmed,
        "volume_confirmed": volume_confirmed,
    }


def _swing_stop(candles: list[dict[str, float]], direction: str, *, fallback_atr: float) -> float:
    highs = np.asarray([float(candle["high"]) for candle in candles], dtype=float)
    lows = np.asarray([float(candle["low"]) for candle in candles], dtype=float)
    close = float(candles[-1]["close"])

    if direction == "long":
        swings = detect_swing_lows(lows, window=1)
        if swings:
            return float(lows[swings[-1]])
        return close - fallback_atr

    swings = detect_swing_highs(highs, window=1)
    if swings:
        return float(highs[swings[-1]])
    return close + fallback_atr


def _reversal_stop(candles: list[dict[str, float]], direction: str, *, fallback_atr: float) -> float:
    recent = candles[-min(len(candles), 6) :]
    buffer = fallback_atr * 0.05
    highs = np.asarray([float(candle["high"]) for candle in recent], dtype=float)
    lows = np.asarray([float(candle["low"]) for candle in recent], dtype=float)

    if direction == "long":
        swings = detect_swing_lows(lows, window=1)
        anchor = float(lows[swings[-1]]) if swings else float(np.min(lows))
        return round(anchor - buffer, 6)

    swings = detect_swing_highs(highs, window=1)
    anchor = float(highs[swings[-1]]) if swings else float(np.max(highs))
    return round(anchor + buffer, 6)


def _confirmation_features(confirmation: dict[str, Any]) -> dict[str, Any]:
    return {
        "reversal_candle": bool(confirmation.get("reversal", False)),
        "rsi_rebound": bool(confirmation.get("rsi_rebound", False)),
        "macd_confirmed": bool(confirmation.get("macd_confirmed", False)),
        "structure_confirmed": bool(confirmation.get("structure_confirmed", False)),
        "volume_confirmed": bool(confirmation.get("volume_confirmed", False)),
    }


def _configured_timeframes(config: dict[str, Any]) -> tuple[str, str, str]:
    timeframes = config["timeframes"]
    return str(timeframes["trend"]), str(timeframes["setup"]), str(timeframes["entry"])


def _trend_bias_direction(trend_indicators: dict[str, Any]) -> str | None:
    ema20 = float(trend_indicators.get("ema20", 0.0))
    ema50 = float(trend_indicators.get("ema50", 0.0))
    ema200 = float(trend_indicators.get("ema200", 0.0))
    close = float(trend_indicators.get("close", 0.0))
    if ema20 >= ema50 > ema200 and close > ema50:
        return "long"
    if ema20 <= ema50 < ema200 and close < ema50:
        return "short"
    return None


def _trend_alignment(trend_indicators: dict[str, Any], direction: str) -> bool:
    ema20 = float(trend_indicators.get("ema20", 0.0))
    ema50 = float(trend_indicators.get("ema50", 0.0))
    ema200 = float(trend_indicators.get("ema200", 0.0))
    close = float(trend_indicators.get("close", 0.0))
    structure = str(trend_indicators.get("structure", "range")).lower()
    if direction == "long":
        return ema20 >= ema50 > ema200 and close > ema200 and structure not in {"bearish", "lh_ll"}
    return ema20 <= ema50 < ema200 and close < ema200 and structure not in {"bullish", "hh_hl"}


def _structure_supports_direction(indicators: dict[str, Any], direction: str) -> bool:
    structure = str(indicators.get("structure", "range")).lower()
    if direction == "long":
        return structure not in {"bearish", "lh_ll"}
    return structure not in {"bullish", "hh_hl"}


def _location_confluence(
    *,
    direction: str,
    price: float,
    setup_indicators: dict[str, Any],
    atr: float,
    symbol: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    setup_cfg = config["setups"]["trend_continuation"]
    tolerance = atr * float(setup_cfg.get("pullback_tolerance_atr", 0.35))
    ema20 = float(setup_indicators.get("ema20", price))
    ema50 = float(setup_indicators.get("ema50", price))
    ema_zone = (min(ema20, ema50) - tolerance) <= price <= (max(ema20, ema50) + tolerance)
    fib = _near_fibonacci_level(price, setup_indicators, atr, symbol, config)
    round_level = _near_round_level(symbol, price, atr)
    major_level = bool(setup_indicators.get("major_level_touch", False))

    count = sum([ema_zone, fib, round_level, major_level])
    quality = "major" if count >= 3 else "secondary" if count >= 2 else "poor"
    return {
        "count": int(count),
        "quality": quality,
        "fib": fib,
        "round_level": round_level,
        "major_level": major_level,
        "ema_zone": ema_zone,
    }


def _near_fibonacci_level(price: float, indicators: dict[str, Any], atr: float, symbol: str, config: dict[str, Any]) -> bool:
    fib_levels = indicators.get("fib_levels")
    if not isinstance(fib_levels, dict):
        return False
    tolerance = atr * (0.15 + (0.05 * float(_asset_config(config, symbol)["fib_confluence_weight"])))
    levels = [fib_levels.get("0.5"), fib_levels.get("0.618"), fib_levels.get("0.382")]
    return any(level is not None and abs(price - float(level)) <= tolerance for level in levels)


def _near_round_level(symbol: str, price: float, atr: float) -> bool:
    if symbol == "BTCUSDT":
        step = 5000.0
    elif symbol == "ETHUSDT":
        step = 100.0
    else:
        step = 0.10
    nearest = round(price / step) * step
    return abs(price - nearest) <= max(atr * 0.25, step * 0.01)


def _is_reversal_candle(direction: str, candle: dict[str, float]) -> bool:
    open_ = float(candle.get("open", candle["close"]))
    close = float(candle["close"])
    high = float(candle["high"])
    low = float(candle["low"])
    candle_range = max(high - low, 1e-9)
    body = abs(close - open_)
    if body / candle_range < 0.35:
        return False
    if direction == "long":
        return close > open_ and close >= (low + (candle_range * 0.6))
    return close < open_ and close <= (high - (candle_range * 0.6))


def _asset_config(config: dict[str, Any], symbol: str) -> dict[str, float]:
    defaults = {"min_volume_ratio": 1.0, "breakout_volume_ratio": 2.0, "fib_confluence_weight": 1.0}
    overrides = config.get("assets", {}).get(symbol, {})
    return {
        "min_volume_ratio": float(overrides.get("min_volume_ratio", defaults["min_volume_ratio"])),
        "breakout_volume_ratio": float(overrides.get("breakout_volume_ratio", defaults["breakout_volume_ratio"])),
        "fib_confluence_weight": float(overrides.get("fib_confluence_weight", defaults["fib_confluence_weight"])),
    }


def _conditions_ratio(primary_count: int, confirmation_count: int, *, target_total: int) -> float:
    return min(1.0, (float(primary_count) + float(confirmation_count)) / float(target_total))


def compute_volume_ratio(candles: list[dict[str, float]], period: int = 20) -> float:
    """Return the current candle volume ratio against the recent moving average."""

    volumes = np.asarray([float(candle["volume"]) for candle in candles], dtype=float)
    if volumes.size < 2:
        return 1.0
    baseline = compute_volume_ma(volumes[:-1], period=period) if volumes.size > 1 else float(volumes[-1])
    return float(volumes[-1] / baseline) if baseline > 0 else 1.0
