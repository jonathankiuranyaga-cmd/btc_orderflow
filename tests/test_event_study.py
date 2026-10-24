"""Tests for the event-study statistics.

The most important test here is the look-ahead guard: the study once measured
a forward return over a window that overlapped the signal window, which turned
a zero edge into a large fake one. Any change to the forward-return window
must fail that test.
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.event_study import (  # noqa: E402
    benjamini_hochberg,
    forward_outcome,
    hypothesis_side,
    mean,
    p_value_from_z,
    sign_flip_p_value,
    stdev,
    window_features,
)


def make_bars(count: int = 400, *, buy_ratio: float = 0.55, price_step: float = 0.0):
    """Synthetic 1m bars with a controlled aggressor split."""
    bars = []
    price = 100.0
    for index in range(count):
        price += price_step
        volume = 100.0
        bars.append(
            {
                "time": 1_700_000_000.0 + index * 60,
                "open": price,
                "high": price * 1.0005,
                "low": price * 0.9995,
                "close": price,
                "volume": volume,
                "buy": volume * buy_ratio,
                "sell": volume * (1 - buy_ratio),
                "trades": 100,
            }
        )
    return bars


# --------------------------------------------------------------------------
# statistics
# --------------------------------------------------------------------------


def test_benjamini_hochberg_is_monotone_and_bounded():
    pvalues = [0.001, 0.01, 0.02, 0.2, 0.9]
    qvalues = benjamini_hochberg(pvalues)
    assert all(0.0 <= value <= 1.0 for value in qvalues)
    assert qvalues[0] < qvalues[-1]
    assert qvalues == sorted(qvalues, reverse=True) or qvalues[0] <= qvalues[1]


def test_benjamini_hochberg_handles_empty():
    assert benjamini_hochberg([]) == []


def test_sign_flip_detects_a_consistent_signal():
    rng = random.Random(11)
    strong = [5.0] * 60
    assert sign_flip_p_value(strong, 500, rng) < 0.05
    noise = [rng.gauss(0, 1) for _ in range(60)]
    assert sign_flip_p_value(noise, 500, rng) > 0.1


def test_sign_flip_needs_a_sample():
    assert sign_flip_p_value([], 100, random.Random(1)) == 1.0
    assert sign_flip_p_value([1.0], 100, random.Random(1)) == 1.0


def test_p_value_from_z_is_symmetric():
    assert p_value_from_z(0.0) == pytest.approx(1.0)
    assert p_value_from_z(2.0) == pytest.approx(p_value_from_z(-2.0))
    assert p_value_from_z(10.0) < 0.001


def test_mean_and_stdev_handle_short_inputs():
    assert mean([]) == 0.0
    assert stdev([]) == 0.0
    assert stdev([1.0]) == 0.0
    assert mean([1.0, 2.0, 3.0]) == pytest.approx(2.0)


# --------------------------------------------------------------------------
# look-ahead guard
# --------------------------------------------------------------------------


def test_window_features_ignore_future_bars():
    config = {
        "min_volume_ratio": 1.8,
        "min_z_score": 1.5,
        "min_delta_fraction": 0.25,
        "min_persistence": 0.6,
        "min_notional": 0.0,
        "max_absorption_move_bps": 4.0,
        "confidence_floor": 0.6,
        "baseline_lookback_bars": 90,
    }
    bars = make_bars(300)
    original = window_features(bars, 200, 5, config)
    assert original is not None

    # Rewrite every bar after the evaluation point with wildly different data.
    for index in range(201, len(bars)):
        bars[index]["close"] *= 3.0
        bars[index]["open"] *= 3.0
        bars[index]["volume"] *= 10.0
        bars[index]["buy"] = bars[index]["volume"]
        bars[index]["sell"] = 0.0

    after = window_features(bars, 200, 5, config)
    assert after is not None
    assert after["delta_fraction"] == pytest.approx(original["delta_fraction"])
    assert after["price_move_bps"] == pytest.approx(original["price_move_bps"])
    assert after["volume_ratio"] == pytest.approx(original["volume_ratio"])


def test_forward_outcome_starts_after_the_signal_bar():
    bars = make_bars(50, price_step=0.0)
    bars[10]["close"] = 100.0
    bars[11]["close"] = 101.0
    bars[12]["close"] = 110.0
    outcome = forward_outcome(bars, 10, 2, 1)
    assert outcome is not None
    # Entry is the close of bar 10, exit the close of bar 12.
    assert outcome["forward_bps"] == pytest.approx((110.0 / 100.0 - 1) * 10_000)


def test_forward_outcome_returns_none_past_the_end():
    bars = make_bars(10)
    assert forward_outcome(bars, 8, 5, 1) is None


def test_forward_outcome_short_side_flips_the_sign():
    bars = make_bars(50)
    bars[10]["close"] = 100.0
    bars[11]["close"] = 99.0
    long_outcome = forward_outcome(bars, 10, 1, 1)
    short_outcome = forward_outcome(bars, 10, 1, -1)
    assert long_outcome is not None and short_outcome is not None
    # The raw forward return is side independent; the signed one is not.
    assert long_outcome["forward_bps"] == pytest.approx(short_outcome["forward_bps"])
    assert long_outcome["signed_bps"] == pytest.approx(-short_outcome["signed_bps"])


# --------------------------------------------------------------------------
# hypothesis direction
# --------------------------------------------------------------------------


def test_sweep_follows_the_aggression():
    feature = {"active": True, "pattern": "SWEEP", "side": 1}
    assert hypothesis_side(feature) == 1
    feature["side"] = -1
    assert hypothesis_side(feature) == -1


def test_absorption_fades_the_aggression():
    feature = {"active": True, "pattern": "ABSORPTION", "side": 1}
    assert hypothesis_side(feature) == -1
    feature["side"] = -1
    assert hypothesis_side(feature) == 1


def test_inactive_footprint_has_no_direction():
    assert hypothesis_side({"active": False, "pattern": "SWEEP", "side": 1}) == 0
    assert hypothesis_side({"active": True, "pattern": "NONE", "side": 1}) == 0
    assert hypothesis_side({"active": True, "pattern": "SWEEP", "side": 0}) == 0
