"""Async monitoring for influential X posts and whale wallet movements."""

from __future__ import annotations

import asyncio
import math
import os
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

import httpx
import websockets

from app.alerts.telegram import send_telegram_message
from app.storage.db import SQLiteWriteQueue, get_external_alert, get_recent_external_alerts
from app.utils.env import load_env_file
from app.utils.json import dumps, loads
from app.utils.logging import ROOT_DIR, get_logger
from app.whales.parsers import WhaleWallet, XInfluencer, load_whale_wallets, load_x_influencers

LOGGER = get_logger("app.whales.service")
WHALE_NEAR_THRESHOLD_RATIO = 0.7
WHALE_TREND_WINDOW_SECONDS = 24 * 60 * 60
WHALE_TREND_MIN_MOVEMENTS = 2
WHALE_TREND_DOMINANCE_RATIO = 0.6
WHALE_TREND_COOLDOWN_SECONDS = 6 * 60 * 60
_KNOWN_EVENT_CACHE_LIMIT = 5000
_SOURCE_NAMES = ("x", "x_llm", "btc", "eth", "xrp")

X_ANALYST_SYSTEM_PROMPT = """Tu es un analyste crypto spécialisé en détection de signaux de marché sur les réseaux sociaux.
Tu analyses des posts X (Twitter) de personnalités influentes susceptibles de faire bouger le marché crypto.

## RÈGLE PRINCIPALE
Si le post n'a AUCUN lien avec la crypto, le trading, la finance, la régulation, ou une personnalité dont les déclarations impactent historiquement les marchés → réponds uniquement : PAS PERTINENT

## PERSONNALITÉS À FORT IMPACT (liste non exhaustive)
Elon Musk, Donald Trump, Michael Saylor, CZ (Binance), Vitalik Buterin, Jerome Powell, Gary Gensler, Brian Armstrong, SBF, Cathie Wood.

## SI LE POST EST PERTINENT
Réponds UNIQUEMENT avec ce format, sans texte avant ni après :

🚨 ALERTE CRYPTO 🚨
👤 [Nom de la personnalité] (@pseudo X) post détecté
🕐 [Heure du post au format HH:MM UTC]

📊 Analyse :
→ Crypto concernée : [BTC / ETH / XRP]
→ Signal détecté : [PUMP 📈 / DUMP 📉 / NEUTRE ➡️]
→ Probabilité d'impact : [X]%
→ Post détecté : "[Citation ou résumé fidèle du post]"
→ Délai estimé : [immédiat / 1-6h / 24-48h]

💬 Contexte : [1-2 phrases max expliquant pourquoi ce post est significatif]

⚠️ Ceci n'est pas un conseil financier.

## RÈGLES DE SCORING
- Probabilité > 70% : déclaration directe, explicite, historique d'impact prouvé
- Probabilité 40-70% : signal indirect, ambiguïté possible
- Probabilité < 40% : signal faible, interprétatif → préférer PAS PERTINENT
- En dessous de 40% de certitude : réponds PAS PERTINENT

## FORMAT STRICT
- Pas de markdown superflu
- Pas d'explication hors template
- Chiffres arrondis à 5% près
- Toujours UTC pour les heures"""


@dataclass(slots=True)
class ExternalAlert:
    """One alert emitted by the whales module."""

    event_id: str
    source: str
    symbol: str
    signal: str
    probability: int
    observed_at: int
    title: str
    message: str
    metadata: dict[str, Any]


class SourceCircuitOpenError(RuntimeError):
    """Raised when one external source is suspended by the circuit breaker."""

    def __init__(self, source: str, message: str) -> None:
        super().__init__(message)
        self.source = source


