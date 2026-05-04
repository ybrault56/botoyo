"""Market regime classification for BotYo."""

from __future__ import annotations

from typing import Any


REGIMES_AUTORISES_ALERTE = {"bull_trend", "bear_trend", "range"}
REGIMES_INTERDITS_ALERTE = {"high_volatility_noise", "low_quality_market"}


def classify_regime(indicators_trend: dict[str, Any], indicators_setup: dict[str, Any], config: dict[str, Any]) -> str:
    """Classify the market regime from trend and setup timeframes."""

    regime_config = config["regime"]

    close_trend = float(indicators_trend.get("close", 0.0))
    adx_trend = float(indicators_trend.get("adx", 0.0))
    ema20_trend = float(indicators_trend.get("ema20", 0.0))
    ema50_trend = float(indicators_trend.get("ema50", 0.0))
    ema200_trend = float(indicators_trend.get("ema200", 0.0))

    close_setup = float(indicators_setup.get("close", close_trend))
    atr_setup = float(indicators_setup.get("atr", 0.0))
    wick_ratio = float(indicators_setup.get("wick_ratio", 0.0))
    repeated_wick_ratio = float(indicators_setup.get("repeated_wick_ratio", wick_ratio))
    volume_ratio = float(indicators_setup.get("volume_ratio", 1.0))
    mean_volume_ratio = float(indicators_setup.get("mean_volume_ratio", volume_ratio))
    range_atr_ratio = float(indicators_setup.get("range_atr_ratio", 1.0))
    clear_stop = bool(indicators_setup.get("clear_stop", True))

    structure_trend = _normalize_structure(indicators_trend.get("structure"))
    structure_setup = _normalize_structure(indicators_setup.get("structure"))
    price = close_setup if close_setup > 0 else close_trend
    atr_price_ratio = 0.0 if price <= 0 else atr_setup / price
    contradictory_structure = (
        structure_trend in {"bullish", "bearish"}
        and structure_setup in {"bullish", "bearish"}
        and structure_trend != structure_setup
    )

    if (
        atr_price_ratio > float(regime_config["high_volatility_noise"]["atr_price_ratio_max"])
        or repeated_wick_ratio > float(regime_config["high_volatility_noise"]["wick_ratio_max"])
        or contradictory_structure
    ):
        return "high_volatility_noise"

    min_volume_ratio = float(regime_config["low_quality_market"]["min_volume_ratio"])
    volume_is_weak = volume_ratio < min_volume_ratio and mean_volume_ratio < min_volume_ratio
    if volume_is_weak or range_atr_ratio < float(regime_config["low_quality_market"]["min_range_atr_ratio"]) or not clear_stop:
        return "low_quality_market"

    bull_cfg = regime_config["bull_trend"]
    if (
        ema20_trend >= ema50_trend > ema200_trend
        and close_trend > ema50_trend
        and close_trend > ema200_trend
        and adx_trend >= float(bull_cfg["adx_min"])
        and structure_trend in {"bullish", "range", "neutral"}
        and structure_setup != "bearish"
    ):
        return "bull_trend"

    bear_cfg = regime_config["bear_trend"]
    if (
        ema20_trend <= ema50_trend < ema200_trend
        and close_trend < ema50_trend
        and close_trend < ema200_trend
        and adx_trend >= float(bear_cfg["adx_min"])
        and structure_trend in {"bearish", "range", "neutral"}
        and structure_setup != "bullish"
    ):
        return "bear_trend"

    if (
        adx_trend < float(regime_config["range"]["adx_max"])
        and structure_trend in {"neutral", "range"}
    ):
        return "range"

    return "high_volatility_noise" if contradictory_structure else "low_quality_market"


