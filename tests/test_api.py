"""API tests for dashboard, admin and journal routes."""

from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path
import signal

import yaml
from fastapi.testclient import TestClient
from fastapi import FastAPI
import uvicorn

from app.main import BotYoServer, create_app, main
from app.storage.db import init_db, upsert_external_alert, upsert_signal
from app.utils.json import dumps
from app.utils.logging import ROOT_DIR


def _temp_config_path() -> Path:
    source = ROOT_DIR / "config" / "bot.yaml"
    temp_dir = Path(tempfile.mkdtemp())
    target = temp_dir / "bot.yaml"
    target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    return target


def _temp_db_path() -> Path:
    temp_dir = Path(tempfile.mkdtemp())
    return temp_dir / "botyo.db"


def _seed_signal(db_path: Path) -> str:
    init_db(db_path)
    signal_id = "sig-ui-001"
    upsert_signal(
        {
            "id": signal_id,
            "symbol": "BTCUSDT",
            "direction": "long",
            "setup_type": "trend_continuation",
            "regime": "bull_trend",
            "score": 84.0,
            "probability": 0.81,
            "entry_low": 65000.0,
            "entry_high": 65120.0,
            "stop": 64200.0,
            "target1": 66240.0,
            "target2": 67100.0,
            "rr": 2.4,
            "validity_hours": 12.0,
            "invalidation_rule": "cloture 1H sous le swing",
            "features_json": dumps(
                {
                    "regime_alignment": "perfect",
                    "timeframe_alignment": 3,
                    "confluence_count": 4,
                    "trend_structure": "bullish",
                    "setup_structure": "bullish",
                    "entry_structure": "bullish",
                    "ema_zone": True,
                    "fib_confluence": True,
                    "round_level_confluence": True,
                    "major_level_touch": False,
                    "rsi_rebound": True,
                    "macd_confirmed": True,
                    "structure_confirmed": True,
                    "reversal_candle": True,
                    "entry_rsi": 48.2,
                    "entry_macd_cross": "bullish",
                    "entry_volume_ratio": 2.15,
                    "clear_stop": True,
                    "confirmation_wick_ratio": 0.18,
                    "score_breakdown": {
                        "regime": 20.0,
                        "structure": 20.0,
                        "setup_quality": 15.0,
                        "location": 10.0,
                        "momentum": 10.0,
                        "volume": 10.0,
                        "entry_quality": 5.0,
                        "stop_quality": 5.0,
                        "rr_quality": 4.0,
                    },
                }
            ),
            "emitted_at": 1775151600,
            "expires_at": 1775194800,
            "status": "active",
            "result_r": None,
            "closed_at": None,
            "mode": "shadow_live",
            "comment": "",
        },
        db_path=db_path,
    )
    return signal_id


def _seed_whale_movements(db_path: Path) -> None:
    init_db(db_path)
    observed_at = int(datetime.now(timezone.utc).timestamp())
    for alert_id, symbol, signal_value, direction, usd_amount, threshold_state, label in (
        ("wallet:red", "BTC", "PUMP", "outflow", 12_000_000.0, "above_threshold", "Binance BTC cold wallet"),
        ("wallet:orange", "ETH", "DUMP", "inflow", 8_000_000.0, "near_threshold", "Lido ETH reserve"),
        ("wallet:green", "XRP", "PUMP", "outflow", 4_000_000.0, "below_threshold", "Ripple XRP treasury"),
    ):
        upsert_external_alert(
            {
                "id": alert_id,
                "source": "wallet_movement",
                "symbol": symbol,
                "signal": signal_value,
                "probability": 75.0,
                "observed_at": observed_at,
                "title": "Wallet test",
                "message": "sample",
                "metadata_json": dumps(
                    {
                        "label": label,
                        "network": symbol,
                        "tx_hash": alert_id,
                        "amount": 150.0,
                        "usd_amount": usd_amount,
                        "threshold": 10_000_000.0,
                        "direction": direction,
                        "threshold_state": threshold_state,
                    }
                ),
                "delivery_status": threshold_state,
            },
            db_path,
        )


