from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
import json
import sqlite3
from pathlib import Path
from statistics import median
from typing import Any

import altair as alt
import requests
import streamlit as st
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from backtest import run_backtest, run_walk_forward
from forecast import forecast_next_minute

BINANCE_BASE = "https://fapi.binance.com"
COINBASE_BASE = "https://api.exchange.coinbase.com"
SYMBOL = "BTCUSDT"
PRODUCT = "BTC-USD"
REFRESH_SECONDS = 3
TRADE_WINDOW_SECONDS = 60
TRADE_RETENTION_SECONDS = 900
DB_PATH = Path(__file__).with_name("paper_trades.db")

SESSION = requests.Session()
SESSION.mount(
    "https://",
    HTTPAdapter(max_retries=Retry(total=3, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504])),
)

_forecast_cache: dict[tuple[int, int], dict[str, Any] | None] = {}

st.set_page_config(page_title="BTC Order Flow | Paper Desk", page_icon="₿", layout="wide")
st.markdown(
    """
    <style>
    :root {
        --canvas: #f2f5f2;
        --paper: #ffffff;
        --ink: #17241d;
        --muted: #65756c;
        --line: #d9e3dc;
        --green: #177a55;
        --red: #c34a43;
        --amber: #a76a21;
        --sidebar: #17271f;
    }
    .stApp, [data-testid="stAppViewContainer"] { background: var(--canvas); color: var(--ink); }
    [data-testid="stHeader"] { background: transparent; }
    main .block-container { max-width: 1600px; padding: 2rem 2.5rem 3rem; }
    h1, h2, h3, p { letter-spacing: 0 !important; }
    h1 { color: var(--ink); font-family: Bahnschrift, "Segoe UI", sans-serif; font-size: 2.25rem; font-weight: 700; }
    h2, h3 { color: var(--ink); font-family: Bahnschrift, "Segoe UI", sans-serif; }
    [data-testid="stCaptionContainer"] { color: var(--muted); }
    [data-testid="stMetric"] {
        background: var(--paper);
        border: 1px solid var(--line);
        border-top: 3px solid var(--green);
        border-radius: 5px;
        padding: 0.9rem 1rem;
        min-height: 6rem;
        box-shadow: 0 2px 7px rgba(23, 36, 29, 0.04);
    }
    [data-testid="stMetricLabel"] p { color: var(--muted); font-size: 0.72rem; font-weight: 700; }
    [data-testid="stMetricValue"] { color: var(--ink); font-size: 1.4rem; font-variant-numeric: tabular-nums; }
    [data-testid="stMetricDelta"] { font-variant-numeric: tabular-nums; }
    [data-testid="stSidebar"] { background: var(--sidebar); border-right: 1px solid #294235; }
    [data-testid="stSidebar"] h1, [data-testid="stSidebar"] h2,
    [data-testid="stSidebar"] h3, [data-testid="stSidebar"] p,
    [data-testid="stSidebar"] label { color: #e3ece6; }
    [data-testid="stSidebar"] [data-testid="stWidgetLabel"] p { color: #d1ded5; }
    [data-testid="stSidebar"] hr { border-color: #40554a; }
    [data-testid="stSidebar"] input { background: #f9fbf9; color: var(--ink); }
    [data-testid="stSidebar"] [data-testid="stSliderThumbValue"] { color: #73c59b !important; }
    [data-testid="stSidebar"] [data-testid="stSlider"] [role="group"] > div > div {
        background-color: #73c59b !important;
    }
    [data-testid="stSidebar"] [role="slider"] { background-color: #73c59b; border-color: #73c59b; }
    [data-testid="stProgress"] > div > div > div { background: var(--green); }
    [data-testid="stAlert"] { border-radius: 4px; }
    [data-testid="stDataFrame"] { border: 1px solid var(--line); border-radius: 5px; }
    div.stButton > button { border-radius: 4px; font-weight: 650; min-height: 2.5rem; }
    div.stButton > button[kind="primary"] { background: var(--green); border-color: var(--green); }
    hr { border-color: var(--line); }
    @media (max-width: 760px) {
        main .block-container { padding: 1.25rem 1rem 2rem; }
        h1 { font-size: 1.8rem; }
    }
    </style>
    """,
    unsafe_allow_html=True,
)
st.title("BTC Order-Flow Paper Desk")
st.caption("Scalping dashboard · public exchange data · paper execution only")


def request_json(url: str, params: dict[str, Any] | None = None) -> Any:
    response = SESSION.get(url, params=params, timeout=8)
    response.raise_for_status()
    return response.json()


def fetch_market_data() -> tuple[list[dict[str, Any]], dict[str, Any], float]:
    endpoints = [
        ("trades", f"{BINANCE_BASE}/fapi/v1/aggTrades", {"symbol": SYMBOL, "limit": 1000}),
        ("book", f"{BINANCE_BASE}/fapi/v1/depth", {"symbol": SYMBOL, "limit": 1000}),
        ("ticker", f"{COINBASE_BASE}/products/{PRODUCT}/ticker", None),
    ]
    results: dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {
            pool.submit(request_json, url, params): name for name, url, params in endpoints
        }
        for future in as_completed(futures):
            name = futures[future]
            results[name] = future.result()
    return results["trades"], results["book"], float(results["ticker"]["price"])


@st.cache_data(ttl=5, show_spinner=False)
def fetch_one_minute_klines() -> list[list[Any]]:
    return request_json(
        f"{BINANCE_BASE}/fapi/v1/klines",
        {"symbol": SYMBOL, "interval": "1m", "limit": 1000},
    )


@st.cache_data(ttl=900, show_spinner=False)
def fetch_backtest_klines(days: int) -> list[list[Any]]:
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


def cached_forecast(
    last_close_ms: int, rows: tuple[tuple[Any, ...], ...]
) -> dict[str, float | int | str] | None:
    key = (last_close_ms, len(rows))
    if key in _forecast_cache:
        return _forecast_cache[key]
    result = forecast_next_minute(list(rows)) if rows else None
    _forecast_cache[key] = result
    return result


