"""Market data access layer: REST polling and the liquidation WebSocket.

This module is the only place that talks to the network. Analytics functions
consume its normalized payloads; the Streamlit layer never imports requests
directly.
"""
from __future__ import annotations

from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
import json
import threading
import time
from typing import Any

import requests
import streamlit as st
import websocket
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config import (
    BINANCE_BASE,
    COINBASE_BASE,
    MARKET_FEEDS,
    PRODUCT,
    SYMBOL,
    USDT_PRODUCT,
)

SESSION = requests.Session()
SESSION.mount(
    "https://",
    HTTPAdapter(max_retries=Retry(total=3, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504])),
)


def request_json(url: str, params: dict[str, Any] | None = None) -> Any:
    response = SESSION.get(url, params=params, timeout=8)
    response.raise_for_status()
    return response.json()


@st.cache_data(ttl=30, show_spinner=False)
def _fetch_coinbase_price_cached(product: str) -> float:
    """Coinbase ticker price, cached for 30s — basis doesn't need 3s freshness."""
    return float(request_json(f"{COINBASE_BASE}/products/{product}/ticker")["price"])


@st.cache_data(ttl=3, show_spinner=False)
def fetch_binance_feeds() -> tuple[dict[str, Any], dict[str, str]]:
    """Poll Binance REST feeds concurrently (fast path, no Coinbase).

    Cached for 3s so rapid dashboard refreshes within the same window are
    instant cache hits. Safe because update_trade_buffer always processes
    whatever trades are in the cache — the staleness clock advances off the
    trade timestamp, not the fetch timestamp.
    """
    endpoints = [
        ("binance_trades", f"{BINANCE_BASE}/fapi/v1/aggTrades", {"symbol": SYMBOL, "limit": 300}),
        ("binance_book", f"{BINANCE_BASE}/fapi/v1/depth", {"symbol": SYMBOL, "limit": 50}),
        ("binance_open_interest", f"{BINANCE_BASE}/fapi/v1/openInterest", {"symbol": SYMBOL}),
        ("binance_funding", f"{BINANCE_BASE}/fapi/v1/premiumIndex", {"symbol": SYMBOL}),
        ("binance_daily_klines", f"{BINANCE_BASE}/fapi/v1/klines", {"symbol": SYMBOL, "interval": "1d", "limit": 2}),
    ]
    results: dict[str, Any] = {}
    errors: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=len(endpoints)) as pool:
        futures = {
            pool.submit(request_json, url, params): name for name, url, params in endpoints
        }
        for future in as_completed(futures):
            name = futures[future]
            try:
                results[name] = future.result()
            except (requests.RequestException, KeyError, TypeError, ValueError) as error:
                errors[name] = str(error)
    return results, errors


def fetch_market_data() -> tuple[dict[str, Any], dict[str, str]]:
    """Poll every REST feed; Binance fresh each call, Coinbase cached 30s.

    Basis (Coinbase) changes slowly and does not need to block every 3s refresh,
    so spot and USDT-USD are served from a short-lived cache while Binance
    feeds (the signal inputs) are fetched fresh.
    """
    binance_results, binance_errors = fetch_binance_feeds()
    errors = dict(binance_errors)
    results: dict[str, Any] = dict(binance_results)

    for product, key in ((PRODUCT, "coinbase_spot"), (USDT_PRODUCT, "usdt_usd")):
        try:
            results[key] = _fetch_coinbase_price_cached(product)
        except (requests.RequestException, KeyError, TypeError, ValueError) as error:
            results[key] = 0.0
            errors[key] = str(error)
    return results, errors


def fetch_one_minute_klines() -> list[list[Any]]:
    return request_json(
        f"{BINANCE_BASE}/fapi/v1/klines",
        {"symbol": SYMBOL, "interval": "1m", "limit": 1000},
    )


