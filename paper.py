"""Paper trading engine: position lifecycle, risk locks, and decision text.

The engine is framework-agnostic — it receives and returns plain dicts so the
Streamlit layer stays a thin adapter. Persistence hooks are imported from
``journal``; analytics from ``analytics``.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from analytics import (
    interpret_price_open_interest as interpret_price_open_interest_impl,
)
from journal import append_trade_journal, delete_session_value, save_session_value


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
    import streamlit as st

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
    position = {
        "side": side,
        "label": "LONG" if side > 0 else "SHORT",
        "entry": entry,
        "quantity": quantity,
        "stop": entry - side * stop_distance,
        "target": entry + side * stop_distance * reward_risk,
        "entry_fee": entry * quantity * fee_bps / 10_000,
        "opened": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "opened_ts": datetime.now(timezone.utc).timestamp(),
        "risk_budget": risk_budget,
    }
    st.session_state.paper_position = position
    save_session_value("paper_position", position)
    return None


def close_position(mark: float, reason: str, fee_bps: float, slippage_bps: float) -> None:
    import streamlit as st

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
    closed_at = datetime.now(timezone.utc)
    opened_ts = float(position.get("opened_ts", closed_at.timestamp()))
    trade_record = {
        "Closed (UTC)": closed_at.strftime("%Y-%m-%d %H:%M:%S"),
        "Side": position["label"],
        "Entry": position["entry"],
        "Exit": exit_fill,
        "Stop": position.get("stop"),
        "Target": position.get("target"),
        "Quantity (BTC)": position["quantity"],
        "Fees (USDT)": position["entry_fee"] + exit_fee,
        "Net PnL (USDT)": net_pnl,
        "Hold (s)": max(0.0, closed_at.timestamp() - opened_ts),
        "R": r_multiple,
        "Exit reason": reason,
    }
    st.session_state.paper_trades.insert(0, trade_record)
    append_trade_journal(trade_record)
    st.session_state.paper_position = None
    delete_session_value("paper_position")
    if net_pnl < 0:
        st.session_state.cooldown_until = datetime.now(timezone.utc).timestamp() + 300.0


def hit_stop_or_target(position: dict[str, Any], mark: float) -> str | None:
    """Which level, if any, did the mark cross? Risk checks never pause for staleness."""
    stop_hit = mark <= position["stop"] if position["side"] > 0 else mark >= position["stop"]
    target_hit = mark >= position["target"] if position["side"] > 0 else mark <= position["target"]
    if stop_hit:
        return "Stop"
    if target_hit:
        return "Target"
    return None


def daily_trade_rows(trades: list[dict[str, Any]], today: str) -> list[dict[str, Any]]:
    return [trade for trade in trades if trade["Closed (UTC)"].startswith(today)]


def is_risk_locked(
    *,
    daily_pnl: float,
    loss_limit: float,
    cooldown_active: bool,
    drawdown_pct: float,
    max_drawdown_percent: float,
    consecutive_losses: int,
    max_consecutive_losses: int,
    daily_trade_count: int,
    max_daily_trades: int,
) -> dict[str, Any]:
    """Evaluate every circuit breaker and report which ones fired."""
    max_drawdown_lock = drawdown_pct >= max_drawdown_percent
    consecutive_loss_lock = consecutive_losses >= max_consecutive_losses
    daily_trade_lock = daily_trade_count >= int(max_daily_trades)
    return {
        "locked": (
            daily_pnl <= -loss_limit
            or cooldown_active
            or max_drawdown_lock
            or consecutive_loss_lock
            or daily_trade_lock
        ),
        "max_drawdown_lock": max_drawdown_lock,
        "consecutive_loss_lock": consecutive_loss_lock,
        "daily_trade_lock": daily_trade_lock,
    }


def describe_lock_reasons(
    *,
    cooldown_active: bool,
    cooldown_remaining: float | None,
    daily_pnl: float,
    loss_limit: float,
    max_drawdown_lock: bool,
    max_drawdown_percent: float,
    drawdown_pct: float,
    consecutive_loss_lock: bool,
    max_consecutive_losses: int,
    consecutive_losses: int,
    daily_trade_lock: bool,
    max_daily_trades: int,
    daily_trade_count: int,
) -> list[str]:
    reasons: list[str] = []
    if cooldown_active:
        reasons.append(f"cooldown active for ~{int(cooldown_remaining or 0)}s")
    if daily_pnl <= -loss_limit:
        reasons.append(f"daily loss limit reached ({daily_pnl:,.2f} USDT)")
    if max_drawdown_lock:
        reasons.append(
            f"drawdown hit {max_drawdown_percent:.1f}% threshold ({drawdown_pct:.1f}%)"
        )
    if consecutive_loss_lock:
        reasons.append(
            f"{max_consecutive_losses} consecutive losses reached ({consecutive_losses})"
        )
    if daily_trade_lock:
        reasons.append(
            f"daily trade cap of {int(max_daily_trades)} reached ({daily_trade_count})"
        )
    return reasons


def forecast_allows_side(
    candle_forecast: dict[str, Any] | None,
    forecast_direction: str,
    side: int,
    *,
    require_clear: bool,
    require_validation_edge: bool,
) -> bool:
    """Does the candle estimate permit a trade in this direction?"""
    if not candle_forecast:
        return False
    if require_validation_edge and bool(candle_forecast.get("below_baseline", False)):
        return False
    if side > 0:
        if require_clear and forecast_direction != "UP_CLEAR":
            return False
        return forecast_direction in ("UP_CLEAR", "UP_WEAK")
    if side < 0:
        if require_clear and forecast_direction != "DOWN_CLEAR":
            return False
        return forecast_direction in ("DOWN_CLEAR", "DOWN_WEAK")
    return False


def resolve_trade_setup(
    *,
    position: dict[str, Any] | None,
    stale_data: bool,
    risk_locked: bool,
    cooldown_active: bool,
    require_validation_edge: bool,
    forecast_below: bool,
    signal: str,
    flow_ok: bool,
    forecast_allows: bool,
) -> tuple[str, int]:
    """Map the desk state to a human setup label and a signed side."""
    if position is not None:
        return "MANAGE OPEN POSITION", 0
    if stale_data:
        return "NO TRADE · STALE DATA", 0
    if risk_locked:
        return "NO TRADE · COOL DOWN (5m)" if cooldown_active else "NO TRADE · DAILY LOSS LIMIT", 0
    if require_validation_edge and forecast_below:
        return "WAIT · MODEL BELOW BASELINE", 0
    if signal == "LONG" and forecast_allows and flow_ok:
        return "PAPER LONG SETUP", 1
    if signal == "SHORT" and forecast_allows and flow_ok:
        return "PAPER SHORT SETUP", -1
    return "WAIT · SIGNALS NOT ALIGNED", 0


def describe_decision_confidence(
    signal: str, forecast_direction: str, flow_ok: bool, stale_data: bool
) -> str:
    if stale_data:
        return "Unavailable · stale data"
    if signal not in ("LONG", "SHORT") or not flow_ok:
        return "Low · heuristic"
    forecast_agrees = forecast_direction.startswith("UP") if signal == "LONG" else forecast_direction.startswith("DOWN")
    return "High · heuristic" if forecast_agrees else "Moderate · heuristic"


def describe_decision_reason(
    signal: str,
    flow: dict[str, Any],
    book: dict[str, Any],
    forecast_direction: str,
    *,
    stale_data: bool,
    risk_locked: bool,
    forecast_below: bool,
    flow_ok: bool,
) -> str:
    if stale_data:
        return "WAIT: Binance trade or book data is stale; signals and paper entries are disabled."
    if risk_locked:
        return "WAIT: a configured paper risk limit is active."
    flow_side = "buyers" if float(flow["delta_fraction"]) > 0 else "sellers" if float(flow["delta_fraction"]) < 0 else "balanced flow"
    book_side = "bid depth heavier" if float(book["imbalance"]) > 0 else "ask depth heavier" if float(book["imbalance"]) < 0 else "book balanced"
    if forecast_below:
        return f"WAIT: {flow_side}, {book_side}, but the model's holdout is below baseline."
    if signal == "LONG" and flow_ok and forecast_direction.startswith("UP"):
        return "LONG: buyer aggression, bid depth, and the directional model align (heuristic)."
    if signal == "SHORT" and flow_ok and forecast_direction.startswith("DOWN"):
        return "SHORT: seller aggression, ask depth, and the directional model align (heuristic)."
    if not flow_ok:
        return f"WAIT: {flow_side}, {book_side}, but 60-second flow volume is insufficient."
    return f"WAIT: {flow_side}, {book_side}, no flow/book confirmation."


def get_signal(flow: dict[str, Any], book: dict[str, Any], min_flow: float, min_book: float) -> str:
    delta_fraction = float(flow["delta_fraction"])
    imbalance = float(book["imbalance"])
    if delta_fraction >= min_flow and imbalance >= min_book:
        return "LONG"
    if delta_fraction <= -min_flow and imbalance <= -min_book:
        return "SHORT"
    return "FLAT"


def detect_market_alerts(
    previous: dict[str, Any],
    signal: str,
    flow_delta: float,
    stale_data: bool,
    zone_distances_bps: list[float],
    proximity_bps: float,
) -> tuple[list[str], dict[str, Any]]:
    flow_side = "buy" if flow_delta > 0 else "sell" if flow_delta < 0 else None
    alerts = []
    if stale_data and not previous.get("stale", False):
        alerts.append("Stale market data: paper signals are disabled.")
    if flow_side and previous.get("flow_side") and flow_side != previous["flow_side"]:
        alerts.append(f"Aggressor flow flipped to {flow_side} dominance.")
    if signal in ("LONG", "SHORT") and signal != previous.get("signal"):
        alerts.append(f"New {signal} paper signal fired.")
    near_zone = any(distance <= proximity_bps for distance in zone_distances_bps)
    if near_zone and not previous.get("near_zone", False):
        alerts.append("Price is approaching a thin-liquidity zone.")
    next_state = {
        "stale": stale_data,
        "flow_side": flow_side,
        "signal": signal if signal in ("LONG", "SHORT") else None,
        "near_zone": near_zone,
    }
    return alerts, next_state


def calculate_conviction(
    signal: str,
    flow: dict[str, Any],
    book: dict[str, Any],
    forecast_direction: str,
    forecast_up: float,
    footprint: dict[str, Any],
    *,
    min_flow: float,
    min_book: float,
    flow_ok: bool,
    stale_data: bool,
) -> dict[str, Any]:
    """Score how much the current inputs agree, on a 0-100 scale.

    Each component is a plain ratio of how far the reading is from neutral
    against the threshold the user configured, so the score moves when the
    thresholds move. It is a summary of agreement, not a probability of profit.
    """
    if stale_data or signal not in ("LONG", "SHORT") or not flow_ok:
        return {"score": 0, "band": "No actionable setup", "components": {}}

    flow_strength = min(abs(float(flow["delta_fraction"])) / max(min_flow, 1e-6), 2.0) / 2.0
    book_strength = min(abs(float(book["imbalance"])) / max(min_book, 1e-6), 2.0) / 2.0
    participation = min(int(flow["trade_count"]) / 20.0, 1.0)
    model_conviction = min(abs(float(forecast_up) - 0.5) * 4.0, 1.0)
    agrees = forecast_direction.startswith("UP") if signal == "LONG" else forecast_direction.startswith("DOWN")
    model_component = model_conviction if agrees else model_conviction * 0.25
    footprint_component = float(footprint.get("confidence", 0.0)) if footprint.get("active") else 0.0

    components = {
        "Flow dominance": round(flow_strength, 3),
        "Book imbalance": round(book_strength, 3),
        "Participation": round(participation, 3),
        "Model agreement": round(model_component, 3),
        "Footprint confidence": round(footprint_component, 3),
    }
    weights = {
        "Flow dominance": 0.30,
        "Book imbalance": 0.20,
        "Participation": 0.15,
        "Model agreement": 0.25,
        "Footprint confidence": 0.10,
    }
    score = int(round(100 * sum(components[key] * weights[key] for key in components)))
    if score >= 75:
        band = "Strong · all inputs agree"
    elif score >= 55:
        band = "Moderate · most inputs agree"
    elif score >= 35:
        band = "Weak · thin agreement"
    else:
        band = "Very weak · marginal agreement"
    return {"score": score, "band": band, "components": components}


def interpret_price_open_interest(price_change_pct: float, oi_change_pct: float) -> str:
    return interpret_price_open_interest_impl(price_change_pct, oi_change_pct)


__all__ = [
    "open_position", "close_position", "hit_stop_or_target", "daily_trade_rows",
    "is_risk_locked", "describe_lock_reasons", "forecast_allows_side", "resolve_trade_setup",
    "describe_decision_confidence", "describe_decision_reason", "get_signal",
    "detect_market_alerts", "calculate_conviction",
]