def init_trade_journal() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS paper_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entry TEXT NOT NULL
            )
            """
        )
        conn.commit()


def save_trade_journal(trades: list[dict[str, Any]]) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM paper_trades")
        for trade in trades:
            conn.execute("INSERT INTO paper_trades(entry) VALUES (?)", (json.dumps(trade, default=str),))
        conn.commit()


def load_trade_journal() -> list[dict[str, Any]]:
    init_trade_journal()
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("SELECT entry FROM paper_trades ORDER BY id").fetchall()
    return [json.loads(row[0]) for row in rows]


def initialize_state() -> None:
    defaults: dict[str, Any] = {
        "trade_buffer": [],
        "last_trade_id": None,
        "paper_position": None,
        "paper_trades": load_trade_journal(),
        "starting_equity": 10_000.0,
        "utc_day": datetime.now(timezone.utc).date().isoformat(),
        "last_data_error": None,
        "last_success_ts": None,
        "equity_history": [],
        "cooldown_until": None,
        "backtest_result": None,
        "backtest_error": None,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def update_trade_buffer(rows: list[dict[str, Any]]) -> None:
    """Add unseen Binance aggregate trades; m=True means sell aggressor."""
    last_id = st.session_state.last_trade_id
    fresh = [row for row in rows if last_id is None or int(row["a"]) > last_id]
    buffer = st.session_state.trade_buffer
    for row in fresh:
        buffer.append(
            {
                "id": int(row["a"]),
                "time": int(row["T"]) / 1000,
                "price": float(row["p"]),
                "quantity": float(row["q"]),
                "sell_aggressor": bool(row["m"]),
            }
        )
    if rows:
        st.session_state.last_trade_id = max(int(row["a"]) for row in rows)
    cutoff = datetime.now(timezone.utc).timestamp() - TRADE_RETENTION_SECONDS
    st.session_state.trade_buffer = [trade for trade in buffer if trade["time"] >= cutoff]


def summarize_flow(trades: list[dict[str, Any]]) -> dict[str, float | int]:
    now = datetime.now(timezone.utc).timestamp()
    recent = [trade for trade in trades if trade["time"] >= now - TRADE_WINDOW_SECONDS]
    buy_qty = sum(t["quantity"] for t in recent if not t["sell_aggressor"])
    sell_qty = sum(t["quantity"] for t in recent if t["sell_aggressor"])
    total = buy_qty + sell_qty
    delta = buy_qty - sell_qty
    return {
        "buy_qty": buy_qty,
        "sell_qty": sell_qty,
        "delta": delta,
        "delta_fraction": delta / total if total else 0.0,
        "trade_count": len(recent),
        "total": total,
    }


def aggregate_trade_candles(
    trades: list[dict[str, Any]], now: float, window_seconds: int, interval_seconds: int = 10
) -> list[dict[str, Any]]:
    candles: dict[int, dict[str, Any]] = {}
    window_start = now - window_seconds
    for trade in sorted(trades, key=lambda item: float(item["time"])):
        trade_time = float(trade["time"])
        if trade_time < window_start or trade_time > now:
            continue
        bucket_start = int(trade_time // interval_seconds) * interval_seconds
        price = float(trade["price"])
        candle = candles.get(bucket_start)
        if candle is None:
            candles[bucket_start] = {
                "timestamp": bucket_start,
                "Open": price,
                "High": price,
                "Low": price,
                "Close": price,
                "Trades": 1,
            }
        else:
            candle["High"] = max(candle["High"], price)
            candle["Low"] = min(candle["Low"], price)
            candle["Close"] = price
            candle["Trades"] += 1

    result = []
    for candle in candles.values():
        candle["UTC time"] = datetime.fromtimestamp(candle.pop("timestamp"), timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        candle["Direction"] = "Up" if candle["Close"] >= candle["Open"] else "Down"
        result.append(candle)
    return result


def summarize_flow_intervals(
    trades: list[dict[str, Any]], now: float, interval_seconds: int = 10, window_seconds: int = 60
) -> list[dict[str, Any]]:
    interval_count = window_seconds // interval_seconds
    buckets = [{"buy_qty": 0.0, "sell_qty": 0.0, "trade_count": 0} for _ in range(interval_count)]
    window_start = now - window_seconds
    for trade in trades:
        trade_time = float(trade["time"])
        if trade_time < window_start or trade_time > now:
            continue
        index = min(int((trade_time - window_start) // interval_seconds), interval_count - 1)
        bucket = buckets[index]
        if trade["sell_aggressor"]:
            bucket["sell_qty"] += float(trade["quantity"])
        else:
            bucket["buy_qty"] += float(trade["quantity"])
        bucket["trade_count"] += 1

    intervals = []
    for index, bucket in enumerate(buckets):
        delta = bucket["buy_qty"] - bucket["sell_qty"]
        intervals.append(
            {
                "Interval": f"{window_seconds - (index + 1) * interval_seconds}-{window_seconds - index * interval_seconds}s ago",
                "Buyer-initiated (BTC)": bucket["buy_qty"],
                "Seller-initiated (BTC)": bucket["sell_qty"],
                "Net delta (BTC)": delta,
                "Trades": bucket["trade_count"],
            }
        )
    return intervals


def describe_aggressor_strength(
    flow: dict[str, Any], min_flow: float, min_trades: int, min_total_btc: float
) -> str:
    if int(flow["trade_count"]) < min_trades or float(flow["total"]) < min_total_btc:
        return "Insufficient 60s flow"
    delta_fraction = float(flow["delta_fraction"])
    if delta_fraction >= min_flow:
        return "Strong buyer aggression"
    if delta_fraction <= -min_flow:
        return "Strong seller aggression"
    if delta_fraction > 0:
        return "Buyers lead, below strong threshold"
    if delta_fraction < 0:
        return "Sellers lead, below strong threshold"
    return "Balanced aggressor flow"


def absorption_heuristic(recent: list[dict[str, Any]], flow: dict[str, Any]) -> dict[str, str] | None:
    """Flag strong one-sided aggression that fails to move price (possible absorption)."""
    delta_abs = abs(float(flow["delta_fraction"]))
    if delta_abs < 0.40:
        return None
    prices = [t["price"] for t in recent]
    first_price, last_price = prices[0], prices[-1]
    move_bps = (last_price / first_price - 1) * 10_000 if first_price else 0.0
    steps = [
        abs(prices[i + 1] / prices[i] - 1) * 10_000
        for i in range(len(prices) - 1)
        if prices[i]
    ]
    typical_step = median(steps) if steps else 0.0
    threshold_bps = max(3.0, typical_step * 2.0)
    if abs(move_bps) >= threshold_bps:
        return None
    side = "buyer" if flow["delta"] > 0 else "seller"
    absorbing_side = "sellers" if flow["delta"] > 0 else "buyers"
    return {
        "message": (
            f"Absorption: strong {side} aggression (delta {flow['delta_fraction']:+.1%}) but price only moved "
            f"{move_bps:+.1f} bps (typical step {typical_step:.1f} bps) — {absorbing_side} may be absorbing."
        )
    }


def summarize_book(book: dict[str, Any], levels: int) -> dict[str, Any]:
    bids_raw = book.get("bids") or []
    asks_raw = book.get("asks") or []
    bids = sorted(((float(price), float(qty)) for price, qty in bids_raw[:levels]), reverse=True)
    asks = sorted((float(price), float(qty)) for price, qty in asks_raw[:levels])
    bid_qty = sum(qty for _, qty in bids)
    ask_qty = sum(qty for _, qty in asks)
    total_qty = bid_qty + ask_qty
    if not bids or not asks:
        best_bid = 0.0
        best_ask = 0.0
    else:
        best_bid, best_ask = bids[0][0], asks[0][0]
    return {
        "bids": bids,
        "asks": asks,
        "bid_qty": bid_qty,
        "ask_qty": ask_qty,
        "imbalance": (bid_qty - ask_qty) / total_qty if total_qty else 0.0,
        "mid": (best_bid + best_ask) / 2 if bids and asks else 0.0,
        "spread": best_ask - best_bid if bids and asks else 0.0,
    }


def calculate_consecutive_losses(trades: list[dict[str, Any]]) -> int:
    streak = 0
    best_streak = 0
    for trade in trades:
        pnl = float(trade.get("Net PnL (USDT)", 0.0))
        if pnl < 0:
            streak += 1
            best_streak = max(best_streak, streak)
        else:
            streak = 0
    return best_streak


def calculate_drawdown_pct(equity_curve: list[dict[str, Any]]) -> float:
    if not equity_curve:
        return 0.0
    values = [float(item.get("eq", 0.0)) for item in equity_curve if "eq" in item]
    if not values:
        return 0.0
    peak = values[0]
    max_drawdown = 0.0
    for value in values:
        if value > peak:
            peak = value
        if peak <= 0:
            continue
        drawdown = (peak - value) / peak * 100.0
        if drawdown > max_drawdown:
            max_drawdown = drawdown
    return max_drawdown


def find_jump_zones(
    book: dict[str, Any], reference_price: float, band_bps: float = 1.0,
    max_distance_bps: float = 12.0, thin_ratio: float = 0.5,
) -> dict[str, dict[str, Any]]:
    zones: dict[str, dict[str, Any]] = {}
    for side, rows in (("up", book["asks"]), ("down", book["bids"])):
        sign = 1 if side == "up" else -1
        distances = [
            sign * (float(price) - reference_price) / reference_price * 10_000
            for price, _ in rows
        ]
        coverage_bps = max((distance for distance in distances if distance >= 0), default=0.0)
        band_count = min(int(max_distance_bps / band_bps), int(coverage_bps / band_bps))
        result: dict[str, Any] = {"zone": None, "coverage_bps": coverage_bps, "bands_checked": band_count}
        if band_count < 4:
            zones[side] = result
            continue

        band_depths = [0.0] * band_count
        for (price, quantity), distance in zip(rows, distances):
            if distance < 0:
                continue
            band_index = int(distance / band_bps)
            if band_index < band_count:
                band_depths[band_index] += float(quantity)

        typical_depth = median(band_depths)
        if typical_depth <= 0:
            zones[side] = result
            continue
        thin_index = next(
            (index for index, depth in enumerate(band_depths) if depth < typical_depth * thin_ratio),
            None,
        )
        if thin_index is not None:
            first_edge = reference_price * (1 + sign * thin_index * band_bps / 10_000)
            second_edge = reference_price * (1 + sign * (thin_index + 1) * band_bps / 10_000)
            result["zone"] = {
                "lower_price": min(first_edge, second_edge),
                "upper_price": max(first_edge, second_edge),
                "near_bps": thin_index * band_bps,
                "depth_btc": band_depths[thin_index],
                "typical_depth_btc": typical_depth,
                "depth_ratio": band_depths[thin_index] / typical_depth,
            }
        zones[side] = result
    return zones


def get_signal(flow: dict[str, Any], book: dict[str, Any], min_flow: float, min_book: float) -> str:
    delta_fraction = float(flow["delta_fraction"])
    imbalance = float(book["imbalance"])
    if delta_fraction >= min_flow and imbalance >= min_book:
        return "LONG"
    if delta_fraction <= -min_flow and imbalance <= -min_book:
        return "SHORT"
    return "FLAT"


def calculate_unrealized(position: dict[str, Any], mark: float, fee_bps: float, slippage_bps: float) -> float:
    side = int(position["side"])
    exit_fill = mark * (1 - side * slippage_bps / 10_000)
    exit_fee = exit_fill * position["quantity"] * fee_bps / 10_000
    return side * (exit_fill - position["entry"]) * position["quantity"] - position["entry_fee"] - exit_fee


def close_position(mark: float, reason: str, fee_bps: float, slippage_bps: float) -> None:
    position = st.session_state.paper_position
    if position is None:
        return
    side = int(position["side"])
    exit_fill = mark * (1 - side * slippage_bps / 10_000)
    exit_fee = exit_fill * position["quantity"] * fee_bps / 10_000
    net_pnl = (
        side * (exit_fill - position["entry"]) * position["quantity"]
        - position["entry_fee"]
        - exit_fee
    )
    risk_budget = position.get("risk_budget", 0.0)
    r_multiple = net_pnl / risk_budget if risk_budget > 0 else 0.0
    trade_record = {
        "Closed (UTC)": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "Side": position["label"],
        "Entry": position["entry"],
        "Exit": exit_fill,
        "Quantity (BTC)": position["quantity"],
        "Net PnL (USDT)": net_pnl,
        "R": r_multiple,
        "Exit reason": reason,
    }
    st.session_state.paper_trades.insert(0, trade_record)
    save_trade_journal(st.session_state.paper_trades)
    st.session_state.paper_position = None
    if net_pnl < 0:
        st.session_state.cooldown_until = datetime.now(timezone.utc).timestamp() + 300.0


def open_position(
    side: int,
    mark: float,
    equity: float,
    risk_fraction: float,
    stop_bps: float,
    max_leverage: float,
    fee_bps: float,
    slippage_bps: float,
    reward_risk: float,
) -> str | None:
    if st.session_state.paper_position is not None:
        return "Close the open position before opening another."
    if equity <= 0 or stop_bps <= 0:
        return "Equity and stop distance must be positive."
    entry = mark * (1 + side * slippage_bps / 10_000)
    stop_distance = entry * stop_bps / 10_000
    risk_budget = equity * risk_fraction
    estimated_stop_loss_per_btc = stop_distance + entry * (2 * fee_bps + 2 * slippage_bps) / 10_000
    quantity = min(risk_budget / estimated_stop_loss_per_btc, equity * max_leverage / entry)
    if quantity <= 0:
        return "Calculated position size is zero."
    st.session_state.paper_position = {
        "side": side,
        "label": "LONG" if side > 0 else "SHORT",
        "entry": entry,
        "quantity": quantity,
        "stop": entry - side * stop_distance,
        "target": entry + side * stop_distance * reward_risk,
        "entry_fee": entry * quantity * fee_bps / 10_000,
        "opened": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "risk_budget": risk_budget,
    }
    return None


initialize_state()

with st.sidebar:
    st.header("Scalping controls")
    starting_equity = st.number_input("Starting paper equity (USDT)", min_value=10.0, value=10_000.0, step=500.0)
    risk_percent = st.slider("Risk per trade (%)", min_value=0.1, max_value=2.0, value=0.5, step=0.1)
    max_leverage = st.slider("Maximum notional / equity", min_value=1.0, max_value=5.0, value=1.0, step=0.5)
    stop_bps = st.number_input("Stop distance (bps)", min_value=2.0, max_value=200.0, value=20.0, step=2.0)
    reward_risk = st.number_input("Target risk/reward", min_value=0.5, max_value=5.0, value=2.0, step=0.25)
    fee_bps = st.number_input("Fee per side (bps)", min_value=0.0, max_value=50.0, value=5.0, step=0.5)
    slippage_bps = st.number_input("Slippage per side (bps)", min_value=0.0, max_value=50.0, value=1.0, step=0.5)
    daily_loss_percent = st.number_input("Daily loss limit (%)", min_value=0.5, max_value=20.0, value=2.0, step=0.5)
    max_drawdown_percent = st.number_input("Max drawdown (%)", min_value=1.0, max_value=100.0, value=25.0, step=1.0)
    max_consecutive_losses = st.number_input("Max consecutive losses", min_value=1, max_value=20, value=4, step=1)
    st.divider()
    st.subheader("Signal thresholds")
    min_flow = st.slider("Minimum 60s delta / volume", min_value=0.05, max_value=0.80, value=0.20, step=0.05)
    min_book = st.slider("Minimum top-book imbalance", min_value=0.05, max_value=0.80, value=0.15, step=0.05)
    min_trades = st.number_input("Min 60s trades", min_value=1, max_value=50, value=5, step=1)
    min_total_btc = st.number_input("Min 60s total (BTC)", min_value=0.0, max_value=100.0, value=1.0, step=0.5)
    book_levels = st.slider("Book levels per side", min_value=5, max_value=50, value=20, step=5)
    st.divider()
    st.subheader("Forecast")
    require_clear = st.checkbox("Require CLEAR forecast only", value=True)
    require_validation_edge = st.checkbox("Require holdout above baseline", value=True)
    if st.button("Clear paper history", use_container_width=True):
        st.session_state.paper_trades = []
        save_trade_journal(st.session_state.paper_trades)
        st.session_state.paper_position = None
        st.session_state.equity_history = []
        st.session_state.cooldown_until = None

if st.session_state.utc_day != datetime.now(timezone.utc).date().isoformat():
    st.session_state.utc_day = datetime.now(timezone.utc).date().isoformat()

st.session_state.starting_equity = starting_equity


@st.fragment(run_every=REFRESH_SECONDS)
def market_dashboard() -> None:
    try:
        raw_trades, raw_book, spot_usd = fetch_market_data()
        raw_klines = fetch_one_minute_klines()
        update_trade_buffer(raw_trades)
        st.session_state.last_data_error = None
        st.session_state.last_success_ts = datetime.now(timezone.utc).timestamp()
    except (requests.RequestException, KeyError, TypeError, ValueError) as error:
        st.session_state.last_data_error = str(error)
        st.error(f"Market data unavailable: {error}")
        st.caption("The paper controls are disabled until a valid BTCUSDT futures price is available.")
        return

    trades = st.session_state.trade_buffer
    flow = summarize_flow(trades)
    aggressor_strength = describe_aggressor_strength(flow, min_flow, int(min_trades), float(min_total_btc))
    book = summarize_book(raw_book, book_levels)
    mark = float(trades[-1]["price"]) if trades else float(book["mid"])
    perp_spot_basis_bps = (mark / spot_usd - 1) * 10_000 if spot_usd else 0.0
    signal = get_signal(flow, book, min_flow, min_book)
    now = datetime.now(timezone.utc).timestamp()
    now_ms = int(now * 1000)
    last_success = st.session_state.last_success_ts or now
    if now - last_success > 9:
        st.warning("Data stale: no successful update in >9 seconds. Paper buttons may be greyed until fresh data returns.")
    cooldown_active = st.session_state.cooldown_until is not None and now < st.session_state.cooldown_until
    completed_klines = tuple(tuple(row) for row in raw_klines if int(row[6]) < now_ms)
    last_close_ms = int(completed_klines[-1][6]) if completed_klines else 0
    candle_forecast = cached_forecast(last_close_ms, completed_klines) if completed_klines else None

    position = st.session_state.paper_position
    if position is not None:
        stop_hit = mark <= position["stop"] if position["side"] > 0 else mark >= position["stop"]
        target_hit = mark >= position["target"] if position["side"] > 0 else mark <= position["target"]
        if stop_hit or target_hit:
            close_position(mark, "Stop" if stop_hit else "Target", fee_bps, slippage_bps)

    closed_pnl = sum(float(trade["Net PnL (USDT)"]) for trade in st.session_state.paper_trades)
    position = st.session_state.paper_position
    unrealized = calculate_unrealized(position, mark, fee_bps, slippage_bps) if position else 0.0
    equity = starting_equity + closed_pnl + unrealized
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    if len(st.session_state.equity_history) == 0 or st.session_state.equity_history[-1].get("t") != ts:
        st.session_state.equity_history.append({"t": ts, "eq": equity})
    if len(st.session_state.equity_history) > 120:
        st.session_state.equity_history = st.session_state.equity_history[-120:]
    today = datetime.now(timezone.utc).date().isoformat()
    daily_realized = sum(
        float(trade["Net PnL (USDT)"])
        for trade in st.session_state.paper_trades
        if trade["Closed (UTC)"].startswith(today)
    )
    daily_pnl = daily_realized + unrealized
    loss_limit = starting_equity * daily_loss_percent / 100
    drawdown_pct = calculate_drawdown_pct(st.session_state.equity_history)
    consecutive_losses = calculate_consecutive_losses(st.session_state.paper_trades)
    max_drawdown_lock = drawdown_pct >= max_drawdown_percent
    consecutive_loss_lock = consecutive_losses >= max_consecutive_losses
    risk_locked = daily_pnl <= -loss_limit or cooldown_active or max_drawdown_lock or consecutive_loss_lock

    st.success("Connected · refreshing every 3 seconds")
    metrics = st.columns(4)
    metrics[0].metric("BTCUSDT perp", f"{mark:,.2f} USDT")
    metrics[1].metric("BTC-USD spot", f"{spot_usd:,.2f} USD")
    metrics[2].metric("60s aggressor delta", f"{flow['delta']:+.3f} BTC")
    metrics[3].metric("Order-flow signal", signal)
    st.caption(
        "BTCUSDT is Binance USDⓈ-M perpetual futures quoted in USDT. BTC-USD is Coinbase spot quoted in USD. "
        f"Perp/spot difference: {perp_spot_basis_bps:+.1f} bps. This is a nominal comparison, not a conversion."
    )

    forecast_direction = str(candle_forecast["direction"]) if candle_forecast else "UNAVAILABLE"
    forecast_below = bool(candle_forecast.get("below_baseline", False)) if candle_forecast else False
    forecast_up = float(candle_forecast["up_score"]) if candle_forecast else 0.5

    if candle_forecast is not None:
        forecast_cols = st.columns(3)
        forecast_cols[0].metric("Directional estimate", str(candle_forecast["direction"]))
        forecast_cols[1].metric("Model up-score (not calibrated probability)", f"{float(candle_forecast['up_score']):.1%}")
        forecast_cols[2].metric(
            "Recent holdout accuracy",
            f"{float(candle_forecast['validation_accuracy']):.1%}",
            f"majority baseline {float(candle_forecast['baseline_accuracy']):.1%}",
        )
        st.progress(min(max(forecast_up, 0.0), 1.0))
        if forecast_below:
            st.warning("Forecast validation accuracy below majority-class baseline — treat edge as weak.")
        st.caption(
            f"Uses {candle_forecast['training_samples']} labeled candles and tests on the latest "
            f"{candle_forecast['validation_samples']} held-out candles. Features: 1/3/5-minute returns, "
            "last candle body, and relative volume. The score is a simple logistic-model output, not a calibrated "
            "probability; the estimate updates when a new one-minute candle closes."
        )

    def forecast_allows_side(side: int) -> bool:
        if not candle_forecast:
            return False
        if require_validation_edge and forecast_below:
            return False
        d = forecast_direction
        if side > 0:
            if require_clear and d != "UP_CLEAR":
                return False
            return d in ("UP_CLEAR", "UP_WEAK")
        if side < 0:
            if require_clear and d != "DOWN_CLEAR":
                return False
            return d in ("DOWN_CLEAR", "DOWN_WEAK")
        return False

    flow_ok = flow["trade_count"] >= int(min_trades) and flow["total"] >= float(min_total_btc)
    jump_zones = find_jump_zones(raw_book, mark)
    upper_zone = jump_zones["up"]["zone"]
    lower_zone = jump_zones["down"]["zone"]
    if upper_zone and signal == "LONG" and flow_ok:
        jump_confirmation = "UP · FLOW ALIGNED"
    elif lower_zone and signal == "SHORT" and flow_ok:
        jump_confirmation = "DOWN · FLOW ALIGNED"
    else:
        jump_confirmation = "NO FLOW CONFIRMATION"
    if position is not None:
        trade_setup = "MANAGE OPEN POSITION"
        setup_side = 0
    elif risk_locked:
        if cooldown_active:
            trade_setup = "NO TRADE · COOL DOWN (5m)"
        else:
            trade_setup = "NO TRADE · DAILY LOSS LIMIT"
        setup_side = 0
    elif require_validation_edge and forecast_below:
        trade_setup = "WAIT · MODEL BELOW BASELINE"
        setup_side = 0
    elif signal == "LONG" and forecast_allows_side(1) and flow_ok:
        trade_setup = "PAPER LONG SETUP"
        setup_side = 1
    elif signal == "SHORT" and forecast_allows_side(-1) and flow_ok:
        trade_setup = "PAPER SHORT SETUP"
        setup_side = -1
    else:
        trade_setup = "WAIT · SIGNALS NOT ALIGNED"
        setup_side = 0

    st.subheader("Trade timing")
    st.write(f"**{trade_setup}**")
    if setup_side:
        reference_entry = mark * (1 + setup_side * slippage_bps / 10_000)
        stop_distance = reference_entry * stop_bps / 10_000
        reference_stop = reference_entry - setup_side * stop_distance
        reference_target = reference_entry + setup_side * stop_distance * reward_risk
        st.caption(
            f"Forecast {forecast_direction} + order-flow {signal} agree · reference entry "
            f"{reference_entry:,.2f} USDT · stop {reference_stop:,.2f} · target {reference_target:,.2f}. "
            "Paper entry remains manual."
        )
    elif position is not None:
        st.caption("Manage or close the existing paper position; no new entry is suggested.")
    elif risk_locked:
        st.caption("New paper entries are disabled until the UTC day changes.")
    else:
        if require_validation_edge and forecast_below:
            st.caption("Paper entries are paused until the model's holdout accuracy beats its majority baseline.")
        else:
            st.caption(
                f"A setup requires the one-minute estimate and order-flow signal to agree. "
                f"Current estimate: {forecast_direction} · current flow signal: {signal} · flow ok: {flow_ok}."
            )

    st.subheader("Potential jump zones · order-book liquidity")
    jump_cols = st.columns(3)
    for column, side, label in (
        (jump_cols[0], "up", "Above · thin asks"),
        (jump_cols[1], "down", "Below · thin bids"),
    ):
        side_data = jump_zones[side]
        zone = side_data["zone"]
        if zone:
            column.metric(
                label,
                f"{zone['lower_price']:,.1f}–{zone['upper_price']:,.1f} USDT",
                f"{zone['near_bps']:.1f} bps away · {zone['depth_ratio']:.2f}× typical depth",
                delta_color="off",
            )
        else:
            column.metric(label, "No thin band found", f"{side_data['coverage_bps']:.1f} bps observed")
    jump_cols[2].metric("Order-flow confirmation", jump_confirmation)
    st.caption(
        "Nearest full 1-bps band with less than half the median resting size, within the captured 1,000-level snapshot. "
        "A thin band may allow faster movement if pressure reaches it; it is not a price prediction, and orders can be canceled."
    )

    st.subheader("Live BTCUSDT price · short-term views")
    chart_columns = st.columns(3)
    for column, minutes in zip(chart_columns, (1, 3, 5)):
        chart_rows = aggregate_trade_candles(trades, now, minutes * 60)
        with column:
            st.subheader(f"Rolling {minutes}-minute view")
            if chart_rows:
                candle_data = alt.Chart(alt.Data(values=chart_rows))
                x_axis = alt.X(
                    "UTC time:T",
                    scale=alt.Scale(type="utc"),
                    axis=alt.Axis(title="Time (UTC)", format="%H:%M:%S", labelAngle=0, tickCount=5),
                )
                direction_color = alt.Color(
                    "Direction:N",
                    scale=alt.Scale(domain=["Up", "Down"], range=["#177a55", "#c34444"]),
                    legend=None,
                )
                candle_tooltip = [
                    alt.Tooltip("UTC time:N", title="Bucket (UTC)"),
                    alt.Tooltip("Open:Q", format=",.2f"),
                    alt.Tooltip("High:Q", format=",.2f"),
                    alt.Tooltip("Low:Q", format=",.2f"),
                    alt.Tooltip("Close:Q", format=",.2f"),
                    alt.Tooltip("Trades:Q"),
                ]
                wicks = candle_data.mark_rule().encode(
                    x=x_axis,
                    y=alt.Y("High:Q", scale=alt.Scale(zero=False, padding=8), title="Price (USDT)"),
                    y2="Low:Q",
                    color=direction_color,
                    tooltip=candle_tooltip,
                )
                bodies = candle_data.mark_bar(size=8).encode(
                    x=x_axis,
                    y=alt.Y("Open:Q", scale=alt.Scale(zero=False, padding=8), title="Price (USDT)"),
                    y2="Close:Q",
                    color=direction_color,
                    tooltip=candle_tooltip,
                )
                price_chart = (
                    alt.layer(wicks, bodies)
                    .properties(height=260)
                    .configure_view(stroke=None)
                    .configure_axis(gridColor="#d9e3dc", labelColor="#65756c", titleColor="#65756c")
                )
                st.altair_chart(
                    price_chart,
                    width="stretch",
                    alt=f"BTCUSDT {minutes}-minute candlestick chart with 10-second OHLC bars",
                )
            else:
                st.info(f"Waiting for recent Binance trades to populate the {minutes}-minute chart.")
    if trades:
        latest_trade_time = datetime.fromtimestamp(trades[-1]["time"], timezone.utc).strftime("%H:%M:%S UTC")
        st.caption(f"Latest Binance trade: {latest_trade_time} · charts refresh every {REFRESH_SECONDS} seconds · times in UTC.")

    left, right = st.columns([1.4, 1])
    with left:
        st.subheader("Aggressor flow · last 60 seconds")
        st.write(
            f"Buyer-initiated: **{flow['buy_qty']:.3f} BTC** · "
            f"Seller-initiated: **{flow['sell_qty']:.3f} BTC** · "
            f"Trades: **{flow['trade_count']}** · Total: **{flow['total']:.3f} BTC**"
        )
        buyer_share = flow["buy_qty"] / flow["total"] if flow["total"] else 0.0
        seller_share = flow["sell_qty"] / flow["total"] if flow["total"] else 0.0
        aggressor_label = describe_aggressor_strength(flow, min_flow, int(min_trades), float(min_total_btc))
        st.write(
            f"**{aggressor_label}** · Buyer share {buyer_share:.1%} · Seller share {seller_share:.1%}"
        )
        flow_intervals = summarize_flow_intervals(trades, now)
        if flow_intervals:
            average_interval_delta = sum(row["Net delta (BTC)"] for row in flow_intervals) / len(flow_intervals)
            st.metric("Average net delta per 10s · last minute", f"{average_interval_delta:+.3f} BTC")
        st.dataframe(flow_intervals, hide_index=True, width="stretch", height=250)
        recent = [trade for trade in trades if trade["time"] >= now - TRADE_WINDOW_SECONDS]
        if recent and abs(float(flow["delta_fraction"])) >= min_flow:
            alert = absorption_heuristic(recent, flow)
            if alert and st.session_state.get("absorption_shown") is not True:
                st.session_state["absorption_shown"] = True
                st.info(alert["message"])
            elif not alert:
                st.session_state["absorption_shown"] = False
    if trades:
        latest_age = now - trades[-1]["time"]
        st.caption(f"Last trade: {latest_age:.1f}s ago")

    with right:
        st.subheader("Binance perpetual order book")
        st.write(
            f"Top {book_levels} levels per side · Bid **{book['bid_qty']:.2f} BTC** · "
            f"Ask **{book['ask_qty']:.2f} BTC** · Imbalance **{book['imbalance']:+.1%}** · "
            f"Spread **{book['spread']:.2f} USDT**"
        )
        depth_rows = [
            {"Side": "Bid", "Price (USDT)": price, "Size (BTC)": qty}
            for price, qty in book["bids"][:10]
        ] + [
            {"Side": "Ask", "Price (USDT)": price, "Size (BTC)": qty}
            for price, qty in book["asks"][:10]
        ]
        st.dataframe(depth_rows, hide_index=True, width="stretch", height=300)

    st.divider()
    st.subheader("Paper trading")
    if risk_locked:
        lock_reasons: list[str] = []
        if cooldown_active:
            lock_reasons.append(f"cooldown active for ~{int(st.session_state.cooldown_until - now)}s")
        if daily_pnl <= -loss_limit:
            lock_reasons.append(f"daily loss limit reached ({daily_pnl:,.2f} USDT)")
        if max_drawdown_lock:
            lock_reasons.append(f"drawdown hit {max_drawdown_percent:.1f}% threshold ({drawdown_pct:.1f}%)")
        if consecutive_loss_lock:
            lock_reasons.append(f"{max_consecutive_losses} consecutive losses reached ({consecutive_losses})")
        st.error("New positions are locked: " + "; ".join(lock_reasons) + ".")
    account_cols = st.columns(4)
    account_cols[0].metric("Paper equity", f"{equity:,.2f} USDT")
    account_cols[1].metric("Today net PnL", f"{daily_pnl:+,.2f} USDT")
    account_cols[2].metric("Realized net PnL", f"{closed_pnl:+,.2f} USDT")
    account_cols[3].metric("Unrealized net PnL", f"{unrealized:+,.2f} USDT")

    position = st.session_state.paper_position
    if position is None:
        long_col, short_col, _ = st.columns([1, 1, 3])
        long_clicked = long_col.button(
            "Open paper long", disabled=risk_locked or setup_side != 1, width="stretch"
        )
        short_clicked = short_col.button(
            "Open paper short", disabled=risk_locked or setup_side != -1, width="stretch"
        )
        if long_clicked or short_clicked:
            error = open_position(
                1 if long_clicked else -1,
                mark,
                equity,
                risk_percent / 100,
                stop_bps,
                max_leverage,
                fee_bps,
                slippage_bps,
                reward_risk,
            )
            if error:
                st.warning(error)
            else:
                st.rerun()
        st.caption("A button unlocks only when the candle estimate and order-flow signal agree. Entries are always manual.")
    else:
        side = position["label"]
        st.write(
            f"**{side} {position['quantity']:.6f} BTC** · entry {position['entry']:,.2f} USDT · "
            f"stop {position['stop']:,.2f} · target {position['target']:,.2f} · "
            f"opened {position['opened']} UTC"
        )
        if st.button("Close paper position", width="content"):
            close_position(mark, "Manual", fee_bps, slippage_bps)
            st.rerun()

    if st.session_state.paper_trades:
        trades_list = st.session_state.paper_trades
        wins = sum(1 for t in trades_list if float(t["Net PnL (USDT)"]) > 0)
        losses = sum(1 for t in trades_list if float(t["Net PnL (USDT)"]) < 0)
        breakeven = len(trades_list) - wins - losses
        total = len(trades_list)
        winrate = (wins / total * 100.0) if total else 0.0
        total_pnl = sum(float(t["Net PnL (USDT)"]) for t in trades_list)
        rs = [abs(float(t.get("R", 0.0))) for t in trades_list if float(t.get("R", 0.0)) != 0]
        avg_r = (sum(rs) / len(rs)) if rs else 0.0
        expectancy = (total_pnl / total) if total else 0.0
        gross_profit = sum(float(t["Net PnL (USDT)"]) for t in trades_list if float(t["Net PnL (USDT)"]) > 0)
        gross_loss = -sum(float(t["Net PnL (USDT)"]) for t in trades_list if float(t["Net PnL (USDT)"]) < 0)
        pf = (gross_profit / gross_loss) if gross_loss > 0 else float("inf")
        eq_curve = list(st.session_state.equity_history)
        max_dd = calculate_drawdown_pct(eq_curve)
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Trades", total)
        c2.metric("Win rate", f"{winrate:.1f}%")
        c3.metric("Avg |R|", f"{avg_r:.2f}")
        c4.metric("Expectancy (USDT)", f"{expectancy:+.2f}")
        c5, c6, c7, c8 = st.columns(4)
        c5.metric("W/L/B", f"{wins}/{losses}/{breakeven}")
        c6.metric("Profit factor", f"{pf:.2f}" if pf != float("inf") else "∞")
        c7.metric("Max DD", f"{max_dd:.2f}%")
        c8.metric("Total PnL", f"{total_pnl:+,.2f} USDT")
        if eq_curve:
            st.subheader("Equity curve")
            st.line_chart(eq_curve, x="t", y="eq", height=240)

    with st.expander("Historical backtest · paper only", key="historical_backtest_expander"):
        st.caption(
            "Chronological Binance BTCUSDT candle study. Walk-forward mode tests five sequential windows; single-holdout "
            "mode tests the latest 20%. Historical order-book imbalance is unavailable, so both approximate live entries."
        )
        study_mode = st.selectbox(
            "Evaluation method",
            options=["5-fold walk-forward", "Single 80/20 holdout"],
            index=0,
            key="backtest_study_mode",
        )
        with st.form("historical_backtest_form", border=False):
            backtest_controls = st.columns(3)
            backtest_balance = backtest_controls[0].number_input(
                "Starting balance (USDT)", min_value=10.0, value=10.0, step=5.0, key="backtest_starting_balance"
            )
            history_days = backtest_controls[1].selectbox(
                "History window", options=[7, 30], index=1, format_func=lambda days: f"{days} days", key="backtest_history_days"
            )
            max_hold_bars = backtest_controls[2].selectbox(
                "Max hold (minutes)", options=[3, 5, 10], index=1, key="backtest_max_hold"
            )
            run_historical_backtest = st.form_submit_button("Run historical replay", width="content")
        if run_historical_backtest:
            try:
                with st.spinner(f"Downloading {history_days} days and replaying the chronological holdout..."):
                    backtest_rows = fetch_backtest_klines(history_days)
                    options = {
                        "risk_percent": risk_percent,
                        "max_leverage": max_leverage,
                        "stop_bps": stop_bps,
                        "reward_risk": reward_risk,
                        "fee_bps": fee_bps,
                        "slippage_bps": slippage_bps,
                        "daily_loss_percent": daily_loss_percent,
                        "min_flow": min_flow,
                        "min_trades": int(min_trades),
                        "min_total_btc": min_total_btc,
                        "require_clear": require_clear,
                        "require_validation_edge": require_validation_edge,
                        "max_hold_bars": max_hold_bars,
                    }
                    if study_mode == "5-fold walk-forward":
                        st.session_state.backtest_result = run_walk_forward(
                            backtest_rows,
                            starting_balance=backtest_balance,
                            fold_count=5,
                            initial_train_fraction=0.5,
                            **options,
                        )
                    else:
                        closed_count = sum(
                            1
                            for row in backtest_rows
                            if len(row) > 6 and int(row[6]) < int(datetime.now(timezone.utc).timestamp() * 1000)
                        )
                        test_bars = max(100, int(closed_count * 0.2))
                        st.session_state.backtest_result = run_backtest(
                            backtest_rows,
                            starting_balance=backtest_balance,
                            test_bars=test_bars,
                            **options,
                        )
                st.session_state.backtest_error = None
            except requests.RequestException as error:
                st.session_state.backtest_result = None
                st.session_state.backtest_error = str(error)

        backtest_result = st.session_state.backtest_result
        if st.session_state.backtest_error:
            st.error(f"Could not fetch historical candles: {st.session_state.backtest_error}")
        elif backtest_result is None:
            st.info("Run a 7- or 30-day replay. Walk-forward is the recommended mode for checking period-to-period stability.")
        else:
            if "folds" in backtest_result:
                st.caption(
                    f"{backtest_result['fold_count']} sequential folds · {backtest_result['test_bars']} total untouched test bars · "
                    f"{backtest_result['test_start']} to {backtest_result['test_end']}. Each fold trains only on earlier data "
                    "and resets to the selected starting balance. Entries use next-candle opens; if stop and target both "
                    "hit in one candle, the replay assumes the stop hit first."
                )
            else:
                st.caption(
                    f"{backtest_result['training_bars']} training-eligible bars · "
                    f"{backtest_result['model_training_samples']} samples used to fit · "
                    f"{backtest_result['test_bars']} untouched test bars · "
                    f"{backtest_result['test_start']} to {backtest_result['test_end']}. "
                    "Entries use next-candle opens; if stop and target both hit in one candle, the replay assumes the stop hit first."
                )
            results_top = st.columns(4)
            results_top[0].metric(
                "Ending balance",
                f"{backtest_result['ending_balance']:.4f} USDT",
                f"{backtest_result['net_pnl']:+.4f} USDT",
            )
            results_top[1].metric("Net return", f"{backtest_result['return_percent']:+.2f}%")
            results_top[2].metric("Closed trades", int(backtest_result["trade_count"]))
            results_top[3].metric("Win rate", f"{backtest_result['win_rate']:.1f}%")
            results_bottom = st.columns(5)
            results_bottom[0].metric("Model accuracy", f"{backtest_result['model_accuracy']:.1%}")
            results_bottom[1].metric("Majority baseline", f"{backtest_result['majority_baseline']:.1%}")
            if "folds" in backtest_result:
                gate_label = f"{backtest_result['gate_passing_folds']}/{backtest_result['fold_count']} folds pass"
                gate_detail = (
                    f"avg {backtest_result['training_validation_accuracy']:.1%} vs "
                    f"{backtest_result['training_validation_baseline']:.1%} baseline"
                )
            else:
                gate_label = "PASS" if backtest_result["entry_gate_passed"] else "BLOCKED"
                gate_detail = (
                    f"{backtest_result['training_validation_accuracy']:.1%} vs "
                    f"{backtest_result['training_validation_baseline']:.1%} baseline"
                )
            results_bottom[2].metric("Pre-test entry gate", gate_label, gate_detail)
            profit_factor = float(backtest_result["profit_factor"])
            results_bottom[3].metric("Profit factor", f"{profit_factor:.2f}" if profit_factor != float("inf") else "N/A")
            drawdown_label = "Worst-fold drawdown" if "folds" in backtest_result else "Max drawdown"
            results_bottom[4].metric(drawdown_label, f"{backtest_result['max_drawdown_percent']:.2f}%")
            if "folds" in backtest_result:
                st.subheader("Walk-forward fold stability")
                st.dataframe(backtest_result["folds"], hide_index=True, width="stretch")
            st.subheader("Directional hit rate by score cutoff")
            st.dataframe(backtest_result["confidence_scan"], hide_index=True, width="stretch")
            st.caption(
                "Cutoffs are fixed in advance and scored only on the held-out candles. The model score is not a calibrated "
                "probability; check test-signal counts and coverage before interpreting hit rate."
            )
            if backtest_result["trades"]:
                st.dataframe(backtest_result["trades"], hide_index=True, width="stretch", height=280)
            elif "folds" in backtest_result and backtest_result["require_validation_edge"] and backtest_result["gate_passing_folds"] == 0:
                st.info("No entries: the pre-test validation gate blocked every walk-forward fold.")
            elif "folds" in backtest_result:
                st.warning("No entries matched the aligned forecast and flow rules in the eligible walk-forward folds.")
            elif backtest_result["require_validation_edge"] and not backtest_result["entry_gate_passed"]:
                st.info("No entries: the earlier validation window did not beat its majority baseline, so the selected gate blocked the test-period trades.")
            else:
                st.warning("No entries matched the selected forecast and flow thresholds in this test window.")
            st.warning(
                "This short historical test is not a guarantee of future performance. It omits historical book imbalance, "
                "funding, liquidation, queue priority, and exchange minimum order sizes; $10 sizing is simulated only."
            )

    with st.expander("Signal and risk methodology"):
        st.markdown(
            "- LONG requires 60-second buyer-initiated volume dominance above the selected threshold **and** positive size imbalance in the selected top book levels.\n"
            "- SHORT requires the inverse. Otherwise the signal is FLAT. This is a transparent heuristic, not a calibrated strategy or a forecast.\n"
            "- Binance aggregate-trade `m=true` means the buyer was maker, so the seller was aggressor.\n"
            "- The next-candle estimate is a separate logistic model trained on Binance BTCUSDT one-minute OHLCV candles. It uses a chronological holdout and shows accuracy next to a majority-class baseline; past accuracy does not imply future accuracy.\n"
            "- When enabled, paper entries are blocked unless recent holdout accuracy beats the majority baseline; this is a conservative filter, not proof of future profit.\n"
            "- A paper entry setup appears only when the one-minute direction and order-flow signal agree and the daily loss limit is not reached. It never submits an order automatically.\n"
            "- Potential jump zones are low-depth 1-bps bands relative to the median of fully observed Binance book bands; flow confirmation is reported separately. Snapshot liquidity can disappear and does not predict a jump.\n"
            "- Paper size is the smaller of risk budget divided by stop distance and equity times the notional cap. Simulated entry/exit include configured slippage and fees.\n"
            "- Stops and targets trigger from the observed perp trade price; modeled fills can differ from real fills. The daily loss lock uses realized plus open net PnL.\n"
            "- Book depth is a snapshot and can be canceled; spoofing, latency, funding, liquidation, partial fills, and exchange fees beyond your inputs are not modeled."
        )


market_dashboard()
