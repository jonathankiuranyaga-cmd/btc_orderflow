"""Shared configuration and design constants.

Every tunable of the desk lives here so the app and its helpers agree on one
set of values. Imports stay module-level constants only: no logic, no I/O.
"""
from __future__ import annotations

from pathlib import Path

BINANCE_BASE = "https://fapi.binance.com"
COINBASE_BASE = "https://api.exchange.coinbase.com"
SYMBOL = "BTCUSDT"
PRODUCT = "BTC-USD"
USDT_PRODUCT = "USDT-USD"

REFRESH_SECONDS = 10
# Full-page auto-reload interval (seconds). Guarantees data freshness even when
# the browser tab is unfocused — Streamlit fragment timers pause when the tab
# is not active, so this JavaScript fallback keeps the data current regardless.
# Session state is restored from SQLite on reload (positions, trades, tape).
AUTO_RELOAD_SECONDS = 10
TRADE_WINDOW_SECONDS = 60
TRADE_RETENTION_SECONDS = 900

# A feed is only stale once it has missed at least one full poll cycle, so the
# threshold must sit above the refresh interval or every poll looks stale.
STALE_AFTER_MS = REFRESH_SECONDS * 1000 + 2500

# The chart shows a short window; the risk lock needs the whole session.
EQUITY_CHART_MAX_SAMPLES = 120
EQUITY_RISK_MAX_SAMPLES = 5000

MARKET_FEEDS = (
    "binance_trades",
    "binance_book",
    "coinbase_spot",
    "usdt_usd",
    "binance_open_interest",
    "binance_funding",
    "binance_daily_klines",
)

FEED_LABELS = {
    "binance_trades": "Binance trades · REST",
    "binance_book": "Binance book · REST",
    "coinbase_spot": "Coinbase BTC-USD · REST",
    "usdt_usd": "Coinbase USDT-USD · REST",
    "binance_open_interest": "Binance open interest · REST",
    "binance_funding": "Binance funding · REST",
    "binance_daily_klines": "Binance daily levels · REST",
}

DB_PATH = Path(__file__).with_name("paper_trades.db")
HEATMAP_PATH = Path(__file__).with_name("heatmap.html")

# Closed-trade journal is append-only; open position and equity peak use
# key/value storage so they survive restarts without polluting the journal.
SESSION_SNAPSHOT_INTERVAL_SECONDS = 20.0

# Closed trades older than this many days are purged automatically on startup
# so the SQLite journal does not grow without bound over weeks of use.
TRADE_JOURNAL_RETENTION_DAYS = 90