def test_get_root_returns_200() -> None:
    db_path = _temp_db_path()
    _seed_signal(db_path)
    _seed_whale_movements(db_path)
    with TestClient(create_app(str(_temp_config_path()), start_supervisor=False)) as client:
        client.app.state.supervisor.db_path = db_path
        response = client.get("/")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store, no-cache, must-revalidate, max-age=0"
    assert "Fib 0.382-0.618" in response.text
    assert "MACD bullish" in response.text
    assert "Segments live recommandes" in response.text
    assert "Readiness live" in response.text
    assert "Diagnostic signal" in response.text
    assert "Mouvements whales" in response.text
    assert "Toutes" in response.text
    assert "BTC" in response.text
    assert "ETH" in response.text
    assert "XRP" in response.text
    assert "Tendance 24h PUMP" in response.text
    assert "Seuil depasse" in response.text
    assert "Proche seuil" in response.text
    assert "Sous seuil" not in response.text
    assert response.text.count('id="dashboard-live"') == 1


def test_get_dashboard_partial_returns_200() -> None:
    db_path = _temp_db_path()
    _seed_signal(db_path)
    _seed_whale_movements(db_path)
    with TestClient(create_app(str(_temp_config_path()), start_supervisor=False)) as client:
        client.app.state.supervisor.db_path = db_path
        response = client.get("/partials/dashboard")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store, no-cache, must-revalidate, max-age=0"
    assert 'id="dashboard-live"' in response.text
    assert "Alertes recentes" in response.text
    assert "Dernier rendu dashboard:" in response.text
    assert "Derniere sync runtime:" in response.text
    assert "Dernieres bougies" in response.text
    assert "Diagnostic signal" in response.text
    assert "Mouvements whales" in response.text


def test_get_dashboard_partial_filters_whales_by_crypto() -> None:
    db_path = _temp_db_path()
    _seed_signal(db_path)
    _seed_whale_movements(db_path)
    with TestClient(create_app(str(_temp_config_path()), start_supervisor=False)) as client:
        client.app.state.supervisor.db_path = db_path
        response = client.get("/partials/dashboard?whale_asset=ETH")

    assert response.status_code == 200
    assert 'data-partial-url="/partials/dashboard?whale_asset=ETH"' in response.text
    assert "Lido ETH reserve" in response.text
    assert "Tendance 24h DUMP" in response.text
    assert "Binance BTC cold wallet" not in response.text
    assert "Ripple XRP treasury" not in response.text


def test_get_admin_returns_200() -> None:
    with TestClient(create_app(str(_temp_config_path()), start_supervisor=False)) as client:
        response = client.get("/admin")
    assert response.status_code == 200
    assert "Walk-forward" in response.text
    assert "Garde-fous live" in response.text


def test_get_journal_returns_200() -> None:
    db_path = _temp_db_path()
    signal_id = _seed_signal(db_path)
    with TestClient(create_app(str(_temp_config_path()), start_supervisor=False)) as client:
        client.app.state.supervisor.db_path = db_path
        response = client.get("/journal")
        detail_response = client.get(f"/journal/{signal_id}")
    assert response.status_code == 200
    assert "Score detail" in response.text
    assert "Performance segments" in response.text
    assert detail_response.status_code == 200
    assert "Signal detail" in detail_response.text
    assert "Lifecycle" in detail_response.text


