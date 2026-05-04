"""Dashboard routes for BotYo."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.storage.db import get_active_signals, get_recent_external_alerts, get_recent_signals
from app.utils.json import loads
from app.web.presenters import present_diagnostics, present_signals, present_whale_movements, present_whale_trend

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    supervisor = request.app.state.supervisor
    whale_asset = _normalize_whale_asset(request.query_params.get("whale_asset"))
    context = {
        "request": request,
        "title": "Dashboard",
        "refresh_seconds": supervisor.config["web"]["dashboard_refresh_seconds"],
        **_dashboard_context(supervisor, whale_asset=whale_asset),
    }
    response = templates.TemplateResponse(request=request, name="dashboard.html", context=context)
    return _apply_no_cache(response)


@router.get("/partials/dashboard", response_class=HTMLResponse)
async def dashboard_partial(request: Request) -> HTMLResponse:
    supervisor = request.app.state.supervisor
    whale_asset = _normalize_whale_asset(request.query_params.get("whale_asset"))
    context = {
        "request": request,
        "refresh_seconds": supervisor.config["web"]["dashboard_refresh_seconds"],
        **_dashboard_context(supervisor, whale_asset=whale_asset),
    }
    response = templates.TemplateResponse(request=request, name="dashboard_content.html", context=context)
    return _apply_no_cache(response)


@router.get("/api/status")
async def api_status(request: Request) -> dict[str, Any]:
    return request.app.state.supervisor.status_snapshot()


@router.get("/api/alerts/active")
async def api_active_alerts(request: Request) -> list[dict[str, Any]]:
    return get_active_signals(db_path=request.app.state.supervisor.db_path)


@router.get("/api/regime")
async def api_regime(request: Request) -> dict[str, str]:
    return dict(request.app.state.supervisor.latest_regimes)


@router.get("/api/diagnostics")
async def api_diagnostics(request: Request) -> list[dict[str, Any]]:
    return request.app.state.supervisor.build_symbol_diagnostics()


@router.get("/api/metrics/summary")
async def api_metrics_summary(request: Request) -> dict[str, Any]:
    return _metrics_summary(request.app.state.supervisor)


def _metrics_summary(supervisor: Any) -> dict[str, Any]:
    performance = supervisor.live_activation_status.get("performance", {})
    return {
        "total_signals": int(performance.get("total_signals", 0)),
        "resolved_executed": int(performance.get("resolved_executed", 0)),
        "win_rate": float(performance.get("win_rate", 0.0)),
        "expectancy": float(performance.get("expectancy", 0.0)),
        "recent_expectancy": float(performance.get("recent_expectancy", 0.0)),
        "recent_win_rate": float(performance.get("recent_win_rate", 0.0)),
        "non_execution_rate": float(performance.get("non_execution_rate", 0.0)),
        "drawdown_r": float(performance.get("drawdown_r", 0.0)),
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }


def _dashboard_context(supervisor: Any, *, whale_asset: str) -> dict[str, Any]:
    active_alerts = present_signals(get_active_signals(db_path=supervisor.db_path))
    recent_alerts = present_signals(get_recent_signals(limit=10, db_path=supervisor.db_path))
    whale_threshold = float(supervisor.config["whales"]["wallets"]["strict_min_usd_trigger"])
    whale_movements = [
        row
        for row in get_recent_external_alerts(limit=500, db_path=supervisor.db_path)
        if str(row.get("source", "")) == "wallet_movement"
    ]
    filtered_whale_movements = [
        row for row in whale_movements if whale_asset == "ALL" or str(row.get("symbol", "")).upper() == whale_asset
    ]
    priority_whale_movements = [
        row
        for row in filtered_whale_movements
        if _whale_threshold_state(row) in {"above_threshold", "near_threshold"}
    ]
    visible_whale_movements = priority_whale_movements[:18] if priority_whale_movements else filtered_whale_movements[:18]
    live_activation = supervisor.live_activation_status
    return {
        "status": supervisor.status_snapshot(),
        "diagnostics": present_diagnostics(supervisor.build_symbol_diagnostics()),
        "active_alerts": active_alerts,
        "recent_alerts": recent_alerts,
        "whale_movements": present_whale_movements(visible_whale_movements, default_threshold=whale_threshold),
        "whale_trend": present_whale_trend(
            whale_movements,
            default_threshold=whale_threshold,
            selected_asset=whale_asset,
            now_ts=int(datetime.now(timezone.utc).timestamp()),
        ),
        "whale_filter_options": _build_whale_filter_options(whale_asset),
        "dashboard_partial_url": _build_dashboard_partial_url(whale_asset),
        "regimes": supervisor.latest_regimes,
        "metrics": _metrics_summary(supervisor),
        "performance": live_activation.get("performance", {}),
        "walk_forward": live_activation.get("walk_forward", {}),
        "live_requirements": live_activation.get("live_requirements", {}),
        "recommended_segments": live_activation.get("recommended_segments", []),
        "latest_candle_labels": _format_latest_candle_times(supervisor.latest_candle_times),
    }


def _format_latest_candle_times(latest_candle_times: dict[str, dict[str, int]]) -> dict[str, dict[str, str]]:
    labels: dict[str, dict[str, str]] = {}
    for symbol, timeframes in latest_candle_times.items():
        labels[symbol] = {
            timeframe: datetime.fromtimestamp(int(timestamp), timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            for timeframe, timestamp in timeframes.items()
        }
    return labels


def _normalize_whale_asset(value: str | None) -> str:
    candidate = str(value or "ALL").strip().upper()
    if candidate in {"BTC", "ETH", "XRP"}:
        return candidate
    return "ALL"


def _build_dashboard_partial_url(whale_asset: str) -> str:
    if whale_asset == "ALL":
        return "/partials/dashboard"
    return f"/partials/dashboard?{urlencode({'whale_asset': whale_asset})}"


def _build_whale_filter_options(selected: str) -> list[dict[str, str | bool]]:
    options = [("ALL", "Toutes"), ("BTC", "BTC"), ("ETH", "ETH"), ("XRP", "XRP")]
    payload: list[dict[str, str | bool]] = []
    for value, label in options:
        query = "" if value == "ALL" else f"?{urlencode({'whale_asset': value})}"
        payload.append(
            {
                "value": value,
                "label": label,
                "active": value == selected,
                "href": f"/{query}",
                "partial_href": _build_dashboard_partial_url(value),
            }
        )
    return payload


def _whale_threshold_state(alert: dict[str, Any]) -> str:
    raw = alert.get("metadata_json")
    if not raw:
        return "below_threshold"
    try:
        payload = loads(raw)
    except Exception:
        return "below_threshold"
    if not isinstance(payload, dict):
        return "below_threshold"
    return str(payload.get("threshold_state", "below_threshold"))


def _apply_no_cache(response: HTMLResponse) -> HTMLResponse:
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response
