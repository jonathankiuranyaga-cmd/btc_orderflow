"""Institutional-footprint detection from Binance aggressor trade flow.

The one-minute flow in the dashboard is dominated by noise. A genuine
institutional footprint looks different: large volume, pushed in one direction,
sustained for minutes rather than seconds, and it leaves a measurable imprint
on the book.

Two execution styles are distinguished:

* **Sweep** - the aggressor lifts or hits offers/bids hard enough to move
  price. Price impact per unit of imbalance is high. Trend continuation.
* **Absorption** - a large passive order sits in front of the aggression and
  absorbs it. Volume and imbalance are high, yet price refuses to move. The
  passive side is the informed participant and usually wins.

Everything here is a pure function of the trade buffer, so it can be tested
without a Streamlit runtime.
"""

from __future__ import annotations

from statistics import median, pstdev
from typing import Any

DEFAULT_CONFIG: dict[str, Any] = {
    "window_seconds": 150,
    "bucket_seconds": 15,
    "baseline_lookback_seconds": 900,
    "min_volume_ratio": 1.8,
    "min_z_score": 1.5,
    "min_delta_fraction": 0.25,
    "min_persistence": 0.6,
    "min_notional": 150_000.0,
    "max_impact_per_imbalance": 25.0,
    "max_absorption_move_bps": 4.0,
    "price_bucket_usd": 10.0,
    "max_price_levels": 12,
}

PATTERN_NONE = "NONE"
PATTERN_SWEEP = "SWEEP"
PATTERN_ABSORPTION = "ABSORPTION"
PATTERN_UNCONFIRMED = "UNCONFIRMED"


def _empty_bucket(bucket_start: float) -> dict[str, Any]:
    return {
        "bucket_start": bucket_start,
        "buy_btc": 0.0,
        "sell_btc": 0.0,
        "delta_btc": 0.0,
        "total_btc": 0.0,
        "notional": 0.0,
        "trades": 0,
        "open": 0.0,
        "high": 0.0,
        "low": 0.0,
        "close": 0.0,
    }


