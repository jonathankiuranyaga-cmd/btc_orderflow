"""Market-regime read: whether current conditions suit scalping.

Reads recent trade flow and the top of the order book to estimate:

* **Volatility** - median absolute inter-trade step in bps over the last 60 s.
* **Participation** - trades per second over the last 60 s.
* **Spread** - top-of-book spread in bps.
* **Cost hurdle** - round-trip cost (fee + slippage, per side) in bps.
* **Move-to-cost ratio** - median move divided by round-trip cost.

When the median move is smaller than the cost of entering and exiting, a
perfectly correct directional signal still loses money. This module reports
that explicitly so the user can see whether scalping is mathematically viable
right now, rather than discovering it from a losing tape afterwards.

Everything here is a pure function of the trade buffer and book snapshot, so it
can be tested without a Streamlit runtime or a live feed.
"""

from __future__ import annotations

from statistics import median
from typing import Any

BAND_POOR = "Poor"
BAND_FAIR = "Fair"
BAND_GOOD = "Good"


def _intertrade_steps_bps(trades: list[dict[str, Any]], now: float, window_seconds: int) -> list[float]:
    """Absolute price steps between consecutive trades inside the window, in bps."""
    window_start = now - window_seconds
    recent = sorted(
        (t for t in trades if window_start <= float(t["time"]) <= now),
        key=lambda t: float(t["time"]),
    )
    steps: list[float] = []
    for previous, current in zip(recent, recent[1:]):
        prev_price = float(previous["price"])
        curr_price = float(current["price"])
        if prev_price > 0:
            steps.append(abs(curr_price / prev_price - 1) * 10_000.0)
    return steps


def calculate_market_regime(
    trades: list[dict[str, Any]],
    book: dict[str, Any],
    mark: float,
    now: float,
    fee_bps: float,
    slippage_bps: float,
    window_seconds: int = 60,
) -> dict[str, Any]:
    """Assess whether current market conditions suit scalping.

    Returns a dict with the raw readings (volatility, participation, spread,
    cost hurdle, move-to-cost ratio) and a suitability band.
    """
    steps = _intertrade_steps_bps(trades, now, window_seconds)
    recent = [t for t in trades if now - window_seconds <= float(t["time"]) <= now]
    participation = len(recent) / max(window_seconds, 1)

    bids = book.get("bids") or []
    asks = book.get("asks") or []
    spread_bps = 0.0
    if bids and asks and mark > 0:
        best_bid = float(bids[0][0])
        best_ask = float(asks[0][0])
        if best_ask > best_bid:
            spread_bps = (best_ask - best_bid) / mark * 10_000.0

    round_trip_cost_bps = 2.0 * (float(fee_bps) + float(slippage_bps))
    volatility_bps = median(steps) if steps else 0.0
    move_to_cost = volatility_bps / round_trip_cost_bps if round_trip_cost_bps > 0 else 0.0

    # A regime is only worth scalping when the median inter-trade move clears
    # the round-trip cost and there is enough participation to fill quickly.
    if move_to_cost >= 1.2 and participation >= 2.0 and spread_bps <= max(round_trip_cost_bps, 1.0):
        band = BAND_GOOD
    elif move_to_cost >= 0.6 and participation >= 1.0:
        band = BAND_FAIR
    else:
        band = BAND_POOR

    return {
        "band": band,
        "volatility_bps": round(volatility_bps, 2),
        "participation_per_sec": round(participation, 2),
        "spread_bps": round(spread_bps, 2),
        "round_trip_cost_bps": round(round_trip_cost_bps, 2),
        "move_to_cost": round(move_to_cost, 2),
        "median_move_clears_cost": move_to_cost >= 1.0,
        "window_seconds": window_seconds,
        "trade_count": len(recent),
    }


def describe_market_regime(regime: dict[str, Any]) -> str:
    """One-line human read of a market-regime result."""
    band = regime.get("band", "Unknown")
    if band == BAND_GOOD:
        return (
            f"Scalping regime favourable · {regime.get('volatility_bps', 0.0):.1f} bps median step "
            f"vs {regime.get('round_trip_cost_bps', 0.0):.1f} bps round trip · "
            f"{regime.get('participation_per_sec', 0.0):.1f} trades/s"
        )
    if band == BAND_FAIR:
        return (
            f"Scalping regime marginal · {regime.get('volatility_bps', 0.0):.1f} bps median step "
            f"vs {regime.get('round_trip_cost_bps', 0.0):.1f} bps round trip · "
            f"{regime.get('participation_per_sec', 0.0):.1f} trades/s"
        )
    return (
        f"Scalping regime unfavourable · {regime.get('volatility_bps', 0.0):.1f} bps median step "
        f"below {regime.get('round_trip_cost_bps', 0.0):.1f} bps round trip · "
        f"{regime.get('participation_per_sec', 0.0):.1f} trades/s"
    )
