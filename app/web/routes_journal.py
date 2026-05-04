"""Journal routes for BotYo."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.storage.db import get_recent_signals, get_signal
from app.web.presenters import present_signal, present_signals

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))


@router.get("/journal", response_class=HTMLResponse)
async def journal_page(request: Request, page: int = Query(default=1, ge=1)) -> HTMLResponse:
    supervisor = request.app.state.supervisor
    page_size = int(supervisor.config["web"]["journal_page_size"])
    signals = get_recent_signals(limit=page * page_size, db_path=supervisor.db_path)
    start_index = (page - 1) * page_size
    items = signals[start_index : start_index + page_size]
    live_activation = supervisor.live_activation_status
    context = {
        "request": request,
        "title": "Journal",
        "signals": present_signals(items),
        "page": page,
        "metrics": _journal_metrics(signals),
        "performance": live_activation.get("performance", {}),
        "walk_forward": live_activation.get("walk_forward", {}),
        "segments": live_activation.get("segments", []),
        "selected_signal": None,
    }
    return templates.TemplateResponse(request=request, name="journal.html", context=context)


@router.get("/journal/{signal_id}", response_class=HTMLResponse)
async def journal_detail(request: Request, signal_id: str) -> HTMLResponse:
    signal = get_signal(signal_id, db_path=request.app.state.supervisor.db_path)
    if signal is None:
        raise HTTPException(status_code=404, detail="signal not found")

    supervisor = request.app.state.supervisor
    page_size = int(supervisor.config["web"]["journal_page_size"])
    signals = get_recent_signals(limit=page_size, db_path=supervisor.db_path)
    live_activation = supervisor.live_activation_status
    context = {
        "request": request,
        "title": "Journal",
        "signals": present_signals(signals),
        "page": 1,
        "metrics": _journal_metrics(signals),
        "performance": live_activation.get("performance", {}),
        "walk_forward": live_activation.get("walk_forward", {}),
        "segments": live_activation.get("segments", []),
        "selected_signal": present_signal(signal),
    }
    return templates.TemplateResponse(request=request, name="journal.html", context=context)


@router.get("/api/journal/{signal_id}")
async def journal_detail_api(request: Request, signal_id: str) -> dict[str, Any]:
    signal = get_signal(signal_id, db_path=request.app.state.supervisor.db_path)
    if signal is None:
        raise HTTPException(status_code=404, detail="signal not found")
    return present_signal(signal)


@router.get("/api/journal/metrics")
async def journal_metrics(request: Request) -> dict[str, Any]:
    signals = get_recent_signals(limit=1000, db_path=request.app.state.supervisor.db_path)
    return _journal_metrics(signals)


@router.get("/api/journal/chart/winrate")
async def journal_chart_winrate(request: Request) -> dict[str, Any]:
    signals = get_recent_signals(limit=1000, db_path=request.app.state.supervisor.db_path)
    closed = [signal for signal in reversed(signals) if signal.get("status") in {"hit_t1", "hit_stop"}]
    labels: list[str] = []
    data: list[float] = []
    wins = 0
    total = 0
    for signal in closed:
        total += 1
        if signal.get("status") == "hit_t1":
            wins += 1
        labels.append(str(signal["id"]))
        data.append(round((wins / total) * 100, 2))
    return {"labels": labels, "data": data}


@router.get("/api/journal/chart/rr")
async def journal_chart_rr(request: Request) -> dict[str, Any]:
    signals = get_recent_signals(limit=1000, db_path=request.app.state.supervisor.db_path)
    rr_values = [float(signal["rr"]) for signal in signals if signal.get("rr") is not None]
    return {"labels": [f"signal_{index}" for index in range(len(rr_values))], "data": rr_values}


def _journal_metrics(signals: list[dict[str, Any]]) -> dict[str, Any]:
    closed = [
        signal
        for signal in signals
        if signal.get("status") in {"hit_t1", "hit_stop", "expired_after_entry", "cancelled_regime_change"}
        and signal.get("result_r") is not None
    ]
    resolved = [signal for signal in signals if signal.get("status") not in {"active", "entered", "rejected"}]
    wins = [signal for signal in closed if signal.get("status") == "hit_t1"]
    losses = [signal for signal in closed if signal.get("status") == "hit_stop"]
    avg_r = sum(float(signal["result_r"]) for signal in closed if signal.get("result_r") is not None)
    avg_r = avg_r / len(closed) if closed else 0.0
    expectancy = avg_r
    return {
        "total_signals": len(signals),
        "resolved_signals": len(resolved),
        "resolved_executed": len(closed),
        "win_rate": round((len(wins) / len(closed)) * 100, 2) if closed else 0.0,
        "loss_count": len(losses),
        "avg_r_multiple": round(avg_r, 2),
        "expectancy": round(expectancy, 2),
        "non_execution_rate": round(((len(resolved) - len(closed)) / len(resolved)) * 100, 2) if resolved else 0.0,
    }
