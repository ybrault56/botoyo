"""Signal presentation helpers for BotYo web routes."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from app.utils.json import loads
from app.web.i18n import DEFAULT_LANG, translate

_BREAKDOWN_LABELS = (
    ("regime", "Regime"),
    ("structure", "Structure"),
    ("setup_quality", "Setup"),
    ("location", "Location"),
    ("momentum", "Momentum"),
    ("volume", "Volume"),
    ("entry_quality", "Entry"),
    ("stop_quality", "Stop"),
    ("rr_quality", "R/R"),
)


def _t(key: str, lang: str) -> str:
    return translate(key, lang)


def present_signals(signals: list[dict[str, Any]], *, lang: str = DEFAULT_LANG) -> list[dict[str, Any]]:
    """Normalize signal payloads for dashboard and journal templates."""

    return [present_signal(signal, lang=lang) for signal in signals]


def present_diagnostics(diagnostics: list[dict[str, Any]], *, lang: str = DEFAULT_LANG) -> list[dict[str, Any]]:
    """Normalize runtime diagnostic payloads for dashboard templates."""

    return [present_diagnostic(item, lang=lang) for item in diagnostics]


def present_whale_movements(
    alerts: list[dict[str, Any]],
    *,
    default_threshold: float,
    lang: str = DEFAULT_LANG,
) -> list[dict[str, Any]]:
    """Normalize wallet movement payloads for dashboard templates."""

    return [present_whale_movement(item, default_threshold=default_threshold, lang=lang) for item in alerts]


def present_whale_trend(
    alerts: list[dict[str, Any]],
    *,
    default_threshold: float,
    selected_asset: str,
    now_ts: int,
    lang: str = DEFAULT_LANG,
) -> dict[str, Any]:
    """Summarize the rolling 24h whale bias for the selected asset."""

    cutoff = int(now_ts) - 86_400
    recent_alerts = [
        alert
        for alert in alerts
        if int(alert.get("observed_at", 0) or 0) >= cutoff
        and (selected_asset == "ALL" or str(alert.get("symbol", "")).upper() == selected_asset)
    ]

    pump_usd = 0.0
    dump_usd = 0.0
    for alert in recent_alerts:
        metadata = _decode_metadata(alert)
        usd_amount = float(metadata.get("usd_amount", 0.0) or 0.0)
        signal = str(alert.get("signal", "")).upper()
        if signal == "PUMP":
            pump_usd += usd_amount
        elif signal == "DUMP":
            dump_usd += usd_amount

    net_usd = pump_usd - dump_usd
    if not recent_alerts:
        label = _t("dashboard.neutral_caps", lang)
        state_class = "trend-neutral"
        summary = _t("dashboard.no_whale_24h_summary", lang)
    elif net_usd >= 0:
        label = "PUMP"
        state_class = "trend-pump"
        summary = _t("dashboard.buyer_bias_24h", lang).format(amount=_format_usd_amount(abs(net_usd)))
    else:
        label = "DUMP"
        state_class = "trend-dump"
        summary = _t("dashboard.seller_bias_24h", lang).format(amount=_format_usd_amount(abs(net_usd)))

    selected_label = _t("dashboard.all", lang) if selected_asset == "ALL" else selected_asset
    return {
        "label": label,
        "state_class": state_class,
        "summary": summary,
        "selected_label": selected_label,
        "pump_usd_label": _format_usd_amount(pump_usd),
        "dump_usd_label": _format_usd_amount(dump_usd),
        "movement_count": len(recent_alerts),
    }


def present_signal(signal: Mapping[str, Any], *, lang: str = DEFAULT_LANG) -> dict[str, Any]:
    """Return a template-friendly signal payload with decoded features."""

    payload = dict(signal)
    features = _decode_features(payload)
    rr = float(payload.get("rr", 0.0) or 0.0)

    payload["features"] = features
    payload["setup_label"] = str(payload.get("setup_type", "")).replace("_", " ").title()
    payload["direction_label"] = str(payload.get("direction", "")).upper()
    payload["regime_label"] = str(payload.get("regime", "")).replace("_", " ").title()
    payload["status_label"] = str(payload.get("status", "")).replace("_", " ").title()
    payload["status_class"] = str(payload.get("status", "")).replace("_", "-")
    payload["probability_pct"] = round(float(payload["probability"]) * 100) if payload.get("probability") is not None else None
    payload["emitted_at_label"] = _format_timestamp(payload.get("emitted_at"))
    payload["expires_at_label"] = _format_timestamp(payload.get("expires_at"))
    payload["closed_at_label"] = _format_timestamp(payload.get("closed_at"))
    payload["entered_at_label"] = _format_timestamp(features.get("entered_at"))
    payload["structure_summary"] = _structure_summary(features)
    payload["location_badges"] = _location_badges(features)
    payload["momentum_badges"] = _momentum_badges(payload.get("direction"), features)
    payload["volume_summary"] = _volume_summary(features, lang=lang)
    payload["risk_summary"] = _risk_summary(rr, payload, features, lang=lang)
    payload["score_breakdown_rows"] = _score_breakdown_rows(features, rr)
    payload["confluence_summary"] = _confluence_summary(payload)
    payload["lifecycle_summary"] = _lifecycle_summary(payload, lang=lang)
    return payload


def present_diagnostic(diagnostic: Mapping[str, Any], *, lang: str = DEFAULT_LANG) -> dict[str, Any]:
    """Return one template-friendly diagnostic payload."""

    payload = dict(diagnostic)
    payload["regime_label"] = str(payload.get("regime", "")).replace("_", " ").title()
    payload["bias_label"] = str(payload.get("bias", "neutral")).replace("_", " ").title()
    payload["blockers"] = [str(item) for item in payload.get("blockers", [])]
    payload["metric_chips"] = [
        f"4H { _structure_label(payload.get('trend_structure')) or _t('dashboard.neutral', lang) }",
        f"1H { _structure_label(payload.get('setup_structure')) or _t('dashboard.neutral', lang) }",
        f"ADX {float(payload.get('trend_adx', 0.0)):.1f}",
        f"Vol {float(payload.get('volume_ratio', 0.0)):.2f}x",
        f"RSI {float(payload.get('entry_rsi', 0.0)):.1f}",
        f"MACD {str(payload.get('entry_macd_cross', 'none'))}",
    ]
    payload["setups"] = [
        {
            **dict(setup),
            "state_label": (
                _t("dashboard.ready", lang)
                if bool(setup.get("eligible", False))
                else _t("dashboard.blocked", lang)
            ),
            "state_class": "active" if bool(setup.get("eligible", False)) else "rejected",
            "direction_label": str(setup.get("direction", "")).upper() if setup.get("direction") else "-",
        }
        for setup in payload.get("setups", [])
    ]
    return payload


def present_whale_movement(alert: Mapping[str, Any], *, default_threshold: float, lang: str = DEFAULT_LANG) -> dict[str, Any]:
    """Return one template-friendly whale wallet movement payload."""

    payload = dict(alert)
    metadata = _decode_metadata(payload)
    usd_amount = float(metadata.get("usd_amount", 0.0) or 0.0)
    threshold = float(metadata.get("threshold", default_threshold) or default_threshold)
    threshold_state = str(metadata.get("threshold_state") or _threshold_state(usd_amount, threshold))
    state_map = {
        "above_threshold": (_t("dashboard.threshold_above", lang), "whale-red"),
        "near_threshold": (_t("dashboard.threshold_near", lang), "whale-orange"),
        "below_threshold": (_t("dashboard.threshold_below", lang), "whale-green"),
    }
    state_label, state_class = state_map.get(threshold_state, (_t("dashboard.threshold_below", lang), "whale-green"))

    payload["metadata"] = metadata
    payload["observed_at_label"] = _format_timestamp(payload.get("observed_at"))
    payload["state_label"] = state_label
    payload["state_class"] = state_class
    payload["symbol_label"] = str(payload.get("symbol", "")).upper()
    payload["signal_label"] = str(payload.get("signal", "")).upper()
    payload["wallet_label"] = str(metadata.get("label", payload.get("title", "Wallet")))
    payload["network"] = str(metadata.get("network", "-"))
    payload["direction_label"] = (
        _t("dashboard.inflow", lang)
        if str(metadata.get("direction", "")).lower() == "inflow"
        else _t("dashboard.outflow", lang)
    )
    payload["amount_label"] = _format_whale_amount(payload["symbol_label"], float(metadata.get("amount", 0.0) or 0.0))
    payload["usd_amount_label"] = _format_usd_amount(usd_amount)
    payload["threshold_label"] = _format_usd_amount(threshold)
    payload["tx_hash_short"] = str(metadata.get("tx_hash", ""))[:12]
    return payload


def _decode_features(signal: Mapping[str, Any]) -> dict[str, Any]:
    embedded = signal.get("features")
    if isinstance(embedded, Mapping):
        return dict(embedded)

    raw = signal.get("features_json")
    if not raw:
        return {}

    try:
        parsed = loads(raw)
    except Exception:
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _decode_metadata(alert: Mapping[str, Any]) -> dict[str, Any]:
    embedded = alert.get("metadata")
    if isinstance(embedded, Mapping):
        return dict(embedded)

    raw = alert.get("metadata_json")
    if not raw:
        return {}

    try:
        parsed = loads(raw)
    except Exception:
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _format_timestamp(value: Any) -> str:
    if value in {None, ""}:
        return "-"
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(int(value), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return str(value)


def _threshold_state(usd_amount: float, threshold: float) -> str:
    if usd_amount >= threshold:
        return "above_threshold"
    if usd_amount >= (threshold * 0.7):
        return "near_threshold"
    return "below_threshold"


def _format_whale_amount(symbol: str, amount: float) -> str:
    if symbol == "BTC":
        return f"{amount:,.4f} BTC"
    if symbol == "ETH":
        return f"{amount:,.2f} ETH"
    return f"{amount:,.0f} XRP"


def _format_usd_amount(amount: float) -> str:
    if amount >= 1_000_000_000:
        return f"${amount / 1_000_000_000:.2f}B"
    if amount >= 1_000_000:
        return f"${amount / 1_000_000:.2f}M"
    if amount >= 1_000:
        return f"${amount / 1_000:.0f}K"
    return f"${amount:.0f}"


def _structure_summary(features: Mapping[str, Any]) -> str:
    trend = _structure_label(features.get("trend_structure"))
    setup = _structure_label(features.get("setup_structure"))
    entry = _structure_label(features.get("entry_structure"))
    if trend or setup or entry:
        labels = [label for label in (trend, setup, entry) if label]
        return " -> ".join(labels)

    alignment = int(features.get("timeframe_alignment", 0) or 0)
    regime_alignment = str(features.get("regime_alignment", "neutral")).replace("_", " ")
    return f"{alignment}/3 aligned | {regime_alignment}"


def _structure_label(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    mapping = {
        "bullish": "Bullish",
        "bearish": "Bearish",
        "range": "Range",
        "hh_hl": "HH/HL",
        "lh_ll": "LH/LL",
    }
    return mapping.get(normalized, normalized.title() if normalized else "")


def _location_badges(features: Mapping[str, Any]) -> list[str]:
    badges: list[str] = []
    if bool(features.get("ema_zone", False)):
        badges.append("EMA20/50")
    if bool(features.get("fib_confluence", False)):
        badges.append("Fib 0.382-0.618")
    if bool(features.get("round_level_confluence", False)):
        badges.append("Round level")
    if bool(features.get("major_level_touch", False)):
        badges.append("Major level")
    if "range_low" in features or "range_high" in features:
        badges.append("Range boundary")
    if not badges:
        count = int(features.get("confluence_count", 0) or 0)
        if count > 0:
            badges.append(f"{count} confirmations")
    return badges


def _momentum_badges(direction: Any, features: Mapping[str, Any]) -> list[str]:
    side = str(direction or "long").lower()
    badges: list[str] = []
    divergence = bool(features.get("entry_bullish_divergence" if side == "long" else "entry_bearish_divergence", False))
    if divergence or bool(features.get("momentum_bonus", False)):
        badges.append("RSI divergence")
    if bool(features.get("rsi_rebound", False)) or bool(features.get(f"entry_rsi_rebound_{side}", False)):
        badges.append("RSI rebound")

    macd_cross = str(features.get("entry_macd_cross", "none"))
    if macd_cross in {"bullish", "bearish"}:
        badges.append(f"MACD {macd_cross}")
    if bool(features.get("structure_confirmed", False)):
        badges.append("Structure break")
    if bool(features.get("reversal_candle", False)):
        badges.append("Reversal candle")
    if not badges and features.get("entry_rsi") is not None:
        badges.append(f"RSI {float(features['entry_rsi']):.1f}")
    return badges


def _volume_summary(features: Mapping[str, Any], *, lang: str) -> str:
    volume_ratio = float(features.get("entry_volume_ratio", features.get("volume_ratio", 1.0)) or 1.0)
    if volume_ratio >= 2.0:
        label = _t("dashboard.volume_explosive", lang)
    elif volume_ratio >= 1.2:
        label = _t("dashboard.volume_confirmed", lang)
    elif volume_ratio >= 0.8:
        label = _t("dashboard.volume_neutral", lang)
    else:
        label = _t("dashboard.volume_thin", lang)
    return f"{volume_ratio:.2f}x | {label}"


def _risk_summary(rr: float, signal: Mapping[str, Any], features: Mapping[str, Any], *, lang: str) -> str:
    rr_score = _rr_quality_score(rr)
    wick_ratio = float(features.get("confirmation_wick_ratio", 0.0) or 0.0) * 100.0
    stop_label = (
        _t("dashboard.clear_stop", lang)
        if bool(features.get("clear_stop", False))
        else _t("dashboard.weak_stop", lang)
    )
    return (
        f"RR {rr:.1f} | score {rr_score:.1f}/5 | "
        f"T1 {float(signal.get('target1', 0.0)):.2f} | wick {wick_ratio:.0f}% | {stop_label}"
    )


def _score_breakdown_rows(features: Mapping[str, Any], rr: float) -> list[dict[str, str]]:
    raw_breakdown = features.get("score_breakdown")
    breakdown = dict(raw_breakdown) if isinstance(raw_breakdown, Mapping) else {}
    if "rr_quality" not in breakdown:
        breakdown["rr_quality"] = _rr_quality_score(rr)

    rows: list[dict[str, str]] = []
    for key, label in _BREAKDOWN_LABELS:
        if key not in breakdown:
            continue
        rows.append({"label": label, "value": f"{float(breakdown[key]):.1f}"})
    return rows


def _rr_quality_score(rr: float) -> float:
    if rr >= 2.5:
        return 5.0
    if rr >= 2.0:
        return 4.0
    if rr >= 1.8:
        return 2.0
    return 0.0


def _confluence_summary(signal: Mapping[str, Any]) -> str:
    features = signal["features"]
    count = int(features.get("confluence_count", 0) or 0)
    setup = str(signal.get("setup_label", "Signal"))
    if count > 0:
        return f"{setup} | {count} confluences | {signal['structure_summary']}"
    return f"{setup} | {signal['structure_summary']}"


def _lifecycle_summary(signal: Mapping[str, Any], *, lang: str) -> str:
    features = signal["features"]
    status = str(signal.get("status_label", "Unknown"))
    executed = _t("dashboard.yes", lang) if bool(features.get("executed", False)) else _t("dashboard.no", lang)
    resolved_reason = str(features.get("resolved_reason", signal.get("comment", "")) or "-")
    return (
        f"{_t('dashboard.lifecycle_status', lang)} {status} | "
        f"{_t('dashboard.lifecycle_executed', lang)} {executed} | "
        f"{_t('dashboard.lifecycle_resolution', lang)} {resolved_reason}"
    )