class WhalesModule:
    """Run the two async background tasks requested for whales monitoring."""

    def __init__(
        self,
        *,
        config: dict[str, Any],
        db_path: str | Path,
        write_queue: SQLiteWriteQueue,
        price_resolver: Callable[[str], float | None],
        technical_context_resolver: Callable[[str], dict[str, Any] | None] | None = None,
    ) -> None:
        self.config = config
        self.db_path = Path(db_path)
        self.write_queue = write_queue
        self.price_resolver = price_resolver
        self._technical_context_resolver = technical_context_resolver
        self._http_client: httpx.AsyncClient | None = None
        self._x_task: asyncio.Task[None] | None = None
        self._wallet_task: asyncio.Task[None] | None = None
        self._known_event_ids: set[str] = set()
        self._known_event_order: deque[str] = deque()
        self._warnings_emitted: set[str] = set()
        self._x_accounts: list[XInfluencer] = []
        self._wallets: list[WhaleWallet] = []
        self._x_last_seen_ids: dict[str, str] = {}
        self._x_user_ids: dict[str, str] = {}
        self._btc_seen_txids: dict[str, set[str]] = {}
        self._source_states: dict[str, dict[str, Any]] = {
            source: _default_source_state() for source in _SOURCE_NAMES
        }
        self._stats: dict[str, Any] = {
            "alerts_detected": 0,
            "alerts_sent": 0,
            "last_alert_at": None,
            "tracked_only_logged": 0,
        }

    async def start(self) -> None:
        """Load local secrets, hydrate state and start both monitoring tasks."""

        whales_cfg = self.config.get("whales", {})
        if not whales_cfg.get("enabled", False):
            return
        if str(self.config.get("bot", {}).get("environment", "")).strip().lower() not in {"shadow_live", "live_alert"}:
            return

        env_path = whales_cfg.get("env_file")
        loaded = load_env_file(env_path)
        if not loaded:
            load_env_file(ROOT_DIR / "whales" / ".env")
        self._x_accounts = load_x_influencers(whales_cfg.get("x", {}).get("accounts_doc"))
        self._wallets = load_whale_wallets(whales_cfg.get("wallets", {}).get("wallets_doc"))
        self._known_event_ids.clear()
        self._known_event_order.clear()
        for row in reversed(get_recent_external_alerts(limit=_KNOWN_EVENT_CACHE_LIMIT, db_path=self.db_path)):
            if row.get("id") is not None:
                self._remember_event_id(str(row["id"]))
        self._http_client = httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=20.0))

        if whales_cfg.get("x", {}).get("enabled", False) and self._x_accounts:
            self._x_task = asyncio.create_task(self._x_loop(), name="botyo-whales-x")

        if whales_cfg.get("wallets", {}).get("enabled", False) and self._wallets:
            self._wallet_task = asyncio.create_task(self._wallet_loop(), name="botyo-whales-wallets")

    async def stop(self) -> None:
        """Stop all monitor tasks and close shared HTTP resources."""

        tasks = [task for task in (self._x_task, self._wallet_task) if task is not None]
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass

        self._x_task = None
        self._wallet_task = None
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    async def reconfigure(self, config: dict[str, Any]) -> None:
        """Restart whales monitoring with the latest configuration."""

        await self.stop()
        self.config = config
        self._warnings_emitted.clear()
        self._x_last_seen_ids.clear()
        self._x_user_ids.clear()
        self._btc_seen_txids.clear()
        self._source_states = {source: _default_source_state() for source in _SOURCE_NAMES}
        await self.start()

    def status_snapshot(self) -> dict[str, Any]:
        """Expose runtime state for the supervisor dashboard/status API."""

        return {
            "enabled": bool(self.config.get("whales", {}).get("enabled", False)),
            "x_task_running": bool(self._x_task is not None and not self._x_task.done()),
            "wallet_task_running": bool(self._wallet_task is not None and not self._wallet_task.done()),
            "tracked_accounts": len(self._x_accounts),
            "tracked_wallets": len(self._wallets),
            "alerts_detected": int(self._stats["alerts_detected"]),
            "alerts_sent": int(self._stats["alerts_sent"]),
            "last_alert_at": self._stats["last_alert_at"],
            "tracked_only_logged": int(self._stats["tracked_only_logged"]),
            "source_states": {source: dict(state) for source, state in self._source_states.items()},
        }

    def _remember_event_id(self, event_id: str) -> None:
        self._known_event_ids.add(event_id)
        self._known_event_order.append(event_id)
        while len(self._known_event_order) > _KNOWN_EVENT_CACHE_LIMIT:
            expired = self._known_event_order.popleft()
            self._known_event_ids.discard(expired)

    def _source_suspended(self, source: str) -> bool:
        return bool(self._source_states.get(source, {}).get("suspended", False))

    def _mark_source_success(self, source: str) -> None:
        state = self._source_states.setdefault(source, _default_source_state())
        if state.get("suspended", False):
            return
        state["state"] = "running"
        state["reason"] = ""
        state["last_success_at"] = _status_timestamp()

    def _record_source_error(self, source: str, exc: Exception) -> None:
        status_code = _error_status_code(exc)
        state = self._source_states.setdefault(source, _default_source_state())
        state["status_code"] = status_code
        state["last_error"] = str(exc)
        state["last_error_at"] = _status_timestamp()
        if _is_permanent_client_error(exc):
            state["state"] = "suspended"
            state["suspended"] = True
            state["reason"] = (
                f"permanent client error {status_code}"
                if status_code is not None
                else "permanent client error"
            )
            LOGGER.error("source %s suspended after permanent client error: %s", source, exc)
            return
        if not state.get("suspended", False):
            state["state"] = "error"
            state["reason"] = "transient error"

    def _skip_suspended_source(self, source: str) -> bool:
        if not self._source_suspended(source):
            return False
        self._warn_once(f"source_suspended_{source}", "source %s suspended until manual reconfigure", source)
        return True

    def _resolve_technical_context(self, symbol: str) -> dict[str, Any] | None:
        if self._technical_context_resolver is None:
            return None
        try:
            context = self._technical_context_resolver(symbol)
        except Exception as exc:
            LOGGER.error("technical whale context failed for %s: %s", symbol, exc)
            return None
        return dict(context) if isinstance(context, Mapping) else None

    async def _x_loop(self) -> None:
        poll_seconds = max(15, int(self.config["whales"]["x"]["poll_seconds"]))
        while True:
            if self._skip_suspended_source("x") or self._skip_suspended_source("x_llm"):
                await asyncio.sleep(poll_seconds)
                continue
            try:
                await self._poll_x_once()
                self._mark_source_success("x")
            except asyncio.CancelledError:
                raise
            except SourceCircuitOpenError as exc:
                LOGGER.error("source %s suspended: %s", exc.source, exc)
            except Exception as exc:
                self._record_source_error("x", exc)
                LOGGER.error("X monitoring loop failed: %s", exc)
            await asyncio.sleep(poll_seconds)

    async def _poll_x_once(self) -> None:
        whales_cfg = self.config["whales"]
        x_cfg = whales_cfg["x"]
        bearer_token = _resolve_env_value(str(x_cfg["bearer_token_env"]), fallback_names=("x_barear",))
        openai_api_key = os.environ.get(str(x_cfg["openai_api_key_env"]), "").strip()
        if not bearer_token:
            self._warn_once("missing_x_token", "X monitoring disabled: missing %s", x_cfg["bearer_token_env"])
            return
        if not openai_api_key:
            self._warn_once("missing_openai_key", "X monitoring disabled: missing %s", x_cfg["openai_api_key_env"])
            return
        if self._http_client is None:
            return
        if self._source_suspended("x") or self._source_suspended("x_llm"):
            return

        headers = {"Authorization": f"Bearer {bearer_token}"}
        for account in self._x_accounts:
            user_id = await self._get_x_user_id(account.handle, headers)
            if not user_id:
                continue
            response = await self._http_client.get(
                f"{str(x_cfg['api_base_url']).rstrip('/')}/users/{user_id}/tweets",
                params={
                    "max_results": int(x_cfg["max_posts_per_account"]),
                    "tweet.fields": "created_at",
                    "exclude": "retweets,replies",
                },
                headers=headers,
            )
            response.raise_for_status()
            payload = response.json()
            posts = payload.get("data", []) if isinstance(payload, dict) else []
            if not isinstance(posts, list) or not posts:
                continue

            latest_post_id = _tweet_id(posts[0])
            previous_seen_id = self._x_last_seen_ids.get(account.handle)
            if previous_seen_id is None:
                self._x_last_seen_ids[account.handle] = latest_post_id
                continue

            new_posts = [post for post in posts if int(_tweet_id(post)) > int(previous_seen_id)]
            for post in sorted(new_posts, key=lambda item: int(_tweet_id(item))):
                alert = await self._analyze_x_post(account, post, openai_api_key)
                if alert is not None:
                    await self._record_and_send_alert(alert)
            self._x_last_seen_ids[account.handle] = latest_post_id

    async def _get_x_user_id(self, handle: str, headers: dict[str, str]) -> str | None:
        cached = self._x_user_ids.get(handle)
        if cached:
            return cached
        if self._http_client is None:
            return None

        response = await self._http_client.get(
            f"{str(self.config['whales']['x']['api_base_url']).rstrip('/')}/users/by",
            params={"usernames": handle},
            headers=headers,
        )
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data", []) if isinstance(payload, dict) else []
        if not isinstance(data, list) or not data:
            return None
        user_id = str(data[0].get("id", "")).strip()
        if not user_id:
            return None
        self._x_user_ids[handle] = user_id
        return user_id

    async def _analyze_x_post(
        self,
        account: XInfluencer,
        post: dict[str, Any],
        openai_api_key: str,
    ) -> ExternalAlert | None:
        if self._http_client is None:
            return None
        if self._source_suspended("x_llm"):
            return None

        post_id = _tweet_id(post)
        full_text = str(post.get("full_text") or post.get("text") or "").strip()
        if not full_text:
            return None

        observed_at = _parse_x_created_at(str(post.get("created_at", "")))
        user_prompt = "\n".join(
            [
                f"Auteur: {account.display_name} (@{account.handle})",
                f"Score d'impact interne: {account.impact_score}/100",
                f"Actifs surveilles par BotYo: {', '.join(account.assets) or 'BTC, ETH, XRP'}",
                f"Heure UTC du post: {datetime.fromtimestamp(observed_at, tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
                "Consigne additionnelle: si le post n'impacte pas clairement BTC, ETH ou XRP, reponds PAS PERTINENT.",
                "Post X a analyser:",
                full_text,
            ]
        )
        response = await self._http_client.post(
            str(self.config["whales"]["x"]["openai_endpoint"]),
            headers={
                "Authorization": f"Bearer {openai_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": str(self.config["whales"]["x"]["llm_model"]),
                "input": [
                    {
                        "role": "developer",
                        "content": [{"type": "input_text", "text": X_ANALYST_SYSTEM_PROMPT}],
                    },
                    {
                        "role": "user",
                        "content": [{"type": "input_text", "text": user_prompt}],
                    },
                ],
                "max_output_tokens": 350,
            },
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            self._record_source_error("x_llm", exc)
            raise SourceCircuitOpenError("x_llm", str(exc)) from exc
        self._mark_source_success("x_llm")
        payload = response.json()
        message = _extract_openai_text(payload).strip()
        if not message or message.upper() == "PAS PERTINENT":
            return None

        signal = _extract_signal_label(message)
        if signal == "NEUTRE":
            return None

        probability = _extract_probability_pct(message)
        if probability < 40:
            return None

        return ExternalAlert(
            event_id=f"x:{post_id}",
            source="x_post",
            symbol=_extract_symbols(message) or ",".join(account.assets or ("BTC", "ETH", "XRP")),
            signal=signal,
            probability=probability,
            observed_at=observed_at,
            title=f"X post {account.handle}",
            message=message,
            metadata={
                "account_handle": account.handle,
                "account_name": account.display_name,
                "post_id": post_id,
                "post_url": f"https://x.com/{account.handle}/status/{post_id}",
                "raw_text": full_text,
            },
        )

    async def _wallet_loop(self) -> None:
        btc_wallets = [wallet for wallet in self._wallets if wallet.asset == "BTC"]
        eth_wallets = [wallet for wallet in self._wallets if wallet.asset == "ETH"]
        xrp_wallets = [wallet for wallet in self._wallets if wallet.asset == "XRP"]
        tasks: list[asyncio.Task[None]] = []

        if btc_wallets:
            tasks.append(asyncio.create_task(self._btc_loop(btc_wallets), name="botyo-whales-btc"))
        if eth_wallets:
            tasks.append(asyncio.create_task(self._eth_loop(eth_wallets), name="botyo-whales-eth"))
        if xrp_wallets:
            tasks.append(asyncio.create_task(self._xrp_loop(xrp_wallets), name="botyo-whales-xrp"))

        try:
            if tasks:
                await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            for task in tasks:
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    async def _btc_loop(self, wallets: list[WhaleWallet]) -> None:
        if self._http_client is None:
            return

        wallets_by_address = {wallet.address: wallet for wallet in wallets}
        poll_seconds = max(15, int(self.config["whales"]["wallets"]["btc_poll_seconds"]))
        api_base = str(self.config["whales"]["wallets"]["btc_api_base_url"]).rstrip("/")

        while True:
            if self._skip_suspended_source("btc"):
                await asyncio.sleep(poll_seconds)
                continue
            try:
                for wallet in wallets:
                    response = await self._http_client.get(f"{api_base}/address/{wallet.address}/txs")
                    response.raise_for_status()
                    txs = response.json()
                    if not isinstance(txs, list):
                        continue
                    seen = self._btc_seen_txids.get(wallet.address)
                    current_ids = {_btc_txid(tx) for tx in txs if _btc_txid(tx)}
                    if seen is None:
                        self._btc_seen_txids[wallet.address] = current_ids
                        continue

                    new_txs = [tx for tx in txs if _btc_txid(tx) and _btc_txid(tx) not in seen]
                    for tx in reversed(new_txs):
                        alert = self._build_btc_alert(wallet, tx, wallets_by_address)
                        if alert is not None:
                            await self._record_and_send_alert(alert)
                    updated_seen = set(seen)
                    updated_seen.update(current_ids)
                    if len(updated_seen) > 500:
                        updated_seen = set(list(updated_seen)[-500:])
                    self._btc_seen_txids[wallet.address] = updated_seen
                self._mark_source_success("btc")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._record_source_error("btc", exc)
                LOGGER.error("BTC whales loop failed: %s", exc)
            await asyncio.sleep(poll_seconds)

    async def _eth_loop(self, wallets: list[WhaleWallet]) -> None:
        wallets_by_address = {wallet.address.lower(): wallet for wallet in wallets}
        wallets_cfg = self.config["whales"]["wallets"]
        api_key = os.environ.get(str(wallets_cfg["eth_api_key_env"]), "").strip()
        if not api_key:
            self._warn_once("missing_eth_key", "ETH monitoring disabled: missing %s", wallets_cfg["eth_api_key_env"])
            return

        websocket_url = str(wallets_cfg["eth_websocket_url"]).format(api_key=api_key)
        subscription_payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_subscribe",
            "params": [
                "alchemy_pendingTransactions",
                {
                    "fromAddress": list(wallets_by_address.keys()),
                    "toAddress": list(wallets_by_address.keys()),
                    "hashesOnly": False,
                },
            ],
        }

        while True:
            if self._skip_suspended_source("eth"):
                await asyncio.sleep(5)
                continue
            try:
                async with websockets.connect(websocket_url, ping_interval=20, ping_timeout=20) as websocket:
                    await websocket.send(dumps(subscription_payload))
                    await websocket.recv()
                    self._mark_source_success("eth")
                    while True:
                        raw_message = await websocket.recv()
                        payload = loads(raw_message)
                        alert = self._build_eth_alert(payload, wallets_by_address)
                        if alert is not None:
                            await self._record_and_send_alert(alert)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._record_source_error("eth", exc)
                LOGGER.error("ETH whales websocket failed: %s", exc)
                await asyncio.sleep(5)

    async def _xrp_loop(self, wallets: list[WhaleWallet]) -> None:
        wallets_by_address = {wallet.address: wallet for wallet in wallets}
        websocket_url = str(self.config["whales"]["wallets"]["xrp_websocket_url"])
        subscribe_payload = {
            "id": "botyo-whales-xrp",
            "command": "subscribe",
            "accounts": list(wallets_by_address.keys()),
        }

        while True:
            if self._skip_suspended_source("xrp"):
                await asyncio.sleep(5)
                continue
            try:
                async with websockets.connect(websocket_url, ping_interval=20, ping_timeout=20) as websocket:
                    await websocket.send(dumps(subscribe_payload))
                    await websocket.recv()
                    self._mark_source_success("xrp")
                    while True:
                        raw_message = await websocket.recv()
                        payload = loads(raw_message)
                        alert = self._build_xrp_alert(payload, wallets_by_address)
                        if alert is not None:
                            await self._record_and_send_alert(alert)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._record_source_error("xrp", exc)
                LOGGER.error("XRP whales websocket failed: %s", exc)
                await asyncio.sleep(5)

    def _build_btc_alert(
        self,
        wallet: WhaleWallet,
        tx: dict[str, Any],
        wallets_by_address: dict[str, WhaleWallet],
    ) -> ExternalAlert | None:
        involved_addresses = _btc_involved_tracked_addresses(tx, wallets_by_address)
        if len(involved_addresses) > 1:
            return None

        net_sats = _btc_net_sats_for_address(tx, wallet.address)
        if net_sats == 0:
            return None
        amount = abs(net_sats) / 100_000_000.0
        price = self.price_resolver("BTCUSDT")
        if price is None:
            return None

        observed_at = _btc_observed_at(tx)
        return _build_wallet_alert(
            wallet=wallet,
            tx_hash=_btc_txid(tx),
            symbol="BTC",
            amount=amount,
            usd_amount=amount * price,
            inflow=net_sats > 0,
            observed_at=observed_at,
            threshold=float(self.config["whales"]["wallets"]["strict_min_usd_trigger"]),
            network="BTC",
            technical_context=self._resolve_technical_context("BTC"),
        )

    def _build_eth_alert(
        self,
        payload: dict[str, Any],
        wallets_by_address: dict[str, WhaleWallet],
    ) -> ExternalAlert | None:
        params = payload.get("params")
        if not isinstance(params, dict):
            return None
        tx = params.get("result")
        if not isinstance(tx, dict):
            return None

        from_address = str(tx.get("from", "")).lower()
        to_address = str(tx.get("to", "")).lower()
        if from_address in wallets_by_address and to_address in wallets_by_address and from_address != to_address:
            return None

        wallet: WhaleWallet | None = None
        inflow = False
        if to_address in wallets_by_address and from_address not in wallets_by_address:
            wallet = wallets_by_address[to_address]
            inflow = True
        elif from_address in wallets_by_address and to_address not in wallets_by_address:
            wallet = wallets_by_address[from_address]
            inflow = False
        if wallet is None:
            return None

        amount = int(str(tx.get("value", "0x0")), 16) / 1_000_000_000_000_000_000
        if amount <= 0.0:
            return None
        price = self.price_resolver("ETHUSDT")
        if price is None:
            return None

        return _build_wallet_alert(
            wallet=wallet,
            tx_hash=str(tx.get("hash", "")),
            symbol="ETH",
            amount=amount,
            usd_amount=amount * price,
            inflow=inflow,
            observed_at=_utc_now_ts(),
            threshold=float(self.config["whales"]["wallets"]["strict_min_usd_trigger"]),
            network="ETH",
            technical_context=self._resolve_technical_context("ETH"),
        )

    def _build_xrp_alert(
        self,
        payload: dict[str, Any],
        wallets_by_address: dict[str, WhaleWallet],
    ) -> ExternalAlert | None:
        tx = payload.get("transaction")
        if not isinstance(tx, dict):
            return None
        if payload.get("type") != "transaction" or not bool(payload.get("validated", False)):
            return None
        if tx.get("TransactionType") != "Payment":
            return None
        amount_raw = tx.get("Amount")
        if not isinstance(amount_raw, str) or not amount_raw.isdigit():
            return None

        source_address = str(tx.get("Account", ""))
        destination_address = str(tx.get("Destination", ""))
        if source_address in wallets_by_address and destination_address in wallets_by_address and source_address != destination_address:
            return None

        wallet: WhaleWallet | None = None
        inflow = False
        if destination_address in wallets_by_address and source_address not in wallets_by_address:
            wallet = wallets_by_address[destination_address]
            inflow = True
        elif source_address in wallets_by_address and destination_address not in wallets_by_address:
            wallet = wallets_by_address[source_address]
            inflow = False
        if wallet is None:
            return None

        amount = int(amount_raw) / 1_000_000.0
        if amount <= 0.0:
            return None
        price = self.price_resolver("XRPUSDT")
        if price is None:
            return None

        return _build_wallet_alert(
            wallet=wallet,
            tx_hash=str(tx.get("hash", "")),
            symbol="XRP",
            amount=amount,
            usd_amount=amount * price,
            inflow=inflow,
            observed_at=_xrpl_to_unix_timestamp(payload.get("date")),
            threshold=float(self.config["whales"]["wallets"]["strict_min_usd_trigger"]),
            network="XRP",
            technical_context=self._resolve_technical_context("XRP"),
        )

    async def _record_and_send_alert(self, alert: ExternalAlert) -> None:
        if alert.event_id in self._known_event_ids:
            return
        if get_external_alert(alert.event_id, db_path=self.db_path) is not None:
            self._remember_event_id(alert.event_id)
            return

        environment = str(self.config["bot"]["environment"]).strip().lower()
        metadata = dict(alert.metadata)
        threshold_state = str(metadata.get("threshold_state", "above_threshold"))
        tracked_only = alert.source == "wallet_movement" and threshold_state != "above_threshold"
        force_send = alert.source == "wallet_trend"
        sent = False
        delivery_status = threshold_state if tracked_only else "shadow"
        if not tracked_only:
            sent = await send_telegram_message(alert.message, self.config, force=force_send)
            delivery_status = "sent" if sent else "shadow"
            if (environment == "live_alert" or force_send) and not sent:
                delivery_status = "failed"

        await self.write_queue.upsert_external_alert(
            {
                "id": alert.event_id,
                "source": alert.source,
                "symbol": alert.symbol,
                "signal": alert.signal,
                "probability": float(alert.probability),
                "observed_at": alert.observed_at,
                "title": alert.title,
                "message": alert.message,
                "metadata_json": dumps(alert.metadata),
                "delivery_status": delivery_status,
            }
        )
        self._remember_event_id(alert.event_id)
        if not tracked_only:
            self._stats["alerts_detected"] = int(self._stats["alerts_detected"]) + 1
            if sent:
                self._stats["alerts_sent"] = int(self._stats["alerts_sent"]) + 1
            self._stats["last_alert_at"] = datetime.fromtimestamp(alert.observed_at, tz=timezone.utc).strftime(
                "%Y-%m-%d %H:%M UTC"
            )
        else:
            self._stats["tracked_only_logged"] = int(self._stats["tracked_only_logged"]) + 1

        if alert.source == "wallet_movement":
            await self._maybe_emit_wallet_trend_alert(symbol=alert.symbol, observed_at=alert.observed_at)

    def _warn_once(self, key: str, message: str, *args: Any) -> None:
        if key in self._warnings_emitted:
            return
        self._warnings_emitted.add(key)
        LOGGER.warning(message, *args)

    async def _maybe_emit_wallet_trend_alert(self, *, symbol: str, observed_at: int) -> None:
        """Emit one Telegram trend alert when the rolling 24h whale bias becomes interesting."""

        trend_alert = _build_wallet_trend_alert(
            alerts=get_recent_external_alerts(limit=5000, db_path=self.db_path),
            symbol=symbol,
            observed_at=observed_at,
            threshold=float(self.config["whales"]["wallets"]["strict_min_usd_trigger"]),
            technical_context=self._resolve_technical_context(symbol),
        )
        if trend_alert is None:
            return
        await self._record_and_send_alert(trend_alert)