def diagnose_regime(indicators_trend: dict[str, Any], indicators_setup: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Expose the regime decision path and its current blockers."""

    regime = classify_regime(indicators_trend, indicators_setup, config)
    regime_config = config["regime"]

    close_trend = float(indicators_trend.get("close", 0.0))
    adx_trend = float(indicators_trend.get("adx", 0.0))
    ema20_trend = float(indicators_trend.get("ema20", 0.0))
    ema50_trend = float(indicators_trend.get("ema50", 0.0))
    ema200_trend = float(indicators_trend.get("ema200", 0.0))
    close_setup = float(indicators_setup.get("close", close_trend))
    atr_setup = float(indicators_setup.get("atr", 0.0))
    wick_ratio = float(indicators_setup.get("wick_ratio", 0.0))
    repeated_wick_ratio = float(indicators_setup.get("repeated_wick_ratio", wick_ratio))
    volume_ratio = float(indicators_setup.get("volume_ratio", 1.0))
    mean_volume_ratio = float(indicators_setup.get("mean_volume_ratio", volume_ratio))
    range_atr_ratio = float(indicators_setup.get("range_atr_ratio", 1.0))
    clear_stop = bool(indicators_setup.get("clear_stop", True))
    structure_trend = _normalize_structure(indicators_trend.get("structure"))
    structure_setup = _normalize_structure(indicators_setup.get("structure"))
    price = close_setup if close_setup > 0 else close_trend
    atr_price_ratio = 0.0 if price <= 0 else atr_setup / price
    contradictory_structure = (
        structure_trend in {"bullish", "bearish"}
        and structure_setup in {"bullish", "bearish"}
        and structure_trend != structure_setup
    )
    min_volume_ratio = float(regime_config["low_quality_market"]["min_volume_ratio"])

    high_volatility_reasons: list[str] = []
    if atr_price_ratio > float(regime_config["high_volatility_noise"]["atr_price_ratio_max"]):
        high_volatility_reasons.append("ATR 1H / prix trop eleve")
    if repeated_wick_ratio > float(regime_config["high_volatility_noise"]["wick_ratio_max"]):
        high_volatility_reasons.append("meches repetees trop larges")
    if contradictory_structure:
        high_volatility_reasons.append("structure 4H / 1H contradictoire")

    low_quality_reasons: list[str] = []
    if volume_ratio < min_volume_ratio and mean_volume_ratio < min_volume_ratio:
        low_quality_reasons.append("volume faible persistant")
    if range_atr_ratio < float(regime_config["low_quality_market"]["min_range_atr_ratio"]):
        low_quality_reasons.append("compression trop faible")
    if not clear_stop:
        low_quality_reasons.append("stop technique peu clair")

    bull_blockers: list[str] = []
    if not (ema20_trend >= ema50_trend > ema200_trend):
        bull_blockers.append("EMA 4H non alignees pour un bull trend")
    if not (close_trend > ema50_trend and close_trend > ema200_trend):
        bull_blockers.append("prix 4H pas au-dessus EMA50 et EMA200")
    if adx_trend < float(regime_config["bull_trend"]["adx_min"]):
        bull_blockers.append("ADX 4H trop faible pour un bull trend")
    if structure_trend not in {"bullish", "range", "neutral"}:
        bull_blockers.append("structure 4H trop baissiere pour un bull trend")
    if structure_setup == "bearish":
        bull_blockers.append("structure 1H baissiere")

    bear_blockers: list[str] = []
    if not (ema20_trend <= ema50_trend < ema200_trend):
        bear_blockers.append("EMA 4H non alignees pour un bear trend")
    if not (close_trend < ema50_trend and close_trend < ema200_trend):
        bear_blockers.append("prix 4H pas sous EMA50 et EMA200")
    if adx_trend < float(regime_config["bear_trend"]["adx_min"]):
        bear_blockers.append("ADX 4H trop faible pour un bear trend")
    if structure_trend not in {"bearish", "range", "neutral"}:
        bear_blockers.append("structure 4H trop haussiere pour un bear trend")
    if structure_setup == "bullish":
        bear_blockers.append("structure 1H haussiere")

    range_blockers: list[str] = []
    if adx_trend >= float(regime_config["range"]["adx_max"]):
        range_blockers.append("ADX 4H trop eleve pour un range")
    if structure_trend not in {"neutral", "range"}:
        range_blockers.append("structure 4H encore directionnelle")
    if ema20_trend >= ema50_trend > ema200_trend and close_trend > ema50_trend:
        bias = "bullish"
    elif ema20_trend <= ema50_trend < ema200_trend and close_trend < ema50_trend:
        bias = "bearish"
    else:
        bias = "neutral"

    blockers = {
        "high_volatility_noise": high_volatility_reasons,
        "low_quality_market": low_quality_reasons or (bull_blockers if bias == "bullish" else bear_blockers if bias == "bearish" else range_blockers),
        "bull_trend": [],
        "bear_trend": [],
        "range": [],
    }.get(regime, [])

    summary_map = {
        "bull_trend": "Bull trend valide",
        "bear_trend": "Bear trend valide",
        "range": "Range exploitable",
        "high_volatility_noise": "Bruit / volatilite trop eleves",
        "low_quality_market": "Marche non propre pour un setup V1",
    }

    return {
        "regime": regime,
        "summary": summary_map.get(regime, regime),
        "bias": bias,
        "trend_structure": structure_trend,
        "setup_structure": structure_setup,
        "trend_adx": round(adx_trend, 2),
        "volume_ratio": round(volume_ratio, 2),
        "mean_volume_ratio": round(mean_volume_ratio, 2),
        "range_atr_ratio": round(range_atr_ratio, 2),
        "atr_price_ratio": round(atr_price_ratio, 4),
        "repeated_wick_ratio": round(repeated_wick_ratio, 2),
        "clear_stop": clear_stop,
        "blockers": blockers[:4],
    }


def _normalize_structure(raw: Any) -> str:
    if raw is None:
        return "neutral"
    normalized = str(raw).strip().lower()
    if normalized in {"hh_hl", "bullish", "uptrend"}:
        return "bullish"
    if normalized in {"lh_ll", "bearish", "downtrend"}:
        return "bearish"
    if normalized in {"range", "neutral", "sideways"}:
        return normalized
    return "neutral"
