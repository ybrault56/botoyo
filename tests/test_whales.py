"""Tests for the whales monitoring module."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import httpx
import pytest
import yaml

from app.storage.db import SQLiteWriteQueue, get_external_alert, get_recent_external_alerts, init_db
from app.utils.logging import ROOT_DIR
from app.whales.parsers import load_whale_wallets, load_x_influencers
from app.whales.service import (
    ExternalAlert,
    WhalesModule,
    _build_wallet_alert,
    _build_wallet_trend_alert,
    _extract_probability_pct,
    _extract_signal_label,
    _resolve_env_value,
)


def _temp_file(name: str, content: str) -> Path:
    path = Path(tempfile.mkdtemp()) / name
    path.write_text(content, encoding="utf-8")
    return path


def _config() -> dict:
    return yaml.safe_load((ROOT_DIR / "config" / "bot.yaml").read_text(encoding="utf-8"))


def test_load_x_influencers_from_markdown() -> None:
    path = _temp_file(
        "X.md",
        """
| Rang | Nom et pseudo X | Followers | Score impact (0-100) | Actifs le plus souvent affectes | Type d'impact dominant |
| --- | --- | --- | --- | --- | --- |
| 1 | Elon Musk (@elonmusk) | 237,812,279 | 92 | BTC | Choc de visibilite |
| 2 | Vitalik Buterin (@VitalikButerin) | 5.9M | 70 | ETH | Narrative |
""".strip(),
    )

    influencers = load_x_influencers(path)

    assert [item.handle for item in influencers] == ["elonmusk", "VitalikButerin"]
    assert influencers[0].impact_score == 92
    assert influencers[1].assets == ("ETH",)


def test_load_whale_wallets_from_markdown() -> None:
    path = _temp_file(
        "WW.md",
        """
| Rang | Adresse / nom | Crypto dominante | Solde estime (approx) | Score impact | Type d'impact dominant |
| --- | --- | --- | --- | --- | --- |
| 1 | 3M219KR5vEneNb47ewrPfWyb5jQ2DjxRP6 (Binance cold wallet) | BTC | 156,026.55 BTC | 95 | Volatilite immediate |
| 2 | 0x9fc3dc011b461664c835f2527fffb1169b3c213e (EF: DeFi Multisig) | ETH | Balance USD ~78.2M | 72 | Signal institutionnel |
""".strip(),
    )

    wallets = load_whale_wallets(path)

    assert wallets[0].address == "3M219KR5vEneNb47ewrPfWyb5jQ2DjxRP6"
    assert wallets[0].entity_type == "exchange"
    assert wallets[1].entity_type == "treasury"


def test_build_wallet_alert_for_exchange_inflow_marks_dump() -> None:
    wallet = load_whale_wallets(ROOT_DIR / "whales" / "WW.md")[0]

    alert = _build_wallet_alert(
        wallet=wallet,
        tx_hash="abc123",
        symbol="BTC",
        amount=150.0,
        usd_amount=12_000_000.0,
        inflow=True,
        observed_at=1_700_000_000,
        threshold=10_000_000.0,
        network="BTC",
    )

    assert alert is not None
    assert alert.signal == "DUMP"
    assert "Signal detecte : DUMP" in alert.message
    assert alert.probability >= 70


def test_build_wallet_alert_below_threshold_is_still_tracked() -> None:
    wallet = load_whale_wallets(ROOT_DIR / "whales" / "WW.md")[0]

    alert = _build_wallet_alert(
        wallet=wallet,
        tx_hash="abc124",
        symbol="BTC",
        amount=10.0,
        usd_amount=4_000_000.0,
        inflow=False,
        observed_at=1_700_000_000,
        threshold=10_000_000.0,
        network="BTC",
    )

    assert alert is not None
    assert alert.metadata["threshold_state"] == "below_threshold"
    assert "Etat de seuil: SOUS LE SEUIL." in alert.message


def test_build_wallet_alert_adds_technical_confirmation_when_available() -> None:
    wallet = load_whale_wallets(ROOT_DIR / "whales" / "WW.md")[0]

    alert = _build_wallet_alert(
        wallet=wallet,
        tx_hash="abc125",
        symbol="BTC",
        amount=150.0,
        usd_amount=12_000_000.0,
        inflow=False,
        observed_at=1_700_000_000,
        threshold=10_000_000.0,
        network="BTC",
        technical_context={
            "regime": "bull_trend",
            "bias": "long",
            "probability": 70,
            "summary": "biais haussier, structure 4H bullish, structure 1H bullish, RSI 15M 58.0",
        },
    )

    assert alert is not None
    assert alert.probability == 80
    assert "Regime technique : bull_trend" in alert.message
    assert "Probabilite combinee whales + TA : 80%" in alert.message
    assert alert.metadata["technical_alignment"] == "aligned"
    assert alert.metadata["technical_probability"] == 70


def test_extract_signal_and_probability_from_llm_output() -> None:
    message = """
