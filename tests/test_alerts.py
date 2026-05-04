"""Tests for alert formatting and alert gates."""

from __future__ import annotations

import asyncio

from app.alerts.telegram import format_alert_message, send_alert, send_telegram_message
from app.supervisor import should_alert


def _config() -> dict:
    import yaml

    from app.utils.logging import ROOT_DIR

    return yaml.safe_load((ROOT_DIR / "config" / "bot.yaml").read_text(encoding="utf-8"))


def _signal() -> dict:
    return {
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
        "invalidation_rule": "cloture 4H sous 83100",
        "emitted_at": "2024-11-15 14:00",
        "mode": "shadow_live",
        "data_complete": True,
    }


def test_format_message_contains_all_required_fields() -> None:
    message = format_alert_message(_signal())
    required_lines = [
        "BotYo",
        "BTCUSDT",
        "LONG",
        "Setup : Trend continuation",
        "Regime : Bull trend",
        "Probabilite : 78%",
        "Score : 81/100",
        "Entree : 84250 - 84500",
        "Stop : 82920",
        "T1 : 86700",
        "T2 : 88900",
        "R/R : 2.1",
        "Validite : 12h",
        "Annulation : cloture 4H sous 83100",
        "Emis a : 2024-11-15 14:00 UTC",
    ]
    for line in required_lines:
        assert line in message


def test_cooldown_blocks_second_identical_alert() -> None:
    config = _config()
    signal = _signal()
    signal["emitted_at"] = 1_700_000_000
    active = [{"symbol": "BTCUSDT", "direction": "long", "emitted_at": 1_700_000_000 - 3600, "status": "expired"}]

    allowed, reason = should_alert(signal, active, config)

    assert allowed is False
    assert "cooldown" in reason


def test_max_three_active_alerts_blocks_fourth() -> None:
    config = _config()
    signal = _signal()
    signal["emitted_at"] = 1_700_000_000
    active = [
        {"symbol": "BTCUSDT", "direction": "short", "emitted_at": 1_699_999_000, "status": "active"},
        {"symbol": "ETHUSDT", "direction": "long", "emitted_at": 1_699_999_100, "status": "active"},
        {"symbol": "XRPUSDT", "direction": "short", "emitted_at": 1_699_999_200, "status": "active"},
    ]

    allowed, reason = should_alert(signal, active, config)

    assert allowed is False
    assert reason == "max active alerts reached"


def test_reject_if_rr_below_minimum() -> None:
    config = _config()
    signal = _signal()
    signal["rr"] = 1.2

    allowed, reason = should_alert(signal, [], config)

    assert allowed is False
    assert reason == "rr below minimum"


def test_reject_if_stop_missing() -> None:
    config = _config()
    signal = _signal()
    signal["stop"] = 0

    allowed, reason = should_alert(signal, [], config)

    assert allowed is False
    assert reason == "missing stop"


def test_shadow_mode_is_not_sent_to_telegram() -> None:
    config = _config()
    config["bot"]["environment"] = "shadow_live"
    config["alerts"]["telegram_bot_token"] = "dummy"
    config["alerts"]["telegram_chat_id"] = "dummy"

    sent = asyncio.run(send_alert(_signal(), config))

    assert sent is False


def test_force_send_can_send_admin_test_in_shadow_mode(monkeypatch) -> None:
    config = _config()
    config["bot"]["environment"] = "shadow_live"
    config["alerts"]["telegram_bot_token"] = "dummy"
    config["alerts"]["telegram_chat_id"] = "dummy"
    config["alerts"]["telegram_bot_token_env"] = ""
    config["alerts"]["telegram_chat_id_env"] = ""
    monkeypatch.delenv("BOTYO_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("BOTYO_TELEGRAM_CHAT_ID", raising=False)
    captured: dict[str, object] = {}

    class DummyResponse:
        def raise_for_status(self) -> None:
            return None

    class DummyClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            return None

        async def __aenter__(self) -> "DummyClient":
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
            return False

        async def post(self, url: str, json: dict[str, object]) -> DummyResponse:
            captured["url"] = url
            captured["json"] = json
            return DummyResponse()

    monkeypatch.setattr("app.alerts.telegram.httpx.AsyncClient", DummyClient)

    sent = asyncio.run(send_alert(_signal(), config, force=True))

    assert sent is True
    assert captured["json"] == {"chat_id": "dummy", "text": format_alert_message(_signal())}


def test_force_send_raw_telegram_message_in_shadow_mode(monkeypatch) -> None:
    config = _config()
    config["bot"]["environment"] = "shadow_live"
    config["alerts"]["telegram_bot_token"] = "dummy"
    config["alerts"]["telegram_chat_id"] = "dummy"
    config["alerts"]["telegram_bot_token_env"] = ""
    config["alerts"]["telegram_chat_id_env"] = ""
    monkeypatch.delenv("BOTYO_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("BOTYO_TELEGRAM_CHAT_ID", raising=False)
    captured: dict[str, object] = {}

    class DummyResponse:
        def raise_for_status(self) -> None:
            return None

    class DummyClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            return None

        async def __aenter__(self) -> "DummyClient":
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
            return False

        async def post(self, url: str, json: dict[str, object]) -> DummyResponse:
            captured["url"] = url
            captured["json"] = json
            return DummyResponse()

    monkeypatch.setattr("app.alerts.telegram.httpx.AsyncClient", DummyClient)

    sent = asyncio.run(send_telegram_message("Calibration live atteinte", config, force=True))

    assert sent is True
    assert captured["json"] == {"chat_id": "dummy", "text": "Calibration live atteinte"}


def test_telegram_credentials_can_come_from_env(monkeypatch) -> None:
    config = _config()
    config["bot"]["environment"] = "live_alert"
    config["alerts"]["telegram_bot_token"] = ""
    config["alerts"]["telegram_chat_id"] = ""
    monkeypatch.setenv("BOTYO_TELEGRAM_BOT_TOKEN", "env-token")
    monkeypatch.setenv("BOTYO_TELEGRAM_CHAT_ID", "env-chat")
    captured: dict[str, object] = {}

    class DummyResponse:
        def raise_for_status(self) -> None:
            return None

    class DummyClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            return None

        async def __aenter__(self) -> "DummyClient":
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
            return False

        async def post(self, url: str, json: dict[str, object]) -> DummyResponse:
            captured["url"] = url
            captured["json"] = json
            return DummyResponse()

    monkeypatch.setattr("app.alerts.telegram.httpx.AsyncClient", DummyClient)

    sent = asyncio.run(send_telegram_message("env delivery", config))

    assert sent is True
    assert captured["url"] == "https://api.telegram.org/botenv-token/sendMessage"
    assert captured["json"] == {"chat_id": "env-chat", "text": "env delivery"}
