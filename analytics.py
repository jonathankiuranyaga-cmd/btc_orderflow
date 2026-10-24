"""Pure analytics for order flow, the order book, and closed trades.

Every function here is deterministic and free of Streamlit/network/SQLite
dependencies, which is what makes it unit-testable in isolation. The app layer
composes these; nothing in this module composes the app layer.
"""
from __future__ import annotations

from datetime import datetime, timezone
from math import floor, sqrt
from statistics import median
from typing import Any

from config import MARKET_FEEDS, STALE_AFTER_MS, TRADE_RETENTION_SECONDS, TRADE_WINDOW_SECONDS


# ---------------------------------------------------------------------------
# trade / flow analytics
# ---------------------------------------------------------------------------

def normalize_agg_trade(row: dict[str, Any]) -> dict[str, Any]:
    """Normalize a Binance aggTrade; m=True means the seller was aggressor."""
    return {
        "id": int(row["a"]),
        "time": int(row["T"]) / 1000,
        "price": float(row["p"]),
        "quantity": float(row["q"]),
        "sell_aggressor": bool(row["m"]),
    }


def count_missed_trades(previous_last_id: int | None, rows: list[dict[str, Any]]) -> int:
    """Aggregate-trade ids are sequential, so a jump in ids means trades were skipped.

    Binance caps /fapi/v1/aggTrades at 1000 rows. If more than 1000 trades print
    between two polls, the oldest rows fall off the response and the 60-second
    flow window silently under-counts aggressor volume.
    """
    if previous_last_id is None or not rows:
        return 0
    oldest_id = min(int(row["a"]) for row in rows)
    return max(0, oldest_id - previous_last_id - 1)