def test_get_journal_detail_api_returns_presented_signal() -> None:
    db_path = _temp_db_path()
    signal_id = _seed_signal(db_path)
    with TestClient(create_app(str(_temp_config_path()), start_supervisor=False)) as client:
        client.app.state.supervisor.db_path = db_path
        response = client.get(f"/api/journal/{signal_id}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["confluence_summary"].startswith("Trend Continuation")
    assert "EMA20/50" in payload["location_badges"]
    assert "MACD bullish" in payload["momentum_badges"]


def test_get_api_status_returns_valid_json() -> None:
    with TestClient(create_app(str(_temp_config_path()), start_supervisor=False)) as client:
        response = client.get("/api/status")
    assert response.status_code == 200
    assert response.json()["mode"] == "shadow_live"
    assert "last_market_sync_at" in response.json()
    assert response.json()["live_activation"]["eligible"] is False
    assert "performance" in response.json()["live_activation"]
    assert "walk_forward" in response.json()["live_activation"]


def test_get_api_alerts_active_returns_json_list() -> None:
    with TestClient(create_app(str(_temp_config_path()), start_supervisor=False)) as client:
        response = client.get("/api/alerts/active")
    assert response.status_code == 200
    assert isinstance(response.json(), list)


def test_get_api_diagnostics_returns_symbol_diagnostics() -> None:
    with TestClient(create_app(str(_temp_config_path()), start_supervisor=False)) as client:
        response = client.get("/api/diagnostics")

    assert response.status_code == 200
    payload = response.json()
    assert isinstance(payload, list)
    assert len(payload) == 3
    assert {"symbol", "regime", "blockers"}.issubset(payload[0].keys())


def test_get_api_metrics_summary_returns_extended_metrics() -> None:
    with TestClient(create_app(str(_temp_config_path()), start_supervisor=False)) as client:
        response = client.get("/api/metrics/summary")
    assert response.status_code == 200
    payload = response.json()
    assert "non_execution_rate" in payload
    assert "drawdown_r" in payload


def test_post_admin_config_with_valid_parameter() -> None:
    config_path = _temp_config_path()
    with TestClient(create_app(str(config_path), start_supervisor=False)) as client:
        response = client.post("/admin/config", json={"path": "web.dashboard_refresh_seconds", "value": 30})
    assert response.status_code == 200

    updated = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert updated["web"]["dashboard_refresh_seconds"] == 30


def test_post_admin_telegram_test_forces_send(monkeypatch) -> None:
    calls: dict[str, object] = {}

    async def fake_send_alert(signal: dict, config: dict, *, force: bool = False) -> bool:
        calls["symbol"] = signal["symbol"]
        calls["force"] = force
        return True

    monkeypatch.setattr("app.web.routes_admin.send_alert", fake_send_alert)

    with TestClient(create_app(str(_temp_config_path()), start_supervisor=False)) as client:
        response = client.post("/admin/telegram/test")

    assert response.status_code == 200
    assert response.json()["sent"] is True
    assert calls == {"symbol": "BTCUSDT", "force": True}


def test_post_admin_shutdown_requests_graceful_shutdown() -> None:
    called = {"count": 0}

    def request_shutdown() -> None:
        called["count"] += 1

    with TestClient(create_app(str(_temp_config_path()), start_supervisor=False)) as client:
        client.app.state.request_shutdown = request_shutdown
        response = client.post("/admin/shutdown")

    assert response.status_code == 200
    assert response.json()["message"] == "shutdown requested"
    assert called["count"] == 1


def test_post_admin_mode_rejects_live_when_calibration_not_ready() -> None:
    with TestClient(create_app(str(_temp_config_path()), start_supervisor=False)) as client:
        response = client.post("/admin/mode", json={"mode": "live_alert", "confirm_live": True})

    assert response.status_code == 400
    assert response.json()["detail"]["message"] == "calibration thresholds not met for live_alert"


def test_post_admin_mode_allows_live_when_eligible() -> None:
    config_path = _temp_config_path()
    with TestClient(create_app(str(config_path), start_supervisor=False)) as client:
        client.app.state.supervisor.live_activation_status = {"eligible": True}
        response = client.post("/admin/mode", json={"mode": "live_alert", "confirm_live": True})

    assert response.status_code == 200
    updated = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert updated["bot"]["environment"] == "live_alert"


def test_main_returns_zero_when_keyboard_interrupt_is_raised(monkeypatch) -> None:
    def fake_run_server(app, config) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("app.main.run_server", fake_run_server)

    assert main(str(_temp_config_path())) == 0


def test_botyo_server_capture_signals_does_not_reraise(monkeypatch) -> None:
    sentinel_thread = object()
    registered_handlers: dict[int, object] = {}
    raised_signals: list[int] = []

    def fake_signal(sig: int, handler):
        previous = registered_handlers.get(sig, signal.default_int_handler)
        registered_handlers[sig] = handler
        return previous

    monkeypatch.setattr("app.main.threading.current_thread", lambda: sentinel_thread)
    monkeypatch.setattr("app.main.threading.main_thread", lambda: sentinel_thread)
    monkeypatch.setattr("app.main.signal.signal", fake_signal)
    monkeypatch.setattr("app.main.signal.raise_signal", lambda sig: raised_signals.append(sig))

    app = FastAPI()
    server = BotYoServer(uvicorn.Config(app, host="127.0.0.1", port=9999, log_level="info"))

    with server.capture_signals():
        server._captured_signals.append(signal.SIGINT)

    assert raised_signals == []
