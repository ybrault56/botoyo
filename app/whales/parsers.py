"""Parsers for monitored X accounts and wallet lists."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from app.utils.logging import ROOT_DIR

_HANDLE_PATTERN = re.compile(r"\(@(?P<handle>[^)]+)\)")
_WALLET_PATTERN = re.compile(r"^(?P<address>[^\s]+)\s+\((?P<label>.+)\)$")


@dataclass(slots=True)
class XInfluencer:
    """One X account tracked by the whales module."""

    rank: int
    display_name: str
    handle: str
    followers_reference: str
    impact_score: int
    assets: tuple[str, ...]
    impact_type: str


@dataclass(slots=True)
class WhaleWallet:
    """One wallet tracked by the whales module."""

    rank: int
    address: str
    label: str
    asset: str
    balance_reference: str
    impact_score: int
    impact_type: str

    @property
    def entity_type(self) -> str:
        label = self.label.lower()
        if any(keyword in label for keyword in ("binance", "robinhood", "bithumb", "exchange", "cold wallet")):
            return "exchange"
        if any(keyword in label for keyword in ("beacon", "staking")):
            return "staking"
        return "treasury"


def load_x_influencers(path: str | Path | None = None) -> list[XInfluencer]:
    """Load monitored X accounts from the markdown table."""

    resolved = _resolve_list_path(path, "X.md")
    influencers: list[XInfluencer] = []
    for columns in _iter_markdown_rows(resolved):
        if len(columns) < 6:
            continue
        handle_match = _HANDLE_PATTERN.search(columns[1])
        if handle_match is None:
            continue
        display_name = columns[1].split("(@", 1)[0].strip()
        influencers.append(
            XInfluencer(
                rank=int(columns[0]),
                display_name=display_name,
                handle=handle_match.group("handle").strip(),
                followers_reference=columns[2],
                impact_score=int(columns[3]),
                assets=_parse_assets(columns[4]),
                impact_type=columns[5],
            )
        )
    return influencers


def load_whale_wallets(path: str | Path | None = None) -> list[WhaleWallet]:
    """Load monitored whale wallets from the markdown table."""

    resolved = _resolve_list_path(path, "WW.md")
    wallets: list[WhaleWallet] = []
    for columns in _iter_markdown_rows(resolved):
        if len(columns) < 6:
            continue
        wallet_match = _WALLET_PATTERN.match(columns[1])
        if wallet_match is None:
            continue
        wallets.append(
            WhaleWallet(
                rank=int(columns[0]),
                address=wallet_match.group("address").strip(),
                label=wallet_match.group("label").strip(),
                asset=columns[2].strip().upper(),
                balance_reference=columns[3],
                impact_score=int(columns[4]),
                impact_type=columns[5],
            )
        )
    return wallets


def _resolve_list_path(path: str | Path | None, default_name: str) -> Path:
    candidate = Path(path) if path is not None else ROOT_DIR / "whales" / default_name
    if not candidate.is_absolute():
        candidate = ROOT_DIR / candidate
    return candidate


def _iter_markdown_rows(path: Path) -> list[list[str]]:
    rows: list[list[str]] = []
    if not path.exists():
        return rows
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line.startswith("|"):
            continue
        columns = [part.strip() for part in line.strip("|").split("|")]
        if not columns or columns[0].lower() == "rang":
            continue
        if all(set(column) <= {"-", " "} for column in columns):
            continue
        rows.append(columns)
    return rows


def _parse_assets(raw_assets: str) -> tuple[str, ...]:
    cleaned = raw_assets.replace("(effet de place)", "")
    parts = [part.strip().upper() for part in cleaned.split(",")]
    return tuple(part for part in parts if part in {"BTC", "ETH", "XRP"})
