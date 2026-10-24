"""BTC Order-Flow Paper Desk — Streamlit composition layer.

This module is deliberately thin. Responsibilities live in focused modules:

* ``config``      — tunables shared by every layer
* ``market_data`` — the only module that touches the network
* ``analytics``   — pure, testable market/trade computations
* ``journal``     — SQLite persistence (repository for trades + session state)
* ``paper``       — paper-trading engine: positions, risk locks, decision text

What remains here is presentation: sidebar controls, tab renderers, and the
refresh loop that composes the above into one view model per cycle.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

import altair as alt
import requests
import streamlit as st

from backtest import run_backtest, run_walk_forward
from forecast import forecast_candle, forecast_next_minute, forecast_study
import ui
from footprint import (
    describe_footprint,
    detect_large_aggressor,
    footprint_bias,
    footprint_rows,
)
from tape import (
    TAPE_HORIZON_SECONDS,
    TAPE_MAX_ROWS,
    log_forecast_prediction,
    make_setup_record,
    resolve_forecast_log,
    should_record_setup,
    summarize_forecast_log,
    summarize_tape,
    update_tape,
)
from market_regime import calculate_market_regime, describe_market_regime

# Re-exported layers: presentation uses these directly, and the public API of
# the app stays import-stable for the test suite.
from config import (  # noqa: F401
    BINANCE_BASE,
    COINBASE_BASE,
    DB_PATH,
    EQUITY_CHART_MAX_SAMPLES,
    EQUITY_RISK_MAX_SAMPLES,
    FEED_LABELS,
    HEATMAP_PATH,
    MARKET_FEEDS,
    PRODUCT,
    REFRESH_SECONDS,
    SESSION_SNAPSHOT_INTERVAL_SECONDS,
    STALE_AFTER_MS,
    SYMBOL,
    TRADE_JOURNAL_RETENTION_DAYS,
    TRADE_RETENTION_SECONDS,
    TRADE_WINDOW_SECONDS,
    USDT_PRODUCT,
)
from market_data import (  # noqa: F401
    SESSION,
    fetch_backtest_klines,
    fetch_market_data,
    fetch_one_minute_klines,
    get_liquidation_feed_state,
    normalize_force_order,
    request_json,
    run_liquidation_consumer,
)
from analytics import (  # noqa: F401
    absorption_heuristic,
    aggregate_trade_candles,
    append_open_interest_sample,
    assess_feed_ages,
    build_why_inputs,
    calculate_consecutive_losses,
    calculate_drawdown_pct,
    calculate_fee_adjusted_trade_metrics,
    calculate_market_levels,
    calculate_microprice,
    calculate_trade_statistics,
    calculate_unrealized,
    calculate_venue_lead_lag,
    count_missed_trades,
    describe_aggressor_strength,
    find_jump_zones,
    interpret_price_open_interest,
    normalize_agg_trade,
    record_book_depth_sample,
    summarize_book,
    summarize_depth_curve,
    summarize_flow,
    summarize_flow_intervals,
    summarize_liquidations,
    update_feed_state,
)
from journal import (  # noqa: F401
    append_trade_journal,
    clear_trade_journal,
    delete_session_value,
    export_all_data,
    init_trade_journal,
    list_backups,
    list_exports,
    load_session_value,
    load_trade_journal,
    purge_trades_older_than,
    read_crash_journal,
    restore_from_backup,
    save_session_value,
    save_trade_journal,
)
from paper import (  # noqa: F401
    calculate_conviction,
    close_position,
    describe_decision_confidence,
    describe_decision_reason,
    detect_market_alerts,
    forecast_allows_side,
    get_signal,
    open_position,
)

_HEATMAP_CACHE: dict[str, str] = {}


def load_heatmap_html() -> str:
    """Read the standalone heatmap once and keep it in memory.

    The file is static, so re-reading it on every three second refresh would
    only add disk work for content that never changes.
    """
    if "html" in _HEATMAP_CACHE:
        return _HEATMAP_CACHE["html"]
    try:
        html = HEATMAP_PATH.read_text(encoding="utf-8")
    except OSError:
        return ""
    _HEATMAP_CACHE["html"] = html
    return html

FORECAST_CACHE_MAX_ENTRIES = 200

_forecast_cache: dict[tuple[int, int], dict[str, Any] | None] = {}
_forecast_cache_10m: dict[tuple[int, int], dict[str, Any] | None] = {}


def _cache_put(cache: dict[tuple[int, int], dict[str, Any] | None], key: tuple[int, int], value: dict[str, Any] | None) -> None:
    """Insert into a forecast cache, evicting the oldest entry when full.

    The cache keys are (last_close_ms, len(rows)) so every new candle close
    with a different row count creates a new entry. Without eviction this dict
    grows for as long as the process runs, which is a memory leak on a
    long-lived scalping desk.
    """
    if len(cache) >= FORECAST_CACHE_MAX_ENTRIES:
        oldest = next(iter(cache))
        del cache[oldest]
    cache[key] = value


def cached_forecast(
    last_close_ms: int, rows: tuple[tuple[Any, ...], ...]
) -> dict[str, float | int | str] | None:
    key = (last_close_ms, len(rows))
    if key in _forecast_cache:
        return _forecast_cache[key]
    result = forecast_next_minute(list(rows)) if rows else None
    _cache_put(_forecast_cache, key, result)
    return result


def cached_forecast_10m(
    last_close_ms: int, rows: tuple[tuple[Any, ...], ...]
) -> dict[str, float | int | str] | None:
    key = (last_close_ms, len(rows))
    if key in _forecast_cache_10m:
        return _forecast_cache_10m[key]
    result = forecast_candle(list(rows), horizon_candles=10) if rows else None
    _cache_put(_forecast_cache_10m, key, result)
    return result


st.set_page_config(
    page_title="BTC Order Flow | Paper Desk",
    page_icon="₿",
    layout="wide",
    initial_sidebar_state="expanded",
)
ui.inject_styles()
ui.header("BTC Order-Flow Paper Desk", "Scalping research · public exchange data · paper execution only")














def cached_forecast(
    last_close_ms: int, rows: tuple[tuple[Any, ...], ...]
) -> dict[str, float | int | str] | None:
    key = (last_close_ms, len(rows))
    if key in _forecast_cache:
        return _forecast_cache[key]
    result = forecast_next_minute(list(rows)) if rows else None
    _forecast_cache[key] = result
    return result


def cached_forecast_10m(
    last_close_ms: int, rows: tuple[tuple[Any, ...], ...]
) -> dict[str, float | int | str] | None:
    key = (last_close_ms, len(rows))
    if key in _forecast_cache_10m:
        return _forecast_cache_10m[key]
    result = forecast_candle(list(rows), horizon_candles=10) if rows else None
    _forecast_cache_10m[key] = result
    return result





def persist_session_snapshot(now: float) -> None:
    """Write the durable parts of the session to disk on a slow cadence.

    The tape, the live forecast log, and the equity peak behind the drawdown
    breaker all need to survive a restart, but writing them on every three
    second refresh would mean a database write per cycle for data that only
    changes a few times a minute.
    """
    last = st.session_state.get("last_snapshot_ts")
    if last is not None and (now - float(last)) < SESSION_SNAPSHOT_INTERVAL_SECONDS:
        return
    st.session_state["last_snapshot_ts"] = now
    try:
        save_session_value("signal_tape", st.session_state.get("signal_tape", []))
        save_session_value("forecast_log", st.session_state.get("forecast_log", []))
        save_session_value("equity_risk_history", st.session_state.get("equity_risk_history", []))
    except sqlite3.Error:
        pass




def initialize_state() -> None:
    defaults: dict[str, Any] = {
        "trade_buffer": [],
        "last_trade_id": None,
        "missed_trades_total": 0,
        "last_missed_trades": 0,
        "paper_position": load_session_value("paper_position"),
        "paper_trades": load_trade_journal(),
        "starting_equity": 10_000.0,
        "utc_day": datetime.now(timezone.utc).date().isoformat(),
        "feed_health": {
            name: {
                "connected": False,
                "ever_connected": False,
                "last_success": None,
                "last_error": None,
                "reconnect_count": 0,
            }
            for name in MARKET_FEEDS
        },
        "market_cache": {name: None for name in MARKET_FEEDS},
        "book_depth_history": [],
        "basis_history": [],
        "venue_price_history": [],
        "open_interest_history": [],
        "open_interest_change_pct": None,
        "alert_state": {"stale": False, "flow_side": None, "signal": None, "near_zone": False},
        "equity_history": [],
        "last_refresh_ts": None,
        "equity_risk_history": load_session_value("equity_risk_history", []),
        "cooldown_until": None,
        "backtest_result": None,
        "backtest_error": None,
        "signal_tape": load_session_value("signal_tape", []),
        "forecast_log": load_session_value("forecast_log", []),
        "forecast_study": [],
        "forecast_study_key": None,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def record_feed_results(results: dict[str, Any], errors: dict[str, str], now: float) -> None:
    update_feed_state(
        st.session_state.feed_health,
        st.session_state.market_cache,
        results,
        errors,
        now,
    )




def render_data_health_strip(
    feed_health: dict[str, dict[str, Any]],
    feed_ages: dict[str, Any],
    stale_reasons: list[str],
    liquidation_health: dict[str, Any] | None = None,
    trade_gaps: dict[str, int] | None = None,
) -> None:
    chips: list[tuple[str, str]] = []
    for name in MARKET_FEEDS:
        state = feed_health[name]
        tone = "ok" if state["connected"] else ("warn" if state["ever_connected"] else "bad")
        label = FEED_LABELS[name].split(" · ")[0]
        chips.append((label, tone))
    if liquidation_health is not None:
        tone = "ok" if liquidation_health["connected"] else ("warn" if liquidation_health["reconnect_count"] else "bad")
        chips.append(("Liquidations · WS", tone))
    ui.section("Data health")
    ui.chips(chips)

    trade_age = feed_ages["trade_age_ms"]
    book_age = feed_ages["book_age_ms"]
    trade_age_text = f"{trade_age:.0f} ms" if trade_age is not None else "N/A"
    book_age_text = f"{book_age:.0f} ms" if book_age is not None else "N/A"
    gap_total = int((trade_gaps or {}).get("total", 0))
    gap_last = int((trade_gaps or {}).get("last", 0))
    gap_text = (
        f"· Trade-feed gaps: {gap_total:,} trades skipped (last {gap_last:,})"
        if gap_total
        else "· Trade-feed gaps: none detected"
    )
    ui.note(
        f"Last-trade age {trade_age_text} · Book snapshot age {book_age_text} {gap_text}. "
        "Market snapshots use REST polling; forced liquidations use Binance public WebSocket."
    )
    if stale_reasons:
        ui.alert("bad", "NO SIGNAL - STALE DATA · " + "; ".join(stale_reasons))
    if gap_total:
        ui.alert(
            "warn",
            f"Trade feed skipped {gap_total:,} aggregate trades across {gap_last:,} in the most recent poll. "
            "The 60-second aggressor delta and CVD under-count until the buffer refills, because the REST "
            "endpoint returns at most 1,000 rows per request.",
        )




def update_trade_buffer(rows: list[dict[str, Any]]) -> None:
    """Add unseen Binance aggregate trades; m=True means sell aggressor."""
    last_id = st.session_state.last_trade_id
    missed = count_missed_trades(last_id, rows)
    if missed:
        st.session_state.missed_trades_total = int(st.session_state.missed_trades_total) + missed
        st.session_state.last_missed_trades = missed
    fresh = [row for row in rows if last_id is None or int(row["a"]) > last_id]
    buffer = st.session_state.trade_buffer
    buffer.extend(normalize_agg_trade(row) for row in fresh)
    if rows:
        st.session_state.last_trade_id = max(int(row["a"]) for row in rows)
    cutoff = datetime.now(timezone.utc).timestamp() - TRADE_RETENTION_SECONDS
    st.session_state.trade_buffer = [trade for trade in buffer if trade["time"] >= cutoff]
















initialize_state()

# Self-delete unused data: purge closed trades older than the retention window
# so the SQLite journal does not grow without bound over weeks of use.
try:
    purged = purge_trades_older_than(TRADE_JOURNAL_RETENTION_DAYS)
    if purged:
        st.toast(f"Purged {purged} closed trade(s) older than {TRADE_JOURNAL_RETENTION_DAYS} days", icon=":material/delete_outline:")
except sqlite3.Error:
    pass

with st.sidebar:
    st.markdown(
        """
        <div style="padding:0.1rem 0 0.35rem 0;">
          <div style="font-size:1.02rem;font-weight:700;color:#eaf1fa;letter-spacing:-0.01em;">
            Desk controls
          </div>
          <div style="font-size:0.68rem;color:#7f95b3;letter-spacing:0.09em;text-transform:uppercase;margin-top:0.15rem;">
            Paper execution only
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.markdown("---")
    session_trades = list(st.session_state.paper_trades)
    session_pnl = sum(float(trade["Net PnL (USDT)"]) for trade in session_trades)
    pnl_color = "#059669" if session_pnl > 0 else ("#dc2626" if session_pnl < 0 else "#64748b")
    st.markdown(
        f"""
        <div style="display:flex;gap:0.5rem;align-items:stretch;margin:0.9rem 0 0.4rem 0;">
          <div style="flex:1;background:rgba(15,23,42,0.45);border:1px solid rgba(148,163,184,0.3);border-radius:12px;padding:0.6rem 0.8rem;">
            <div style="font-size:0.6rem;font-weight:700;letter-spacing:0.12em;text-transform:uppercase;color:#93a8c4;">Closed trades</div>
            <div style="font-size:1.25rem;font-weight:700;color:#eaf1fa;font-variant-numeric:tabular-nums;">{len(session_trades)}</div>
          </div>
          <div style="flex:1;background:rgba(15,23,42,0.45);border:1px solid rgba(148,163,184,0.3);border-radius:12px;padding:0.6rem 0.8rem;">
            <div style="font-size:0.6rem;font-weight:700;letter-spacing:0.12em;text-transform:uppercase;color:#93a8c4;">Session PnL</div>
            <div style="font-size:1.25rem;font-weight:700;color:{pnl_color};font-variant-numeric:tabular-nums;">{session_pnl:+,.2f}</div>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.subheader("Account and risk")
    starting_equity = st.number_input("Starting paper equity (USDT)", min_value=10.0, value=10_000.0, step=500.0)
    risk_percent = st.slider("Risk per trade (%)", min_value=0.1, max_value=2.0, value=0.5, step=0.1)
    max_leverage = st.slider("Maximum notional / equity", min_value=1.0, max_value=5.0, value=1.0, step=0.5)
    stop_bps = st.number_input("Stop distance (bps)", min_value=2.0, max_value=200.0, value=20.0, step=2.0)
    reward_risk = st.number_input("Target risk/reward", min_value=0.5, max_value=5.0, value=2.0, step=0.25)
    st.subheader("Execution costs")
    fee_bps = st.number_input("Fee per side (bps)", min_value=0.0, max_value=50.0, value=5.0, step=0.5)
    slippage_bps = st.number_input("Slippage per side (bps)", min_value=0.0, max_value=50.0, value=1.0, step=0.5)
    st.subheader("Circuit breakers")
    daily_loss_percent = st.number_input("Daily loss limit (%)", min_value=0.5, max_value=20.0, value=2.0, step=0.5)
    max_drawdown_percent = st.number_input("Max drawdown (%)", min_value=1.0, max_value=100.0, value=25.0, step=1.0)
    max_consecutive_losses = st.number_input("Max consecutive losses", min_value=1, max_value=20, value=4, step=1)
    max_daily_trades = st.number_input("Max trades per UTC day", min_value=1, max_value=200, value=20, step=1)
    st.divider()
    st.subheader("Signal thresholds")
    min_flow = st.slider("Minimum 60s delta / volume", min_value=0.05, max_value=0.80, value=0.20, step=0.05)
    min_book = st.slider("Minimum top-book imbalance", min_value=0.05, max_value=0.80, value=0.15, step=0.05)
    min_trades = st.number_input("Min 60s trades", min_value=1, max_value=50, value=5, step=1)
    min_total_btc = st.number_input("Min 60s total (BTC)", min_value=0.0, max_value=100.0, value=1.0, step=0.5)
    book_levels = st.slider("Book levels per side", min_value=5, max_value=50, value=20, step=5)
    st.divider()
    st.subheader("Forecast gates")
    require_clear = st.checkbox("Require CLEAR forecast only", value=True)
    require_validation_edge = st.checkbox("Require holdout above baseline", value=True)
    st.divider()
    st.subheader("Visual alerts")
    enable_stale_alert = st.checkbox("Stale feed alert", value=True)
    enable_flow_flip_alert = st.checkbox("Aggressor flow flip alert", value=True)
    enable_signal_alert = st.checkbox("LONG/SHORT signal alert", value=True)
    enable_zone_alert = st.checkbox("Thin-zone proximity alert", value=True)
    zone_alert_bps = st.slider("Zone alert distance (bps)", min_value=1, max_value=50, value=5, step=1)
    large_trade_threshold = st.number_input("Large print threshold (BTC)", min_value=0.01, max_value=100.0, value=0.5, step=0.1)
    st.divider()
    if st.button("Clear paper history", use_container_width=True, type="secondary"):
        st.session_state.paper_trades = []
        clear_trade_journal()
        st.session_state.paper_position = None
        delete_session_value("paper_position")
        st.session_state.equity_history = []
        st.session_state.equity_risk_history = []
        delete_session_value("equity_risk_history")
        st.session_state.cooldown_until = None
        st.toast("Paper history cleared", icon=":material/delete_outline:")

    st.caption(
        f"Journal: {len(st.session_state.paper_trades)} closed trade(s) · "
        f"auto-purge older than {TRADE_JOURNAL_RETENTION_DAYS} days on startup · "
        f"forecast cache capped at {FORECAST_CACHE_MAX_ENTRIES} entries"
    )

    st.divider()
    st.subheader("🛡️ Data safety")
    col_exp, col_restore = st.columns(2)
    with col_exp:
        if st.button("Export all data", use_container_width=True, help="Copies DB + crash journal to exports/ folder"):
            export_dir = export_all_data()
            st.session_state["last_export"] = str(export_dir)
            st.toast(f"Exported to {export_dir.name}", icon=":material/save:")
    with col_restore:
        backups = list_backups()
        if backups:
            latest = backups[0]
            if st.button("Restore latest backup", use_container_width=True, help=f"Restores {latest.name} (app restart required)"):
                restore_from_backup(latest)
                st.toast(f"Restored {latest.name} — restart the app", icon=":material/restore:")

    exports = list_exports()
    if exports:
        st.caption(f"Exports: {len(exports)} folder(s) in exports/ · latest: {exports[0].name}")
    if backups:
        st.caption(f"Backups: {len(backups)} copy(ies) in backups/ · latest: {backups[0].name}")

    crash_count = len(read_crash_journal())
    st.caption(f"Crash journal: {crash_count} logged event(s) · replay with read_crash_journal()")

if st.session_state.utc_day != datetime.now(timezone.utc).date().isoformat():
    st.session_state.utc_day = datetime.now(timezone.utc).date().isoformat()

st.session_state.starting_equity = starting_equity


@st.fragment(run_every=REFRESH_SECONDS)
def market_dashboard() -> None:
    now = datetime.now(timezone.utc).timestamp()
    st.session_state["last_refresh_ts"] = now

    # Live refresh indicator + manual force-refresh
    last = st.session_state.get("last_refresh_ts")
    live_col, refresh_col = st.columns([6, 1])
    with live_col:
        if last is not None:
            age = max(0, int(now - float(last)))
            st.caption(f"🔴 Live · last refresh {age}s ago · auto every {REFRESH_SECONDS}s")
        else:
            st.caption(f"🔴 Live · auto every {REFRESH_SECONDS}s")
    with refresh_col:
        if st.button("↻ Refresh now", use_container_width=True, help="Force an immediate market data fetch"):
            st.rerun()

    decision_slot = st.empty()
    liquidation_state = get_liquidation_feed_state()
    with liquidation_state["lock"]:
        liquidation_events = list(liquidation_state["events"])
        liquidation_connected = liquidation_state["connected"]
        liquidation_reconnect_count = liquidation_state["reconnect_count"]
        last_liquidation_at = liquidation_state["last_event_at"]
    feed_results, feed_errors = fetch_market_data()
    liquidation_health = {
        "connected": liquidation_connected,
        "reconnect_count": liquidation_reconnect_count,
        "last_event_age_ms": max(0.0, (now - last_liquidation_at) * 1000) if last_liquidation_at else None,
    }
    record_feed_results(feed_results, feed_errors, now)
    cached_data = st.session_state.market_cache
    raw_trades = cached_data["binance_trades"] or []
    raw_book = cached_data["binance_book"] or {"bids": [], "asks": []}
    spot_usd = float(cached_data["coinbase_spot"] or 0.0)
    usdt_usd = float(cached_data["usdt_usd"] or 0.0)
    open_interest_data = cached_data["binance_open_interest"] or {}
    funding_data = cached_data["binance_funding"] or {}
    if cached_data["binance_trades"] is not None:
        update_trade_buffer(raw_trades)
    try:
        raw_klines = fetch_one_minute_klines()
    except requests.RequestException as error:
        raw_klines = []
        st.warning(f"Forecast candles temporarily unavailable: {error}")

    trades = st.session_state.trade_buffer
    latest_trade = max(trades, key=lambda trade: float(trade["time"])) if trades else None
    latest_trade_time = float(latest_trade["time"]) if latest_trade else None
    book_updated_at = st.session_state.feed_health["binance_book"]["last_success"]
    feed_ages = assess_feed_ages(latest_trade_time, book_updated_at, now)
    stale_reasons = list(feed_ages["stale_reasons"])
    for name, label in (("binance_trades", "Binance trades disconnected"), ("binance_book", "Binance book disconnected")):
        if not st.session_state.feed_health[name]["connected"] and label not in stale_reasons:
            stale_reasons.append(label)
    stale_data = bool(stale_reasons)

    if cached_data["binance_trades"] is None or cached_data["binance_book"] is None:
        render_data_health_strip(
            st.session_state.feed_health,
            feed_ages,
            stale_reasons,
            liquidation_health,
            {"total": int(st.session_state.missed_trades_total), "last": int(st.session_state.last_missed_trades)},
        )
        st.info("Waiting for initial Binance trade and order-book snapshots. Paper entries remain disabled.")
        return

    flow = summarize_flow(trades, now)
    liquidation_totals = summarize_liquidations(liquidation_events, now)
    aggressor_strength = describe_aggressor_strength(flow, min_flow, int(min_trades), float(min_total_btc))
    book = summarize_book(raw_book, book_levels)
    st.session_state.book_depth_history = record_book_depth_sample(
        st.session_state.book_depth_history,
        book,
        now,
    )
    footprint = detect_large_aggressor(
        trades,
        now,
        book_depth_history=st.session_state.book_depth_history,
        feed_gap=bool(st.session_state.last_missed_trades),
    )
    mark = float(latest_trade["price"]) if latest_trade else float(book["mid"])
    market_regime = calculate_market_regime(trades, book, mark, now, fee_bps, slippage_bps)
    market_levels = calculate_market_levels(
        trades,
        cached_data["binance_daily_klines"] or [],
        mark,
    )
    current_open_interest = float(open_interest_data.get("openInterest", 0.0))
    if "binance_open_interest" in feed_results and current_open_interest > 0:
        oi_history, oi_change = append_open_interest_sample(
            st.session_state.open_interest_history,
            current_open_interest,
            now,
        )
        st.session_state.open_interest_history = oi_history
        st.session_state.open_interest_change_pct = oi_change
    oi_change_pct = st.session_state.open_interest_change_pct
    recent_price_trades = [trade for trade in trades if float(trade["time"]) >= now - 60]
    price_change_pct = (
        (float(recent_price_trades[-1]["price"]) / float(recent_price_trades[0]["price"]) - 1) * 100
        if len(recent_price_trades) >= 2 and float(recent_price_trades[0]["price"]) > 0
        else 0.0
    )
    price_oi_reading = (
        interpret_price_open_interest(price_change_pct, oi_change_pct)
        if oi_change_pct is not None
        else "Warming up · requires 60 seconds of open-interest samples"
    )
    funding_rate = float(funding_data.get("lastFundingRate", 0.0))
    next_funding_ms = int(funding_data.get("nextFundingTime", 0))
    seconds_to_funding = max(0, int(next_funding_ms / 1000 - now)) if next_funding_ms else None
    funding_countdown = (
        f"{seconds_to_funding // 3600}h {(seconds_to_funding % 3600) // 60}m"
        if seconds_to_funding is not None
        else "Unavailable"
    )
    spot_usdt_equivalent = spot_usd / usdt_usd if spot_usd > 0 and usdt_usd > 0 else None
    perp_spot_basis_bps = (mark / spot_usdt_equivalent - 1) * 10_000 if spot_usdt_equivalent else None
    if spot_usdt_equivalent is not None and mark > 0:
        venue_history = st.session_state.venue_price_history
        venue_history.append({"time": now, "spot": spot_usdt_equivalent, "perp": mark})
        st.session_state.venue_price_history = venue_history[-180:]
    lead_lag = calculate_venue_lead_lag(st.session_state.venue_price_history)
    if perp_spot_basis_bps is not None:
        basis_history = st.session_state.basis_history
        basis_history.append(
            {
                "UTC time": datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "Basis (bps)": perp_spot_basis_bps,
            }
        )
        st.session_state.basis_history = basis_history[-240:]
    signal = "NO SIGNAL - STALE DATA" if stale_data else get_signal(flow, book, min_flow, min_book)
    now_ms = int(now * 1000)
    cooldown_active = st.session_state.cooldown_until is not None and now < st.session_state.cooldown_until
    completed_klines = tuple(tuple(row) for row in raw_klines if int(row[6]) < now_ms)
    last_close_ms = int(completed_klines[-1][6]) if completed_klines else 0
    candle_forecast = cached_forecast(last_close_ms, completed_klines) if completed_klines else None
    candle_forecast_10m = (
        cached_forecast_10m(last_close_ms, completed_klines) if completed_klines else None
    )

    # The forecast reading, the flow gate, and the conviction score are needed
    # by both the headline metrics and the decision banner, so they are derived
    # once here rather than in two places.
    forecast_direction = str(candle_forecast["direction"]) if candle_forecast else "UNAVAILABLE"
    forecast_below = bool(candle_forecast.get("below_baseline", False)) if candle_forecast else False
    forecast_up = float(candle_forecast["up_score"]) if candle_forecast else 0.5
    flow_ok = flow["trade_count"] >= int(min_trades) and flow["total"] >= float(min_total_btc)
    conviction = calculate_conviction(
        signal,
        flow,
        book,
        forecast_direction,
        forecast_up,
        footprint,
        min_flow=min_flow,
        min_book=min_book,
        flow_ok=flow_ok,
        stale_data=stale_data,
    )

    # Live forecast tracker: log each live call and resolve it against the next
    # candle, so the session shows the model's real hit rate rather than only
    # the historical holdout number.
    forecast_log = resolve_forecast_log(st.session_state.forecast_log, completed_klines)
    if candle_forecast is not None and completed_klines:
        forecast_direction_raw = str(candle_forecast["direction"])
        if forecast_direction_raw.startswith("UP"):
            forecast_log = log_forecast_prediction(
                forecast_log,
                "UP",
                float(candle_forecast["latest_close"]),
                int(completed_klines[-1][0]),
                now,
            )
        elif forecast_direction_raw.startswith("DOWN"):
            forecast_log = log_forecast_prediction(
                forecast_log,
                "DOWN",
                float(candle_forecast["latest_close"]),
                int(completed_klines[-1][0]),
                now,
            )
    st.session_state.forecast_log = forecast_log

    position = st.session_state.paper_position
    if position is not None:
        # A risk limit must never be disabled by a feed problem: keep evaluating
        # the stop and target against the last observed mark even when stale.
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
    if len(st.session_state.equity_history) > EQUITY_CHART_MAX_SAMPLES:
        st.session_state.equity_history = st.session_state.equity_history[-EQUITY_CHART_MAX_SAMPLES:]
    # The risk lock needs the whole session, not just the chart window, otherwise
    # the equity peak is forgotten after a few minutes and the breaker never fires.
    risk_history = list(st.session_state.equity_risk_history)
    if not risk_history or risk_history[-1].get("t") != ts:
        risk_history.append({"t": ts, "eq": equity})
    if len(risk_history) > EQUITY_RISK_MAX_SAMPLES:
        risk_history = risk_history[-EQUITY_RISK_MAX_SAMPLES:]
    st.session_state.equity_risk_history = risk_history
    persist_session_snapshot(now)
    today = datetime.now(timezone.utc).date().isoformat()
    daily_realized = sum(
        float(trade.get("Net PnL (USDT)", 0.0))
        for trade in st.session_state.paper_trades
        if str(trade.get("Closed (UTC)", "")).startswith(today)
    )
    daily_pnl = daily_realized + unrealized
    loss_limit = starting_equity * daily_loss_percent / 100
    daily_trade_count = sum(
        1
        for trade in st.session_state.paper_trades
        if str(trade.get("Closed (UTC)", "")).startswith(today)
    )
    daily_trade_lock = daily_trade_count >= int(max_daily_trades)
    drawdown_pct = calculate_drawdown_pct(risk_history)
    consecutive_losses = calculate_consecutive_losses(st.session_state.paper_trades)
    max_drawdown_lock = drawdown_pct >= max_drawdown_percent
    consecutive_loss_lock = consecutive_losses >= max_consecutive_losses
    risk_locked = (
        daily_pnl <= -loss_limit
        or cooldown_active
        or max_drawdown_lock
        or consecutive_loss_lock
        or daily_trade_lock
    )

    metrics = st.columns(5)
    metrics[0].metric(
        "BTCUSDT perpetual",
        f"{mark:,.2f}",
        f"{price_change_pct:+.2f}% · 60s" if price_change_pct else None,
        help="Binance USDⓈ-M perpetual futures price in USDT"
    )
    metrics[1].metric(
        "Coinbase BTC-USD spot",
        f"{spot_usd:,.2f}" if spot_usd > 0 else "Unavailable",
        "USD",
        help="Coinbase spot price in USD"
    )
    metrics[2].metric(
        "60s aggressor delta",
        f"{flow['delta']:+.3f} BTC",
        f"buy {flow['buy_qty']:.2f} · sell {flow['sell_qty']:.2f}",
        delta_color="off",
        help="Difference between buyer-initiated and seller-initiated volume over last 60s"
    )
    metrics[3].metric(
        "Basis vs spot",
        f"{perp_spot_basis_bps:+.1f} bps" if perp_spot_basis_bps is not None else "Unavailable",
        "USDT-adjusted",
        delta_color="off",
        help="Perpetual premium/discount vs spot converted to USDT equivalent"
    )
    metrics[4].metric(
        "Order-flow signal",
        signal,
        f"conviction {conviction['score']}/100",
        delta_color="off",
        help="LONG/SHORT based on aggressor flow and book imbalance"
    )
    regime_cols = st.columns(2)
    regime_cols[0].metric(
        "Scalping regime",
        market_regime["band"],
        f"{market_regime['volatility_bps']:.1f} bps median step vs {market_regime['round_trip_cost_bps']:.1f} bps round trip",
        delta_color="off",
    )
    regime_cols[1].metric(
        "Move vs cost",
        f"{market_regime['move_to_cost']:.2f}×",
        f"{market_regime['participation_per_sec']:.1f} trades/s · spread {market_regime['spread_bps']:.1f} bps",
        delta_color="off",
    )
    st.caption(
        describe_market_regime(market_regime)
        + (
            " · median move does not clear the round-trip cost, so even correct signals lose to fees."
            if not market_regime["median_move_clears_cost"]
            else ""
        )
    )
    if perp_spot_basis_bps is not None:
        ui.note(
            "BTCUSDT perpetual is quoted in USDT; Coinbase BTC-USD spot is quoted in USD. "
            f"Using USDT/USD at {usdt_usd:.6f} USD per USDT, the converted basis is {perp_spot_basis_bps:+.1f} bps. "
            f"Binance spot-equivalent reference: {spot_usdt_equivalent:,.2f} USDT."
        )
        st.line_chart(
            st.session_state.basis_history,
            x="UTC time",
            y="Basis (bps)",
            height=160,
            alt="Rolling Binance perpetual premium or discount versus USD spot converted to USDT",
        )
    else:
        ui.note(
            "BTCUSDT perpetual is quoted in USDT; Coinbase BTC-USD spot is quoted in USD. "
            "A comparable basis is unavailable until both the spot quote and USDT/USD reference are available."
        )

    render_data_health_strip(
        st.session_state.feed_health,
        feed_ages,
        stale_reasons,
        liquidation_health,
        {"total": int(st.session_state.missed_trades_total), "last": int(st.session_state.last_missed_trades)},
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
    elif stale_data:
        trade_setup = "NO TRADE · STALE DATA"
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

    # Signal tape: record every distinct setup that appears, whether or not the
    # user ever clicks a button, then follow it to target, stop, or timeout.
    tape = update_tape(st.session_state.signal_tape, mark, now)
    if setup_side and not stale_data:
        if should_record_setup(tape, setup_side, now):
            tape_entry = mark * (1 + setup_side * slippage_bps / 10_000)
            tape_stop_distance = tape_entry * stop_bps / 10_000
            tape.append(
                make_setup_record(
                    side=setup_side,
                    mark=mark,
                    entry=tape_entry,
                    stop=tape_entry - setup_side * tape_stop_distance,
                    target=tape_entry + setup_side * tape_stop_distance * reward_risk,
                    stop_bps=stop_bps,
                    now=now,
                    cost_bps=2 * fee_bps + 2 * slippage_bps,
                    context={
                        "Flow signal": signal,
                        "Forecast": forecast_direction,
                        "Up score": round(float(forecast_up), 4),
                        "60s delta (BTC)": round(float(flow["delta"]), 3),
                        "60s trades": int(flow["trade_count"]),
                        "Book imbalance": round(float(book["imbalance"]), 4),
                    },
                )
            )
            if len(tape) > TAPE_MAX_ROWS:
                tape = tape[-TAPE_MAX_ROWS:]
    st.session_state.signal_tape = tape

    decision_signal = "NO SIGNAL - STALE DATA" if stale_data else "WAIT"
    if setup_side == 1:
        decision_signal = "LONG"
    elif setup_side == -1:
        decision_signal = "SHORT"
    confidence = describe_decision_confidence(decision_signal, forecast_direction, flow_ok, stale_data)
    fee_metrics = calculate_fee_adjusted_trade_metrics(stop_bps, reward_risk, fee_bps, slippage_bps)
    active_position = st.session_state.paper_position
    if setup_side:
        decision_entry = mark * (1 + setup_side * slippage_bps / 10_000)
        decision_stop_distance = decision_entry * stop_bps / 10_000
        decision_stop = decision_entry - setup_side * decision_stop_distance
        decision_target = decision_entry + setup_side * decision_stop_distance * reward_risk
    elif active_position is not None:
        decision_entry = float(active_position["entry"])
        decision_stop = float(active_position["stop"])
        decision_target = float(active_position["target"])
    else:
        decision_entry = decision_stop = decision_target = None
    reason = describe_decision_reason(
        signal,
        flow,
        book,
        forecast_direction,
        stale_data=stale_data,
        risk_locked=risk_locked,
        forecast_below=forecast_below,
        flow_ok=flow_ok,
    )
    microprice = calculate_microprice(book)
    why_inputs = build_why_inputs(
        flow,
        book,
        microprice,
        perp_spot_basis_bps,
        forecast_direction,
        book["mid"],
        lead_lag,
        price_change_pct,
        oi_change_pct,
        funding_rate,
        liquidation_totals,
    )
    zone_distances = [
        float(zone["near_bps"])
        for zone in (upper_zone, lower_zone)
        if zone is not None
    ]
    market_alerts, next_alert_state = detect_market_alerts(
        st.session_state.alert_state,
        decision_signal,
        float(flow["delta"]),
        stale_data,
        zone_distances,
        float(zone_alert_bps),
    )
    st.session_state.alert_state = next_alert_state
    alert_settings = (
        ("Stale market data", enable_stale_alert),
        ("Aggressor flow flipped", enable_flow_flip_alert),
        ("New LONG", enable_signal_alert),
        ("New SHORT", enable_signal_alert),
        ("Price is approaching", enable_zone_alert),
    )
    for message in market_alerts:
        if not any(message.startswith(prefix) and enabled for prefix, enabled in alert_settings):
            continue
        st.toast(message, icon=":material/notifications_active:")

    (
        tab_context,
        tab_timing,
        tab_charts,
        tab_flow,
        tab_paper,
        tab_tape,
        tab_prediction,
        tab_research,
    ) = st.tabs(
        [
            "🌐 Context",
            "⏱ Trade timing",
            "📈 Charts",
            "📊 Flow & book",
            "💰 Paper trading",
            "🧾 Signal tape",
            "🔮 Prediction",
            "🔬 Research",
        ]
    )

    with tab_context:
        st.subheader("Positioning and funding")
        positioning_cols = st.columns(4)
        positioning_cols[0].metric(
            "Open interest",
            f"{current_open_interest:,.2f} BTC" if current_open_interest > 0 else "Unavailable",
            help="Total open futures contracts"
        )
        positioning_cols[1].metric(
            "Open-interest change · 60s",
            f"{oi_change_pct:+.3f}%" if oi_change_pct is not None else "Warming up",
            help="Change in open interest over last 60 seconds"
        )
        positioning_cols[2].metric("Price/OI read", price_oi_reading, help="Interpretation of price vs OI movement")
        positioning_cols[3].metric(
            "Funding · next settlement",
            f"{funding_rate * 100:+.4f}% · {funding_countdown}",
            help="Current funding rate and time to next settlement"
        )
        liquidation_cols = st.columns(3)
        liquidation_cols[0].metric("Long liquidations · 60s", f"{liquidation_totals['long_usdt']:,.0f} USDT")
        liquidation_cols[1].metric("Short liquidations · 60s", f"{liquidation_totals['short_usdt']:,.0f} USDT")
        liquidation_cols[2].metric("Forced orders · 60s", int(liquidation_totals["count"]))
        lead_lag_value = (
            f"{lead_lag['leader']} · {lead_lag['lag_seconds']:.1f}s"
            if lead_lag["lag_seconds"] is not None
            else lead_lag["leader"]
        )
        st.metric(
            "Venue lead-lag · rolling",
            lead_lag_value,
            f"correlation {lead_lag['correlation']:.2f}" if lead_lag["correlation"] is not None else None,
        )
        levels_summary = [
            f"15m VWAP {market_levels['rolling_vwap']:,.2f}" if market_levels["rolling_vwap"] is not None else "15m VWAP unavailable",
            f"UTC session H/L {market_levels['session_high']:,.2f} / {market_levels['session_low']:,.2f}"
            if market_levels["session_high"] is not None and market_levels["session_low"] is not None
            else "UTC session H/L unavailable",
            f"Previous-day H/L {market_levels['previous_day_high']:,.2f} / {market_levels['previous_day_low']:,.2f}"
            if market_levels["previous_day_high"] is not None and market_levels["previous_day_low"] is not None
            else "Previous-day H/L unavailable",
            f"Round levels {market_levels['round_500']:,.0f} / {market_levels['round_1000']:,.0f} USDT"
            if market_levels["round_500"] is not None and market_levels["round_1000"] is not None
            else "Round levels unavailable",
        ]
        st.caption(" · ".join(levels_summary))

        if candle_forecast is not None:
            forecast_cols = st.columns(4)
            forecast_cols[0].metric("Directional estimate", str(candle_forecast["direction"]))
            forecast_cols[1].metric("Model up-score (not calibrated probability)", f"{float(candle_forecast['up_score']):.1%}")
            forecast_cols[2].metric(
                "Recent holdout accuracy",
                f"{float(candle_forecast['validation_accuracy']):.1%}",
                f"majority baseline {float(candle_forecast['baseline_accuracy']):.1%}",
            )
            live_forecast_summary = summarize_forecast_log(forecast_log)
            forecast_cols[3].metric(
                "Live hit rate this session",
                f"{float(live_forecast_summary['hit_rate']):.1%}" if live_forecast_summary["resolved"] else "Warming up",
                f"{int(live_forecast_summary['resolved'])} resolved · {int(live_forecast_summary['pending'])} pending",
                delta_color="off",
            )
            st.progress(min(max(forecast_up, 0.0), 1.0))
            if forecast_below:
                st.warning("Forecast validation accuracy below majority-class baseline — treat edge as weak.", icon="⚠️")
            st.caption(
                f"Uses {candle_forecast['training_samples']} labeled candles and tests on the latest "
                f"{candle_forecast['validation_samples']} held-out candles. Features: 1/3/5-minute returns, "
                "last candle body, and relative volume. The score is a simple logistic-model output, not a calibrated "
                "probability; the estimate updates when a new one-minute candle closes."
            )

    with tab_timing:
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
            st.caption("New paper entries are blocked by the active risk limit shown under Paper trading.")
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
                direction = "above" if side == "up" else "below"
                column.metric(
                    label,
                    "No liquidity void",
                    f"None within {side_data['coverage_bps']:.1f} bps {direction}",
                )
        jump_cols[2].metric("Order-flow confirmation", jump_confirmation)
        st.caption(
            "Nearest full 1-bps band with less than half the median resting size, within the captured 1,000-level snapshot. "
            "A thin band may allow faster movement if pressure reaches it; it is not a price prediction, and orders can be canceled."
        )

    with tab_charts:
        st.subheader("Live BTCUSDT price · short-term views")
        chart_columns = st.columns(3)
        position_for_chart = st.session_state.paper_position
        marked_levels = []
        if setup_side:
            marked_levels = [
                {"Level": "Entry", "Color": "Entry", "Price": decision_entry},
                {"Level": "Stop", "Color": "Stop", "Price": decision_stop},
                {"Level": "Target", "Color": "Target", "Price": decision_target},
            ]
        elif position_for_chart is not None:
            marked_levels = [
                {"Level": "Entry", "Color": "Entry", "Price": float(position_for_chart["entry"])},
                {"Level": "Stop", "Color": "Stop", "Price": float(position_for_chart["stop"])},
                {"Level": "Target", "Color": "Target", "Price": float(position_for_chart["target"])},
            ]
        for key, level_name in (
            ("rolling_vwap", "Rolling VWAP"),
            ("session_high", "Session high"),
            ("session_low", "Session low"),
            ("previous_day_high", "Previous day high"),
            ("previous_day_low", "Previous day low"),
            ("round_500", "Nearest $500"),
            ("round_1000", "Nearest $1,000"),
        ):
            value = market_levels[key]
            if value is not None:
                marked_levels.append({"Level": level_name, "Color": level_name, "Price": value})
        chart_color = alt.Color(
            "Color:N",
            scale=alt.Scale(
                domain=[
                    "Up", "Down", "Thin asks", "Thin bids", "Entry", "Stop", "Target",
                    "Rolling VWAP", "Session high", "Session low", "Previous day high",
                    "Previous day low", "Nearest $500", "Nearest $1,000",
                ],
                range=[
                    "#177a55", "#c34444", "#c34444", "#177a55", "#1769aa", "#c34444", "#a76a21",
                    "#1769aa", "#a76a21", "#a76a21", "#6a5acd", "#6a5acd", "#64748b", "#334155",
                ],
            ),
            legend=None,
        )
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
                    candle_tooltip = [
                        alt.Tooltip("UTC time:T", title="Bucket (UTC)"),
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
                        color=chart_color,
                        tooltip=candle_tooltip,
                    )
                    bodies = candle_data.mark_bar(size=8).encode(
                        x=x_axis,
                        y=alt.Y("Open:Q", scale=alt.Scale(zero=False, padding=8), title="Price (USDT)"),
                        y2="Close:Q",
                        color=chart_color,
                        tooltip=candle_tooltip,
                    )
                    chart_layers = [wicks, bodies]
                    zone_rows = []
                    for side, zone_name in (("up", "Thin asks"), ("down", "Thin bids")):
                        zone = jump_zones[side]["zone"]
                        if zone:
                            zone_rows.append(
                                {
                                    "start": chart_rows[0]["UTC time"],
                                    "end": chart_rows[-1]["UTC time"],
                                    "lower": zone["lower_price"],
                                    "upper": zone["upper_price"],
                                    "Zone": zone_name,
                                    "Color": zone_name,
                                }
                            )
                    if zone_rows:
                        chart_layers.append(
                            alt.Chart(alt.Data(values=zone_rows))
                            .mark_rect(opacity=0.12)
                            .encode(
                                x=alt.X("start:T", axis=None),
                                x2="end:T",
                                y="lower:Q",
                                y2="upper:Q",
                                color=chart_color,
                                tooltip=["Zone:N", alt.Tooltip("lower:Q", format=",.2f"), alt.Tooltip("upper:Q", format=",.2f")],
                            )
                        )
                    if marked_levels:
                        chart_layers.append(
                            alt.Chart(alt.Data(values=marked_levels))
                            .mark_rule(strokeDash=[4, 3], strokeWidth=1.5)
                            .encode(
                                y=alt.Y("Price:Q", title="Price (USDT)"),
                                color=chart_color,
                                tooltip=["Level:N", alt.Tooltip("Price:Q", format=",.2f")],
                            )
                        )
                    price_chart = (
                        alt.layer(*chart_layers)
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
        st.subheader("Microstructure · 1-second candles, volume, and CVD")
        second_rows = aggregate_trade_candles(trades, now, 60, interval_seconds=1)
        if second_rows:
            second_data = alt.Chart(alt.Data(values=second_rows))
            second_x = alt.X(
                "UTC time:T",
                scale=alt.Scale(type="utc"),
                axis=alt.Axis(title=None, format="%H:%M:%S", labelAngle=0, tickCount=8),
            )
            second_wicks = second_data.mark_rule().encode(
                x=second_x,
                y=alt.Y("High:Q", scale=alt.Scale(zero=False, padding=5), title="Price (USDT)"),
                y2="Low:Q",
                color=chart_color,
                tooltip=[
                    alt.Tooltip("UTC time:T", title="Bucket (UTC)"),
                    alt.Tooltip("Open:Q", format=",.2f"),
                    alt.Tooltip("High:Q", format=",.2f"),
                    alt.Tooltip("Low:Q", format=",.2f"),
                    alt.Tooltip("Close:Q", format=",.2f"),
                ],
            )
            second_bodies = second_data.mark_bar(size=5).encode(
                x=second_x,
                y=alt.Y("Open:Q", scale=alt.Scale(zero=False, padding=5), title="Price (USDT)"),
                y2="Close:Q",
                color=chart_color,
            )
            micro_price = alt.layer(second_wicks, second_bodies).properties(height=210)
            volume_chart = (
                second_data.mark_bar()
                .encode(
                    x=second_x,
                    y=alt.Y("Volume (BTC):Q", title="Volume (BTC)"),
                    color=chart_color,
                    tooltip=[alt.Tooltip("Volume (BTC):Q", format=",.4f"), alt.Tooltip("Trades:Q")],
                )
                .properties(height=90)
            )
            cvd_chart = (
                second_data.mark_line(color="#1769aa", strokeWidth=2)
                .encode(
                    x=second_x,
                    y=alt.Y("CVD (BTC):Q", title="CVD (BTC)"),
                    tooltip=[alt.Tooltip("CVD (BTC):Q", format="+,.4f")],
                )
                .properties(height=90)
            )
            microstructure_chart = (
                alt.vconcat(micro_price, volume_chart, cvd_chart, spacing=4)
                .resolve_scale(x="shared")
                .configure_view(stroke=None)
                .configure_axis(gridColor="#d9e3dc", labelColor="#65756c", titleColor="#65756c")
            )
            st.altair_chart(
                microstructure_chart,
                width="stretch",
                alt="BTCUSDT one-second candles with traded volume and cumulative aggressor delta over the last minute",
            )
        else:
            st.info("Waiting for enough recent Binance trades to build one-second candles.")
        if trades:
            latest_trade_time = datetime.fromtimestamp(trades[-1]["time"], timezone.utc).strftime("%H:%M:%S UTC")
            st.caption(f"Latest Binance trade: {latest_trade_time} · charts refresh every {REFRESH_SECONDS} seconds · times in UTC.")

        with st.expander("Order-book heatmap · websocket depth, trades, and CVD", key="heatmap_expander"):
            st.caption(
                "The standalone heatmap is embedded here for convenience. It opens its own Binance and Coinbase "
                "websocket streams, so it runs independently of the polling loop above and does not delay it."
            )
            heatmap_html = load_heatmap_html()
            if heatmap_html:
                st.iframe(heatmap_html, height=900, width="stretch")
            else:
                st.info("heatmap.html was not found next to app.py.")

    with tab_flow:
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
            flow_intervals = flow["intervals"]
            if flow_intervals:
                average_interval_delta = sum(row["Net delta (BTC)"] for row in flow_intervals) / len(flow_intervals)
                st.metric("Average net delta per 10s · last minute", f"{average_interval_delta:+.3f} BTC")
            st.dataframe(flow_intervals, hide_index=True, width="stretch", height=250)
            recent = [trade for trade in trades if trade["time"] >= now - TRADE_WINDOW_SECONDS]
            large_prints = [trade for trade in recent if float(trade["quantity"]) >= large_trade_threshold]
            st.markdown("**Large prints · last 60 seconds**")
            if large_prints:
                st.dataframe(
                    [
                        {
                            "UTC time": datetime.fromtimestamp(trade["time"], timezone.utc).strftime("%H:%M:%S.%f")[:-3],
                            "Aggressor": "Sell" if trade["sell_aggressor"] else "Buy",
                            "Price (USDT)": trade["price"],
                            "Size (BTC)": trade["quantity"],
                        }
                        for trade in reversed(large_prints[-20:])
                    ],
                    hide_index=True,
                    width="stretch",
                    height=180,
                )
            else:
                st.caption(f"No prints at or above {large_trade_threshold:.2f} BTC in the last 60 seconds.")
            if recent and abs(float(flow["delta_fraction"])) >= min_flow:
                alert = absorption_heuristic(recent, flow)
                if alert and st.session_state.get("absorption_shown") is not True:
                    st.session_state["absorption_shown"] = True
                    st.info(alert["message"])
                elif not alert:
                    st.session_state["absorption_shown"] = False
            st.subheader("Institutional footprint · last 2.5 minutes")
            footprint_summary = describe_footprint(footprint)
            footprint_bias_label = footprint_bias(footprint)
            if footprint["active"]:
                st.success(f"**{footprint_summary}** · Bias: **{footprint_bias_label}**")
            else:
                st.caption(footprint_summary)
            metric_columns = st.columns(4)
            metric_columns[0].metric("Volume vs baseline", f"{footprint['volume_ratio']:.2f}x")
            metric_columns[1].metric("Delta fraction", f"{footprint['delta_fraction']:+.1%}")
            metric_columns[2].metric("Persistence", f"{footprint['persistence']:.0%}")
            metric_columns[3].metric("Confidence", f"{footprint['confidence']:.0%}")
            with st.expander("Footprint scorecard"):
                st.dataframe(
                    [
                        {"Factor": name, "Fired": "yes" if fired else "no"}
                        for name, fired in footprint["factors"].items()
                    ],
                    hide_index=True,
                    width="stretch",
                )
                st.caption(
                    f"Aggression {footprint['aggression_notional']:,.0f} USDT · "
                    f"price moved {footprint['price_move_bps']:+.1f} bps against a typical "
                    f"{footprint['expected_move_bps']:.1f} bps"
                )
                depletion = footprint.get("book_depletion")
                if depletion:
                    st.caption(
                        f"Passive {depletion['passive_side']} size {depletion['change_pct']:+.1f}% "
                        f"over {depletion['elapsed_seconds']:.0f}s"
                    )
            if footprint["price_levels"]:
                st.markdown("**Price-level footprint**")
                st.dataframe(
                    [
                        {
                            "Price (USDT)": level["price"],
                            "Buy (BTC)": level["buy_btc"],
                            "Sell (BTC)": level["sell_btc"],
                            "Imbalance": level["imbalance"],
                            "Unbalanced notional": level["unbalanced_notional"],
                        }
                        for level in footprint["price_levels"]
                    ],
                    hide_index=True,
                    width="stretch",
                    height=220,
                )
            if footprint["buckets"]:
                st.markdown("**Detection window · 15 second buckets**")
                st.dataframe(
                    footprint_rows(footprint["buckets"], now, footprint["direction"]),
                    hide_index=True,
                    width="stretch",
                    height=220,
                )
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
            depth_curve = summarize_depth_curve(book)
            if depth_curve:
                depth_chart = (
                    alt.Chart(alt.Data(values=depth_curve))
                    .mark_area(opacity=0.35)
                    .encode(
                        x=alt.X("Distance from mid (bps):Q", title="Distance from mid (bps)"),
                        y=alt.Y("Cumulative size (BTC):Q", title="Cumulative size (BTC)"),
                        color=alt.Color(
                            "Side:N",
                            scale=alt.Scale(domain=["Bid", "Ask"], range=["#177a55", "#c34444"]),
                        ),
                        tooltip=[
                            "Side:N",
                            alt.Tooltip("Distance from mid (bps):Q", format="+.2f"),
                            alt.Tooltip("Cumulative size (BTC):Q", format=",.3f"),
                        ],
                    )
                    .properties(height=180)
                    .configure_view(stroke=None)
                    .configure_axis(gridColor="#d9e3dc", labelColor="#65756c", titleColor="#65756c")
                )
                st.altair_chart(depth_chart, width="stretch", alt="Cumulative bid and ask depth by distance from the mid price")
            else:
                st.info("Depth chart unavailable until both sides of the book are present.")

    with tab_paper:
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
            if daily_trade_lock:
                lock_reasons.append(f"daily trade cap of {int(max_daily_trades)} reached ({daily_trade_count})")
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
                    st.toast(
                        f"Paper {'long' if long_clicked else 'short'} opened · updates on next refresh",
                        icon=":material/add_circle:",
                    )
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
                st.toast("Paper position closed · updates on next refresh", icon=":material/close:")
            if stale_data:
                st.warning(
                    "Market data is stale: the close uses the last observed price, so the modelled fill may differ "
                    "from the live market. Exiting is still allowed so a position is never trapped by an outage."
                )

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
            max_dd = calculate_drawdown_pct(list(st.session_state.equity_risk_history))
            stats = calculate_trade_statistics(trades_list)
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Trades", total)
            c2.metric("Win rate", f"{winrate:.1f}%")
            c3.metric("Avg |R|", f"{avg_r:.2f}")
            c4.metric("Expectancy (USDT)", f"{expectancy:+.2f}")
            c5, c6, c7, c8 = st.columns(4)
            c5.metric("W/L/B", f"{wins}/{losses}/{breakeven}")
            c6.metric("Profit factor", f"{pf:.2f}" if pf != float("inf") else "∞")
            c7.metric("Max DD (session)", f"{max_dd:.2f}%")
            c8.metric("Total PnL", f"{total_pnl:+,.2f} USDT")
            c9, c10, c11, c12 = st.columns(4)
            sharpe_like = stats["sharpe_like"]
            sortino_like = stats["sortino_like"]
            expectancy_r = stats["expectancy_r"]
            avg_hold = stats["avg_hold_seconds"]
            c9.metric(
                "Sharpe · per trade",
                f"{sharpe_like:.2f}" if sharpe_like is not None else "—",
                "mean R / std R, unannualised",
                delta_color="off",
            )
            c10.metric(
                "Sortino · per trade",
                f"{sortino_like:.2f}" if sortino_like is not None else "—",
                "mean R / downside dev",
                delta_color="off",
            )
            c11.metric(
                "Expectancy (R)",
                f"{expectancy_r:+.3f}" if expectancy_r is not None else "—",
                f"avg hold {avg_hold:.0f}s" if avg_hold is not None else "avg hold —",
                delta_color="off",
            )
            c12.metric(
                "Fees paid",
                f"{float(stats['total_fees']):,.2f} USDT",
                f"streaks {int(stats['max_consecutive_wins'])}W / {int(stats['max_consecutive_losses'])}L",
                delta_color="off",
            )
            st.caption(
                "Per-trade Sharpe and Sortino are unannualised mean-over-deviation ratios on R multiples; "
                "a scalping sample of a few dozen trades cannot support an annualised figure."
            )
            if eq_curve:
                st.subheader("Equity curve")
                st.line_chart(eq_curve, x="t", y="eq", height=240)
            
            st.subheader("Trade history")
            history_df = []
            for t in trades_list:
                history_df.append({
                    "Closed (UTC)": t.get("Closed (UTC)"),
                    "Side": t.get("Side"),
                    "Entry": t.get("Entry"),
                    "Exit": t.get("Exit"),
                    "Quantity (BTC)": t.get("Quantity (BTC)"),
                    "Stop": t.get("Stop"),
                    "Target": t.get("Target"),
                    "Fees (USDT)": t.get("Fees (USDT)"),
                    "Net PnL (USDT)": t.get("Net PnL (USDT)"),
                    "R": t.get("R"),
                    "Hold (s)": t.get("Hold (s)"),
                    "Exit reason": t.get("Exit reason"),
                })
            st.dataframe(history_df, hide_index=True, width="stretch", height=300)
            import csv
            import io
            output = io.StringIO()
            if history_df:
                writer = csv.DictWriter(output, fieldnames=history_df[0].keys())
                writer.writeheader()
                writer.writerows(history_df)
            csv_data = output.getvalue()
            st.download_button(
                label="Export trades as CSV",
                data=csv_data,
                file_name=f"btc_paper_trades_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.csv",
                mime="text/csv",
                use_container_width=False,
            )

    with tab_tape:
        tape_summary = summarize_tape(tape)
        st.subheader("Signal tape · every setup the rule produced")
        st.caption(
            "Each distinct setup is recorded the moment the order-flow signal and the candle model agree, "
            "whether or not a position was ever opened. The tape then follows the observed perp trade price "
            f"until the target is hit, the stop is hit, or {TAPE_HORIZON_SECONDS // 60} minutes pass. "
            "Results are net of the configured round-trip fees and slippage."
        )
        tape_cols = st.columns(4)
        tape_cols[0].metric("Setups recorded", int(tape_summary["setups"]))
        tape_cols[1].metric("Resolved", int(tape_summary["resolved"]))
        tape_cols[2].metric("Target rate", f"{float(tape_summary['target_rate']):.1%}")
        tape_cols[3].metric("Total R", f"{float(tape_summary['total_r']):+.2f}R")
        tape_cols2 = st.columns(4)
        tape_cols2[0].metric("Targets", int(tape_summary["targets"]))
        tape_cols2[1].metric("Stops", int(tape_summary["stops"]))
        tape_cols2[2].metric("Timeouts", int(tape_summary["timeouts"]))
        tape_cols2[3].metric("Expectancy", f"{float(tape_summary['expectancy_r']):+.3f}R per setup")
        tape_cols3 = st.columns(4)
        tape_cols3[0].metric("Avg MFE", f"{float(tape_summary['avg_mfe_bps']):+.1f} bps")
        tape_cols3[1].metric("Avg MAE", f"{float(tape_summary['avg_mae_bps']):+.1f} bps")
        tape_cols3[2].metric("Best", f"{float(tape_summary['best_r']):+.2f}R")
        tape_cols3[3].metric("Worst", f"{float(tape_summary['worst_r']):+.2f}R")
        if tape_summary["resolved"]:
            if float(tape_summary["expectancy_r"]) > 0:
                st.success(
                    f"Across {tape_summary['resolved']} resolved setups the rule averaged "
                    f"{float(tape_summary['expectancy_r']):+.3f}R per setup."
                )
            else:
                st.warning(
                    f"Across {tape_summary['resolved']} resolved setups the rule averaged "
                    f"{float(tape_summary['expectancy_r']):+.3f}R per setup. A negative expectancy means the "
                    "rule as configured is not paying for its costs."
                )
        else:
            st.info("No setup has resolved yet. The tape fills in once a recorded setup reaches its target, its stop, or the horizon.")
        if tape:
            tape_rows = [
                {
                    "Recorded (UTC)": row.get("Recorded (UTC)"),
                    "Side": row.get("Side"),
                    "Status": row.get("status"),
                    "Entry": round(float(row.get("entry", 0.0)), 2),
                    "Exit": round(float(row["exit"]), 2) if row.get("exit") is not None else None,
                    "Result (bps)": round(float(row["result_bps"]), 1) if row.get("result_bps") is not None else None,
                    "Net R": round(float(row["net_r"]), 3) if row.get("net_r") is not None else None,
                    "MFE (bps)": round(float(row.get("mfe_bps", 0.0)), 1),
                    "MAE (bps)": round(float(row.get("mae_bps", 0.0)), 1),
                    "Forecast": row.get("Forecast"),
                    "60s delta": row.get("60s delta (BTC)"),
                    "Book imb.": row.get("Book imbalance"),
                }
                for row in tape
            ]
            st.dataframe(tape_rows, hide_index=True, width="stretch", height=300)
            tape_csv_header = (
                "Recorded (UTC),Side,Status,Entry,Exit,Result (bps),Net R,MFE (bps),MAE (bps),"
                "Forecast,60s delta,Book imbalance\n"
            )
            tape_csv_lines = [
                ",".join(
                    [
                        str(row["Recorded (UTC)"]),
                        str(row["Side"]),
                        str(row["Status"]),
                        f"{float(row['Entry']):.2f}",
                        "" if row["Exit"] is None else f"{float(row['Exit']):.2f}",
                        "" if row["Result (bps)"] is None else f"{float(row['Result (bps)']):.2f}",
                        "" if row["Net R"] is None else f"{float(row['Net R']):.4f}",
                        f"{float(row['MFE (bps)']):.2f}",
                        f"{float(row['MAE (bps)']):.2f}",
                        str(row["Forecast"]),
                        str(row["60s delta"]),
                        str(row["Book imb."]),
                    ]
                )
                for row in tape_rows
            ]
            export_cols = st.columns([1, 3])
            export_cols[0].download_button(
                "Export tape CSV",
                data=tape_csv_header + "\n".join(tape_csv_lines),
                file_name="btc_signal_tape.csv",
                mime="text/csv",
                use_container_width=True,
            )
            if export_cols[1].button("Clear signal tape", key="clear_signal_tape"):
                st.session_state.signal_tape = []
                delete_session_value("signal_tape")
                tape = []
                st.toast("Signal tape cleared", icon=":material/delete_outline:")
        else:
            st.info("The tape is empty. It records the first setup as soon as the signal and the model agree.")

        st.divider()
        st.subheader("Live forecast scorecard")
        forecast_summary = summarize_forecast_log(forecast_log)
        st.caption(
            "Every forecast the dashboard displays is logged with the candle it was based on, then scored "
            "against the next candle once that candle closes. This is the model's real hit rate in the "
            "current session, next to its historical holdout accuracy."
        )
        f_cols = st.columns(4)
        f_cols[0].metric("Forecasts logged", int(forecast_summary["logged"]))
        f_cols[1].metric("Resolved", int(forecast_summary["resolved"]))
        f_cols[2].metric("Live hit rate", f"{float(forecast_summary['hit_rate']):.1%}")
        f_cols[3].metric("Pending", int(forecast_summary["pending"]))
        if forecast_summary["resolved"]:
            live_rate = float(forecast_summary["hit_rate"])
            holdout = float(candle_forecast["validation_accuracy"]) if candle_forecast else None
            if holdout is not None:
                if live_rate >= holdout:
                    st.success(f"Live hit rate {live_rate:.1%} is at or above the {holdout:.1%} holdout accuracy.")
                else:
                    st.warning(
                        f"Live hit rate {live_rate:.1%} is below the {holdout:.1%} holdout accuracy. "
                        "Treat the model as weak in the current regime."
                    )

    with tab_prediction:
        st.subheader("10-minute candle direction · study across all available data")
        st.caption(
            "Walks forward across the full Binance 1-minute history: at each candle, trains on all earlier candles "
            "(expanding window) and predicts whether the candle 10 minutes ahead closes up or down. Each point on "
            "the graph is one prediction scored against what actually happened. The score is a logistic-model output, "
            "not a calibrated probability. Accuracy is reported next to the majority-class baseline."
        )

        # Lazy, cached study: only recomputed when a new candle closes.
        study_key = (last_close_ms, len(raw_klines))
        if st.session_state.get("forecast_study_key") != study_key:
            with st.spinner("Computing 10-minute walk-forward study across all candles..."):
                st.session_state.forecast_study = forecast_study(
                    tuple(tuple(row) for row in raw_klines),
                    horizon_candles=10,
                    now_ms=int(now * 1000),
                )
                st.session_state.forecast_study_key = study_key
        study_rows = st.session_state.get("forecast_study") or []

        live_10m = candle_forecast_10m
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("10-minute estimate", str(live_10m["direction"]) if live_10m else "Unavailable")
        c2.metric("Up score (not probability)", f"{float(live_10m['up_score']):.1%}" if live_10m else "—")
        c3.metric("Latest close", f"{float(live_10m['latest_close']):,.2f}" if live_10m else "—")
        c4.metric("Horizon", "10 minutes")

        if study_rows:
            correct = sum(1 for r in study_rows if r["correct"])
            accuracy = correct / len(study_rows)
            up_count = sum(1 for r in study_rows if r["actual_up"])
            baseline = max(up_count, len(study_rows) - up_count) / len(study_rows)
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Study accuracy", f"{accuracy:.1%}")
            m2.metric("Majority baseline", f"{baseline:.1%}")
            m3.metric("Points studied", len(study_rows))
            edge = accuracy - baseline
            m4.metric("Edge over baseline", f"{edge:+.1%}", delta_color="off" if abs(edge) < 0.02 else ("normal" if edge > 0 else "inverse"))

            chart_rows = [
                {
                    "time": r["time_ms"],
                    "up_score": r["up_score"],
                    "actual_up": r["actual_up"],
                    "correct": r["correct"],
                }
                for r in study_rows
            ]
            chart = (
                alt.Chart(alt.Data(values=chart_rows))
                .mark_line(point=True)
                .encode(
                    x=alt.X("time:T", title="Prediction time (UTC)"),
                    y=alt.Y("up_score:Q", title="Up score", scale=alt.Scale(domain=[0, 1])),
                    color=alt.Color("correct:N", scale=alt.Scale(domain=[True, False], range=["#177a55", "#c34444"])),
                    tooltip=[
                        alt.Tooltip("time:T", title="Time (UTC)"),
                        alt.Tooltip("up_score:Q", title="Up score", format=".1%"),
                        alt.Tooltip("actual_up:N", title="Actual up"),
                        alt.Tooltip("correct:N", title="Correct"),
                    ],
                )
                .properties(height=300)
                .configure_view(stroke=None)
            )
            st.altair_chart(chart, width="stretch", alt="10-minute walk-forward predictions vs actuals")
            st.caption("Green = prediction matched the actual 10-minute outcome · red = missed · line height is the model's up score.")
        else:
            st.info("Not enough candle history yet to build a 10-minute study. Keep the feed running; the study appears once there are sufficient completed candles.")

    with tab_research:
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
    with decision_slot.container():
        ui.decision_banner(
            signal=decision_signal,
            confidence=confidence,
            conviction_score=int(conviction["score"]),
            conviction_band=str(conviction["band"]),
            state=trade_setup,
            entry=decision_entry,
            stop=decision_stop,
            target=decision_target,
            reason=reason,
            reward_risk=f"{fee_metrics['reward_risk']:.2f}:1",
            breakeven=f"{fee_metrics['breakeven_win_rate']:.1%}",
        )
        if conviction["components"]:
            conviction_cols = st.columns(5)
            for column, (name, value) in zip(conviction_cols, conviction["components"].items()):
                column.metric(name, f"{float(value) * 100:.0f}%", delta_color="off")
        if why_inputs["conflict"]:
            ui.alert("warn", "Conflicting directional inputs: wait for confirmation.")
        with st.expander("Why this decision", expanded=False):
            st.dataframe(why_inputs["rows"], hide_index=True, width="stretch")


with st.spinner("Fetching market data..."):
    market_dashboard()
