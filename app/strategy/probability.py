"""Probability calibration engine and edge-performance diagnostics for BotYo."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Mapping

import numpy as np

from app.utils.json import loads

_OPEN_STATUSES = {"active", "entered"}
_DIRECTLY_EXECUTED_STATUSES = {"entered", "hit_t1", "hit_stop", "expired_after_entry"}
_RESOLVED_STATUSES = {
    "expired",
    "cancelled",
    "hit_t1",
    "hit_stop",
    "expired_without_entry",
    "expired_after_entry",
    "cancelled_regime_change",
}


@dataclass(slots=True)
class _IsotonicCalibrator:
    thresholds: np.ndarray
    values: np.ndarray

    def predict(self, score: float) -> float:
        return float(np.interp(score, self.thresholds, self.values))


class ProbabilityEngine:
    """Estimate calibrated probabilities for scored setups."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self._total_samples = 0
        self._samples_by_setup_direction: dict[tuple[str, str], int] = {}
        self._samples_by_asset_setup_direction: dict[tuple[str, str, str], int] = {}
        self._calibrators: dict[tuple[str, str], _IsotonicCalibrator] = {}
        self._global_calibrator: _IsotonicCalibrator | None = None
        self._edge_report: dict[str, Any] = _empty_edge_report()
        self._live_segment_keys: set[str] = set()

    def estimate(self, score: float, features: dict[str, Any], setup_type: str, direction: str) -> dict[str, Any]:
        """Estimate the probability for a setup score."""

        probability_cfg = self.config["probability"]
        normalized_score = max(0.0, min(1.0, score / 100.0))
        score_probability = _score_derived_shadow_probability(score, self.config)
        symbol = str(features.get("symbol", "UNKNOWN"))
        segment_key = self._format_key((symbol, setup_type, direction))
        setup_samples = self._samples_by_setup_direction.get((setup_type, direction), 0)
        asset_samples = self._samples_by_asset_setup_direction.get((symbol, setup_type, direction), 0)

        calibrator = self._calibrators.get((setup_type, direction), self._global_calibrator)
        calibrated_probability = calibrator.predict(normalized_score) if calibrator is not None else score_probability
        calibrated_probability = max(0.0, min(1.0, calibrated_probability))

        has_live_samples = (
            self._total_samples >= int(probability_cfg["min_total_samples_for_live"])
            and setup_samples >= int(probability_cfg["min_samples_per_setup_direction"])
            and asset_samples >= int(probability_cfg["min_samples_per_asset_setup_direction"])
        )
        strict_live_ready = bool(self._edge_report["live_requirements"]["eligible"])
        segment_live_eligible = segment_key in self._live_segment_keys

        if not has_live_samples or not strict_live_ready:
            probability, calibration_weight = _pre_live_probability(
                score_probability=score_probability,
                calibrated_probability=calibrated_probability,
                calibrated_available=calibrator is not None,
                total_samples=self._total_samples,
                setup_samples=setup_samples,
                asset_samples=asset_samples,
                config=self.config,
            )
            mode = "shadow" if probability >= float(probability_cfg["probability_threshold_shadow"]) else "reject"
            return {
                "probability": probability,
                "mode": mode,
                "calibrated": calibrator is not None,
                "calibration_weight": calibration_weight,
                "segment_live_eligible": False,
                "strict_live_ready": strict_live_ready,
            }

        probability = calibrated_probability
        if probability >= float(probability_cfg["probability_threshold_live"]) and segment_live_eligible:
            mode = "live"
        elif probability >= float(probability_cfg["probability_threshold_shadow"]):
            mode = "shadow"
        else:
            mode = "reject"

        return {
            "probability": probability,
            "mode": mode,
            "calibrated": calibrator is not None,
            "calibration_weight": 1.0 if calibrator is not None else 0.0,
            "segment_live_eligible": segment_live_eligible,
            "strict_live_ready": strict_live_ready,
        }

    def recalibrate(self, signals_history: list[dict[str, Any]]) -> None:
        """Rebuild the global and per-setup isotonic calibrators from executable resolved signals."""

        records = _order_and_dedupe_records([_build_signal_record(signal, self.config) for signal in signals_history])
        calibration_records = [record for record in records if record["calibration_candidate"]]
        self._total_samples = len(calibration_records)
        min_global_samples = _min_global_calibration_samples(self.config)
        min_setup_samples = _min_setup_calibration_samples(self.config)

        setup_counts: dict[tuple[str, str], int] = defaultdict(int)
        asset_counts: dict[tuple[str, str, str], int] = defaultdict(int)
        grouped_scores: dict[tuple[str, str], list[float]] = defaultdict(list)
        grouped_labels: dict[tuple[str, str], list[float]] = defaultdict(list)
        global_scores: list[float] = []
        global_labels: list[float] = []

        for record in calibration_records:
            setup_key = (record["setup_type"], record["direction"])
            asset_key = (record["symbol"], record["setup_type"], record["direction"])
            setup_counts[setup_key] += 1
            asset_counts[asset_key] += 1
            grouped_scores[setup_key].append(record["score_norm"])
            grouped_labels[setup_key].append(record["label"])
            global_scores.append(record["score_norm"])
            global_labels.append(record["label"])

        self._samples_by_setup_direction = dict(setup_counts)
        self._samples_by_asset_setup_direction = dict(asset_counts)
        self._global_calibrator = (
            _fit_isotonic(global_scores, global_labels)
            if _can_fit_calibrator(global_scores, global_labels, min_global_samples)
            else None
        )
        self._calibrators = {
            key: _fit_isotonic(grouped_scores[key], grouped_labels[key])
            for key in grouped_scores
            if _can_fit_calibrator(grouped_scores[key], grouped_labels[key], min_setup_samples)
        }

        live_counts_ready = _live_counts_ready(
            total_samples=len(calibration_records),
            samples_by_setup_direction=self._samples_by_setup_direction,
            samples_by_asset_setup_direction=self._samples_by_asset_setup_direction,
            config=self.config,
        )
        records = _with_model_probabilities(
            records=records,
            config=self.config,
            global_calibrator=self._global_calibrator,
            calibrators=self._calibrators,
            samples_by_setup_direction=self._samples_by_setup_direction,
            samples_by_asset_setup_direction=self._samples_by_asset_setup_direction,
            live_counts_ready=live_counts_ready,
        )
        calibration_records = [record for record in records if record["calibration_candidate"]]

        self._edge_report = _build_edge_report(
            records=records,
            calibration_records=calibration_records,
            config=self.config,
            samples_by_setup_direction=self._samples_by_setup_direction,
            samples_by_asset_setup_direction=self._samples_by_asset_setup_direction,
        )
        self._live_segment_keys = {
            str(item["key"])
            for item in self._edge_report["recommended_segments"]
            if bool(item.get("eligible", False))
        }

    def get_sample_counts(self) -> dict[str, Any]:
        """Return executable resolved counts for the admin UI."""

        return {
            "total": self._total_samples,
            "by_setup_direction": {self._format_key(key): value for key, value in self._samples_by_setup_direction.items()},
            "by_asset_setup_direction": {
                self._format_key(key): value
                for key, value in self._samples_by_asset_setup_direction.items()
            },
        }

    def get_edge_report(self) -> dict[str, Any]:
        """Return detailed edge-performance diagnostics."""

        return dict(self._edge_report)

    def get_live_activation_status(self) -> dict[str, Any]:
        """Return the current live activation readiness state."""

        probability_cfg = self.config["probability"]
        required_total = int(probability_cfg["min_total_samples_for_live"])
        required_setup = int(probability_cfg["min_samples_per_setup_direction"])
        required_asset = int(probability_cfg["min_samples_per_asset_setup_direction"])

        best_setup_key, best_setup_count = self._best_count(self._samples_by_setup_direction)
        best_asset_key, best_asset_count = self._best_count(self._samples_by_asset_setup_direction)
        edge_live = self._edge_report["live_requirements"]

        eligible = (
            self._total_samples >= required_total
            and best_setup_count >= required_setup
            and best_asset_count >= required_asset
            and bool(edge_live["eligible"])
        )

        return {
            "eligible": eligible,
            "required": {
                "total": required_total,
                "setup_direction": required_setup,
                "asset_setup_direction": required_asset,
            },
            "current": {
                "total": self._total_samples,
                "best_setup_direction": {
                    "key": self._format_key(best_setup_key),
                    "count": best_setup_count,
                },
                "best_asset_setup_direction": {
                    "key": self._format_key(best_asset_key),
                    "count": best_asset_count,
                },
            },
            "remaining": {
                "total": max(required_total - self._total_samples, 0),
                "setup_direction": max(required_setup - best_setup_count, 0),
                "asset_setup_direction": max(required_asset - best_asset_count, 0),
            },
            "samples": self.get_sample_counts(),
            "performance": self._edge_report["performance"],
            "walk_forward": self._edge_report["walk_forward"],
            "live_requirements": edge_live,
            "segments": list(self._edge_report["segments"]),
            "recommended_segments": list(self._edge_report["recommended_segments"]),
        }

    @staticmethod
    def _best_count(counts: dict[Any, int]) -> tuple[Any, int]:
        if not counts:
            return "", 0
        return max(counts.items(), key=lambda item: item[1])

    @staticmethod
    def _format_key(key: Any) -> str:
        if isinstance(key, tuple):
            return ":".join(str(part) for part in key)
        return str(key)


