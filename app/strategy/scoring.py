"""Setup scoring and decisioning for BotYo."""

from __future__ import annotations

from typing import Any


def score_setup(setup: dict[str, Any], indicators_by_tf: dict[str, dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    """Score a detected setup and derive risk targets."""

    weights = config["scoring"]["weights"]
    thresholds = config["scoring"]["thresholds"]
    risk_cfg = config["risk"]
    entry_tf = str(config["timeframes"]["entry"])
    setup_tf = str(config["timeframes"]["setup"])
    features = dict(setup.get("features", {}))
    entry_indicators = indicators_by_tf.get(entry_tf, {})
    atr = float(features.get("atr", entry_indicators.get("atr", 1.0)))

    entry_low, entry_high = map(float, setup["entry_zone"])
    entry_mid = (entry_low + entry_high) / 2.0
    stop = float(setup["stop"])
    stop_distance = abs(entry_mid - stop)
    direction = setup["direction"]

    reward_distance = float(features.get("reward_distance", stop_distance * float(risk_cfg["preferred_rr_for_alert"])))
    rr = reward_distance / stop_distance if stop_distance > 0 else 0.0
    target1 = _target(entry_mid, stop_distance, direction, float(risk_cfg["targets"]["t1_r"]))
    target2 = _target(entry_mid, stop_distance, direction, float(risk_cfg["targets"]["t2_r"]))

    breakdown = {
        "regime": _regime_score(features.get("regime_alignment"), float(weights["regime"])),
        "structure": _structure_score(features.get("timeframe_alignment", 0), float(weights["structure"])),
        "setup_quality": _setup_quality_score(features.get("conditions_met", 0.0), float(weights["setup_quality"])),
        "location": _location_score(features.get("location_quality"), features.get("confluence_count", 0), float(weights["location"])),
        "momentum": _momentum_score(direction, indicators_by_tf, features, config, float(weights["momentum"])),
        "volume": _volume_score(float(features.get("volume_ratio", 1.0)), float(weights["volume"])),
        "entry_quality": _entry_quality_score(entry_low, entry_high, atr, float(weights["entry_quality"])),
        "stop_quality": _stop_quality_score(bool(features.get("clear_stop", False)), stop_distance, atr, float(weights["stop_quality"])),
        "rr_quality": _rr_quality_score(rr, float(weights["rr_quality"])),
    }

    forced_reject = False
    if breakdown["regime"] <= 0.0 or breakdown["setup_quality"] <= 0.0:
        forced_reject = True
    if stop_distance <= 0.0:
        forced_reject = True
    if rr < float(risk_cfg["min_rr_for_alert"]):
        forced_reject = True
    if breakdown["entry_quality"] <= 0.0:
        forced_reject = True
    if breakdown["stop_quality"] <= 0.0:
        forced_reject = True

    score = round(sum(breakdown.values()), 2)
    decision = "reject"
    if not forced_reject:
        if score >= float(thresholds["priority_from"]):
            decision = "alert_priority"
        elif score >= float(thresholds["live_from"]):
            decision = "alert"
        elif score >= float(thresholds["shadow_from"]):
            decision = "shadow"

    if forced_reject or score < float(thresholds["reject_below"]):
        decision = "reject"

    return {
        "score": min(score, float(config["scoring"]["max_score"])),
        "breakdown": breakdown,
        "decision": decision,
        "entry_low": entry_low,
        "entry_high": entry_high,
        "stop": stop,
        "target1": target1,
        "target2": target2,
        "rr": round(rr, 2),
        "validity_hours": int(setup["validity_hours"]),
        "invalidation_rule": setup["invalidation_rule"],
    }


def _regime_score(alignment: Any, weight: float) -> float:
    if alignment == "perfect":
        return weight
    if alignment == "compatible":
        return 12.0
    return 0.0


def _structure_score(alignment: Any, weight: float) -> float:
    if int(alignment) >= 3:
        return weight
    if int(alignment) == 2:
        return 14.0
    if int(alignment) == 1:
        return 8.0
    return 0.0


def _setup_quality_score(conditions_met: Any, weight: float) -> float:
    ratio = max(0.0, min(1.0, float(conditions_met)))
    if ratio >= 1.0:
        return weight
    if ratio <= 0.0:
        return 0.0
    return round(max(5.0, weight * ratio), 2)


def _location_score(location_quality: Any, confluence_count: Any, weight: float) -> float:
    confluence = int(confluence_count)
    if location_quality == "major" or confluence >= 3:
        return weight
    if location_quality == "secondary" or confluence >= 2:
        return 8.0
    if confluence >= 1:
        return 6.0
    return 2.0


def _momentum_score(
    direction: str,
    indicators_by_tf: dict[str, dict[str, Any]],
    features: dict[str, Any],
    config: dict[str, Any],
    weight: float,
) -> float:
    entry_tf = str(config["timeframes"]["entry"])
    setup_tf = str(config["timeframes"]["setup"])
    indicators_cfg = config["data"]["indicators"]
    entry_indicators = indicators_by_tf.get(entry_tf, {})
    setup_indicators = indicators_by_tf.get(setup_tf, {})
    rsi = float(entry_indicators.get("rsi", 50.0))
    adx = float(entry_indicators.get("adx", setup_indicators.get("adx", 20.0)))
    macd_cross = str(entry_indicators.get("macd_cross", "none"))
    macd_histogram = float(entry_indicators.get("macd_histogram", 0.0))
    oversold = float(indicators_cfg.get("rsi_oversold", 30.0))
    overbought = float(indicators_cfg.get("rsi_overbought", 70.0))
    divergence = bool(features.get("momentum_bonus")) or bool(
        entry_indicators.get("bullish_divergence" if direction == "long" else "bearish_divergence", False)
    )
    if divergence:
        return weight

    bullish_confirmation = direction == "long" and macd_cross == "bullish" and rsi >= oversold and macd_histogram >= 0.0
    bearish_confirmation = direction == "short" and macd_cross == "bearish" and rsi <= overbought and macd_histogram <= 0.0
    if bullish_confirmation or bearish_confirmation:
        return weight

    if direction == "long" and rsi > overbought:
        return 1.0
    if direction == "short" and rsi < oversold:
        return 1.0
    if 35.0 <= rsi <= 65.0 and adx >= 20.0:
        return 7.0
    return 5.0 if adx >= 18.0 else 2.0


def _volume_score(volume_ratio: float, weight: float) -> float:
    if volume_ratio >= 2.0:
        return weight
    if 1.2 <= volume_ratio < 2.0:
        return 6.0
    return 2.0


def _entry_quality_score(entry_low: float, entry_high: float, atr: float, weight: float) -> float:
    width = entry_high - entry_low
    if atr <= 0.0:
        return 0.0
    width_ratio = width / atr
    if width_ratio < 0.15:
        return weight
    if width_ratio <= 0.25:
        return 3.0
    return 0.0


def _stop_quality_score(clear_stop: bool, stop_distance: float, atr: float, weight: float) -> float:
    if not clear_stop or atr <= 0.0:
        return 0.0
    distance_ratio = stop_distance / atr
    if 0.8 <= distance_ratio <= 2.2:
        return weight
    if 0.5 <= distance_ratio < 0.8 or 2.2 < distance_ratio <= 2.5:
        return 3.0
    return 0.0


def _rr_quality_score(rr: float, weight: float) -> float:
    if rr >= 2.5:
        return weight
    if rr >= 2.0:
        return 4.0
    return 0.0


def _target(entry_mid: float, stop_distance: float, direction: str, multiple: float) -> float:
    if direction == "long":
        return round(entry_mid + (stop_distance * multiple), 6)
    return round(entry_mid - (stop_distance * multiple), 6)
