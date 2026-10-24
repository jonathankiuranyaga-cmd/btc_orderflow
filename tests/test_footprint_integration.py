"""Tests for the dashboard-side footprint integration.

The engine itself is covered in tests/test_footprint.py. These cover the glue:
that the book-depth history is actually recorded per poll, that it is trimmed
rather than growing without bound, and that the detector consumes it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import record_book_depth_sample  # noqa: E402
from footprint import detect_large_aggressor  # noqa: E402


def make_book(bid_qty: float, ask_qty: float, mid: float = 100.0) -> dict:
    return {"bid_qty": bid_qty, "ask_qty": ask_qty, "mid": mid}


def test_book_depth_sample_is_recorded_with_a_timestamp():
    history = record_book_depth_sample([], make_book(5.0, 3.0), now=1000.0)
    assert len(history) == 1
    assert history[0]["time"] == 1000.0
    assert history[0]["bid_qty"] == 5.0
    assert history[0]["ask_qty"] == 3.0
    assert history[0]["mid"] == 100.0


def test_book_depth_history_accumulates_across_polls():
    history: list[dict[str, float]] = []
    for index in range(5):
        history = record_book_depth_sample(history, make_book(5.0, 3.0), now=1000.0 + index)
    assert len(history) == 5
    assert [entry["time"] for entry in history] == [1000.0, 1001.0, 1002.0, 1003.0, 1004.0]


def test_book_depth_history_is_trimmed_to_the_retention_window():
    history: list[dict[str, float]] = []
    for index in range(10):
        history = record_book_depth_sample(history, make_book(5.0, 3.0), now=1000.0 + index)
    # Retention of 4 seconds keeps samples at or after now - 4, then appends.
    history = record_book_depth_sample(history, make_book(5.0, 3.0), now=1010.0, retention_seconds=4.0)
    assert [entry["time"] for entry in history] == [1006.0, 1007.0, 1008.0, 1009.0, 1010.0]


def test_book_depletion_is_detected_from_the_recorded_history():
    history: list[dict[str, float]] = []
    for index in range(12):
        # Resting asks thin out over the window while the price holds.
        history = record_book_depth_sample(history, make_book(50.0, 80.0 - index * 5), now=1000.0 + index)
    footprint = detect_large_aggressor(
        [
            {"time": 1011.0, "price": 100.0, "quantity": 1.0, "sell_aggressor": False}
            for _ in range(30)
        ],
        now=1011.0,
        book_depth_history=history,
    )
    depletion = footprint["book_depletion"]
    assert depletion is not None
    assert depletion["passive_side"] == "asks"
    assert depletion["depleted"] is True
    assert depletion["change_pct"] < 0


def test_detector_consumes_the_history_without_raising():
    history = [record_book_depth_sample([], make_book(5.0, 3.0), now=1000.0 + i) for i in range(3)]
    footprint = detect_large_aggressor(
        [
            {"time": 1002.0, "price": 100.0, "quantity": 0.5, "sell_aggressor": index % 2 == 0}
            for index in range(40)
        ],
        now=1002.0,
        book_depth_history=history,
    )
    assert "pattern" in footprint
    assert "confidence" in footprint
    assert footprint["window_seconds"] == 150


def test_empty_history_is_accepted():
    footprint = detect_large_aggressor(
        [{"time": 1000.0, "price": 100.0, "quantity": 1.0, "sell_aggressor": False}],
        now=1000.0,
        book_depth_history=[],
    )
    assert footprint["book_depletion"] is None
