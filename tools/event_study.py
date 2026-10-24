"""Event study for BTC order-flow footprints.

This measures whether an order-flow footprint carries any forward predictive
information, and at which horizon, before any strategy rules are layered on
top. Run this instead of tuning strategy parameters: it is the cheapest way
to find out whether a signal is real or whether it is a coincidence that a
parameter sweep will happily rediscover every week.

Method
------
* Flow comes from Binance futures 1m klines. Kline field 9 is taker buy base
  volume, so aggressor-side delta is available at 1m resolution for the whole
  history window without replaying millions of aggTrades.
* A window ending at bar ``i`` uses only bars ``<= i``. Forward returns use
  bars ``> i``. There is no look-ahead anywhere in the pipeline.
* Statistical inference uses a non-overlapping sample grid per horizon. A 60m
  forward return measured at every 1m boundary produces 60 overlapping
  observations per event, which would inflate every t-stat by roughly
  sqrt(60). Spacing the grid by the horizon removes that.
* Because the family of tests (pattern x horizon x window) is large, raw
  p-values are corrected with Benjamini-Hochberg FDR, and a permutation
  (placebo) test reports where the real statistic sits in the distribution of
  randomly relabelled event times.

Usage
-----
    python tools/event_study.py --days 30
    python tools/event_study.py --days 30 --placebos 500
    python tools/event_study.py --days 7 --source agg
    python tools/event_study.py --days 30 --windows 1 3 5 15 30 --horizons 1 3 5 15 30 60
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

BINANCE_BASE = "https://fapi.binance.com"
SYMBOL = "BTCUSDT"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = PROJECT_ROOT / ".cache"

# Cost model mirrored from backtest.py: fee and slippage are both per side.
DEFAULT_FEE_BPS = 5.0
DEFAULT_SLIPPAGE_BPS = 1.0

# Footprint thresholds, mirrored from footprint.DEFAULT_CONFIG and re-expressed
# for a bar-resolution stream instead of a 15s tick stream.
DEFAULT_STUDY_CONFIG: dict[str, Any] = {
    "min_volume_ratio": 1.8,
    "min_z_score": 1.5,
    "min_delta_fraction": 0.25,
    "min_persistence": 0.6,
    "min_notional": 0.0,
    "max_absorption_move_bps": 4.0,
    "confidence_floor": 0.6,
    "baseline_lookback_bars": 90,
}

FACTOR_WEIGHTS = {
    "Volume surge": 1.0,
    "Statistical volume spike": 1.0,
    "One-sided flow": 1.5,
    "Sustained across buckets": 1.5,
    "Institutional notional": 1.0,
    "Price absorbed": 1.5,
}
MAX_SCORE = sum(FACTOR_WEIGHTS.values())


# --------------------------------------------------------------------------
# small statistics helpers (no third-party dependencies in this project)
# --------------------------------------------------------------------------


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def stdev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    average = mean(values)
    variance = sum((value - average) ** 2 for value in values) / (len(values) - 1)
    return math.sqrt(variance)


def hit_rate(values: list[float]) -> float:
    return sum(1 for value in values if value > 0) / len(values) if values else 0.0


def t_statistic(sample_mean: float, null_mean: float, null_stdev: float, count: int) -> float:
    """z-score of a sample mean against an unconditional null distribution."""
    if count < 2 or null_stdev <= 0:
        return 0.0
    return (sample_mean - null_mean) / (null_stdev / math.sqrt(count))


def normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def p_value_from_z(z: float) -> float:
    return max(0.0, min(1.0, 2.0 * (1.0 - normal_cdf(abs(z)))))


def benjamini_hochberg(pvalues: list[float]) -> list[float]:
    """Return BH-adjusted q-values (monotone, capped at 1)."""
    count = len(pvalues)
    if not count:
        return []
    order = sorted(range(count), key=lambda index: pvalues[index])
    adjusted = [0.0] * count
    running = 1.0
    for rank in range(count - 1, -1, -1):
        index = order[rank]
        value = pvalues[index] * count / (rank + 1)
        running = min(running, value)
        adjusted[index] = min(1.0, running)
    return adjusted


# --------------------------------------------------------------------------
# data loading
# --------------------------------------------------------------------------


def _cache_path(name: str) -> Path:
    return CACHE_DIR / name


def _read_cache(path: Path, max_age_hours: float) -> Any | None:
    if not path.exists():
        return None
    age_hours = (datetime.now(timezone.utc).timestamp() - path.stat().st_mtime) / 3600
    if age_hours > max_age_hours:
        return None
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None


def _write_cache(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle)


def fetch_klines(days: int, interval: str = "1m") -> list[dict[str, Any]]:
    """Paginated Binance futures klines with aggressor-side volume split."""
    path = _cache_path(f"study_klines_{interval}_{days}d.json")
    cached = _read_cache(path, max_age_hours=1)
    if cached is not None:
        return cached

    session = requests.Session()
    step_ms = _interval_ms(interval)
    end_time = int(datetime.now(timezone.utc).timestamp() * 1000)
    cursor = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
    bars: list[dict[str, Any]] = []
    while cursor < end_time:
        batch = session.get(
            f"{BINANCE_BASE}/fapi/v1/klines",
            params={
                "symbol": SYMBOL,
                "interval": interval,
                "startTime": cursor,
                "endTime": end_time,
                "limit": 1000,
            },
            timeout=30,
        ).json()
        if not batch:
            break
        for row in batch:
            volume = float(row[5])
            taker_buy = float(row[9])
            bars.append(
                {
                    "time": int(row[0]) / 1000.0,
                    "open": float(row[1]),
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                    "volume": volume,
                    "buy": taker_buy,
                    "sell": max(0.0, volume - taker_buy),
                    "trades": int(row[8]),
                }
            )
        next_cursor = int(batch[-1][0]) + step_ms
        if next_cursor <= cursor or len(batch) < 1000:
            break
        cursor = next_cursor

    _write_cache(path, bars)
    return bars


def fetch_taker_ratio(days: int, period: str = "5m") -> list[dict[str, Any]]:
    """Independent aggressor-side source: Binance taker buy/sell volume ratio."""
    path = _cache_path(f"study_taker_{period}_{days}d.json")
    cached = _read_cache(path, max_age_hours=1)
    if cached is not None:
        return cached

    session = requests.Session()
    step_ms = _interval_ms(period)
    end_time = int(datetime.now(timezone.utc).timestamp() * 1000)
    cursor = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
    rows: list[dict[str, Any]] = []
    while cursor < end_time:
        batch = session.get(
            f"{BINANCE_BASE}/futures/data/takerlongshortRatio",
            params={
                "symbol": SYMBOL,
                "period": period,
                "limit": 500,
                "startTime": cursor,
                "endTime": end_time,
            },
            timeout=30,
        ).json()
        if not batch:
            break
        for row in batch:
            rows.append(
                {
                    "time": int(row["timestamp"]) / 1000.0,
                    "buy": float(row["buyVol"]),
                    "sell": float(row["sellVol"]),
                }
            )
        next_cursor = int(batch[-1]["timestamp"]) + step_ms
        if next_cursor <= cursor or len(batch) < 500:
            break
        cursor = next_cursor

    _write_cache(path, rows)
    return rows


def _interval_ms(interval: str) -> int:
    units = {"m": 60_000, "h": 3_600_000, "d": 86_400_000}
    return int(interval[:-1]) * units[interval[-1]]


# --------------------------------------------------------------------------
# footprint features on a bar stream
# --------------------------------------------------------------------------


def window_features(
    bars: list[dict[str, Any]],
    end_index: int,
    window_bars: int,
    config: dict[str, Any],
) -> dict[str, Any] | None:
    """Footprint factors for the window ending at ``end_index``.

    Only bars at or before ``end_index`` are read, so the result is safe to
    use as a signal at that bar's close.
    """
    lookback = int(config["baseline_lookback_bars"])
    start = end_index - window_bars + 1
    baseline_start = start - lookback
    if start < 0 or baseline_start < 0:
        return None

    window = bars[start : end_index + 1]
    baseline = bars[baseline_start:start]
    if len(window) < window_bars or len(baseline) < max(10, lookback // 3):
        return None

    baseline_volumes = [bar["volume"] for bar in baseline]
    baseline_mean = mean(baseline_volumes)
    baseline_stdev = stdev(baseline_volumes)

    window_volumes = [bar["volume"] for bar in window]
    window_bucket_volume = mean(window_volumes)
    volume_ratio = window_bucket_volume / baseline_mean if baseline_mean > 0 else 0.0
    z_score = (
        (window_bucket_volume - baseline_mean) / baseline_stdev if baseline_stdev > 0 else 0.0
    )

    buy = sum(bar["buy"] for bar in window)
    sell = sum(bar["sell"] for bar in window)
    total = buy + sell
    delta_btc = buy - sell
    delta_fraction = delta_btc / total if total > 0 else 0.0

    dominant_sign = 1 if delta_btc > 0 else -1
    signed = [bar["buy"] - bar["sell"] for bar in window]
    active_bars = [value for value in signed if abs(value) > 0]
    persistence = (
        sum(1 for value in active_bars if (value > 0) == (dominant_sign > 0)) / len(active_bars)
        if active_bars
        else 0.0
    )

    notional = sum(bar["volume"] * bar["close"] for bar in window)
    aggression_notional = notional * abs(delta_fraction)

    price_move_bps = (window[-1]["close"] / window[0]["open"] - 1) * 10_000 if window[0]["open"] else 0.0

    baseline_moves = []
    for previous, current in zip(baseline, baseline[1:]):
        if previous["close"]:
            baseline_moves.append(abs(current["close"] / previous["close"] - 1) * 10_000)
    median_move = median(baseline_moves)
    expected_move_bps = median_move * math.sqrt(len(window))
    response_threshold = max(float(config["max_absorption_move_bps"]), expected_move_bps * 0.6)
    price_absorbed = abs(price_move_bps) < response_threshold

    factors = {
        "Volume surge": volume_ratio >= float(config["min_volume_ratio"]),
        "Statistical volume spike": z_score >= float(config["min_z_score"]),
        "One-sided flow": abs(delta_fraction) >= float(config["min_delta_fraction"]),
        "Sustained across buckets": persistence >= float(config["min_persistence"]),
        "Institutional notional": aggression_notional >= float(config["min_notional"]),
        "Price absorbed": price_absorbed and abs(delta_fraction) >= float(config["min_delta_fraction"]),
    }
    score = sum(weight for name, weight in FACTOR_WEIGHTS.items() if factors[name])
    confidence = score / MAX_SCORE if MAX_SCORE else 0.0

    core = (
        factors["Volume surge"]
        and factors["One-sided flow"]
        and factors["Sustained across buckets"]
        and factors["Institutional notional"]
    )
    active = core and confidence >= float(config["confidence_floor"])

    if not active:
        pattern = "NONE"
    elif factors["Price absorbed"]:
        pattern = "ABSORPTION"
    else:
        pattern = "SWEEP"

    return {
        "end_index": end_index,
        "time": bars[end_index]["time"],
        "pattern": pattern,
        "active": active,
        "confidence": confidence,
        "score": score,
        "direction": "BUY" if delta_btc > 0 else "SELL" if delta_btc < 0 else "",
        "side": dominant_sign if delta_btc else 0,
        "volume_ratio": volume_ratio,
        "z_score": z_score,
        "delta_fraction": delta_fraction,
        "persistence": persistence,
        "price_move_bps": price_move_bps,
        "expected_move_bps": expected_move_bps,
        "price_absorbed": price_absorbed,
        "notional": notional,
        "factors": factors,
    }


def hypothesis_side(feature: dict[str, Any]) -> int:
    """Trade direction implied by the footprint hypothesis.

    A sweep is momentum: aggression that moved price is expected to continue.
    Absorption is reversion: aggression that failed to move price marks an
    informed passive side, so the aggressive direction fades.
    """
    if not feature.get("active") or not feature.get("side"):
        return 0
    if feature["pattern"] == "ABSORPTION":
        return -feature["side"]
    if feature["pattern"] == "SWEEP":
        return feature["side"]
    return 0


# --------------------------------------------------------------------------
# forward outcomes
# --------------------------------------------------------------------------


def forward_outcome(
    bars: list[dict[str, Any]],
    end_index: int,
    horizon_bars: int,
    side: int,
    max_hold_bars: int | None = None,
) -> dict[str, Any] | None:
    """Forward return, MFE and MAE for a hypothesis over ``horizon_bars``."""
    end = end_index + horizon_bars
    if end >= len(bars):
        return None
    entry = bars[end_index]["close"]
    if not entry:
        return None
    exit_price = bars[end]["close"]
    forward_bps = (exit_price / entry - 1) * 10_000

    hold = horizon_bars if max_hold_bars is None else min(horizon_bars, max_hold_bars)
    slice_end = min(end_index + hold, len(bars) - 1)
    path = bars[end_index + 1 : slice_end + 1]
    if not path:
        return None
    if side > 0:
        mfe = (max(bar["high"] for bar in path) / entry - 1) * 10_000
        mae = (min(bar["low"] for bar in path) / entry - 1) * 10_000
    else:
        mfe = (entry / min(bar["low"] for bar in path) - 1) * 10_000
        mae = (entry / max(bar["high"] for bar in path) - 1) * 10_000

    return {
        "forward_bps": forward_bps,
        "signed_bps": side * forward_bps,
        "mfe_bps": mfe,
        "mae_bps": mae,
    }


# --------------------------------------------------------------------------
# event study
# --------------------------------------------------------------------------


def collect_events(
    bars: list[dict[str, Any]],
    window_bars: int,
    horizon_bars: int,
    config: dict[str, Any],
    max_samples: int = 40000,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (events, unconditional) on a non-overlapping sample grid."""
    events: list[dict[str, Any]] = []
    unconditional: list[dict[str, Any]] = []
    lookback = int(config["baseline_lookback_bars"])
    first_end = lookback + window_bars
    last_end = len(bars) - horizon_bars - 1
    if first_end >= last_end:
        return events, unconditional

    # Samples must be at least one horizon apart or their forward returns
    # overlap and every t-stat is inflated by roughly sqrt(horizon).
    step = max(1, horizon_bars)
    available = (last_end - first_end) // step + 1
    if available > max_samples:
        step *= math.ceil(available / max_samples)

    for end_index in range(first_end, last_end + 1, step):
        feature = window_features(bars, end_index, window_bars, config)
        if feature is None:
            continue
        side = hypothesis_side(feature)
        # The null pool always stores the *raw* forward return so that it is a
        # clean unconditional reference distribution. Mixing signed values for
        # events and raw values for non-events would contaminate both the null
        # estimate and the placebo pool.
        outcome = forward_outcome(bars, end_index, horizon_bars, 1)
        if outcome is None:
            continue
        unconditional.append({"end_index": end_index, **outcome})
        if not feature.get("active") or not side:
            continue
        events.append(
            {
                "end_index": end_index,
                "time": feature["time"],
                "pattern": feature["pattern"],
                "side": side,
                "confidence": feature["confidence"],
                "volume_ratio": feature["volume_ratio"],
                "delta_fraction": feature["delta_fraction"],
                "persistence": feature["persistence"],
                "price_move_bps": feature["price_move_bps"],
                **outcome,
            }
        )
    return events, unconditional


