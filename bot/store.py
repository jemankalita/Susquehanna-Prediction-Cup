"""SQLite store: market snapshots (Super Market vs Kalshi), our orders, and our fills."""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from .config import ROOT

DB_PATH = ROOT / "logs" / "bot.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    ts TEXT NOT NULL, exchange_id TEXT NOT NULL, market_id TEXT, title TEXT,
    sm_bid REAL, sm_ask REAL, sm_last REAL, k_bid REAL, k_ask REAL, fair REAL,
    PRIMARY KEY (ts, exchange_id)
);
CREATE TABLE IF NOT EXISTS orders (
    ts TEXT NOT NULL, mode TEXT, kind TEXT, market_id TEXT, exchange_id TEXT, title TEXT,
    side TEXT, price REAL, qty INTEGER, fair REAL, result TEXT
);
CREATE TABLE IF NOT EXISTS fills (
    fill_id TEXT PRIMARY KEY, order_id TEXT, exchange_id TEXT, market_id TEXT,
    side TEXT, price REAL, qty REAL, filled_at TEXT, fair_at_sync REAL
);
CREATE INDEX IF NOT EXISTS ix_snap_ex ON snapshots (exchange_id, ts);
"""


class Store:
    def __init__(self, path: Path = DB_PATH):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with closing(self._connect()) as con:
            con.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=30)
        con.execute("PRAGMA journal_mode=WAL")
        return con

    def save_snapshots(self, rows: list[tuple]) -> None:
        """rows: (ts, exchange_id, market_id, title, sm_bid, sm_ask, sm_last, k_bid, k_ask, fair)"""
        with closing(self._connect()) as con, con:
            con.executemany("INSERT OR IGNORE INTO snapshots VALUES (?,?,?,?,?,?,?,?,?,?)", rows)

    def save_order(self, row: dict) -> None:
        cols = ("ts", "mode", "kind", "market_id", "exchange_id", "title", "side", "price", "qty", "fair", "result")
        with closing(self._connect()) as con, con:
            con.execute(f"INSERT INTO orders VALUES ({','.join('?' * len(cols))})", tuple(row.get(c) for c in cols))

    def save_fills(self, fills: list[dict], fairs: dict[str, float | None]) -> int:
        """Insert unseen fills; fair_at_sync is the fair of the side bought at sync time. Returns new count."""
        rows = []
        for f in fills:
            fair_yes = fairs.get(str(f["exchangeId"]))
            side_fair = None if fair_yes is None else (fair_yes if f["side"] == "yes" else 1 - fair_yes)
            rows.append((str(f["id"]), str(f.get("orderId")), str(f["exchangeId"]), str(f.get("marketId")),
                         f["side"], float(f["price"]), abs(float(f["quantity"])), f.get("filledAt"), side_fair))
        with closing(self._connect()) as con, con:
            before = con.total_changes
            con.executemany("INSERT OR IGNORE INTO fills VALUES (?,?,?,?,?,?,?,?,?)", rows)
            return con.total_changes - before

    def query(self, sql: str, params: tuple = ()) -> list[tuple]:
        with closing(self._connect()) as con:
            return con.execute(sql, params).fetchall()
