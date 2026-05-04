"""Admin routes for BotYo."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.alerts.telegram import send_alert
from app.supervisor import load_config, save_config
from app.web.i18n import i18n_context

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))


@router.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request) -> HTMLResponse:
    supervisor = request.app.state.supervisor
    live_activation = supervisor.live_activation_status
    context = {
        "request": request,
        **i18n_context(request, title_key="admin"),
        "config": supervisor.config,
        "mode": supervisor.config["bot"]["environment"],
        "samples": supervisor.probability_engine.get_sample_counts(),
        "live_activation": live_activation,
        "performance": live_activation.get("performance", {}),
        "walk_forward": live_activation.get("walk_forward", {}),
        "live_requirements": live_activation.get("live_requirements", {}),
        "segments": live_activation.get("segments", []),
        "recommended_segments": live_activation.get("recommended_segments", []),
        "last_recalibrated_at": (
            supervisor.last_recalibrated_at.strftime("%Y-%m-%d %H:%M UTC")
            if supervisor.last_recalibrated_at is not None
            else "jamais"
        ),
    }
    return templates.TemplateResponse(request=request, name="admin.html", context=context)


@router.post("/admin/config")
async def update_config(request: Request) -> dict[str, Any]:
    payload = await request.json()
    path = str(payload["path"])
    value = payload["value"]

    supervisor = request.app.state.supervisor
    config = load_config(supervisor.config_path)
    _set_dotted_value(config, path, value)
    save_config(config, supervisor.config_path)
    await supervisor.reload_config()
    return {"status": "ok", "path": path, "value": value}


@router.post("/admin/mode")
async def update_mode(request: Request) -> dict[str, Any]:
    payload = await request.json()
    new_mode = str(payload["mode"])
    confirm_live = bool(payload.get("confirm_live", False))

    if new_mode == "live_alert" and not confirm_live:
        raise HTTPException(status_code=400, detail="explicit confirmation required for live_alert")

    supervisor = request.app.state.supervisor
    if new_mode == "live_alert" and not supervisor.live_activation_status.get("eligible", False):
        raise HTTPException(
            status_code=400,
            detail={
                "message": "calibration thresholds not met for live_alert",
                "live_activation": supervisor.live_activation_status,
            },
        )
    config = load_config(supervisor.config_path)
    config["bot"]["environment"] = new_mode
    save_config(config, supervisor.config_path)
    await supervisor.reload_config()
    return {"status": "ok", "mode": new_mode}


@router.post("/admin/telegram/test")
async def telegram_test(request: Request) -> dict[str, Any]:
    supervisor = request.app.state.supervisor
    signal = {
        "symbol": "BTCUSDT",
        "direction": "long",
        "setup_type": "trend_continuation",
        "regime": "bull_trend",
        "probability": 0.78,
        "score": 81,
        "entry_low": 84250,
        "entry_high": 84500,
        "stop": 82920,
        "target1": 86700,
        "target2": 88900,
        "rr": 2.1,
        "validity_hours": 12,
        "invalidation_rule": "test admin",
        "emitted_at": "2024-11-15 14:00",
    }
    sent = await send_alert(signal, supervisor.config, force=True)
    return {"status": "ok", "sent": sent}


@router.post("/admin/recalibrate")
async def recalibrate_probability(request: Request) -> dict[str, Any]:
    supervisor = request.app.state.supervisor
    live_activation = await supervisor.recalibrate_probability(notify=True)
    return {
        "status": "ok",
        "samples": supervisor.probability_engine.get_sample_counts(),
        "live_activation": live_activation,
    }


@router.post("/admin/shutdown")
async def shutdown_bot(request: Request, background_tasks: BackgroundTasks) -> dict[str, Any]:
    shutdown_callback = getattr(request.app.state, "request_shutdown", None)
    if shutdown_callback is None:
        raise HTTPException(status_code=503, detail="shutdown callback unavailable")
    background_tasks.add_task(shutdown_callback)
    return {"status": "ok", "message": "shutdown requested"}


def _set_dotted_value(config: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    cursor: Any = config
    for key in parts[:-1]:
        if key not in cursor:
            raise HTTPException(status_code=400, detail=f"unknown config path: {path}")
        cursor = cursor[key]
    if parts[-1] not in cursor:
        raise HTTPException(status_code=400, detail=f"unknown config path: {path}")
    cursor[parts[-1]] = value
