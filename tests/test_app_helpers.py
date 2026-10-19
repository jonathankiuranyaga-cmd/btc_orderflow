from app import (
    aggregate_trade_candles,
    describe_aggressor_strength,
    summarize_book,
    summarize_flow,
    summarize_flow_intervals,
)


def test_summarize_book_handles_empty_book() -> None:
    result = summarize_book({"bids": [], "asks": []}, 5)

    assert result["bids"] == []
    assert result["asks"] == []
    assert result["bid_qty"] == 0.0
    assert result["ask_qty"] == 0.0
    assert result["imbalance"] == 0.0
    assert result["mid"] == 0.0
    assert result["spread"] == 0.0


def test_summarize_flow_handles_empty_trade_list() -> None:
    result = summarize_flow([])

    assert result["buy_qty"] == 0.0
    assert result["sell_qty"] == 0.0
    assert result["delta"] == 0.0
    assert result["delta_fraction"] == 0.0
    assert result["trade_count"] == 0
    assert result["total"] == 0.0


def test_describe_aggressor_strength_identifies_strong_side_and_sample_quality() -> None:
    thresholds = (0.2, 5, 1.0)

    assert describe_aggressor_strength(
        {"trade_count": 10, "total": 4.0, "delta_fraction": 0.3}, *thresholds
    ) == "Strong buyer aggression"
    assert describe_aggressor_strength(
        {"trade_count": 10, "total": 4.0, "delta_fraction": -0.3}, *thresholds
    ) == "Strong seller aggression"
    assert describe_aggressor_strength(
        {"trade_count": 10, "total": 4.0, "delta_fraction": 0.1}, *thresholds
    ) == "Buyers lead, below strong threshold"
    assert describe_aggressor_strength(
        {"trade_count": 2, "total": 0.5, "delta_fraction": 0.9}, *thresholds
    ) == "Insufficient 60s flow"


def test_summarize_flow_intervals_returns_six_10_second_buckets() -> None:
    trades = [
        {"time": 945.0, "quantity": 1.0, "sell_aggressor": False},
        {"time": 955.0, "quantity": 2.0, "sell_aggressor": True},
        {"time": 965.0, "quantity": 3.0, "sell_aggressor": False},
        {"time": 975.0, "quantity": 1.0, "sell_aggressor": True},
        {"time": 985.0, "quantity": 2.0, "sell_aggressor": False},
        {"time": 995.0, "quantity": 4.0, "sell_aggressor": False},
        {"time": 995.0, "quantity": 1.0, "sell_aggressor": True},
        {"time": 930.0, "quantity": 100.0, "sell_aggressor": False},
    ]

    intervals = summarize_flow_intervals(trades, now=1000.0)

    assert len(intervals) == 6
    assert intervals[0]["Interval"] == "50-60s ago"
    assert intervals[-1]["Interval"] == "0-10s ago"
    assert [row["Net delta (BTC)"] for row in intervals] == [1.0, -2.0, 3.0, -1.0, 2.0, 3.0]
    assert sum(row["Net delta (BTC)"] for row in intervals) / 6 == 1.0


def test_aggregate_trade_candles_builds_10_second_ohlc_bars() -> None:
    trades = [
        {"time": 1001.0, "price": 100.0},
        {"time": 1004.0, "price": 105.0},
        {"time": 1007.0, "price": 98.0},
        {"time": 1009.0, "price": 102.0},
        {"time": 1012.0, "price": 101.0},
        {"time": 990.0, "price": 50.0},
        {"time": 1061.0, "price": 200.0},
    ]

    candles = aggregate_trade_candles(trades, now=1060.0, window_seconds=60)

    assert len(candles) == 2
    assert candles[0]["Open"] == 100.0
    assert candles[0]["High"] == 105.0
    assert candles[0]["Low"] == 98.0
    assert candles[0]["Close"] == 102.0
    assert candles[0]["Direction"] == "Up"
    assert candles[0]["Trades"] == 4
    assert candles[1]["Open"] == candles[1]["High"] == candles[1]["Low"] == candles[1]["Close"] == 101.0
