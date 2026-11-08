"""CSV logging for the sports underdog +1.5 sim."""
from __future__ import annotations

import csv
import os
from datetime import datetime, timezone
from typing import Optional, Tuple

TRADES_HEADER = [
    "logged_at",
    "league",
    "slug",
    "title",
    "kickoff",
    "home",
    "away",
    "favourite",
    "underdog",
    "fav_ml_ask",
    "dog_ml_ask",
    "spread_market_id",
    "spread_title",
    "dog_plus_ask",
    "dog_plus_odds",
    "dog_table_pos",
    "fav_table_pos",
    "dog_form",
    "dog_form_pts",
    "strength_ok",
    "strength_reasons",
    "action",
    "fill_cents",
    "contracts",
    "cost_usd",
    "capital_after",
    "resolution",
    "pnl",
    "notes",
]

ASK_HEADER = [
    "logged_at",
    "slug",
    "spread_market_id",
    "dog_plus_ask",
    "dog_plus_odds",
    "seconds_to_kickoff",
]


def _ensure_dir(path: str) -> None:
    folder = os.path.dirname(os.path.abspath(path))
    if folder:
        os.makedirs(folder, exist_ok=True)


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def append_row(path: str, header: list, row: dict) -> None:
    _ensure_dir(path)
    exists = os.path.isfile(path) and os.path.getsize(path) > 0
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=header, extrasaction="ignore")
        if not exists:
            writer.writeheader()
        out = {k: row.get(k, "") for k in header}
        if not out.get("logged_at"):
            out["logged_at"] = _stamp()
        writer.writerow(out)


def log_trade(path: str, row: dict) -> None:
    append_row(path, TRADES_HEADER, row)


def log_ask(path: str, row: dict) -> None:
    append_row(path, ASK_HEADER, row)


def default_log_paths(league: str, base_dir: Optional[str] = None) -> Tuple[str, str]:
    root = base_dir or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
    league = (league or "epl").lower()
    return (
        os.path.join(root, f"sports_{league}_trades.csv"),
        os.path.join(root, f"sports_{league}_asks.csv"),
    )