def _build_signal_record(signal: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    features = _decode_features(signal)
    status = str(signal.get("status", "")).strip()
    executed = bool(features.get("executed", False)) or status in _DIRECTLY_EXECUTED_STATUSES
    resolved = status in _RESOLVED_STATUSES
    calibration_candidate = resolved and executed
    score = float(signal.get("score", 0.0))
    score_norm = max(0.0, min(1.0, score / 100.0))
    score_probability = _score_derived_shadow_probability(score, config)
    probability = signal.get("probability")
    probability_value = _coerce_probability(probability, default=score_probability)
    result_r = signal.get("result_r")
    if result_r is None:
        if status == "hit_t1":
            result_r = float(config["risk"]["targets"]["t1_r"])
        elif status == "hit_stop":
            result_r = -1.0
    return {
        "id": str(signal.get("id", "")),
        "symbol": str(signal.get("symbol", "UNKNOWN")),
        "setup_type": str(signal.get("setup_type", "unknown")),
        "direction": str(signal.get("direction", "unknown")),
        "regime": str(signal.get("regime", "unknown")),
        "status": status,
        "score": score,
        "score_norm": score_norm,
        "probability": probability_value,
        "score_probability": score_probability,
        "model_probability": probability_value,
        "emitted_at": int(signal.get("emitted_at", 0) or 0),
        "event_time_key": _signal_event_time_key(signal, features, config),
        "executed": executed,
        "resolved": resolved,
        "accepted": status != "rejected",
        "calibration_candidate": calibration_candidate,
        "label": 1.0 if status == "hit_t1" else 0.0,
        "result_r": float(result_r) if result_r is not None else None,
        "features": features,
    }


def _order_and_dedupe_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(
        records,
        key=lambda record: (int(record["event_time_key"]), int(record["emitted_at"]), str(record["id"])),
    )
    deduped: dict[tuple[str, str, str, int], dict[str, Any]] = {}
    for record in ordered:
        key = (
            str(record["symbol"]),
            str(record["setup_type"]),
            str(record["direction"]),
            int(record["event_time_key"]),
        )
        current = deduped.get(key)
        if current is None or _record_quality(record) >= _record_quality(current):
            deduped[key] = record
    return sorted(
        deduped.values(),
        key=lambda record: (int(record["event_time_key"]), int(record["emitted_at"]), str(record["id"])),
    )


def _record_quality(record: dict[str, Any]) -> tuple[int, int, int, int, int, int]:
    return (
        int(bool(record["calibration_candidate"])),
        int(bool(record["resolved"])),
        int(bool(record["executed"])),
        int(bool(record["accepted"])),
        int(record["result_r"] is not None),
        _status_rank(str(record["status"])),
    )


def _status_rank(status: str) -> int:
    ranking = {
        "hit_t1": 6,
        "hit_stop": 5,
        "expired_after_entry": 4,
        "expired_without_entry": 3,
        "cancelled_regime_change": 2,
        "expired": 2,
        "cancelled": 2,
        "entered": 1,
        "active": 1,
        "rejected": 0,
    }
    return ranking.get(status, 0)


def _signal_event_time_key(signal: Mapping[str, Any], features: Mapping[str, Any], config: dict[str, Any]) -> int:
    emitted_at = int(signal.get("emitted_at", 0) or 0)
    close_time = features.get("signal_close_time")
    if close_time is not None:
        return int(close_time)
    open_time = features.get("signal_open_time")
    interval_seconds = int(features.get("entry_interval_seconds", 0) or 0)
    if open_time is not None and interval_seconds > 0:
        return int(open_time) + interval_seconds
    if interval_seconds <= 0:
        entry_tf = str(config.get("timeframes", {}).get("entry", "15M"))
        interval_seconds = {"15M": 900, "1H": 3600, "4H": 14400}.get(entry_tf, 900)
    return emitted_at - (emitted_at % interval_seconds)


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


def _coerce_probability(value: Any, *, default: float) -> float:
    if value is None:
        return max(0.0, min(1.0, default))
    probability = float(value)
    if probability > 1.0:
        probability /= 100.0
    return max(0.0, min(1.0, probability))


def _score_derived_shadow_probability(score: float, config: Mapping[str, Any]) -> float:
    probability_cfg = config["probability"]
    scoring_cfg = config.get("scoring", {}) if isinstance(config, Mapping) else {}
    thresholds = scoring_cfg.get("thresholds", {}) if isinstance(scoring_cfg, Mapping) else {}
    shadow_score = float(thresholds.get("shadow_from", 63.0)) / 100.0
    shadow_score = max(0.01, min(0.99, shadow_score))
    score_norm = max(0.0, min(1.0, float(score) / 100.0))
    shadow_threshold = max(0.0, min(1.0, float(probability_cfg["probability_threshold_shadow"])))
    live_threshold = max(0.0, min(1.0, float(probability_cfg["probability_threshold_live"])))
    floor = max(0.0, min(shadow_threshold, float(probability_cfg.get("shadow_probability_floor", 0.50))))
    cap_default = max(shadow_threshold, live_threshold - 0.01)
    cap = max(shadow_threshold, min(1.0, float(probability_cfg.get("shadow_probability_cap", cap_default))))

    if score_norm <= shadow_score:
        ratio = score_norm / shadow_score
        probability = floor + (ratio * (shadow_threshold - floor))
    else:
        ratio = (score_norm - shadow_score) / max(1.0 - shadow_score, 1e-9)
        probability = shadow_threshold + (ratio * (cap - shadow_threshold))
    return max(0.0, min(1.0, probability))


def _pre_live_probability(
    *,
    score_probability: float,
    calibrated_probability: float,
    calibrated_available: bool,
    total_samples: int,
    setup_samples: int,
    asset_samples: int,
    config: Mapping[str, Any],
) -> tuple[float, float]:
    if not calibrated_available:
        return max(0.0, min(1.0, score_probability)), 0.0

    probability_cfg = config["probability"]
    readiness = min(
        total_samples / max(int(probability_cfg["min_total_samples_for_live"]), 1),
        setup_samples / max(int(probability_cfg["min_samples_per_setup_direction"]), 1),
        asset_samples / max(int(probability_cfg["min_samples_per_asset_setup_direction"]), 1),
    )
    max_weight = max(0.0, min(1.0, float(probability_cfg.get("pre_live_calibration_weight_max", 0.35))))
    weight = max(0.0, min(max_weight, readiness))
    probability = score_probability + ((calibrated_probability - score_probability) * weight)
    if score_probability >= float(probability_cfg["probability_threshold_shadow"]):
        probability = max(probability, float(probability_cfg["probability_threshold_shadow"]))
    return max(0.0, min(1.0, probability)), weight


def _live_counts_ready(
    *,
    total_samples: int,
    samples_by_setup_direction: Mapping[tuple[str, str], int],
    samples_by_asset_setup_direction: Mapping[tuple[str, str, str], int],
    config: Mapping[str, Any],
) -> bool:
    probability_cfg = config["probability"]
    best_setup_count = max(samples_by_setup_direction.values()) if samples_by_setup_direction else 0
    best_asset_count = max(samples_by_asset_setup_direction.values()) if samples_by_asset_setup_direction else 0
    return (
        total_samples >= int(probability_cfg["min_total_samples_for_live"])
        and best_setup_count >= int(probability_cfg["min_samples_per_setup_direction"])
        and best_asset_count >= int(probability_cfg["min_samples_per_asset_setup_direction"])
    )


def _with_model_probabilities(
    *,
    records: list[dict[str, Any]],
    config: Mapping[str, Any],
    global_calibrator: _IsotonicCalibrator | None,
    calibrators: Mapping[tuple[str, str], _IsotonicCalibrator],
    samples_by_setup_direction: Mapping[tuple[str, str], int],
    samples_by_asset_setup_direction: Mapping[tuple[str, str, str], int],
    live_counts_ready: bool,
) -> list[dict[str, Any]]:
    annotated: list[dict[str, Any]] = []
    for record in records:
        setup_key = (str(record["setup_type"]), str(record["direction"]))
        asset_key = (str(record["symbol"]), str(record["setup_type"]), str(record["direction"]))
        calibrator = calibrators.get(setup_key, global_calibrator)
        calibrated_probability = (
            max(0.0, min(1.0, calibrator.predict(float(record["score_norm"]))))
            if calibrator is not None
            else float(record["score_probability"])
        )
        if live_counts_ready and calibrator is not None:
            model_probability = calibrated_probability
        else:
            model_probability, _ = _pre_live_probability(
                score_probability=float(record["score_probability"]),
                calibrated_probability=calibrated_probability,
                calibrated_available=calibrator is not None,
                total_samples=sum(samples_by_setup_direction.values()),
                setup_samples=int(samples_by_setup_direction.get(setup_key, 0)),
                asset_samples=int(samples_by_asset_setup_direction.get(asset_key, 0)),
                config=config,
            )
        annotated.append({**record, "model_probability": model_probability})
    return annotated


def _build_edge_report(
    *,
    records: list[dict[str, Any]],
    calibration_records: list[dict[str, Any]],
    config: dict[str, Any],
    samples_by_setup_direction: dict[tuple[str, str], int],
    samples_by_asset_setup_direction: dict[tuple[str, str, str], int],
) -> dict[str, Any]:
    probability_cfg = config["probability"]
    live_cfg = dict(probability_cfg.get("live_requirements", {}))
    recent_window = int(live_cfg.get("recent_window", 30))
    min_segment_expectancy = float(live_cfg.get("min_segment_expectancy", 0.0))
    min_global_expectancy = float(live_cfg.get("min_global_expectancy", 0.0))
    min_recent_expectancy = float(live_cfg.get("min_recent_expectancy", 0.0))
    min_recent_win_rate = float(live_cfg.get("min_recent_win_rate", 0.0))
    max_non_execution_rate = float(live_cfg.get("max_non_execution_rate", 100.0))
    max_drawdown_r = float(live_cfg.get("max_drawdown_r", 999.0))
    max_recent_calibration_gap = float(live_cfg.get("max_recent_calibration_gap", 1.0))
    min_walk_forward_expectancy = float(live_cfg.get("min_walk_forward_expectancy", -999.0))
    max_walk_forward_brier = float(live_cfg.get("max_walk_forward_brier", 1.0))
    top_segments_live = int(live_cfg.get("top_segments_live", 4))

    resolved_records = [record for record in records if record["resolved"] and record["accepted"]]
    executed_records = [record for record in calibration_records if record["result_r"] is not None]
    recent_records = executed_records[-recent_window:] if recent_window > 0 else executed_records
    walk_forward = _walk_forward_summary(executed_records, config)
    performance = _performance_summary(records, resolved_records, executed_records, recent_records)

    counts_ready = (
        len(executed_records) >= int(probability_cfg["min_total_samples_for_live"])
        and (max(samples_by_setup_direction.values()) if samples_by_setup_direction else 0)
        >= int(probability_cfg["min_samples_per_setup_direction"])
        and (max(samples_by_asset_setup_direction.values()) if samples_by_asset_setup_direction else 0)
        >= int(probability_cfg["min_samples_per_asset_setup_direction"])
    )
    strict_ready = (
        counts_ready
        and performance["expectancy"] >= min_global_expectancy
        and performance["recent_expectancy"] >= min_recent_expectancy
        and performance["recent_win_rate"] >= min_recent_win_rate
        and performance["non_execution_rate"] <= max_non_execution_rate
        and performance["drawdown_r"] <= max_drawdown_r
        and performance["recent_calibration_gap"] <= max_recent_calibration_gap
        and walk_forward["ready"]
        and walk_forward["average_live_expectancy"] >= min_walk_forward_expectancy
        and walk_forward["average_brier"] <= max_walk_forward_brier
    )

    segment_rows = _segment_rows(records, executed_records, recent_window)
    eligible_segments = [
        {
            **row,
            "eligible": (
                row["resolved_executed"] >= int(probability_cfg["min_samples_per_asset_setup_direction"])
                and row["expectancy"] >= min_segment_expectancy
                and row["recent_expectancy"] >= min_recent_expectancy
                and row["non_execution_rate"] <= max_non_execution_rate
                and row["recent_calibration_gap"] <= max_recent_calibration_gap
            ),
        }
        for row in segment_rows
    ]
    eligible_segments.sort(
        key=lambda row: (
            not row["eligible"],
            -row["expectancy"],
            -row["win_rate"],
            -row["resolved_executed"],
        )
    )

    return {
        "performance": performance,
        "walk_forward": walk_forward,
        "segments": eligible_segments,
        "live_requirements": {
            "eligible": strict_ready,
            "counts_ready": counts_ready,
            "min_global_expectancy": min_global_expectancy,
            "min_recent_expectancy": min_recent_expectancy,
            "min_recent_win_rate": min_recent_win_rate,
            "max_non_execution_rate": max_non_execution_rate,
            "max_drawdown_r": max_drawdown_r,
            "max_recent_calibration_gap": max_recent_calibration_gap,
            "min_walk_forward_expectancy": min_walk_forward_expectancy,
            "max_walk_forward_brier": max_walk_forward_brier,
        },
        "recommended_segments": eligible_segments[:top_segments_live],
    }


def _performance_summary(
    records: list[dict[str, Any]],
    resolved_records: list[dict[str, Any]],
    executed_records: list[dict[str, Any]],
    recent_records: list[dict[str, Any]],
) -> dict[str, Any]:
    resolved_executed = len(executed_records)
    wins = sum(1 for record in executed_records if record["status"] == "hit_t1")
    result_rs = [float(record["result_r"]) for record in executed_records if record["result_r"] is not None]
    recent_rs = [float(record["result_r"]) for record in recent_records if record["result_r"] is not None]
    recent_wins = sum(1 for record in recent_records if record["status"] == "hit_t1")
    avg_probability = (
        float(np.mean([record["model_probability"] for record in executed_records]))
        if executed_records
        else 0.0
    )
    recent_avg_probability = (
        float(np.mean([record["model_probability"] for record in recent_records]))
        if recent_records
        else 0.0
    )
    stored_avg_probability = float(np.mean([record["probability"] for record in executed_records])) if executed_records else 0.0
    stored_recent_avg_probability = (
        float(np.mean([record["probability"] for record in recent_records]))
        if recent_records
        else 0.0
    )
    non_execution_count = sum(1 for record in resolved_records if not record["executed"])
    expectancy = (sum(result_rs) / len(result_rs)) if result_rs else 0.0
    recent_expectancy = (sum(recent_rs) / len(recent_rs)) if recent_rs else 0.0
    win_rate = (wins / resolved_executed) * 100 if resolved_executed else 0.0
    recent_win_rate = (recent_wins / len(recent_records)) * 100 if recent_records else 0.0

    return {
        "total_signals": len(records),
        "resolved_signals": len(resolved_records),
        "resolved_executed": resolved_executed,
        "wins": wins,
        "win_rate": round(win_rate, 2),
        "expectancy": round(expectancy, 4),
        "recent_expectancy": round(recent_expectancy, 4),
        "recent_win_rate": round(recent_win_rate, 2),
        "non_execution_rate": round((non_execution_count / len(resolved_records)) * 100, 2) if resolved_records else 0.0,
        "drawdown_r": round(_drawdown_r(result_rs), 4),
        "average_probability": round(avg_probability, 4),
        "recent_average_probability": round(recent_avg_probability, 4),
        "recent_calibration_gap": round(abs(recent_avg_probability - (recent_win_rate / 100.0)), 4),
        "stored_average_probability": round(stored_avg_probability, 4),
        "stored_recent_average_probability": round(stored_recent_avg_probability, 4),
        "stored_recent_calibration_gap": round(abs(stored_recent_avg_probability - (recent_win_rate / 100.0)), 4),
    }


def _segment_rows(records: list[dict[str, Any]], executed_records: list[dict[str, Any]], recent_window: int) -> list[dict[str, Any]]:
    totals_by_segment: dict[str, int] = defaultdict(int)
    resolved_by_segment: dict[str, int] = defaultdict(int)
    executed_by_segment: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for record in records:
        key = f"{record['symbol']}:{record['setup_type']}:{record['direction']}"
        if record["accepted"]:
            totals_by_segment[key] += 1
        if record["resolved"] and record["accepted"]:
            resolved_by_segment[key] += 1
    for record in executed_records:
        key = f"{record['symbol']}:{record['setup_type']}:{record['direction']}"
        executed_by_segment[key].append(record)

    rows: list[dict[str, Any]] = []
    for key, total_count in totals_by_segment.items():
        resolved_executed = executed_by_segment.get(key, [])
        result_rs = [float(record["result_r"]) for record in resolved_executed if record["result_r"] is not None]
        recent_segment = resolved_executed[-recent_window:] if recent_window > 0 else resolved_executed
        recent_rs = [float(record["result_r"]) for record in recent_segment if record["result_r"] is not None]
        wins = sum(1 for record in resolved_executed if record["status"] == "hit_t1")
        recent_wins = sum(1 for record in recent_segment if record["status"] == "hit_t1")
        recent_avg_probability = (
            float(np.mean([record["model_probability"] for record in recent_segment]))
            if recent_segment
            else 0.0
        )
        recent_win_rate = (recent_wins / len(recent_segment)) * 100 if recent_segment else 0.0
        rows.append(
            {
                "key": key,
                "total_signals": total_count,
                "resolved_signals": resolved_by_segment.get(key, 0),
                "resolved_executed": len(resolved_executed),
                "win_rate": round((wins / len(resolved_executed)) * 100, 2) if resolved_executed else 0.0,
                "expectancy": round((sum(result_rs) / len(result_rs)), 4) if result_rs else 0.0,
                "recent_expectancy": round((sum(recent_rs) / len(recent_rs)), 4) if recent_rs else 0.0,
                "non_execution_rate": round(
                    ((resolved_by_segment.get(key, 0) - len(resolved_executed)) / resolved_by_segment.get(key, 1)) * 100,
                    2,
                )
                if resolved_by_segment.get(key, 0)
                else 0.0,
                "recent_calibration_gap": round(abs(recent_avg_probability - (recent_win_rate / 100.0)), 4),
            }
        )
    return rows


def _walk_forward_summary(records: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    live_cfg = dict(config["probability"].get("live_requirements", {}))
    threshold_live = float(config["probability"]["probability_threshold_live"])
    folds = max(1, int(live_cfg.get("walk_forward_folds", 3)))
    minimum_records = max(
        int(live_cfg.get("walk_forward_min_records", 60)),
        folds * int(live_cfg.get("walk_forward_min_test_records_per_fold", 10)),
    )
    if len(records) < minimum_records:
        return {
            "ready": False,
            "folds": 0,
            "average_brier": 1.0,
            "average_live_expectancy": -999.0,
            "average_live_count": 0.0,
        }

    ordered = sorted(records, key=lambda record: record["emitted_at"])
    min_train = max(int(live_cfg.get("walk_forward_min_train_records", 40)), len(ordered) // 2)
    if min_train >= len(ordered):
        return {
            "ready": False,
            "folds": 0,
            "average_brier": 1.0,
            "average_live_expectancy": -999.0,
            "average_live_count": 0.0,
        }
    remaining = len(ordered) - min_train
    fold_size = max(int(live_cfg.get("walk_forward_min_test_records_per_fold", 10)), remaining // folds)
    results: list[dict[str, float]] = []

    for fold in range(folds):
        train_end = min_train + (fold * fold_size)
        test_start = train_end
        test_end = min(test_start + fold_size, len(ordered))
        if test_end - test_start < 5:
            continue
        train = ordered[:train_end]
        test = ordered[test_start:test_end]
        global_calibrator, per_setup = _fit_calibrators_from_records(train, config)
        predictions: list[tuple[float, float]] = []
        live_results: list[float] = []

        for record in test:
            calibrator = per_setup.get((record["setup_type"], record["direction"]), global_calibrator)
            prediction = (
                calibrator.predict(record["score_norm"])
                if calibrator is not None
                else _score_derived_shadow_probability(float(record["score"]), config)
            )
            predictions.append((prediction, record["label"]))
            if prediction >= threshold_live and record["result_r"] is not None:
                live_results.append(float(record["result_r"]))

        if not predictions:
            continue
        results.append(
            {
                "brier": float(np.mean([(prediction - label) ** 2 for prediction, label in predictions])),
                "live_expectancy": float(np.mean(live_results)) if live_results else 0.0,
                "live_count": float(len(live_results)),
            }
        )

    if not results:
        return {
            "ready": False,
            "folds": 0,
            "average_brier": 1.0,
            "average_live_expectancy": -999.0,
            "average_live_count": 0.0,
        }

    return {
        "ready": True,
        "folds": len(results),
        "average_brier": round(float(np.mean([item["brier"] for item in results])), 4),
        "average_live_expectancy": round(float(np.mean([item["live_expectancy"] for item in results])), 4),
        "average_live_count": round(float(np.mean([item["live_count"] for item in results])), 2),
    }


def _fit_calibrators_from_records(
    records: list[dict[str, Any]],
    config: dict[str, Any],
) -> tuple[_IsotonicCalibrator | None, dict[tuple[str, str], _IsotonicCalibrator]]:
    grouped_scores: dict[tuple[str, str], list[float]] = defaultdict(list)
    grouped_labels: dict[tuple[str, str], list[float]] = defaultdict(list)
    global_scores: list[float] = []
    global_labels: list[float] = []

    for record in records:
        key = (record["setup_type"], record["direction"])
        grouped_scores[key].append(record["score_norm"])
        grouped_labels[key].append(record["label"])
        global_scores.append(record["score_norm"])
        global_labels.append(record["label"])

    min_global_samples = _min_global_calibration_samples(config)
    min_setup_samples = _min_setup_calibration_samples(config)
    global_calibrator = (
        _fit_isotonic(global_scores, global_labels)
        if _can_fit_calibrator(global_scores, global_labels, min_global_samples)
        else None
    )
    calibrators = {
        key: _fit_isotonic(grouped_scores[key], grouped_labels[key])
        for key in grouped_scores
        if _can_fit_calibrator(grouped_scores[key], grouped_labels[key], min_setup_samples)
    }
    return global_calibrator, calibrators


def _drawdown_r(results: list[float]) -> float:
    if not results:
        return 0.0
    peak = 0.0
    equity = 0.0
    max_drawdown = 0.0
    for result in results:
        equity += float(result)
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    return max_drawdown


def _min_global_calibration_samples(config: Mapping[str, Any]) -> int:
    return int(config["probability"].get("min_global_samples_for_calibration", 20))


def _min_setup_calibration_samples(config: Mapping[str, Any]) -> int:
    return int(config["probability"].get("min_setup_direction_samples_for_calibration", 8))


def _can_fit_calibrator(scores: list[float], labels: list[float], minimum_samples: int) -> bool:
    if len(scores) < int(minimum_samples):
        return False
    return len({float(label) for label in labels}) >= 2


def _fit_isotonic(scores: list[float], labels: list[float]) -> _IsotonicCalibrator:
    x = np.asarray(scores, dtype=float)
    y = np.asarray(labels, dtype=float)
    if x.size == 0:
        return _IsotonicCalibrator(thresholds=np.asarray([0.0, 1.0], dtype=float), values=np.asarray([0.5, 0.5], dtype=float))

    order = np.argsort(x)
    x = x[order]
    y = y[order]

    blocks = [{"start": score, "end": score, "weight": 1.0, "value": label} for score, label in zip(x, y, strict=True)]

    index = 0
    while index < len(blocks) - 1:
        if blocks[index]["value"] <= blocks[index + 1]["value"]:
            index += 1
            continue
        total_weight = blocks[index]["weight"] + blocks[index + 1]["weight"]
        merged = {
            "start": blocks[index]["start"],
            "end": blocks[index + 1]["end"],
            "weight": total_weight,
            "value": (
                (blocks[index]["value"] * blocks[index]["weight"])
                + (blocks[index + 1]["value"] * blocks[index + 1]["weight"])
            )
            / total_weight,
        }
        blocks[index : index + 2] = [merged]
        if index > 0:
            index -= 1

    thresholds = np.asarray([float(block["start"]) for block in blocks] + [float(blocks[-1]["end"])], dtype=float)
    values = np.asarray([float(block["value"]) for block in blocks] + [float(blocks[-1]["value"])], dtype=float)
    return _IsotonicCalibrator(thresholds=thresholds, values=values)


def _empty_edge_report() -> dict[str, Any]:
    return {
        "performance": {
            "total_signals": 0,
            "resolved_signals": 0,
            "resolved_executed": 0,
            "wins": 0,
            "win_rate": 0.0,
            "expectancy": 0.0,
            "recent_expectancy": 0.0,
            "recent_win_rate": 0.0,
            "non_execution_rate": 0.0,
            "drawdown_r": 0.0,
            "average_probability": 0.0,
            "recent_average_probability": 0.0,
            "recent_calibration_gap": 0.0,
            "stored_average_probability": 0.0,
            "stored_recent_average_probability": 0.0,
            "stored_recent_calibration_gap": 0.0,
        },
        "walk_forward": {
            "ready": False,
            "folds": 0,
            "average_brier": 1.0,
            "average_live_expectancy": -999.0,
            "average_live_count": 0.0,
        },
        "live_requirements": {
            "eligible": False,
            "counts_ready": False,
            "min_global_expectancy": 0.0,
            "min_recent_expectancy": 0.0,
            "min_recent_win_rate": 0.0,
            "max_non_execution_rate": 100.0,
            "max_drawdown_r": 999.0,
            "max_recent_calibration_gap": 1.0,
            "min_walk_forward_expectancy": -999.0,
            "max_walk_forward_brier": 1.0,
        },
        "segments": [],
        "recommended_segments": [],
    }
