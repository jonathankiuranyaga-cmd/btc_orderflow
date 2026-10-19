import os
import sqlite3

import app


def test_trade_journal_round_trip(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "paper_trades.db"
    monkeypatch.setattr(app, "DB_PATH", db_path)

    entries = [
        {"Side": "LONG", "Net PnL (USDT)": 12.5, "Exit reason": "Target"},
        {"Side": "SHORT", "Net PnL (USDT)": -4.0, "Exit reason": "Stop"},
    ]

    app.init_trade_journal()
    app.save_trade_journal(entries)
    loaded = app.load_trade_journal()

    assert loaded == entries
    assert db_path.exists()

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0]
    assert rows == 2