def summarize_flow(trades: list[dict[str, Any]], now: float | None = None) -> dict[str, Any]:
    if now is None:
        now = datetime.now(timezone.utc).timestamp()
    window_start = now - TRADE_WINDOW_SECONDS
    recent = [
        trade for trade in trades
        if window_start <= float(trade["time"]) <= now
    ]
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
        "intervals": summarize_flow_intervals(recent, now),
    }


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
        quantity = float(trade.get("quantity", 0.0))
        delta = -quantity if trade.get("sell_aggressor", False) else quantity
        candle = candles.get(bucket_start)
        if candle is None:
            candles[bucket_start] = {
                "timestamp": bucket_start,
                "Open": price,
                "High": price,
                "Low": price,
                "Close": price,
                "Trades": 1,
                "Volume (BTC)": quantity,
                "Delta (BTC)": delta,
            }
        else:
            candle["High"] = max(candle["High"], price)
            candle["Low"] = min(candle["Low"], price)
            candle["Close"] = price
            candle["Trades"] += 1
            candle["Volume (BTC)"] += quantity
            candle["Delta (BTC)"] += delta

    result = []
    cumulative_delta = 0.0
    for candle in candles.values():
        candle["UTC time"] = datetime.fromtimestamp(candle.pop("timestamp"), timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        candle["Direction"] = "Up" if candle["Close"] >= candle["Open"] else "Down"
        candle["Color"] = candle["Direction"]
        cumulative_delta += candle["Delta (BTC)"]
        candle["CVD (BTC)"] = cumulative_delta
        result.append(candle)
    return result


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


def summarize_liquidations(events: list[dict[str, Any]], now: float, window_seconds: int = 60) -> dict[str, float | int]:
    recent = [event for event in events if now - window_seconds <= float(event["time"]) <= now]
    long_notional = sum(float(event["notional"]) for event in recent if event["liquidated"] == "longs")
    short_notional = sum(float(event["notional"]) for event in recent if event["liquidated"] == "shorts")
    return {
        "long_usdt": long_notional,
        "short_usdt": short_notional,
        "total_usdt": long_notional + short_notional,
        "count": len(recent),
    }


# ---------------------------------------------------------------------------
# book analytics
# ---------------------------------------------------------------------------

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


def record_book_depth_sample(
    history: list[dict[str, float]],
    book: dict[str, Any],
    now: float,
    retention_seconds: float = TRADE_RETENTION_SECONDS,
) -> list[dict[str, float]]:
    """Append the current resting size so depletion can be measured later.

    The footprint engine compares the passive side of the aggression across the
    detection window. That needs a history of snapshots, not just the latest
    one, so every poll contributes a sample.
    """
    sample = {
        "time": now,
        "bid_qty": float(book.get("bid_qty", 0.0)),
        "ask_qty": float(book.get("ask_qty", 0.0)),
        "mid": float(book.get("mid", 0.0)),
    }
    updated = [entry for entry in history if float(entry.get("time", 0.0)) >= now - retention_seconds]
    updated.append(sample)
    return updated


def summarize_depth_curve(book: dict[str, Any]) -> list[dict[str, float | str]]:
    mid = float(book.get("mid", 0.0))
    if mid <= 0:
        return []
    curve = []
    for side, levels in (("Bid", book.get("bids", [])), ("Ask", book.get("asks", []))):
        cumulative_qty = 0.0
        sorted_levels = sorted(levels, key=lambda item: abs(float(item[0]) / mid - 1))
        for price, quantity in sorted_levels:
            cumulative_qty += float(quantity)
            curve.append(
                {
                    "Side": side,
                    "Distance from mid (bps)": (float(price) / mid - 1) * 10_000,
                    "Cumulative size (BTC)": cumulative_qty,
                }
            )
    return curve


def find_jump_zones(
    book: dict[str, Any], reference_price: float, band_bps: float = 1.0,
    max_distance_bps: float = 12.0, thin_ratio: float = 0.5,
) -> dict[str, dict[str, Any]]:
    zones: dict[str, dict[str, Any]] = {}
    if not reference_price or reference_price <= 0:
        empty = {"zone": None, "coverage_bps": 0.0, "bands_checked": 0}
        return {"up": dict(empty), "down": dict(empty)}
    for side, rows in (("up", book.get("asks") or []), ("down", book.get("bids") or [])):
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


def calculate_microprice(book: dict[str, Any]) -> float | None:
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    if not bids or not asks:
        return None
    bid_price, bid_qty = bids[0]
    ask_price, ask_qty = asks[0]
    total_qty = float(bid_qty) + float(ask_qty)
    if total_qty <= 0:
        return None
    return (float(ask_price) * float(bid_qty) + float(bid_price) * float(ask_qty)) / total_qty


# ---------------------------------------------------------------------------
# market context
# ---------------------------------------------------------------------------

def interpret_price_open_interest(price_change_pct: float, oi_change_pct: float) -> str:
    if price_change_pct > 0 and oi_change_pct > 0:
        return "Price up + OI up · new longs likely"
    if price_change_pct < 0 and oi_change_pct > 0:
        return "Price down + OI up · new shorts likely"
    if price_change_pct > 0 and oi_change_pct < 0:
        return "Price up + OI down · short covering / squeeze likely"
    if price_change_pct < 0 and oi_change_pct < 0:
        return "Price down + OI down · long liquidation / position reduction likely"
    return "Mixed or unchanged · no clear price/OI read"


def append_open_interest_sample(
    history: list[dict[str, float]], current_oi: float, now: float, lookback_seconds: int = 60
) -> tuple[list[dict[str, float]], float | None]:
    prior = next(
        (sample for sample in reversed(history) if sample["time"] <= now - lookback_seconds),
        None,
    )
    updated = list(history)
    if not updated or updated[-1]["time"] < now:
        updated.append({"time": now, "open_interest": current_oi})
    updated = updated[-300:]
    if prior is None or prior["open_interest"] == 0:
        return updated, None
    return updated, (current_oi / prior["open_interest"] - 1) * 100


def calculate_market_levels(
    trades: list[dict[str, Any]], daily_klines: list[list[Any]], mark: float
) -> dict[str, float | None]:
    rolling_qty = sum(float(trade["quantity"]) for trade in trades)
    rolling_vwap = (
        sum(float(trade["price"]) * float(trade["quantity"]) for trade in trades) / rolling_qty
        if rolling_qty > 0
        else None
    )
    current_day = daily_klines[-1] if daily_klines else None
    previous_day = daily_klines[-2] if len(daily_klines) > 1 else None
    return {
        "rolling_vwap": rolling_vwap,
        "session_high": float(current_day[2]) if current_day and len(current_day) > 3 else None,
        "session_low": float(current_day[3]) if current_day and len(current_day) > 3 else None,
        "previous_day_high": float(previous_day[2]) if previous_day and len(previous_day) > 3 else None,
        "previous_day_low": float(previous_day[3]) if previous_day and len(previous_day) > 3 else None,
        "round_500": floor(mark / 500 + 0.5) * 500 if mark > 0 else None,
        "round_1000": floor(mark / 1000 + 0.5) * 1000 if mark > 0 else None,
    }


def calculate_venue_lead_lag(history: list[dict[str, float]], max_lag: int = 3) -> dict[str, Any]:
    if len(history) < max_lag + 4:
        return {"leader": "Warming up", "lag_seconds": None, "correlation": None}
    spot_returns = [history[index]["spot"] / history[index - 1]["spot"] - 1 for index in range(1, len(history))]
    perp_returns = [history[index]["perp"] / history[index - 1]["perp"] - 1 for index in range(1, len(history))]
    time_steps = [history[index]["time"] - history[index - 1]["time"] for index in range(1, len(history))]
    sample_period = median(time_steps) if time_steps else 0.0
    candidates = []
    for lag in range(-max_lag, max_lag + 1):
        if lag > 0:
            spot_slice, perp_slice = spot_returns[:-lag], perp_returns[lag:]
        elif lag < 0:
            spot_slice, perp_slice = spot_returns[-lag:], perp_returns[:lag]
        else:
            spot_slice, perp_slice = spot_returns, perp_returns
        if len(spot_slice) < 3:
            continue
        spot_mean = sum(spot_slice) / len(spot_slice)
        perp_mean = sum(perp_slice) / len(perp_slice)
        numerator = sum((spot - spot_mean) * (perp - perp_mean) for spot, perp in zip(spot_slice, perp_slice))
        spot_ss = sum((spot - spot_mean) ** 2 for spot in spot_slice)
        perp_ss = sum((perp - perp_mean) ** 2 for perp in perp_slice)
        denominator = (spot_ss * perp_ss) ** 0.5
        if denominator > 0:
            candidates.append((numerator / denominator, lag))
    if not candidates:
        return {"leader": "No clear lead", "lag_seconds": None, "correlation": None}
    correlation, lag = max(candidates, key=lambda item: item[0])
    if correlation < 0.2 or lag == 0:
        leader = "No clear lead"
    else:
        leader = "Coinbase spot" if lag > 0 else "Binance perp"
    return {
        "leader": leader,
        "lag_seconds": abs(lag) * sample_period if leader != "No clear lead" else None,
        "correlation": correlation,
    }


def assess_feed_ages(
    last_trade_time: float | None, last_book_update: float | None, now: float, stale_after_ms: int | None = None
) -> dict[str, Any]:
    if stale_after_ms is None:
        stale_after_ms = STALE_AFTER_MS
    trade_age_ms = max(0.0, (now - last_trade_time) * 1000) if last_trade_time is not None else None
    book_age_ms = max(0.0, (now - last_book_update) * 1000) if last_book_update is not None else None
    stale_reasons = []
    if trade_age_ms is None or trade_age_ms > stale_after_ms:
        stale_reasons.append("Binance trades stale")
    if book_age_ms is None or book_age_ms > stale_after_ms:
        stale_reasons.append("Binance book stale")
    return {
        "trade_age_ms": trade_age_ms,
        "book_age_ms": book_age_ms,
        "stale_after_ms": stale_after_ms,
        "stale_reasons": stale_reasons,
        "stale": bool(stale_reasons),
    }


def update_feed_state(
    feed_health: dict[str, dict[str, Any]],
    market_cache: dict[str, Any],
    results: dict[str, Any],
    errors: dict[str, str],
    now: float,
) -> None:
    for name in MARKET_FEEDS:
        state = feed_health[name]
        if name in results:
            if state["ever_connected"] and not state["connected"]:
                state["reconnect_count"] += 1
            state["connected"] = True
            state["ever_connected"] = True
            state["last_success"] = now
            state["last_error"] = None
            market_cache[name] = results[name]
        else:
            state["connected"] = False
            state["last_error"] = errors.get(name, "No response")


# ---------------------------------------------------------------------------
# trade / risk statistics
# ---------------------------------------------------------------------------

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


def calculate_trade_statistics(trades: list[dict[str, Any]]) -> dict[str, float | int | None]:
    """Risk-adjusted statistics for the closed paper trades.

    Trades are stored newest first, so the series is reversed to chronological
    order before any streak or sequence statistic is computed. The Sharpe and
    Sortino figures are per-trade and unannualised: a scalping sample of a few
    dozen trades cannot support an annualised estimate, so they are reported as
    a plain mean-over-deviation ratio instead.
    """
    stats: dict[str, float | int | None] = {
        "sharpe_like": None,
        "sortino_like": None,
        "expectancy_r": None,
        "avg_hold_seconds": None,
        "total_fees": 0.0,
        "max_consecutive_wins": 0,
        "max_consecutive_losses": 0,
        "largest_win": None,
        "largest_loss": None,
        "gross_profit": 0.0,
        "gross_loss": 0.0,
        "avg_win": None,
        "avg_loss": None,
    }
    if not trades:
        return stats

    ordered = list(reversed(trades))
    pnls = [float(trade.get("Net PnL (USDT)", 0.0)) for trade in ordered]
    rs = [float(trade["R"]) for trade in ordered if trade.get("R") is not None]
    holds = [float(trade["Hold (s)"]) for trade in ordered if trade.get("Hold (s)") is not None]
    fees = [float(trade["Fees (USDT)"]) for trade in ordered if trade.get("Fees (USDT)") is not None]

    wins = [pnl for pnl in pnls if pnl > 0]
    losses = [pnl for pnl in pnls if pnl < 0]
    stats["gross_profit"] = sum(wins)
    stats["gross_loss"] = -sum(losses)
    stats["total_fees"] = sum(fees)
    stats["largest_win"] = max(pnls)
    stats["largest_loss"] = min(pnls)
    if wins:
        stats["avg_win"] = sum(wins) / len(wins)
    if losses:
        stats["avg_loss"] = sum(losses) / len(losses)
    if holds:
        stats["avg_hold_seconds"] = sum(holds) / len(holds)

    if rs:
        mean_r = sum(rs) / len(rs)
        stats["expectancy_r"] = mean_r
        if len(rs) > 1:
            variance = sum((value - mean_r) ** 2 for value in rs) / (len(rs) - 1)
            std_r = sqrt(variance)
            if std_r > 0:
                stats["sharpe_like"] = mean_r / std_r
            downside = [value for value in rs if value < 0]
            if downside:
                # Standard downside deviation: the dispersion of returns below the
                # target, with gains contributing zero, over every observation.
                downside_deviation = sqrt(
                    sum(min(0.0, value) ** 2 for value in rs) / len(rs)
                )
                if downside_deviation > 0:
                    stats["sortino_like"] = mean_r / downside_deviation

    win_streak = 0
    loss_streak = 0
    for pnl in pnls:
        if pnl > 0:
            win_streak += 1
            loss_streak = 0
        elif pnl < 0:
            loss_streak += 1
            win_streak = 0
        else:
            win_streak = 0
            loss_streak = 0
        stats["max_consecutive_wins"] = max(int(stats["max_consecutive_wins"]), win_streak)
        stats["max_consecutive_losses"] = max(int(stats["max_consecutive_losses"]), loss_streak)
    return stats


def calculate_fee_adjusted_trade_metrics(
    stop_bps: float, reward_risk: float, fee_bps: float, slippage_bps: float
) -> dict[str, float]:
    round_trip_cost_bps = 2 * (fee_bps + slippage_bps)
    net_loss_bps = stop_bps + round_trip_cost_bps
    net_win_bps = max(0.0, stop_bps * reward_risk - round_trip_cost_bps)
    return {
        "reward_risk": net_win_bps / net_loss_bps if net_loss_bps > 0 else 0.0,
        "breakeven_win_rate": net_loss_bps / (net_loss_bps + net_win_bps) if net_win_bps > 0 else 1.0,
        "round_trip_cost_bps": round_trip_cost_bps,
    }


def calculate_unrealized(position: dict[str, Any], mark: float, fee_bps: float, slippage_bps: float) -> float:
    side = int(position["side"])
    exit_fill = mark * (1 - side * slippage_bps / 10_000)
    exit_fee = exit_fill * position["quantity"] * fee_bps / 10_000
    return side * (exit_fill - position["entry"]) * position["quantity"] - position["entry_fee"] - exit_fee


__all__ = [
    "normalize_agg_trade", "count_missed_trades", "summarize_flow", "summarize_flow_intervals",
    "aggregate_trade_candles", "describe_aggressor_strength", "absorption_heuristic",
    "summarize_liquidations", "summarize_book", "record_book_depth_sample", "summarize_depth_curve",
    "find_jump_zones", "calculate_microprice", "interpret_price_open_interest",
    "append_open_interest_sample", "calculate_market_levels", "calculate_venue_lead_lag",
    "assess_feed_ages", "update_feed_state", "calculate_consecutive_losses", "calculate_drawdown_pct",
    "calculate_trade_statistics", "calculate_fee_adjusted_trade_metrics", "calculate_unrealized",
    "build_why_inputs",
]


def build_why_inputs(
    flow: dict[str, Any],
    book: dict[str, Any],
    microprice: float | None,
    basis_bps: float | None,
    forecast_direction: str,
    mid: float,
    lead_lag: dict[str, Any] | None = None,
    price_change_pct: float = 0.0,
    oi_change_pct: float | None = None,
    funding_rate: float | None = None,
    liquidation_totals: dict[str, Any] | None = None,
) -> dict[str, Any]:
    flow_fraction = float(flow["delta_fraction"])
    book_imbalance = float(book["imbalance"])
    if flow_fraction >= 0.05:
        flow_bias = "Bullish"
    elif flow_fraction <= -0.05:
        flow_bias = "Bearish"
    else:
        flow_bias = "Neutral"
    if book_imbalance >= 0.05:
        book_bias = "Bullish"
    elif book_imbalance <= -0.05:
        book_bias = "Bearish"
    else:
        book_bias = "Neutral"
    microprice_bps = ((microprice / mid) - 1) * 10_000 if microprice is not None and mid > 0 else None
    if microprice_bps is None or abs(microprice_bps) < 0.1:
        microprice_bias = "Neutral"
        microprice_reading = "Unavailable" if microprice_bps is None else f"{microprice_bps:+.2f} bps from mid"
    elif microprice_bps > 0:
        microprice_bias = "Bullish"
        microprice_reading = f"{microprice_bps:+.2f} bps from mid"
    else:
        microprice_bias = "Bearish"
        microprice_reading = f"{microprice_bps:+.2f} bps from mid"
    if basis_bps is None:
        basis_bias, basis_reading = "Neutral", "Unavailable"
    elif basis_bps > 1.0:
        basis_bias, basis_reading = "Bullish", f"Perp premium {basis_bps:+.1f} bps"
    elif basis_bps < -1.0:
        basis_bias, basis_reading = "Bearish", f"Perp discount {basis_bps:+.1f} bps"
    else:
        basis_bias, basis_reading = "Neutral", f"Near parity ({basis_bps:+.1f} bps)"
    forecast_bias = "Bullish" if forecast_direction.startswith("UP") else "Bearish" if forecast_direction.startswith("DOWN") else "Neutral"
    if oi_change_pct is None:
        oi_bias, oi_reading = "Neutral", "Warming up"
    else:
        oi_bias = "Bullish" if price_change_pct > 0 else "Bearish" if price_change_pct < 0 else "Neutral"
        oi_reading = f"{interpret_price_open_interest(price_change_pct, oi_change_pct)} ({oi_change_pct:+.3f}% OI)"
    if funding_rate is None:
        funding_bias, funding_reading = "Neutral", "Unavailable"
    elif funding_rate > 0:
        funding_bias, funding_reading = "Bearish", f"Positive · longs pay shorts ({funding_rate * 100:+.4f}%)"
    elif funding_rate < 0:
        funding_bias, funding_reading = "Bullish", f"Negative · shorts pay longs ({funding_rate * 100:+.4f}%)"
    else:
        funding_bias, funding_reading = "Neutral", "Near zero"
    if liquidation_totals is None or float(liquidation_totals.get("total_usdt", 0.0)) <= 0:
        liquidation_bias, liquidation_reading = "Neutral", "No forced orders in the last 60s"
    else:
        long_liquidations = float(liquidation_totals["long_usdt"])
        short_liquidations = float(liquidation_totals["short_usdt"])
        liquidation_bias = (
            "Bearish" if long_liquidations > short_liquidations
            else "Bullish" if short_liquidations > long_liquidations
            else "Neutral"
        )
        liquidation_reading = f"Longs {long_liquidations:,.0f} · shorts {short_liquidations:,.0f} USDT"
    rows = [
        {"Input": "Aggressor flow", "Reading": f"Delta {flow_fraction:+.1%}", "Bias": flow_bias},
        {"Input": "Top-N book imbalance", "Reading": f"{book_imbalance:+.1%}", "Bias": book_bias},
        {"Input": "Microprice vs mid", "Reading": microprice_reading, "Bias": microprice_bias},
        {"Input": "USDT-normalized basis", "Reading": basis_reading, "Bias": basis_bias},
        {"Input": "Candle model", "Reading": forecast_direction, "Bias": forecast_bias},
        {"Input": "Price vs open interest", "Reading": oi_reading, "Bias": oi_bias},
        {"Input": "Funding rate", "Reading": funding_reading, "Bias": funding_bias},
        {
            "Input": "Coinbase/Binance lead-lag",
            "Reading": (
                f"{lead_lag['leader']} · {lead_lag['lag_seconds']:.1f}s · r={lead_lag['correlation']:.2f}"
                if lead_lag and lead_lag["lag_seconds"] is not None
                else (lead_lag["leader"] if lead_lag else "Unavailable")
            ),
            "Bias": "Neutral",
        },
        {"Input": "Liquidation pressure", "Reading": liquidation_reading, "Bias": liquidation_bias},
    ]
    directional_biases = {row["Bias"] for row in rows if row["Bias"] in ("Bullish", "Bearish")}
    return {"rows": rows, "conflict": len(directional_biases) > 1}