def sign_flip_p_value(signed: list[float], iterations: int, rng: random.Random) -> float:
    """Exact-style randomization test for "the footprint carries no direction".

    Under the null, an event's forward return is as likely to run against the
    hypothesis as with it, so flipping the sign of each event's signed return
    reproduces the null distribution of the sample mean. This needs no
    assumption about the variance of the underlying returns, which matters
    because footprint events sit on unusually volatile stretches of tape.
    """
    if len(signed) < 2:
        return 1.0
    observed = abs(mean(signed))
    extreme = 0
    for _ in range(iterations):
        flipped = [value if rng.random() < 0.5 else -value for value in signed]
        if abs(mean(flipped)) >= observed - 1e-12:
            extreme += 1
    return (extreme + 1) / (iterations + 1)


def summarize_group(
    events: list[dict[str, Any]],
    unconditional: list[dict[str, Any]],
    cost_bps: float,
    rng: random.Random | None = None,
    iterations: int = 2000,
) -> dict[str, Any]:
    signed = [event["signed_bps"] for event in events]
    null_signed = [row["signed_bps"] for row in unconditional]
    null_mean = mean(null_signed)
    null_stdev = stdev(null_signed)
    sample_mean = mean(signed)
    z = t_statistic(sample_mean, null_mean, null_stdev, len(signed))
    flip_p = sign_flip_p_value(signed, iterations, rng) if rng is not None else p_value_from_z(z)
    return {
        "count": len(signed),
        "hit_rate": hit_rate(signed),
        "mean_bps": sample_mean,
        "median_bps": median(signed),
        "net_bps": sample_mean - cost_bps,
        "null_mean_bps": null_mean,
        "z": z,
        "p_value": flip_p,
        "mfe_median": median([event["mfe_bps"] for event in events]),
        "mae_median": median([event["mae_bps"] for event in events]),
        "avg_confidence": mean([event["confidence"] for event in events]),
    }