def _build_wallet_alert(
    *,
    wallet: WhaleWallet,
    tx_hash: str,
    symbol: str,
    amount: float,
    usd_amount: float,
    inflow: bool,
    observed_at: int,
    threshold: float,
    network: str,
    technical_context: Mapping[str, Any] | None = None,
) -> ExternalAlert | None:
    signal, context_hint, clarity_bonus = _wallet_signal(wallet.entity_type, inflow)
    whale_probability = _wallet_probability(usd_amount, wallet.impact_score, threshold, clarity_bonus)
    technical_overlay = _build_technical_overlay(signal=signal.split(" ", 1)[0], whale_probability=whale_probability, technical_context=technical_context)
    probability = technical_overlay["combined_probability"]
    threshold_state = _wallet_threshold_state(usd_amount, threshold)
    event_id = f"wallet:{network.lower()}:{tx_hash}:{wallet.address}"
    timestamp = datetime.fromtimestamp(observed_at, tz=timezone.utc).strftime("%H:%M UTC")
    threshold_label = {
        "above_threshold": "SEUIL DEPASSE",
        "near_threshold": "PROCHE DU SEUIL",
        "below_threshold": "SOUS LE SEUIL",
    }[threshold_state]
    threshold_context = {
        "above_threshold": f"Le montant depasse le seuil strict de {_format_usd(threshold)}.",
        "near_threshold": f"Le montant reste proche du seuil strict de {_format_usd(threshold)}.",
        "below_threshold": f"Le montant reste sous le seuil strict de {_format_usd(threshold)}.",
    }[threshold_state]
    message = "\n".join(
        [
            "🚨 ALERTE WHALES 🚨",
            f"👛 {wallet.label} ({symbol}) mouvement detecte",
            f"🕐 {timestamp}",
            "",
            "📊 Analyse :",
            f"→ Crypto concernee : {symbol}",
            f"→ Signal detecte : {signal}",
            f"→ Probabilite d'impact : {probability}%",
            f"→ Probabilite whales seule : {whale_probability}%",
            f"→ Mouvement detecte : {_format_amount(symbol, amount)} (~{_format_usd(usd_amount)})",
            f"→ Reseau / Tx : {network} / {tx_hash[:12]}...",
            *technical_overlay["lines"],
            "",
            (
                "💬 Contexte : "
                f"{context_hint} {threshold_context} "
                f"Etat de seuil: {threshold_label}. "
                f"Ce wallet a un score d'impact de {wallet.impact_score}/100."
            ),
            "",
            "⚠️ Ceci n'est pas un conseil financier.",
        ]
    )
    return ExternalAlert(
        event_id=event_id,
        source="wallet_movement",
        symbol=symbol,
        signal=signal.split(" ", 1)[0],
        probability=probability,
        observed_at=observed_at,
        title=f"Wallet movement {wallet.label}",
        message=message,
        metadata={
            "address": wallet.address,
            "label": wallet.label,
            "asset": symbol,
            "network": network,
            "tx_hash": tx_hash,
            "usd_amount": round(usd_amount, 2),
            "amount": amount,
            "direction": "inflow" if inflow else "outflow",
            "threshold": round(threshold, 2),
            "threshold_state": threshold_state,
            "whale_probability": whale_probability,
            **technical_overlay["metadata"],
        },
    )