def fetch_backtest_klines(days: int) -> list[list[Any]]:
    """Page through one-minute candles covering the last ``days`` days."""
    end_time = int(datetime.now(timezone.utc).timestamp() * 1000)
    cursor = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
    rows: list[list[Any]] = []
    while cursor < end_time:
        batch = request_json(
            f"{BINANCE_BASE}/fapi/v1/klines",
            {
                "symbol": SYMBOL,
                "interval": "1m",
                "startTime": cursor,
                "endTime": end_time,
                "limit": 1000,
            },
        )
        if not batch:
            break
        rows.extend(batch)
        next_cursor = int(batch[-1][0]) + 60_000
        if next_cursor <= cursor or len(batch) < 1000:
            break
        cursor = next_cursor
    return rows


def normalize_force_order(message: dict[str, Any]) -> dict[str, Any] | None:
    """Convert a Binance forceOrder stream frame into our event shape."""
    if message.get("e") != "forceOrder" or not isinstance(message.get("o"), dict):
        return None
    order = message["o"]
    quantity = float(order.get("z", order.get("q", 0.0)))
    average_price = float(order.get("ap", 0.0))
    price = average_price if average_price > 0 else float(order.get("p", 0.0))
    side = str(order.get("S", "")).upper()
    if quantity <= 0 or price <= 0 or side not in ("BUY", "SELL"):
        return None
    return {
        "time": int(message.get("E", 0)) / 1000,
        "side": side,
        "quantity": quantity,
        "price": price,
        "notional": quantity * price,
        "liquidated": "shorts" if side == "BUY" else "longs",
    }


def run_liquidation_consumer(state: dict[str, Any]) -> None:
    """Background WebSocket worker with exponential-backoff reconnects."""
    url = "wss://fstream.binance.com/ws/btcusdt@forceOrder"
    retry_delay = 1.0

    def on_open(_: Any) -> None:
        nonlocal retry_delay
        with state["lock"]:
            if state["ever_connected"]:
                state["reconnect_count"] += 1
            state["connected"] = True
            state["ever_connected"] = True
            state["last_error"] = None
            state["connected_since"] = time.time()
        # A successful connection resets the backoff so a later blip reconnects fast.
        retry_delay = 1.0

    def on_message(_: Any, payload: str) -> None:
        try:
            message = json.loads(payload)
            event = normalize_force_order(message)
        except (json.JSONDecodeError, TypeError, ValueError, KeyError):
            return
        if event is None:
            return
        with state["lock"]:
            state["events"].append(event)
            state["last_event_at"] = event["time"]

    def on_error(_: Any, error: Any) -> None:
        with state["lock"]:
            state["last_error"] = str(error)

    def on_close(_: Any, __: Any, ___: Any) -> None:
        with state["lock"]:
            state["connected"] = False

    while True:
        client = websocket.WebSocketApp(
            url,
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )
        try:
            client.run_forever(ping_interval=20, ping_timeout=10)
        except Exception as error:
            on_error(client, error)
        with state["lock"]:
            state["connected"] = False
        time.sleep(retry_delay)
        retry_delay = min(retry_delay * 2, 30.0)


@st.cache_resource(show_spinner=False)
def get_liquidation_feed_state() -> dict[str, Any]:
    """Cached worker state: one thread per process, shared across reruns."""
    state: dict[str, Any] = {
        "lock": threading.Lock(),
        "events": deque(maxlen=5000),
        "connected": False,
        "ever_connected": False,
        "connected_since": None,
        "last_event_at": None,
        "last_error": None,
        "reconnect_count": 0,
    }
    worker = threading.Thread(
        target=run_liquidation_consumer,
        args=(state,),
        daemon=True,
        name="binance-force-order-feed",
    )
    worker.start()
    return state


@st.cache_data(ttl=5, show_spinner=False)
def fetch_one_minute_klines() -> list[list[Any]]:
    return request_json(
        f"{BINANCE_BASE}/fapi/v1/klines",
        {"symbol": SYMBOL, "interval": "1m", "limit": 1000},
    )


__all__ = [
    "SESSION",
    "request_json",
    "fetch_market_data",
    "fetch_binance_feeds",
    "fetch_one_minute_klines",
    "fetch_backtest_klines",
    "normalize_force_order",
    "run_liquidation_consumer",
    "get_liquidation_feed_state",
    "MARKET_FEEDS",
]