def bucket_flow(
    trades: list[dict[str, Any]],
    now: float,
    bucket_seconds: int,
    window_seconds: int,
) -> list[dict[str, Any]]:
    """Aggregate aggressor flow into fixed time buckets, oldest bucket first.

    Bins with no trades are omitted: the caller decides whether an empty bin
    is meaningful (the baseline treats it as zero volume, the detection window
    treats it as a gap in the aggression).
    """
    if bucket_seconds <= 0 or window_seconds <= 0:
        return []
    window_start = now - window_seconds
    buckets: dict[float, dict[str, Any]] = {}
    for trade in trades:
        trade_time = float(trade["time"])
        if trade_time < window_start or trade_time > now:
            continue
        price = float(trade["price"])
        quantity = float(trade.get("quantity", 0.0))
        bucket_start = int(trade_time // bucket_seconds) * bucket_seconds
        bucket = buckets.get(bucket_start)
        if bucket is None:
            bucket = _empty_bucket(bucket_start)
            buckets[bucket_start] = bucket
            bucket["open"] = price
            bucket["high"] = price
            bucket["low"] = price
            bucket["close"] = price
        bucket["high"] = max(bucket["high"], price)
        bucket["low"] = min(bucket["low"], price)
        bucket["close"] = price
        bucket["trades"] += 1
        bucket["total_btc"] += quantity
        bucket["notional"] += price * quantity
        if trade.get("sell_aggressor", False):
            bucket["sell_btc"] += quantity
        else:
            bucket["buy_btc"] += quantity
    for bucket in buckets.values():
        bucket["delta_btc"] = bucket["buy_btc"] - bucket["sell_btc"]
        bucket["imbalance"] = (
            bucket["delta_btc"] / bucket["total_btc"] if bucket["total_btc"] else 0.0
        )
    return [buckets[key] for key in sorted(buckets)]


def baseline_volume(
    trades: list[dict[str, Any]],
    now: float,
    bucket_seconds: int,
    window_seconds: int,
    lookback_seconds: int,
) -> dict[str, float]:
    """Per-bucket volume and price-dynamics statistics from the region before
    the detection window.

    Quiet bins count as zero volume, otherwise a bursty tape would look calm
    simply because the busy minutes dominate the sample. The median absolute
    bucket-to-bucket move gives the reference for how far price normally
    travels in this window, which is what separates a sweep from absorption.
    """
    empty = {
        "median": 0.0,
        "mean": 0.0,
        "stdev": 0.0,
        "bins": 0.0,
        "quiet_bin": 0.0,
        "move_median_bps": 0.0,
    }
    if bucket_seconds <= 0 or lookback_seconds <= 0:
        return empty
    window_start = now - window_seconds
    baseline_start = now - lookback_seconds
    volumes: dict[float, float] = {}
    closes: dict[float, float] = {}
    for trade in trades:
        trade_time = float(trade["time"])
        if trade_time < baseline_start or trade_time >= window_start:
            continue
        bucket_start = int(trade_time // bucket_seconds) * bucket_seconds
        volumes[bucket_start] = volumes.get(bucket_start, 0.0) + float(trade.get("quantity", 0.0))
        closes[bucket_start] = float(trade["price"])

    # The lookup grid has to follow the same bucket boundaries the volumes were
    # bucketed on, otherwise every populated bin is missed and the baseline
    # reads as a flat zero.
    first_bin = int(baseline_start // bucket_seconds) * bucket_seconds
    last_bin = int(window_start // bucket_seconds) * bucket_seconds
    bin_count = int((last_bin - first_bin) // bucket_seconds) + 1
    if bin_count <= 0:
        return empty
    samples = [volumes.get(first_bin + index * bucket_seconds, 0.0) for index in range(bin_count)]
    positive = [value for value in samples if value > 0]

    moves: list[float] = []
    previous_close: float | None = None
    for index in range(bin_count):
        bucket_start = first_bin + index * bucket_seconds
        close = closes.get(bucket_start)
        if close is None:
            continue
        if previous_close:
            moves.append(abs(close / previous_close - 1) * 10_000)
        previous_close = close

    return {
        "median": median(samples) if samples else 0.0,
        "mean": (sum(samples) / len(samples)) if samples else 0.0,
        "stdev": pstdev(samples) if len(samples) > 1 else 0.0,
        "bins": float(bin_count),
        "quiet_bin": median(positive) if positive else 0.0,
        "move_median_bps": median(moves) if moves else 0.0,
    }


def price_level_footprint(
    trades: list[dict[str, Any]],
    now: float,
    window_seconds: int,
    price_bucket_usd: float,
    max_levels: int = 12,
) -> list[dict[str, Any]]:
    """Per-price-level buy and sell volume: the footprint of who paid up.

    A single price level absorbing a large one-sided imbalance is the clearest
    trace of a large resting order. Levels are ranked by the notional that
    changed hands on the unbalanced side, not by raw size.
    """
    if price_bucket_usd <= 0 or window_seconds <= 0:
        return []
    window_start = now - window_seconds
    levels: dict[float, dict[str, Any]] = {}
    for trade in trades:
        trade_time = float(trade["time"])
        if trade_time < window_start or trade_time > now:
            continue
        price = float(trade["price"])
        quantity = float(trade.get("quantity", 0.0))
        level_price = round(price / price_bucket_usd) * price_bucket_usd
        level = levels.get(level_price)
        if level is None:
            level = {
                "price": level_price,
                "buy_btc": 0.0,
                "sell_btc": 0.0,
                "total_btc": 0.0,
                "notional": 0.0,
                "trades": 0,
            }
            levels[level_price] = level
        level["total_btc"] += quantity
        level["notional"] += price * quantity
        level["trades"] += 1
        if trade.get("sell_aggressor", False):
            level["sell_btc"] += quantity
        else:
            level["buy_btc"] += quantity

    rows: list[dict[str, Any]] = []
    for level in levels.values():
        delta = level["buy_btc"] - level["sell_btc"]
        level["delta_btc"] = delta
        level["imbalance"] = delta / level["total_btc"] if level["total_btc"] else 0.0
        # Notional resting on the side that was NOT being hit, i.e. the size that
        # actually changed hands against the passive order.
        passive_notional = (
            level["sell_btc"] * level["price"] if delta > 0 else level["buy_btc"] * level["price"]
        )
        level["passive_notional"] = passive_notional
        level["unbalanced_notional"] = abs(delta) * level["price"]
        rows.append(level)
    rows.sort(key=lambda row: (row["unbalanced_notional"], row["total_btc"]), reverse=True)
    return rows[:max_levels]


def detect_book_depletion(
    history: list[dict[str, float]],
    now: float,
    window_seconds: int,
    direction: str,
) -> dict[str, Any] | None:
    """Change in resting size on the passive side of the aggression.

    Aggressive buyers hit offers, so the ask side is the passive side. If that
    side thins while price refuses to fall, the size was absorbed and the
    resting order was lifted. If it thickens instead, a new passive order is
    being quoted into the aggression.
    """
    if len(history) < 2:
        return None
    passive_key = "ask_qty" if direction == "BUY" else "bid_qty"
    target_time = now - window_seconds
    samples = [sample for sample in history if sample.get("time", 0.0) <= target_time]
    if not samples:
        samples = [history[0]]
    prior = samples[-1]
    latest = history[-1]
    prior_size = float(prior.get(passive_key, 0.0))
    latest_size = float(latest.get(passive_key, 0.0))
    if prior_size <= 0:
        return None
    change_pct = (latest_size / prior_size - 1) * 100
    return {
        "passive_side": "asks" if direction == "BUY" else "bids",
        "prior_size": prior_size,
        "latest_size": latest_size,
        "change_pct": change_pct,
        "depleted": change_pct < -10.0,
        "replenished": change_pct > 10.0,
        "elapsed_seconds": float(latest.get("time", now)) - float(prior.get("time", now)),
    }


def detect_large_aggressor(
    trades: list[dict[str, Any]],
    now: float,
    config: dict[str, Any] | None = None,
    book_depth_history: list[dict[str, float]] | None = None,
    feed_gap: bool = False,
) -> dict[str, Any]:
    """Detect a sustained multi-minute institutional footprint.

    Returns a dict describing the footprint, the individual factors that fired,
    and the reasoning behind the classification. The detector is deliberately
    conservative: it needs volume, persistence, notional and direction to agree
    before it reports anything.
    """
    settings = dict(DEFAULT_CONFIG)
    if config:
        settings.update(config)

    window_seconds = int(settings["window_seconds"])
    bucket_seconds = int(settings["bucket_seconds"])
    buckets = bucket_flow(trades, now, bucket_seconds, window_seconds)
    baseline = baseline_volume(
        trades,
        now,
        bucket_seconds,
        window_seconds,
        int(settings["baseline_lookback_seconds"]),
    )
    levels = price_level_footprint(
        trades,
        now,
        window_seconds,
        float(settings["price_bucket_usd"]),
        int(settings["max_price_levels"]),
    )

    total_btc = sum(bucket["total_btc"] for bucket in buckets)
    buy_btc = sum(bucket["buy_btc"] for bucket in buckets)
    sell_btc = sum(bucket["sell_btc"] for bucket in buckets)
    notional = sum(bucket["notional"] for bucket in buckets)
    delta_btc = buy_btc - sell_btc
    delta_fraction = delta_btc / total_btc if total_btc else 0.0

    prices = [float(trade["price"]) for trade in trades if float(trade["time"]) >= now - window_seconds]
    price_move_bps = (
        (prices[-1] / prices[0] - 1) * 10_000 if len(prices) >= 2 and prices[0] else 0.0
    )

    observed_bin_count = max(1, len(buckets))
    window_bin_volume = total_btc / observed_bin_count
    # Fall back to the median of active bins when the tape is sparse, so the
    # comparison stays meaningful instead of dividing by a zero median.
    reference = baseline["median"] if baseline["median"] > 0 else baseline["quiet_bin"]
    volume_ratio = window_bin_volume / reference if reference > 0 else 0.0
    z_score = (
        (window_bin_volume - baseline["mean"]) / baseline["stdev"]
        if baseline["stdev"] > 0
        else 0.0
    )

    direction = "BUY" if delta_btc > 0 else "SELL" if delta_btc < 0 else ""
    dominant_sign = 1 if delta_btc > 0 else -1
    signed_buckets = [bucket["delta_btc"] for bucket in buckets if bucket["total_btc"] > 0]
    persistence = (
        sum(1 for value in signed_buckets if (value > 0) == (dominant_sign > 0)) / len(signed_buckets)
        if signed_buckets
        else 0.0
    )

    aggression_btc = max(buy_btc, sell_btc)
    aggression_notional = 0.0
    for bucket in buckets:
        aggression_notional += (bucket["buy_btc"] if dominant_sign > 0 else bucket["sell_btc"]) * bucket["close"]

    impact_per_imbalance = (
        abs(price_move_bps) / (abs(delta_fraction) * 100) if delta_fraction else 0.0
    )

    # How far price normally travels over a window this long on a tape this
    # quiet. A sweep drags price further than that; absorption leaves it short.
    expected_move_bps = baseline["move_median_bps"] * (len(buckets) ** 0.5)
    response_threshold_bps = max(
        float(settings["max_absorption_move_bps"]), expected_move_bps * 0.6
    )
    response_ratio = (
        abs(price_move_bps) / response_threshold_bps if response_threshold_bps > 0 else 0.0
    )
    price_absorbed = abs(price_move_bps) < response_threshold_bps

    book_depletion = (
        detect_book_depletion(book_depth_history, now, window_seconds, direction)
        if book_depth_history and direction
        else None
    )

    factors: dict[str, bool] = {
        "Volume surge": volume_ratio >= float(settings["min_volume_ratio"]),
        "Statistical volume spike": z_score >= float(settings["min_z_score"]),
        "One-sided flow": abs(delta_fraction) >= float(settings["min_delta_fraction"]),
        "Sustained across buckets": persistence >= float(settings["min_persistence"]),
        "Institutional notional": aggression_notional >= float(settings["min_notional"]),
        "Price absorbed": (
            price_absorbed
            and abs(delta_fraction) >= float(settings["min_delta_fraction"])
        ),
        "Book absorbed": bool(book_depletion and (book_depletion["depleted"] or book_depletion["replenished"])),
        "Clean data": not feed_gap,
    }
    weights = {
        "Volume surge": 1.0,
        "Statistical volume spike": 1.0,
        "One-sided flow": 1.5,
        "Sustained across buckets": 1.5,
        "Institutional notional": 1.0,
        "Price absorbed": 1.5,
        "Book absorbed": 1.0,
        "Clean data": 0.5,
    }
    max_score = sum(weights.values())
    score = sum(weight for name, weight in weights.items() if factors.get(name))
    confidence = score / max_score if max_score else 0.0

    core_factors = (
        factors["Volume surge"]
        and factors["One-sided flow"]
        and factors["Sustained across buckets"]
        and factors["Institutional notional"]
    )
    active = core_factors and confidence >= 0.6

    if not active:
        pattern = PATTERN_NONE
    elif factors["Price absorbed"]:
        pattern = PATTERN_ABSORPTION
    elif not price_absorbed:
        pattern = PATTERN_SWEEP
    else:
        pattern = PATTERN_UNCONFIRMED

    rationale: list[str] = []
    if not factors["Volume surge"]:
        rationale.append(
            f"volume is {volume_ratio:.2f}x the quiet baseline, below the "
            f"{float(settings['min_volume_ratio']):.1f}x threshold"
        )
    if not factors["Sustained across buckets"]:
        rationale.append(
            f"aggression flipped direction across buckets (persistence {persistence:.0%})"
        )
    if not factors["One-sided flow"]:
        rationale.append(f"delta fraction {delta_fraction:+.1%} is too balanced")
    if not factors["Institutional notional"]:
        rationale.append(f"aggressive notional {aggression_notional:,.0f} USDT is below the threshold")
    if factors["Price absorbed"] and active:
        rationale.append(
            f"price moved only {price_move_bps:+.1f} bps against {aggression_btc:.1f} BTC of "
            f"{'buying' if dominant_sign > 0 else 'selling'} aggression"
        )
    if feed_gap:
        rationale.append("trade feed skipped rows in this window, so the footprint is understated")

    return {
        "active": active,
        "direction": direction,
        "side": dominant_sign if direction else 0,
        "pattern": pattern,
        "confidence": confidence,
        "score": score,
        "max_score": max_score,
        "factors": factors,
        "window_seconds": window_seconds,
        "bucket_seconds": bucket_seconds,
        "buckets": buckets,
        "bucket_count": len(buckets),
        "price_levels": levels,
        "volume_ratio": volume_ratio,
        "z_score": z_score,
        "baseline_bucket_volume": reference,
        "window_bucket_volume": window_bin_volume,
        "delta_fraction": delta_fraction,
        "delta_btc": delta_btc,
        "aggression_btc": aggression_btc,
        "aggression_notional": aggression_notional,
        "total_btc": total_btc,
        "notional": notional,
        "persistence": persistence,
        "price_move_bps": price_move_bps,
        "impact_per_imbalance": impact_per_imbalance,
        "expected_move_bps": expected_move_bps,
        "response_threshold_bps": response_threshold_bps,
        "response_ratio": response_ratio,
        "price_absorbed": price_absorbed,
        "book_depletion": book_depletion,
        "data_quality_ok": not feed_gap,
        "rationale": rationale,
    }


def describe_footprint(footprint: dict[str, Any]) -> str:
    """One-line human read of a footprint result."""
    if not footprint.get("active"):
        if footprint.get("rationale"):
            return "No institutional footprint: " + "; ".join(footprint["rationale"][:2])
        return "No institutional footprint detected"
    direction = footprint.get("direction", "")
    pattern = footprint.get("pattern", PATTERN_UNCONFIRMED)
    confidence = float(footprint.get("confidence", 0.0))
    if pattern == PATTERN_ABSORPTION:
        passive = "sellers" if direction == "BUY" else "buyers"
        return (
            f"{direction} aggression absorbed by passive {passive} · {confidence:.0%} confidence · "
            f"{footprint.get('aggression_btc', 0.0):.1f} BTC over {footprint.get('window_seconds', 0)}s"
        )
    if pattern == PATTERN_SWEEP:
        return (
            f"{direction} sweep · {confidence:.0%} confidence · "
            f"{footprint.get('aggression_btc', 0.0):.1f} BTC moved price {footprint.get('price_move_bps', 0.0):+.1f} bps"
        )
    return (
        f"{direction} aggression · {confidence:.0%} confidence · "
        f"pattern unconfirmed, treat with caution"
    )


def footprint_bias(footprint: dict[str, Any]) -> str:
    """Directional read of a footprint for the decision panel.

    Absorption is the informative case: the passive side is the informed
    participant, so aggressive selling into a bid that refuses to fall is a
    bullish read, and the mirror image is bearish.
    """
    if not footprint.get("active"):
        return "Neutral"
    pattern = footprint.get("pattern")
    direction = footprint.get("direction")
    if pattern == PATTERN_ABSORPTION:
        return "Bullish" if direction == "SELL" else "Bearish"
    if pattern == PATTERN_SWEEP:
        return "Bullish" if direction == "BUY" else "Bearish"
    return "Neutral"


def footprint_rows(buckets: list[dict[str, Any]], now: float, direction: str) -> list[dict[str, Any]]:
    """Tabular view of the detection window for the dashboard."""
    rows: list[dict[str, Any]] = []
    total = len(buckets)
    for index, bucket in enumerate(buckets):
        age = now - bucket["bucket_start"]
        rows.append(
            {
                "UTC time": _format_bucket(bucket["bucket_start"]),
                "Age (s)": int(age),
                "Buy (BTC)": bucket["buy_btc"],
                "Sell (BTC)": bucket["sell_btc"],
                "Net delta (BTC)": bucket["delta_btc"],
                "Imbalance": bucket["imbalance"],
                "Volume (BTC)": bucket["total_btc"],
                "Notional (USDT)": bucket["notional"],
                "Trades": bucket["trades"],
                "Direction": "Buy" if bucket["delta_btc"] > 0 else "Sell" if bucket["delta_btc"] < 0 else "Flat",
            }
        )
    rows.reverse()
    return rows


def _format_bucket(bucket_start: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(bucket_start, timezone.utc).strftime("%H:%M:%S UTC")