def _build_wallet_trend_alert(
    *,
    alerts: list[dict[str, Any]],
    symbol: str,
    observed_at: int,
    threshold: float,
    technical_context: Mapping[str, Any] | None = None,
) -> ExternalAlert | None:
    """Build one aggregated 24h trend alert when wallet flow is clearly biased."""

    upper_symbol = str(symbol).upper()
    cutoff = int(observed_at) - WHALE_TREND_WINDOW_SECONDS
    relevant_alerts = [
        alert
        for alert in alerts
        if str(alert.get("source", "")) == "wallet_movement"
        and str(alert.get("symbol", "")).upper() == upper_symbol
        and int(alert.get("observed_at", 0) or 0) >= cutoff
    ]
    if len(relevant_alerts) < WHALE_TREND_MIN_MOVEMENTS:
        return None

    pump_usd = 0.0
    dump_usd = 0.0
    for alert in relevant_alerts:
        metadata = _decode_alert_metadata(alert)
        usd_amount = float(metadata.get("usd_amount", 0.0) or 0.0)
        signal = str(alert.get("signal", "")).upper()
        if signal == "PUMP":
            pump_usd += usd_amount
        elif signal == "DUMP":
            dump_usd += usd_amount

    total_usd = pump_usd + dump_usd
    if total_usd <= 0.0:
        return None

    net_usd = pump_usd - dump_usd
    dominance_ratio = abs(net_usd) / total_usd
    if abs(net_usd) < threshold or dominance_ratio < WHALE_TREND_DOMINANCE_RATIO:
        return None

    signal = "PUMP" if net_usd > 0 else "DUMP"
    event_id = f"wallet-trend:{upper_symbol}:{signal}:{int(observed_at) // WHALE_TREND_COOLDOWN_SECONDS}"
    whale_probability = _wallet_trend_probability(abs(net_usd), threshold, dominance_ratio, len(relevant_alerts))
    technical_overlay = _build_technical_overlay(signal=signal, whale_probability=whale_probability, technical_context=technical_context)
    probability = technical_overlay["combined_probability"]
    timestamp = datetime.fromtimestamp(observed_at, tz=timezone.utc).strftime("%H:%M UTC")
    direction_context = "acheteur" if signal == "PUMP" else "vendeur"
    message = "\n".join(
        [
            "BotYo",
            f"WHALES {upper_symbol}",
            signal,
            "Setup : Whale trend 24h",
            f"Probabilite : {probability}%",
            f"Probabilite whales seule : {whale_probability}%",
            f"Fenetre : 24h glissantes",
            f"Mouvements : {len(relevant_alerts)}",
            f"Pump cumule : {_format_usd(pump_usd)}",
            f"Dump cumule : {_format_usd(dump_usd)}",
            f"Net dominant : {_format_usd(abs(net_usd))}",
            f"Dominance : {round(dominance_ratio * 100)}%",
            *technical_overlay["lines"],
            f"Contexte : biais {direction_context} whales sur 24h, au-dessus du seuil {_format_usd(threshold)}",
            f"Emis a : {datetime.fromtimestamp(observed_at, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC",
            "Annulation : invalidation automatique si le biais 24h retombe sous le seuil de dominance",
        ]
    )
    return ExternalAlert(
        event_id=event_id,
        source="wallet_trend",
        symbol=upper_symbol,
        signal=signal,
        probability=probability,
        observed_at=observed_at,
        title=f"Whale trend {upper_symbol} 24h",
        message=message,
        metadata={
            "window_hours": 24,
            "movement_count": len(relevant_alerts),
            "pump_usd": round(pump_usd, 2),
            "dump_usd": round(dump_usd, 2),
            "net_usd": round(net_usd, 2),
            "dominance_ratio": round(dominance_ratio, 4),
            "threshold": round(threshold, 2),
            "whale_probability": whale_probability,
            **technical_overlay["metadata"],
        },
    )


