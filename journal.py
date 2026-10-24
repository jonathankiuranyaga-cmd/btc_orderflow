"""Durable persistence for the paper journal and session state.

A tiny repository over SQLite: closed trades in one append-only table, restart
survival key/value state in another. All SQL lives here so no other module
needs to know the schema.

Safety layer:
* WAL journal mode so a crash mid-write cannot corrupt the database.
* Timestamped backup before every destructive write (purge / clear / replace),
  stored in ``backups/`` next to the DB.
* Append-only JSONL crash journal so every market snapshot, decision, and trade
  can be replayed even if SQLite is lost or corrupted.
* Self-healing on startup: if the primary DB is unreadable, the newest backup
  is restored automatically.
* One-click export of the full data set (DB + crash journal + session state)
  to a timestamped folder.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from config import DB_PATH

BACKUP_DIR = Path(__file__).with_name("backups")
CRASH_JOURNAL_PATH = Path(__file__).with_name("crash_journal.jsonl")
EXPORT_DIR = Path(__file__).with_name("exports")
MAX_BACKUPS = 25


def _db_path() -> str:
    """Resolve the DB path on every call so tests can retarget ``app.DB_PATH``."""
    import sys

    app_module = sys.modules.get("app")
    if app_module is not None and getattr(app_module, "DB_PATH", None) is not None:
        return str(app_module.DB_PATH)
    return str(DB_PATH)


def _ensure_dirs() -> None:
    BACKUP_DIR.mkdir(exist_ok=True)
    EXPORT_DIR.mkdir(exist_ok=True)


def _rotate_backups() -> None:
    """Keep only the newest MAX_BACKUPS timestamped copies."""
    backups = sorted(BACKUP_DIR.glob("paper_trades_*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in backups[MAX_BACKUPS:]:
        old.unlink(missing_ok=True)


def backup_database(label: str = "auto") -> Path | None:
    """Copy the live DB to a timestamped backup in ``backups/``.

    Returns the backup path, or None when there is nothing to back up yet.
    """
    _ensure_dirs()
    src = Path(_db_path())
    if not src.exists():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    dst = BACKUP_DIR / f"paper_trades_{stamp}_{label}.db"
    shutil.copy2(src, dst)
    _rotate_backups()
    return dst


def _connect_wal(db_path: str) -> sqlite3.Connection:
    """Open a connection in WAL mode with crash-safe settings."""
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    return conn


def init_trade_journal() -> None:
    """Create tables if missing and enable WAL mode; restore from backup if corrupt."""
    _ensure_dirs()
    db_path = _db_path()
    try:
        conn = _connect_wal(db_path)
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
        conn.close()
    except sqlite3.DatabaseError:
        # Primary DB is corrupt: restore the newest backup automatically.
        backups = sorted(BACKUP_DIR.glob("paper_trades_*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
        if backups:
            shutil.copy2(backups[0], db_path)
            conn = _connect_wal(db_path)
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
            conn.close()


def save_session_value(key: str, value: Any) -> None:
    """Persist one piece of session state so a restart does not lose it."""
    init_trade_journal()
    backup_database("pre_session_write")
    with _connect_wal(_db_path()) as conn:
        conn.execute(
            "INSERT INTO session_state(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value, default=str)),
        )
        conn.commit()


def load_session_value(key: str, default: Any = None) -> Any:
    init_trade_journal()
    try:
        with _connect_wal(_db_path()) as conn:
            row = conn.execute("SELECT value FROM session_state WHERE key = ?", (key,)).fetchone()
    except sqlite3.DatabaseError:
        return default
    if row is None:
        return default
    try:
        return json.loads(row[0])
    except (TypeError, ValueError):
        return default


def delete_session_value(key: str) -> None:
    init_trade_journal()
    backup_database("pre_session_delete")
    with _connect_wal(_db_path()) as conn:
        conn.execute("DELETE FROM session_state WHERE key = ?", (key,))
        conn.commit()


def save_trade_journal(trades: list[dict[str, Any]]) -> None:
    """Replace the journal contents (used when paper history is cleared).

    ``trades`` uses the session convention of newest first. Rows are stored in
    chronological order so that ``load_trade_journal`` can hand them back
    newest first again after a restart.
    """
    init_trade_journal()
    backup_database("pre_journal_replace")
    with _connect_wal(_db_path()) as conn:
        conn.execute("DELETE FROM paper_trades")
        for trade in reversed(trades):
            conn.execute("INSERT INTO paper_trades(entry) VALUES (?)", (json.dumps(trade, default=str),))
        conn.commit()


def append_trade_journal(trade: dict[str, Any]) -> None:
    """Append one closed trade. The journal is append-only so history survives restarts."""
    init_trade_journal()
    backup_database("pre_trade_append")
    with _connect_wal(_db_path()) as conn:
        conn.execute("INSERT INTO paper_trades(entry) VALUES (?)", (json.dumps(trade, default=str),))
        conn.commit()
    _append_crash_journal({"type": "trade_closed", "trade": trade})


def clear_trade_journal() -> None:
    init_trade_journal()
    backup_database("pre_journal_clear")
    with _connect_wal(_db_path()) as conn:
        conn.execute("DELETE FROM paper_trades")
        conn.commit()
    _append_crash_journal({"type": "journal_cleared"})


def load_trade_journal() -> list[dict[str, Any]]:
    """Newest first, matching the in-memory ordering used by the session."""
    init_trade_journal()
    try:
        with _connect_wal(_db_path()) as conn:
            rows = conn.execute("SELECT entry FROM paper_trades ORDER BY id DESC").fetchall()
    except sqlite3.DatabaseError:
        return []
    return [json.loads(row[0]) for row in rows]


def purge_trades_older_than(days: int) -> int:
    """Delete closed trades whose ``Closed (UTC)`` timestamp is older than ``days``.

    Returns the number of rows deleted. Safe to call on every startup; the
    journal stays append-only for active history while old, unused data is
    reclaimed automatically. A backup is taken before any deletion so purged
    data can be recovered if needed.
    """
    init_trade_journal()
    backup_database("pre_purge")
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    with _connect_wal(_db_path()) as conn:
        rows = conn.execute("SELECT id, entry FROM paper_trades ORDER BY id DESC").fetchall()
        deleted = 0
        for row_id, entry in rows:
            try:
                trade = json.loads(entry)
                closed_at = datetime.fromisoformat(str(trade.get("Closed (UTC)", "")))
                if closed_at < cutoff:
                    conn.execute("DELETE FROM paper_trades WHERE id = ?", (row_id,))
                    deleted += 1
            except (TypeError, ValueError, AttributeError):
                continue
        conn.commit()
    _append_crash_journal({"type": "purge", "deleted": deleted, "older_than_days": days})
    return deleted


# ---------------------------------------------------------------------------
# Crash journal: append-only JSONL replay log
# ---------------------------------------------------------------------------


def _append_crash_journal(record: dict[str, Any]) -> None:
    """Append one line to the crash journal for full session replay."""
    _ensure_dirs()
    envelope = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "record": record,
    }
    with CRASH_JOURNAL_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(envelope, default=str) + "\n")


def log_market_snapshot(
    now: float,
    feed_results: dict[str, Any],
    feed_errors: dict[str, str],
    decision_signal: str | None,
    forecast: dict[str, Any] | None,
    market_regime: dict[str, Any] | None,
) -> None:
    """Record one full refresh cycle: feeds, decision, forecast, regime."""
    _append_crash_journal(
        {
            "type": "market_snapshot",
            "now": now,
            "feed_errors": feed_errors,
            "decision_signal": decision_signal,
            "forecast": forecast,
            "market_regime": market_regime,
        }
    )


def log_position_event(event: str, position: dict[str, Any] | None, mark: float | None) -> None:
    """Record open / close / stop / target events for the paper position."""
    _append_crash_journal(
        {
            "type": "position_event",
            "event": event,
            "position": position,
            "mark": mark,
        }
    )


def read_crash_journal(limit: int | None = None) -> list[dict[str, Any]]:
    """Read crash-journal lines, newest first."""
    if not CRASH_JOURNAL_PATH.exists():
        return []
    lines = CRASH_JOURNAL_PATH.read_text(encoding="utf-8").splitlines()
    records = [json.loads(line) for line in lines if line.strip()]
    if limit is not None:
        records = records[-limit:]
    return list(reversed(records))


# ---------------------------------------------------------------------------
# Export / restore
# ---------------------------------------------------------------------------


def export_all_data(export_name: str | None = None) -> Path:
    """Copy DB, crash journal, and session-state snapshot into a timestamped folder.

    Returns the export directory path. Safe to run while the app is live.
    """
    _ensure_dirs()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    name = export_name or f"export_{stamp}"
    dest = EXPORT_DIR / name
    dest.mkdir(parents=True, exist_ok=True)

    db_path = Path(_db_path())
    if db_path.exists():
        shutil.copy2(db_path, dest / db_path.name)
    if CRASH_JOURNAL_PATH.exists():
        shutil.copy2(CRASH_JOURNAL_PATH, dest / CRASH_JOURNAL_PATH.name)
    return dest


def list_backups() -> list[Path]:
    """List available backups, newest first."""
    _ensure_dirs()
    return sorted(BACKUP_DIR.glob("paper_trades_*.db"), key=lambda p: p.stat().st_mtime, reverse=True)


def list_exports() -> list[Path]:
    """List available export folders, newest first."""
    _ensure_dirs()
    return sorted(EXPORT_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True) if EXPORT_DIR.exists() else []


def restore_from_backup(backup_path: Path | str) -> None:
    """Replace the live DB with a backup file. Caller must restart the app."""
    _ensure_dirs()
    src = Path(backup_path)
    if not src.exists():
        raise FileNotFoundError(f"Backup not found: {src}")
    db_path = Path(_db_path())
    backup_database("pre_restore")
    shutil.copy2(src, db_path)
