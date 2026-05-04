"""Telegram alert formatting and delivery."""

from __future__ import annotations

from datetime import datetime, timezone
import os
from typing import Any

import httpx

from app.utils.env import load_env_file
from app.utils.logging import get_logger

LOGGER = get_logger("app.alerts.telegram")


def format_alert_message(signal: dict[str, Any]) -> str:
    """Format a signal according to the mandatory DEVBOOK template."""

    symbol = str(signal["symbol"]).upper()
    probability_pct = round(float(signal["probability"]) * 100) if float(signal["probability"]) <= 1 else round(float(signal["probability"]))
    score = round(float(signal["score"]))
    emitted_at = _format_emitted_at(signal["emitted_at"])

    lines = [
        "BotYo",
        symbol,
        str(signal["direction"]).upper(),
        f"Setup : {_pretty_label(str(signal['setup_type']))}",
        f"Regime : {_pretty_label(str(signal['regime']))}",
        f"Probabilite : {probability_pct}%",
        f"Score : {score}/100",
        f"Entree : {_format_price(symbol, signal['entry_low'])} - {_format_price(symbol, signal['entry_high'])}",
        f"Stop : {_format_price(symbol, signal['stop'])}",
        f"T1 : {_format_price(symbol, signal['target1'])}",
        f"T2 : {_format_price(symbol, signal['target2'])}",
        f"R/R : {float(signal['rr']):.1f}",
        f"Validite : {int(signal['validity_hours'])}h",
        f"Annulation : {signal['invalidation_rule']}",
        f"Emis a : {emitted_at} UTC",
    ]
    return "\n".join(lines)


async def send_alert(signal: dict[str, Any], config: dict[str, Any], *, force: bool = False) -> bool:
    """Send a Telegram alert with optional force mode for explicit admin tests."""

    return await send_telegram_message(format_alert_message(signal), config, force=force)


async def send_telegram_message(text: str, config: dict[str, Any], *, force: bool = False) -> bool:
    """Send a raw Telegram text message respecting BotYo runtime guards."""

    environment = str(config["bot"]["environment"])
    if environment != "live_alert" and not force:
        LOGGER.info("shadow mode active, alert journalisee sans envoi Telegram")
        return False
    if force and environment != "live_alert":
        LOGGER.info("forced telegram send requested outside live_alert mode")

    load_env_file()
    token = _resolve_credential(
        config["alerts"],
        env_key="telegram_bot_token_env",
        legacy_key="telegram_bot_token",
    )
    chat_id = _resolve_credential(
        config["alerts"],
        env_key="telegram_chat_id_env",
        legacy_key="telegram_chat_id",
    )
    if not token or not chat_id:
        LOGGER.warning("telegram credentials missing, alert not sent")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=20.0)) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
        LOGGER.info("telegram message sent")
        return True
    except httpx.HTTPError as exc:
        LOGGER.error("telegram send failed: %s", exc)
        return False


def _pretty_label(value: str) -> str:
    return value.replace("_", " ").capitalize()


def _format_emitted_at(value: Any) -> str:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    return str(value).replace(" UTC", "")


def _format_price(symbol: str, value: Any) -> str:
    price = float(value)
    if symbol.startswith("BTC"):
        return f"{price:.0f}"
    return f"{price:.2f}"


def _resolve_credential(alerts_cfg: dict[str, Any], *, env_key: str, legacy_key: str) -> str:
    env_name = str(alerts_cfg.get(env_key, "")).strip()
    if env_name:
        env_value = os.environ.get(env_name, "").strip()
        if env_value:
            return env_value
    return str(alerts_cfg.get(legacy_key, "")).strip()
