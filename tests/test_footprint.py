"""Tests for the institutional-footprint detector."""

from __future__ import annotations

from footprint import (
    PATTERN_ABSORPTION,
    PATTERN_NONE,
    PATTERN_SWEEP,
    baseline_volume,
    bucket_flow,
    describe_footprint,
    detect_book_depletion,
    detect_large_aggressor,
    footprint_bias,
    footprint_rows,
    price_level_footprint,
)

NOW = 1_000_000.0
WINDOW = 150
BUCKET = 15
BASE_PRICE = 82_000.0


def quiet_tape(now: float = NOW) -> list[dict]:
    """A calm tape: one small print per bucket across the baseline region."""
    trades = []
    time = now - 900
    while time < now - WINDOW:
        trades.append(
            {
                "time": time,
                "price": BASE_PRICE,
                "quantity": 0.1,
                "sell_aggressor": False,
            }
        )
        time += BUCKET
    return trades


def aggression(
    now: float = NOW,
    side: int = 1,
    per_bucket: float = 4.0,
    prints: int = 4,
    price_start: float = BASE_PRICE,
    price_end: float = BASE_PRICE,
    start: float | None = None,
) -> list[dict]:
    """Sustained one-sided aggression across the detection window."""
    trades = []
    window_start = now - WINDOW if start is None else start
    bucket_count = int((now - window_start) // BUCKET)
    for bucket_index in range(bucket_count):
        bucket_start = window_start + bucket_index * BUCKET
        share = bucket_index / max(1, bucket_count - 1)
        price = price_start + (price_end - price_start) * share
        for print_index in range(prints):
            trades.append(
                {
                    "time": bucket_start + 1 + print_index,
                    "price": price,
                    "quantity": per_bucket / prints,
                    "sell_aggressor": side < 0,
                }
            )
    return trades


def test_bucket_flow_groups_by_bucket_and_keeps_oldest_first():
    trades = [
        {"time": 900.0, "price": 100.0, "quantity": 1.0, "sell_aggressor": False},
        {"time": 905.0, "price": 101.0, "quantity": 2.0, "sell_aggressor": True},
        {"time": 920.0, "price": 102.0, "quantity": 3.0, "sell_aggressor": False},
    ]

    buckets = bucket_flow(trades, now=925.0, bucket_seconds=15, window_seconds=60)

    assert [bucket["bucket_start"] for bucket in buckets] == [900.0, 915.0]
    assert buckets[0]["buy_btc"] == 1.0
    assert buckets[0]["sell_btc"] == 2.0
    assert buckets[0]["delta_btc"] == -1.0
    assert buckets[0]["trades"] == 2
    assert buckets[1]["total_btc"] == 3.0
    assert buckets[1]["delta_btc"] == 3.0


def test_baseline_volume_uses_bucket_aligned_grid():
    trades = [
        {"time": 100.0, "price": 100.0, "quantity": 2.0, "sell_aggressor": False},
        {"time": 130.0, "price": 100.0, "quantity": 4.0, "sell_aggressor": False},
    ]

    baseline = baseline_volume(
        trades, now=400.0, bucket_seconds=15, window_seconds=150, lookback_seconds=300
    )

    # Both prints must land in the aligned grid rather than being missed, so
    # the baseline mean is positive instead of a flat zero.
    assert baseline["mean"] > 0
    assert baseline["bins"] > 0
    # Quiet bins count as zero volume.
    assert baseline["median"] == 0.0
    assert baseline["quiet_bin"] == 3.0


def test_detects_sweep_when_aggression_moves_price():
    trades = quiet_tape() + aggression(side=1, price_start=BASE_PRICE, price_end=BASE_PRICE + 60)

    result = detect_large_aggressor(trades, now=NOW)

    assert result["active"] is True
    assert result["direction"] == "BUY"
    assert result["pattern"] == PATTERN_SWEEP
    assert result["volume_ratio"] > 5
    assert result["persistence"] == 1.0
    assert result["price_move_bps"] > 0
    assert result["confidence"] >= 0.6


def test_detects_absorption_when_aggression_does_not_move_price():
    trades = quiet_tape() + aggression(side=-1, price_start=BASE_PRICE, price_end=BASE_PRICE - 1)

    result = detect_large_aggressor(trades, now=NOW)

    assert result["active"] is True
    assert result["direction"] == "SELL"
    assert result["pattern"] == PATTERN_ABSORPTION
    assert result["impact_per_imbalance"] < 25.0
    assert result["price_move_bps"] < 5


def test_absorption_of_selling_is_a_bullish_read():
    trades = quiet_tape() + aggression(side=-1, price_end=BASE_PRICE - 1)

    result = detect_large_aggressor(trades, now=NOW)

    assert result["pattern"] == PATTERN_ABSORPTION
    assert footprint_bias(result) == "Bullish"


def test_sweep_of_buying_is_a_bullish_read():
    trades = quiet_tape() + aggression(side=1, price_end=BASE_PRICE + 60)

    result = detect_large_aggressor(trades, now=NOW)

    assert result["pattern"] == PATTERN_SWEEP
    assert footprint_bias(result) == "Bullish"


def test_calm_tape_produces_no_footprint():
    result = detect_large_aggressor(quiet_tape(), now=NOW)

    assert result["active"] is False
    assert result["pattern"] == PATTERN_NONE
    assert footprint_bias(result) == "Neutral"
    assert result["rationale"]


def test_balanced_aggression_is_rejected():
    trades = quiet_tape()
    # Equal buys and sells at high volume: large but not one-sided.
    for index in range(40):
        trades.append(
            {
                "time": NOW - WINDOW + index * 3,
                "price": BASE_PRICE,
                "quantity": 0.5,
                "sell_aggressor": index % 2 == 0,
            }
        )

    result = detect_large_aggressor(trades, now=NOW)

    assert result["active"] is False
    assert result["factors"]["One-sided flow"] is False


def test_flipping_direction_breaks_persistence():
    trades = quiet_tape()
    for bucket_index in range(10):
        side = 1 if bucket_index % 2 == 0 else -1
        trades.append(
            {
                "time": NOW - WINDOW + bucket_index * BUCKET + 1,
                "price": BASE_PRICE,
                "quantity": 1.5,
                "sell_aggressor": side < 0,
            }
        )

    result = detect_large_aggressor(trades, now=NOW)

    assert result["persistence"] <= 0.6
    assert result["factors"]["Sustained across buckets"] is False


def test_feed_gap_reduces_confidence_and_is_flagged():
    trades = quiet_tape() + aggression(side=1, price_end=BASE_PRICE + 60)

    clean = detect_large_aggressor(trades, now=NOW)
    gapped = detect_large_aggressor(trades, now=NOW, feed_gap=True)

    assert clean["factors"]["Clean data"] is True
    assert gapped["factors"]["Clean data"] is False
    assert gapped["confidence"] < clean["confidence"]
    assert gapped["data_quality_ok"] is False
    assert any("trade feed skipped" in note for note in gapped["rationale"])


def test_price_level_footprint_ranks_by_unbalanced_notional():
    trades = [
        {"time": NOW - 10, "price": 82_000.0, "quantity": 5.0, "sell_aggressor": False},
        {"time": NOW - 9, "price": 82_010.0, "quantity": 0.2, "sell_aggressor": False},
        {"time": NOW - 8, "price": 82_020.0, "quantity": 0.3, "sell_aggressor": True},
    ]

    levels = price_level_footprint(trades, now=NOW, window_seconds=60, price_bucket_usd=10.0)

    assert levels[0]["price"] == 82_000.0
    assert levels[0]["buy_btc"] == 5.0
    assert levels[0]["imbalance"] == 1.0
    assert levels[0]["unbalanced_notional"] == 5.0 * 82_000.0
    assert levels[0]["passive_notional"] == 0.0


def test_book_depletion_detects_thinning_passive_side():
    history = [
        {"time": NOW - 150, "bid_qty": 40.0, "ask_qty": 30.0},
        {"time": NOW, "bid_qty": 41.0, "ask_qty": 12.0},
    ]

    depletion = detect_book_depletion(history, now=NOW, window_seconds=150, direction="BUY")

    assert depletion is not None
    assert depletion["passive_side"] == "asks"
    assert depletion["depleted"] is True
    assert depletion["replenished"] is False
    assert depletion["change_pct"] < 0


def test_book_replenishment_is_a_passive_quote():
    history = [
        {"time": NOW - 150, "bid_qty": 40.0, "ask_qty": 10.0},
        {"time": NOW, "bid_qty": 41.0, "ask_qty": 25.0},
    ]

    depletion = detect_book_depletion(history, now=NOW, window_seconds=150, direction="BUY")

    assert depletion["replenished"] is True
    assert depletion["depleted"] is False


def test_book_depletion_needs_history():
    assert detect_book_depletion([], now=NOW, window_seconds=150, direction="BUY") is None


def test_footprint_rows_are_newest_first_with_direction():
    trades = quiet_tape() + aggression(side=1, price_end=BASE_PRICE + 60)

    result = detect_large_aggressor(trades, now=NOW)
    rows = footprint_rows(result["buckets"], now=NOW, direction=result["direction"])

    assert rows
    assert rows[0]["Age (s)"] < rows[-1]["Age (s)"]
    assert all(row["Direction"] == "Buy" for row in rows)


def test_describe_footprint_explains_absence_and_presence():
    calm = detect_large_aggressor(quiet_tape(), now=NOW)
    assert "No institutional footprint" in describe_footprint(calm)

    trades = quiet_tape() + aggression(side=-1, price_end=BASE_PRICE - 1)
    active = detect_large_aggressor(trades, now=NOW)
    assert "absorbed" in describe_footprint(active)


def test_empty_trade_list_is_handled():
    result = detect_large_aggressor([], now=NOW)

    assert result["active"] is False
    assert result["total_btc"] == 0.0
    assert result["delta_fraction"] == 0.0
    assert result["volume_ratio"] == 0.0