def _build_technical_overlay(
    *,
    signal: str,
    whale_probability: int,
    technical_context: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(technical_context, Mapping):
        return {"combined_probability": whale_probability, "lines": [], "metadata": {}}

    technical_probability = int(_rounded_probability(technical_context.get("probability"), default=whale_probability))
    regime = str(technical_context.get("regime", "unknown"))
    bias = str(technical_context.get("bias", "neutral"))
    summary = str(technical_context.get("summary", "")).strip()
    alignment = _technical_alignment(signal, bias, regime)
    combined_probability = _combine_whale_probabilities(
        whale_probability=whale_probability,
        technical_probability=technical_probability,
        alignment=alignment,
        regime=regime,
    )
    lines = [
        f"→ Regime technique : {regime}",
        f"→ Validation technique : {technical_probability}% ({_alignment_label(alignment)})",
    ]
    if summary:
        lines.append(f"→ Lecture technique : {summary}")
    lines.append(f"→ Probabilite combinee whales + TA : {combined_probability}%")
    return {
        "combined_probability": combined_probability,
        "lines": lines,
        "metadata": {
            "technical_probability": technical_probability,
            "combined_probability": combined_probability,
            "technical_alignment": alignment,
            "technical_regime": regime,
            "technical_bias": bias,
            "technical_summary": summary,
        },
    }


def _wallet_threshold_state(usd_amount: float, threshold: float) -> str:
    if usd_amount >= threshold:
        return "above_threshold"
    if usd_amount >= (threshold * WHALE_NEAR_THRESHOLD_RATIO):
        return "near_threshold"
    return "below_threshold"


def _wallet_signal(entity_type: str, inflow: bool) -> tuple[str, str, int]:
    if entity_type == "exchange":
        if inflow:
            return "DUMP 📉", "Flux entrant vers un wallet de type exchange suivi par BotYo.", 12
        return "PUMP 📈", "Flux sortant d'un wallet de type exchange, lecture accumulation probable.", 10
    if entity_type == "staking":
        if inflow:
            return "PUMP 📈", "Flux entrant vers un wallet de staking, baisse potentielle de l'offre liquide.", 11
        return "DUMP 📉", "Flux sortant d'un wallet de staking, retour potentiel vers le marche.", 9
    if inflow:
        return "PUMP 📈", "Renforcement d'un wallet de reserve ou de tresorerie suivi par BotYo.", 8
    return "DUMP 📉", "Sortie d'un wallet de reserve ou de tresorerie, risque de pression vendeuse.", 8


def _wallet_probability(usd_amount: float, impact_score: int, threshold: float, clarity_bonus: int) -> int:
    ratio = max(usd_amount / max(threshold, 1.0), 1.0)
    magnitude_points = min(30.0, math.log10(ratio) * 30.0)
    impact_points = max(0.0, (float(impact_score) - 50.0) * 0.35)
    probability = 45.0 + magnitude_points + impact_points + float(clarity_bonus)
    probability = max(55.0, min(95.0, probability))
    return int(round(probability / 5.0) * 5)


def _wallet_trend_probability(net_usd: float, threshold: float, dominance_ratio: float, movement_count: int) -> int:
    ratio = max(net_usd / max(threshold, 1.0), 1.0)
    magnitude_points = min(20.0, math.log10(ratio) * 25.0)
    dominance_points = max(0.0, (dominance_ratio - WHALE_TREND_DOMINANCE_RATIO) * 60.0)
    count_points = min(10.0, max(0, movement_count - WHALE_TREND_MIN_MOVEMENTS) * 2.5)
    probability = 55.0 + magnitude_points + dominance_points + count_points
    probability = max(60.0, min(95.0, probability))
    return int(round(probability / 5.0) * 5)


def _technical_alignment(signal: str, bias: str, regime: str) -> str:
    signal_bias = "long" if str(signal).upper() == "PUMP" else "short" if str(signal).upper() == "DUMP" else "neutral"
    normalized_bias = str(bias).strip().lower()
    normalized_regime = str(regime).strip().lower()
    if normalized_regime in {"high_volatility_noise", "low_quality_market"}:
        return "opposed"
    if signal_bias == "neutral" or normalized_bias in {"", "neutral"}:
        return "neutral"
    return "aligned" if signal_bias == normalized_bias else "opposed"


def _combine_whale_probabilities(
    *,
    whale_probability: int,
    technical_probability: int,
    alignment: str,
    regime: str,
) -> int:
    combined = (float(whale_probability) * 0.65) + (float(technical_probability) * 0.35)
    if alignment == "aligned":
        combined += 5.0
    elif alignment == "opposed":
        combined -= 10.0
    if str(regime).strip().lower() in {"high_volatility_noise", "low_quality_market"}:
        combined = min(combined, 65.0)
    return int(_rounded_probability(combined, default=whale_probability))


def _rounded_probability(value: Any, *, default: int) -> int:
    try:
        probability = float(value)
    except (TypeError, ValueError):
        probability = float(default)
    probability = max(40.0, min(95.0, probability))
    return int(round(probability / 5.0) * 5)


def _alignment_label(alignment: str) -> str:
    return {
        "aligned": "ALIGNE",
        "neutral": "NEUTRE",
        "opposed": "CONTRADICTOIRE",
    }.get(str(alignment), "NEUTRE")


def _extract_openai_text(payload: dict[str, Any]) -> str:
    direct = payload.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()

    output = payload.get("output")
    if not isinstance(output, list):
        return ""
    parts: list[str] = []
    for item in output:
        if not isinstance(item, dict):
            continue
        for content in item.get("content", []):
            if not isinstance(content, dict):
                continue
            if content.get("type") in {"output_text", "text"}:
                text = content.get("text")
                if isinstance(text, str):
                    parts.append(text)
    return "\n".join(part.strip() for part in parts if part.strip())


def _extract_signal_label(message: str) -> str:
    normalized = message.upper()
    if "PUMP" in normalized:
        return "PUMP"
    if "DUMP" in normalized:
        return "DUMP"
    if "NEUTRE" in normalized:
        return "NEUTRE"
    return "UNKNOWN"


def _extract_probability_pct(message: str) -> int:
    for line in message.splitlines():
        normalized = line.lower()
        if "impact" in normalized and "%" in normalized:
            digits = "".join(character for character in line if character.isdigit())
            if digits:
                return int(digits)
    return 0


def _extract_symbols(message: str) -> str:
    marker = "Crypto"
    for line in message.splitlines():
        if marker.lower() in line.lower() and ":" in line:
            return line.split(":", 1)[1].strip()
    return ""


def _tweet_id(post: dict[str, Any]) -> str:
    value = post.get("id_str", post.get("id", ""))
    return str(value)


def _parse_x_created_at(raw_value: str) -> int:
    try:
        return int(datetime.strptime(raw_value, "%a %b %d %H:%M:%S %z %Y").timestamp())
    except ValueError:
        try:
            normalized = raw_value.replace("Z", "+00:00")
            return int(datetime.fromisoformat(normalized).timestamp())
        except ValueError:
            return _utc_now_ts()


def _format_amount(symbol: str, amount: float) -> str:
    if symbol == "BTC":
        return f"{amount:,.4f} BTC"
    if symbol == "ETH":
        return f"{amount:,.2f} ETH"
    return f"{amount:,.0f} XRP"


def _format_usd(amount: float) -> str:
    if amount >= 1_000_000_000:
        return f"${amount / 1_000_000_000:.2f}B"
    return f"${amount / 1_000_000:.2f}M"


def _decode_alert_metadata(alert: dict[str, Any]) -> dict[str, Any]:
    raw = alert.get("metadata_json")
    if not raw:
        return {}
    try:
        parsed = loads(raw)
    except Exception:
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _btc_txid(tx: dict[str, Any]) -> str:
    return str(tx.get("txid", ""))


def _btc_net_sats_for_address(tx: dict[str, Any], address: str) -> int:
    incoming = 0
    for output in tx.get("vout", []):
        if isinstance(output, dict) and output.get("scriptpubkey_address") == address:
            incoming += int(output.get("value", 0))

    outgoing = 0
    for vin in tx.get("vin", []):
        if not isinstance(vin, dict):
            continue
        prevout = vin.get("prevout")
        if isinstance(prevout, dict) and prevout.get("scriptpubkey_address") == address:
            outgoing += int(prevout.get("value", 0))
    return incoming - outgoing


def _btc_involved_tracked_addresses(tx: dict[str, Any], wallets_by_address: dict[str, WhaleWallet]) -> set[str]:
    addresses: set[str] = set()
    for output in tx.get("vout", []):
        if isinstance(output, dict):
            address = output.get("scriptpubkey_address")
            if isinstance(address, str) and address in wallets_by_address:
                addresses.add(address)
    for vin in tx.get("vin", []):
        if not isinstance(vin, dict):
            continue
        prevout = vin.get("prevout")
        if isinstance(prevout, dict):
            address = prevout.get("scriptpubkey_address")
            if isinstance(address, str) and address in wallets_by_address:
                addresses.add(address)
    return addresses


def _btc_observed_at(tx: dict[str, Any]) -> int:
    status = tx.get("status")
    if isinstance(status, dict) and isinstance(status.get("block_time"), int):
        return int(status["block_time"])
    return _utc_now_ts()


def _xrpl_to_unix_timestamp(value: Any) -> int:
    if isinstance(value, int):
        return value + 946684800
    return _utc_now_ts()


def _utc_now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def _resolve_env_value(primary_name: str, *, fallback_names: tuple[str, ...] = ()) -> str:
    value = os.environ.get(primary_name, "").strip()
    if value:
        return value
    for name in fallback_names:
        candidate = os.environ.get(name, "").strip()
        if candidate:
            return candidate
    return ""


def _default_source_state() -> dict[str, Any]:
    return {
        "state": "idle",
        "suspended": False,
        "status_code": None,
        "reason": "",
        "last_error": "",
        "last_error_at": None,
        "last_success_at": None,
    }


def _status_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _error_status_code(exc: Exception) -> int | None:
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if isinstance(status_code, int):
        return status_code
    direct_status = getattr(exc, "status_code", None)
    return int(direct_status) if isinstance(direct_status, int) else None


def _is_permanent_client_error(exc: Exception) -> bool:
    status_code = _error_status_code(exc)
    return status_code is not None and 400 <= status_code < 500 and status_code != 429
