"""Regression tests for the phase audit fixes.

Each test pins the behaviour that was broken so a future change cannot
silently reintroduce it.
"""

import app
from app import (
    calculate_drawdown_pct,
    calculate_trade_statistics,
    count_missed_trades,
    find_jump_zones,
    summarize_book,
)


def test_stale_threshold_exceeds_poll_interval():
    """D1: a 2s threshold under a 3s poll flagged a healthy feed as stale."""
    assert app.STALE_AFTER_MS > app.REFRESH_SECONDS * 1000


def test_count_missed_trades_detects_skipped_aggregate_trades():
    """D9: the REST endpoint caps at 1000 rows, so trades can be dropped."""
    rows = [{"a": 1005}, {"a": 1006}, {"a": 1007}]

    assert count_missed_trades(1004, rows) == 0
    assert count_missed_trades(1001, rows) == 3
    assert count_missed_trades(None, rows) == 0
    assert count_missed_trades(1000, []) == 0


def test_count_missed_trades_never_goes_negative():
    """A duplicate or out-of-order id must not report a negative gap."""
    assert count_missed_trades(2000, [{"a": 1000}, {"a": 1001}]) == 0


def test_find_jump_zones_handles_missing_reference_price():
    """D6: a zero reference price divided by zero and crashed the dashboard."""
    zones = find_jump_zones({"bids": [["1.0", "1.0"]], "asks": [["2.0", "1.0"]]}, 0.0)

    assert zones["up"]["zone"] is None
    assert zones["down"]["zone"] is None
    assert zones["up"]["bands_checked"] == 0


def test_find_jump_zones_handles_empty_book_sides():
    zones = find_jump_zones({"bids": [], "asks": []}, 100.0)

    assert zones["up"]["zone"] is None
    assert zones["down"]["zone"] is None


def test_drawdown_uses_the_peak_not_the_first_sample():
    """D4: the breaker must remember the session peak, not the chart window."""
    curve = [{"eq": 100.0}, {"eq": 120.0}, {"eq": 60.0}]

    assert calculate_drawdown_pct(curve) == 50.0


def test_trade_statistics_reports_risk_adjusted_metrics():
    # The journal stores newest first, so this list reads +10, -10, +20 in time.
    trades = [
        {"Net PnL (USDT)": 20.0, "R": 2.0, "Hold (s)": 30.0, "Fees (USDT)": 1.0},
        {"Net PnL (USDT)": -10.0, "R": -1.0, "Hold (s)": 60.0, "Fees (USDT)": 1.0},
        {"Net PnL (USDT)": 10.0, "R": 1.0, "Hold (s)": 90.0, "Fees (USDT)": 1.0},
    ]

    stats = calculate_trade_statistics(trades)

    assert stats["gross_profit"] == 30.0
    assert stats["gross_loss"] == 10.0
    assert stats["total_fees"] == 3.0
    assert abs(stats["expectancy_r"] - (2.0 / 3.0)) < 1e-9
    assert stats["avg_hold_seconds"] == 60.0
    assert stats["max_consecutive_wins"] == 1
    assert stats["max_consecutive_losses"] == 1
    assert stats["largest_win"] == 20.0
    assert stats["largest_loss"] == -10.0
    assert stats["sharpe_like"] is not None
    assert stats["sortino_like"] is not None


def test_trade_statistics_handles_empty_journal():
    stats = calculate_trade_statistics([])

    assert stats["sharpe_like"] is None
    assert stats["sortino_like"] is None
    assert stats["expectancy_r"] is None
    assert stats["total_fees"] == 0.0
    assert stats["max_consecutive_losses"] == 0


def test_trade_statistics_counts_streaks_in_chronological_order():
    """The journal stores newest first, so streaks must be measured in time order."""
    # Chronological order is: -5, -5, +5.
    newest_first = [
        {"Net PnL (USDT)": 5.0},
        {"Net PnL (USDT)": -5.0},
        {"Net PnL (USDT)": -5.0},
    ]

    stats = calculate_trade_statistics(newest_first)

    assert stats["max_consecutive_losses"] == 2
    assert stats["max_consecutive_wins"] == 1


def test_journal_round_trip_keeps_newest_first_order(tmp_path, monkeypatch):
    """D16: the journal must return trades newest first after a restart."""
    db_path = tmp_path / "paper_trades.db"
    monkeypatch.setattr(app, "DB_PATH", db_path)

    entries = [
        {"Side": "LONG", "Net PnL (USDT)": 12.5},
        {"Side": "SHORT", "Net PnL (USDT)": -4.0},
    ]

    app.save_trade_journal(entries)
    assert app.load_trade_journal() == entries

    app.append_trade_journal({"Side": "LONG", "Net PnL (USDT)": 3.0})
    loaded = app.load_trade_journal()
    assert loaded[0] == {"Side": "LONG", "Net PnL (USDT)": 3.0}
    assert loaded[1:] == entries

    app.clear_trade_journal()
    assert app.load_trade_journal() == []


def test_summarize_book_mid_is_zero_without_both_sides():
    book = summarize_book({"bids": [["100.0", "1.0"]], "asks": []}, 5)

    assert book["mid"] == 0.0
    assert book["spread"] == 0.0