🚨 ALERTE CRYPTO 🚨
📊 Analyse :
→ Signal détecté : PUMP 📈
→ Probabilité d'impact : 75%
""".strip()

    assert _extract_signal_label(message) == "PUMP"
    assert _extract_probability_pct(message) == 75


def test_resolve_env_value_supports_x_barear_alias(monkeypatch) -> None:
    monkeypatch.delenv("BOTYO_X_BEARER_TOKEN", raising=False)
    monkeypatch.setenv("x_barear", "alias-token")

    assert _resolve_env_value("BOTYO_X_BEARER_TOKEN", fallback_names=("x_barear",)) == "alias-token"


def test_record_and_send_alert_persists_external_alert(monkeypatch) -> None:
    config = _config()
    db_path = Path(tempfile.mkdtemp()) / "botyo.db"
    init_db(db_path)
    write_queue = SQLiteWriteQueue(db_path)
    calls: list[str] = []

    async def fake_send(message: str, config: dict, *, force: bool = False) -> bool:
        _ = config, force
        calls.append(message)
        return True

    monkeypatch.setattr("app.whales.service.send_telegram_message", fake_send)

    module = WhalesModule(
        config=config,
        db_path=db_path,
        write_queue=write_queue,
        price_resolver=lambda symbol: {"BTCUSDT": 80000.0, "ETHUSDT": 4000.0, "XRPUSDT": 2.0}.get(symbol),
    )

    async def runner() -> None:
        await write_queue.start()
        await module._record_and_send_alert(
            ExternalAlert(
                event_id="wallet:btc:test",
                source="wallet_movement",
                symbol="BTC",
                signal="DUMP",
                probability=80,
                observed_at=1_700_000_000,
                title="Wallet test",
                message="alert body",
                metadata={"tx_hash": "abc"},
            )
        )
        await write_queue.stop()

    asyncio.run(runner())

    stored = get_external_alert("wallet:btc:test", db_path=db_path)

    assert stored is not None
    assert stored["delivery_status"] == "sent"
    assert calls == ["alert body"]


def test_record_and_send_alert_tracks_below_threshold_without_sending(monkeypatch) -> None:
    config = _config()
    db_path = Path(tempfile.mkdtemp()) / "botyo.db"
    init_db(db_path)
    write_queue = SQLiteWriteQueue(db_path)
    calls: list[str] = []

    async def fake_send(message: str, config: dict, *, force: bool = False) -> bool:
        _ = config, force
        calls.append(message)
        return True

    monkeypatch.setattr("app.whales.service.send_telegram_message", fake_send)

    module = WhalesModule(
        config=config,
        db_path=db_path,
        write_queue=write_queue,
        price_resolver=lambda symbol: {"BTCUSDT": 80000.0, "ETHUSDT": 4000.0, "XRPUSDT": 2.0}.get(symbol),
    )

    async def runner() -> None:
        await write_queue.start()
        await module._record_and_send_alert(
            ExternalAlert(
                event_id="wallet:btc:green",
                source="wallet_movement",
                symbol="BTC",
                signal="PUMP",
                probability=60,
                observed_at=1_700_000_000,
                title="Wallet green",
                message="tracked only",
                metadata={
                    "tx_hash": "def",
                    "threshold_state": "below_threshold",
                    "usd_amount": 4_000_000.0,
                },
            )
        )
        await write_queue.stop()

    asyncio.run(runner())

    stored = get_external_alert("wallet:btc:green", db_path=db_path)

    assert stored is not None
    assert stored["delivery_status"] == "below_threshold"
    assert calls == []


def test_build_wallet_trend_alert_requires_clear_24h_bias() -> None:
    alerts = [
        {
            "id": "wallet:btc:p1",
            "source": "wallet_movement",
            "symbol": "BTC",
            "signal": "PUMP",
            "observed_at": 1_700_000_000,
            "metadata_json": '{"usd_amount": 12000000.0}',
        },
        {
            "id": "wallet:btc:p2",
            "source": "wallet_movement",
            "symbol": "BTC",
            "signal": "PUMP",
            "observed_at": 1_700_000_900,
            "metadata_json": '{"usd_amount": 11000000.0}',
        },
    ]

    alert = _build_wallet_trend_alert(
        alerts=alerts,
        symbol="BTC",
        observed_at=1_700_001_000,
        threshold=10_000_000.0,
    )

    assert alert is not None
    assert alert.source == "wallet_trend"
    assert alert.signal == "PUMP"
    assert "Whale trend BTC 24h" == alert.title
    assert "Pump cumule : $23.00M" in alert.message


def test_build_wallet_trend_alert_adds_technical_confirmation_when_available() -> None:
    alerts = [
        {
            "id": "wallet:btc:d1",
            "source": "wallet_movement",
            "symbol": "BTC",
            "signal": "DUMP",
            "observed_at": 1_700_000_000,
            "metadata_json": '{"usd_amount": 12000000.0}',
        },
        {
            "id": "wallet:btc:d2",
            "source": "wallet_movement",
            "symbol": "BTC",
            "signal": "DUMP",
            "observed_at": 1_700_000_900,
            "metadata_json": '{"usd_amount": 11000000.0}',
        },
    ]

    alert = _build_wallet_trend_alert(
        alerts=alerts,
        symbol="BTC",
        observed_at=1_700_001_000,
        threshold=10_000_000.0,
        technical_context={
            "regime": "bear_trend",
            "bias": "short",
            "probability": 65,
            "summary": "biais baissier, structure 4H bearish, structure 1H bearish, RSI 15M 41.0",
        },
    )

    assert alert is not None
    assert alert.probability == 85
    assert "Validation technique : 65% (ALIGNE)" in alert.message
    assert alert.metadata["technical_regime"] == "bear_trend"
    assert alert.metadata["combined_probability"] == 85


def test_record_and_send_alert_emits_wallet_trend_alert_to_telegram(monkeypatch) -> None:
    config = _config()
    db_path = Path(tempfile.mkdtemp()) / "botyo.db"
    init_db(db_path)
    write_queue = SQLiteWriteQueue(db_path)
    calls: list[tuple[str, bool]] = []

    async def fake_send(message: str, config: dict, *, force: bool = False) -> bool:
        _ = config
        calls.append((message, force))
        return True

    monkeypatch.setattr("app.whales.service.send_telegram_message", fake_send)

    module = WhalesModule(
        config=config,
        db_path=db_path,
        write_queue=write_queue,
        price_resolver=lambda symbol: {"BTCUSDT": 80000.0, "ETHUSDT": 4000.0, "XRPUSDT": 2.0}.get(symbol),
    )

    async def runner() -> None:
        await write_queue.start()
        await module._record_and_send_alert(
            ExternalAlert(
                event_id="wallet:btc:p1",
                source="wallet_movement",
                symbol="BTC",
                signal="PUMP",
                probability=80,
                observed_at=1_700_000_000,
                title="Wallet test 1",
                message="movement 1",
                metadata={
                    "tx_hash": "abc",
                    "threshold_state": "above_threshold",
                    "usd_amount": 12_000_000.0,
                },
            )
        )
        await module._record_and_send_alert(
            ExternalAlert(
                event_id="wallet:btc:p2",
                source="wallet_movement",
                symbol="BTC",
                signal="PUMP",
                probability=80,
                observed_at=1_700_000_900,
                title="Wallet test 2",
                message="movement 2",
                metadata={
                    "tx_hash": "def",
                    "threshold_state": "above_threshold",
                    "usd_amount": 11_000_000.0,
                },
            )
        )
        await write_queue.stop()

    asyncio.run(runner())

    stored_trends = [
        row for row in get_recent_external_alerts(limit=20, db_path=db_path) if row["source"] == "wallet_trend"
    ]

    assert len(stored_trends) == 1
    assert stored_trends[0]["delivery_status"] == "sent"
    assert calls[0] == ("movement 1", False)
    assert calls[1] == ("movement 2", False)
    assert calls[2][1] is True
    assert "WHALES BTC" in calls[2][0]


def test_x_loop_suspends_source_on_permanent_402(monkeypatch) -> None:
    config = _config()
    db_path = Path(tempfile.mkdtemp()) / "botyo.db"
    init_db(db_path)
    module = WhalesModule(
        config=config,
        db_path=db_path,
        write_queue=SQLiteWriteQueue(db_path),
        price_resolver=lambda symbol: None,
    )

    request = httpx.Request("GET", "https://api.x.com/2/users/test/tweets")
    response = httpx.Response(402, request=request)

    async def fake_poll_x_once() -> None:
        raise httpx.HTTPStatusError("payment required", request=request, response=response)

    sleep_calls: list[int] = []

    async def fake_sleep(seconds: int) -> None:
        sleep_calls.append(seconds)
        raise asyncio.CancelledError()

    monkeypatch.setattr(module, "_poll_x_once", fake_poll_x_once)
    monkeypatch.setattr("app.whales.service.asyncio.sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(module._x_loop())

    snapshot = module.status_snapshot()

    assert snapshot["source_states"]["x"]["suspended"] is True
    assert snapshot["source_states"]["x"]["status_code"] == 402
    assert sleep_calls == [45]
