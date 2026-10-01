"""Append-only CSV journal of every order the bot plans or places."""
from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

from .config import ROOT

JOURNAL_PATH = ROOT / "logs" / "journal.csv"
FIELDS = ["ts_utc", "mode", "kind", "market_id", "exchange_id", "title", "side", "price", "qty", "fair", "edge", "result"]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def record(row: dict, path: Path = JOURNAL_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
        if is_new:
            writer.writeheader()
        writer.writerow({"ts_utc": utc_now_iso(), **row})