def placebo_percentile(
    events: list[dict[str, Any]],
    unconditional: list[dict[str, Any]],
    iterations: int,
    rng: random.Random,
) -> float:
    """Where the real gross expectancy sits inside randomly relabelled event sets.

    Reported on the gross statistic, before the cost deduction. Subtracting a
    constant from both the real and the placebo values would not change the
    ranking but would make the output read as if the signal were negative.
    """
    if not events or not unconditional:
        return 0.0
    real = mean([event["signed_bps"] for event in events])
    pool = [row["signed_bps"] for row in unconditional]
    if len(pool) < 2:
        return 0.0
    beats = 0
    for _ in range(iterations):
        sample = [rng.choice(pool) for _ in events]
        if mean(sample) >= real:
            beats += 1
    return beats / iterations


def bh_adjust(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach Benjamini-Hochberg q-values across the family of tests."""
    if not rows:
        return rows
    qvalues = benjamini_hochberg([row["p_value"] for row in rows])
    for row, qvalue in zip(rows, qvalues):
        row["q_value"] = qvalue
    return rows


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------


def format_time(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).strftime("%m-%d %H:%M")


def print_hurdles(bars: list[dict[str, Any]], cost_bps: float, fee_bps: float) -> None:
    print("\n=== Cost hurdle ===")
    for label, minutes in (("1m", 1), ("5m", 5), ("15m", 15), ("30m", 30), ("60m", 60)):
        moves = []
        for previous, current in zip(bars, bars[minutes:]):
            if previous["close"]:
                moves.append(abs(current["close"] / previous["close"] - 1) * 10_000)
        typical = median(moves)
        verdict = "clears cost" if typical > cost_bps else "BELOW cost"
        print(f"  median |{label:<3} move| : {typical:6.1f} bps   {verdict}")
    print(f"  round-trip cost     : {cost_bps:.1f} bps (fee {fee_bps:.1f} + slippage {DEFAULT_SLIPPAGE_BPS:.0f} per side)")
    for stop in (20, 30, 40, 60):
        for target_r in (1.0, 1.5, 2.0):
            win = stop * target_r - cost_bps
            loss = stop + cost_bps
            if win <= 0:
                continue
            required = loss / (win + loss)
            print(
                f"  stop {stop:>3} bps, target {stop * target_r:>5.1f} bps -> "
                f"required win rate {required:5.1%}  (cost is {cost_bps / stop:.0%} of the stop)"
            )


def print_results(
    rows: list[dict[str, Any]],
    windows: list[int],
    horizons: list[int],
    cost_bps: float,
    placebos: int,
    rng: random.Random,
    events_by_key: dict[tuple[int, int], tuple[list[dict[str, Any]], list[dict[str, Any]]]],
) -> None:
    print("\n=== Forward-return matrix (net bps after cost) ===")
    header = "  window |" + "".join(f" {h:>8}m" for h in horizons)
    print(header)
    for window in windows:
        cells = []
        for horizon in horizons:
            row = next(
                (r for r in rows if r["window"] == window and r["horizon"] == horizon), None
            )
            if row is None or row["count"] == 0:
                cells.append(f" {'-':>8}")
            else:
                marker = "*" if row["net_bps"] > 0 else " "
                cells.append(f" {row['net_bps']:+7.1f}{marker}")
        print(f"  {window:>6}m |" + "".join(cells))

    print("\n=== Per-pattern detail (net bps after cost) ===")
    for row in rows:
        if row["count"] == 0:
            continue
        print(
            f"  window {row['window']:>3}m  horizon {row['horizon']:>3}m  {row['pattern']:<11}"
            f" n={row['count']:>5}  hit {row['hit_rate']:>5.1%}  "
            f"mean {row['mean_bps']:+7.1f}  net {row['net_bps']:+7.1f}  "
            f"z {row['z']:+5.2f}  p {row['p_value']:.3f}  q {row['q_value']:.3f}  "
            f"MFE {row['mfe_median']:>6.1f}  MAE {row['mae_median']:>6.1f}"
        )

    significant = [row for row in rows if row.get("q_value", 1.0) < 0.10]
    print(f"\n=== Verdict ===")
    print(f"  tests run: {len(rows)}   surviving FDR q<0.10: {len(significant)}")
    if not significant:
        print("  No test survives multiple-testing correction. The footprint shows no")
        print("  reliable forward edge at these thresholds. Do not build strategy rules on it.")
    else:
        for row in significant:
            print(
                f"  SURVIVES: window {row['window']}m horizon {row['horizon']}m {row['pattern']} "
                f"net {row['net_bps']:+.1f} bps q={row['q_value']:.3f}"
            )

    if placebos > 0:
        print(f"\n=== Placebo check ({placebos} relabellings) ===")
        for window in windows:
            for horizon in horizons:
                events, unconditional = events_by_key.get((window, horizon), ([], []))
                if len(events) < 20:
                    continue
                percentile = placebo_percentile(events, unconditional, placebos, rng)
                real = mean([event["signed_bps"] for event in events])
                print(
                    f"  window {window:>3}m horizon {horizon:>3}m  gross {real:+7.2f} bps  "
                    f"beats {percentile:5.1%} of random labellings"
                )


def print_dose_response(
    events: list[dict[str, Any]],
    window: int,
    horizon: int,
    cost_bps: float,
) -> None:
    """Does the edge grow with event strength?

    If a selective subset of events carries more edge per trade, then trading
    fewer, higher-conviction events is the route to profitability. If the edge
    is flat across strength buckets, no amount of selectivity rescues it.
    """
    if len(events) < 40:
        return
    print(f"\n=== Dose response · window {window}m · horizon {horizon}m ===")
    for key, label in (("confidence", "confidence"), ("volume_ratio", "volume ratio"), ("delta_fraction", "|delta|")):
        values = [abs(event[key]) if key == "delta_fraction" else event[key] for event in events]
        ordered = sorted(values)
        first = ordered[len(ordered) // 3]
        second = ordered[2 * len(ordered) // 3]
        buckets: dict[str, list[float]] = {}
        for event, value in zip(events, values):
            tier = "low" if value < first else "mid" if value < second else "high"
            buckets.setdefault(tier, []).append(event["signed_bps"])
        line = f"  by {label:<12}:"
        for tier in ("low", "mid", "high"):
            group = buckets.get(tier)
            if not group:
                continue
            line += f"  {tier}(n={len(group):>4}) {mean(group):+6.2f} bps"
        print(line)


def main() -> int:
    parser = argparse.ArgumentParser(description="Event study for BTC order-flow footprints.")
    parser.add_argument("--days", type=int, default=30, help="history window in days")
    parser.add_argument("--windows", type=int, nargs="+", default=[1, 3, 5, 15, 30],
                        help="footprint window lengths in minutes")
    parser.add_argument("--horizons", type=int, nargs="+", default=[1, 3, 5, 15, 30, 60],
                        help="forward-return horizons in minutes")
    parser.add_argument("--fee-bps", type=float, default=DEFAULT_FEE_BPS)
    parser.add_argument("--slippage-bps", type=float, default=DEFAULT_SLIPPAGE_BPS)
    parser.add_argument(
        "--maker-fee-bps",
        type=float,
        default=None,
        help="if set, price the run at a post-only maker fee instead of taker",
    )
    parser.add_argument("--placebos", type=int, default=0, help="permutation iterations (0 disables)")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--min-volume-ratio", type=float, default=DEFAULT_STUDY_CONFIG["min_volume_ratio"])
    parser.add_argument("--min-delta-fraction", type=float, default=DEFAULT_STUDY_CONFIG["min_delta_fraction"])
    parser.add_argument("--min-persistence", type=float, default=DEFAULT_STUDY_CONFIG["min_persistence"])
    parser.add_argument("--confidence-floor", type=float, default=DEFAULT_STUDY_CONFIG["confidence_floor"])
    parser.add_argument("--baseline-lookback", type=int, default=DEFAULT_STUDY_CONFIG["baseline_lookback_bars"])
    parser.add_argument("--max-samples", type=int, default=20000,
                        help="cap on grid points per configuration, to bound runtime")
    args = parser.parse_args()

    config = dict(DEFAULT_STUDY_CONFIG)
    config["min_volume_ratio"] = args.min_volume_ratio
    config["min_delta_fraction"] = args.min_delta_fraction
    config["min_persistence"] = args.min_persistence
    config["confidence_floor"] = args.confidence_floor
    config["baseline_lookback_bars"] = args.baseline_lookback

    fee_bps = args.maker_fee_bps if args.maker_fee_bps is not None else args.fee_bps
    cost_bps = 2 * (fee_bps + args.slippage_bps)
    rng = random.Random(args.seed)
    if args.maker_fee_bps is not None:
        print(f"Using post-only maker fee: {fee_bps:.2f} bps per side.")

    print(f"Fetching {args.days} days of {SYMBOL} 1m klines...")
    bars = fetch_klines(args.days, "1m")
    if len(bars) < 2000:
        print("Not enough history to run the study.")
        return 1
    print(f"Loaded {len(bars)} bars: {format_time(bars[0]['time'])} -> {format_time(bars[-1]['time'])}")

    print_hurdles(bars, cost_bps, fee_bps)

    rows: list[dict[str, Any]] = []
    events_by_key: dict[tuple[int, int], tuple[list[dict[str, Any]], list[dict[str, Any]]]] = {}
    for window in args.windows:
        for horizon in args.horizons:
            if horizon < window:
                continue
            events, unconditional = collect_events(bars, window, horizon, config, args.max_samples)
            events_by_key[(window, horizon)] = (events, unconditional)
            for pattern in ("SWEEP", "ABSORPTION"):
                subset = [event for event in events if event["pattern"] == pattern]
                summary = summarize_group(subset, unconditional, cost_bps, rng)
                summary.update({"window": window, "horizon": horizon, "pattern": pattern})
                rows.append(summary)

    rows = bh_adjust(rows)
    print_results(rows, args.windows, args.horizons, cost_bps, args.placebos, rng, events_by_key)

    for window in args.windows:
        for horizon in args.horizons:
            if horizon < window:
                continue
            events, _ = events_by_key.get((window, horizon), ([], []))
            absorption = [event for event in events if event["pattern"] == "ABSORPTION"]
            if len(absorption) >= 40:
                print_dose_response(absorption, window, horizon, cost_bps)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
