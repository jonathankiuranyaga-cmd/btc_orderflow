"""Durable persistence for the paper journal and session state.

A tiny repository over SQLite: closed trades in one append-only table, restart
survival key/value state in another. All SQL lives here so no other module
needs to know the schema.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from config import DB_PATH


def _db_path() -> str:
    """Resolve the DB path on every call so tests can retarget ``app.DB_PATH``."""
    import sys

    app_module = sys.modules.get("app")
    if app_module is not None and getattr(app_module, "DB_PATH", None) is not None:
        return str(app_module.DB_PATH)
    return str(DB_PATH)


def init_trade_journal() -> None:
    with sqlite3.connect(_db_path()) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS paper_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entry TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS session_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        conn.commit()


def save_session_value(key: str, value: Any) -> None:
    """Persist one piece of session state so a restart does not lose it.

    The closed-trade journal is append-only, but an open position and the
    equity peak behind the drawdown breaker are not trades, so they need their
    own key/value storage.
    """
    init_trade_journal()
    with sqlite3.connect(_db_path()) as conn:
        conn.execute(
            "INSERT INTO session_state(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value, default=str)),
        )
        conn.commit()


def load_session_value(key: str, default: Any = None) -> Any:
    init_trade_journal()
    with sqlite3.connect(_db_path()) as conn:
        row = conn.execute("SELECT value FROM session_state WHERE key = ?", (key,)).fetchone()
    if row is None:
        return default
    try:
        return json.loads(row[0])
    except (TypeError, ValueError):
        return default


def delete_session_value(key: str) -> None:
    init_trade_journal()
    with sqlite3.connect(_db_path()) as conn:
        conn.execute("DELETE FROM session_state WHERE key = ?", (key,))
        conn.commit()


def save_trade_journal(trades: list[dict[str, Any]]) -> None:
    """Replace the journal contents (used when paper history is cleared).

    ``trades`` uses the session convention of newest first. Rows are stored in
    chronological order so that ``load_trade_journal`` can hand them back
    newest first again after a restart.
    """
    init_trade_journal()
    with sqlite3.connect(_db_path()) as conn:
        conn.execute("DELETE FROM paper_trades")
        for trade in reversed(trades):
            conn.execute("INSERT INTO paper_trades(entry) VALUES (?)", (json.dumps(trade, default=str),))
        conn.commit()


def append_trade_journal(trade: dict[str, Any]) -> None:
    """Append one closed trade. The journal is append-only so history survives restarts."""
    init_trade_journal()
    with sqlite3.connect(_db_path()) as conn:
        conn.execute("INSERT INTO paper_trades(entry) VALUES (?)", (json.dumps(trade, default=str),))
        conn.commit()


def clear_trade_journal() -> None:
    init_trade_journal()
    with sqlite3.connect(_db_path()) as conn:
        conn.execute("DELETE FROM paper_trades")
        conn.commit()


def load_trade_journal() -> list[dict[str, Any]]:
    """Newest first, matching the in-memory ordering used by the session."""
    init_trade_journal()
    with sqlite3.connect(_db_path()) as conn:
        rows = conn.execute("SELECT entry FROM paper_trades ORDER BY id DESC").fetchall()
    return [json.loads(row[0]) for row in rows]
