from app import (
    REFRESH_SECONDS,
    STALE_AFTER_MS,
    aggregate_trade_candles,
    append_open_interest_sample,
    assess_feed_ages,
    build_why_inputs,
    calculate_fee_adjusted_trade_metrics,
    calculate_market_levels,
    calculate_microprice,
    calculate_venue_lead_lag,
    describe_decision_confidence,
    describe_decision_reason,
    detect_market_alerts,
    describe_aggressor_strength,
    normalize_agg_trade,
    interpret_price_open_interest,
    normalize_force_order,
    summarize_book,
    summarize_depth_curve,
    summarize_flow,
    summarize_flow_intervals,
    summarize_liquidations,
    update_feed_state,
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


def test_summarize_depth_curve_uses_signed_distance_and_cumulative_size() -> None:
    book = {
        "mid": 100.0,
        "bids": [(99.9, 1.0), (99.8, 2.0)],
        "asks": [(100.1, 0.5), (100.2, 1.5)],
    }

    curve = summarize_depth_curve(book)

    assert [row["Cumulative size (BTC)"] for row in curve if row["Side"] == "Bid"] == [1.0, 3.0]
    assert [row["Cumulative size (BTC)"] for row in curve if row["Side"] == "Ask"] == [0.5, 2.0]
    assert all(row["Distance from mid (bps)"] < 0 for row in curve if row["Side"] == "Bid")
    assert all(row["Distance from mid (bps)"] > 0 for row in curve if row["Side"] == "Ask")


def test_force_order_parser_and_60_second_liquidation_totals() -> None:
    long_liquidation = normalize_force_order(
        {"e": "forceOrder", "E": 100_000, "o": {"S": "SELL", "z": "0.2", "ap": "50000"}}
    )
    short_liquidation = normalize_force_order(
        {"e": "forceOrder", "E": 110_000, "o": {"S": "BUY", "z": "0.1", "ap": "51000"}}
    )
    assert long_liquidation is not None and long_liquidation["liquidated"] == "longs"
    assert short_liquidation is not None and short_liquidation["liquidated"] == "shorts"
    totals = summarize_liquidations([long_liquidation, short_liquidation], now=120.0)
    assert totals["long_usdt"] == 10_000.0
    assert totals["short_usdt"] == 5_100.0
    assert totals["total_usdt"] == 15_100.0
    assert totals["count"] == 2
    assert normalize_force_order({"e": "aggTrade"}) is None


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


def test_summarize_flow_uses_one_sample_for_sides_delta_and_intervals() -> None:
    trades = [
        {"time": 999.0, "quantity": 1.25, "sell_aggressor": False},
        {"time": 995.0, "quantity": 0.75, "sell_aggressor": True},
        {"time": 990.0, "quantity": 0.50, "sell_aggressor": False},
        {"time": 939.0, "quantity": 100.0, "sell_aggressor": False},
        {"time": 1001.0, "quantity": 200.0, "sell_aggressor": True},
    ]

    flow = summarize_flow(trades, now=1000.0)
    intervals = flow["intervals"]

    assert flow["buy_qty"] == 1.75
    assert flow["sell_qty"] == 0.75
    assert flow["delta"] == flow["buy_qty"] - flow["sell_qty"] == 1.0
    assert sum(row["Buyer-initiated (BTC)"] for row in intervals) == flow["buy_qty"]
    assert sum(row["Seller-initiated (BTC)"] for row in intervals) == flow["sell_qty"]
    assert sum(row["Net delta (BTC)"] for row in intervals) == flow["delta"]
    assert flow["delta"] / len(intervals) == 1.0 / 6


def test_assess_feed_ages_tolerates_at_least_one_full_poll_cycle() -> None:
    # The staleness threshold has to sit above the refresh interval, otherwise a
    # healthy feed reads as stale on most refreshes, which disables paper
    # entries and skips stop/target checks.
    assert STALE_AFTER_MS > REFRESH_SECONDS * 1000

    fresh = assess_feed_ages(last_trade_time=98.1, last_book_update=99.0, now=100.0)
    assert round(fresh["trade_age_ms"]) == 1900
    assert round(fresh["book_age_ms"]) == 1000
    assert fresh["stale"] is False
    assert fresh["stale_after_ms"] == STALE_AFTER_MS

    # One missed poll (3s) must not be enough to call the feed stale.
    one_missed_poll = assess_feed_ages(last_trade_time=97.0, last_book_update=97.0, now=100.0)
    assert one_missed_poll["stale"] is False

    truly_stale = assess_feed_ages(
        last_trade_time=100.0 - (STALE_AFTER_MS / 1000) - 0.1,
        last_book_update=99.0,
        now=100.0,
    )
    assert truly_stale["stale"] is True
    assert "Binance trades stale" in truly_stale["stale_reasons"]

    missing_book = assess_feed_ages(last_trade_time=99.0, last_book_update=None, now=100.0)
    assert missing_book["stale"] is True
    assert "Binance book stale" in missing_book["stale_reasons"]


def test_fee_adjusted_trade_metrics_include_round_trip_costs() -> None:
    result = calculate_fee_adjusted_trade_metrics(stop_bps=20.0, reward_risk=2.0, fee_bps=5.0, slippage_bps=1.0)

    assert result["round_trip_cost_bps"] == 12.0
    assert result["reward_risk"] == 28.0 / 32.0
    assert result["breakeven_win_rate"] == 32.0 / 60.0


def test_microprice_weights_touch_prices_by_opposite_queue_size() -> None:
    result = calculate_microprice({"bids": [[100.0, 3.0]], "asks": [[102.0, 1.0]]})

    assert result == 101.5
    assert calculate_microprice({"bids": [], "asks": []}) is None


def test_interpret_price_open_interest_covers_four_regimes() -> None:
    assert "new longs" in interpret_price_open_interest(0.2, 0.1)
    assert "new shorts" in interpret_price_open_interest(-0.2, 0.1)
    assert "squeeze" in interpret_price_open_interest(0.2, -0.1)
    assert "long liquidation" in interpret_price_open_interest(-0.2, -0.1)


def test_append_open_interest_sample_calculates_60_second_change() -> None:
    history = [{"time": 1_000.0, "open_interest": 100.0}, {"time": 1_030.0, "open_interest": 102.0}]

    updated, change = append_open_interest_sample(history, current_oi=105.0, now=1_061.0)

    assert len(updated) == 3
    assert change is not None and abs(change - 5.0) < 1e-9
    _, warming_up = append_open_interest_sample([], current_oi=105.0, now=1_061.0)
    assert warming_up is None


def test_calculate_market_levels_uses_trade_vwap_and_daily_kline_bounds() -> None:
    trades = [
        {"price": 100.0, "quantity": 1.0},
        {"price": 102.0, "quantity": 3.0},
    ]
    daily_klines = [[0, "99", "110", "90", "105"], [1, "105", "120", "100", "115"]]

    levels = calculate_market_levels(trades, daily_klines, mark=82_730.0)

    assert levels["rolling_vwap"] == 101.5
    assert levels["session_high"] == 120.0
    assert levels["session_low"] == 100.0
    assert levels["previous_day_high"] == 110.0
    assert levels["previous_day_low"] == 90.0
    assert levels["round_500"] == 82_500
    assert levels["round_1000"] == 83_000


def test_calculate_venue_lead_lag_detects_coinbase_lead() -> None:
    spot_prices = [100.0, 101.0, 99.0, 102.0, 98.0, 103.0, 97.0, 104.0, 96.0]
    history = [
        {"time": index * 3.0, "spot": spot_price, "perp": spot_prices[max(0, index - 1)]}
        for index, spot_price in enumerate(spot_prices)
    ]

    result = calculate_venue_lead_lag(history, max_lag=1)

    assert result["leader"] == "Coinbase spot"
    assert result["lag_seconds"] == 3.0
    assert result["correlation"] == 1.0
    assert calculate_venue_lead_lag(history[:3])["leader"] == "Warming up"


def test_decision_confidence_is_heuristic_and_reason_explains_wait() -> None:
    flow = {"delta_fraction": 0.3}
    book = {"imbalance": -0.2}

    assert describe_decision_confidence("LONG", "UP_CLEAR", True, False) == "High · heuristic"
    assert describe_decision_confidence("LONG", "DOWN_CLEAR", True, False) == "Moderate · heuristic"
    assert describe_decision_confidence("LONG", "UP_CLEAR", True, True) == "Unavailable · stale data"
    assert describe_decision_reason(
        "FLAT", flow, book, "NO CLEAR EDGE", stale_data=False, risk_locked=False,
        forecast_below=False, flow_ok=True,
    ) == "WAIT: buyers, ask depth heavier, no flow/book confirmation."
    why = build_why_inputs(flow, book, 100.02, 101.0, "UP_WEAK", 100.0)
    assert why["conflict"] is True
    assert next(row for row in why["rows"] if row["Input"] == "Liquidation pressure")["Bias"] == "Neutral"


def test_detect_market_alerts_only_fires_on_transitions() -> None:
    previous = {"stale": False, "flow_side": "buy", "signal": None, "near_zone": False}
    alerts, state = detect_market_alerts(previous, "SHORT", -1.0, True, [3.0], 5.0)

    assert len(alerts) == 4
    assert state == {"stale": True, "flow_side": "sell", "signal": "SHORT", "near_zone": True}
    repeated, _ = detect_market_alerts(state, "SHORT", -2.0, True, [2.0], 5.0)
    assert repeated == []


def test_why_panel_classifies_liquidations_and_funding() -> None:
    why = build_why_inputs(
        {"delta_fraction": 0.0},
        {"imbalance": 0.0},
        None,
        None,
        "NO CLEAR EDGE",
        100.0,
        price_change_pct=-0.2,
        oi_change_pct=0.5,
        funding_rate=0.001,
        liquidation_totals={"long_usdt": 10_000.0, "short_usdt": 2_000.0, "total_usdt": 12_000.0},
    )

    rows = {row["Input"]: row for row in why["rows"]}
    assert rows["Price vs open interest"]["Reading"].startswith("Price down + OI up")
    assert rows["Funding rate"]["Bias"] == "Bearish"
    assert rows["Liquidation pressure"]["Bias"] == "Bearish"


def test_binance_aggtrade_m_flag_maps_to_aggressor_side() -> None:
    buyer_aggressor = normalize_agg_trade({"a": 1, "T": 10_000, "p": "100", "q": "0.4", "m": False})
    seller_aggressor = normalize_agg_trade({"a": 2, "T": 11_000, "p": "101", "q": "0.6", "m": True})

    assert buyer_aggressor["sell_aggressor"] is False
    assert seller_aggressor["sell_aggressor"] is True
    flow = summarize_flow([buyer_aggressor, seller_aggressor], now=12.0)
    assert flow["buy_qty"] == 0.4
    assert flow["sell_qty"] == 0.6
    assert abs(flow["delta"] + 0.2) < 1e-9


def test_update_feed_state_tracks_disconnect_reconnect_and_last_good_data() -> None:
    feed_health = {
        "binance_trades": {
            "connected": True,
            "ever_connected": True,
            "last_success": 1.0,
            "last_error": None,
            "reconnect_count": 0,
        },
        "binance_book": {
            "connected": False,
            "ever_connected": False,
            "last_success": None,
            "last_error": None,
            "reconnect_count": 0,
        },
        "coinbase_spot": {
            "connected": False,
            "ever_connected": False,
            "last_success": None,
            "last_error": None,
            "reconnect_count": 0,
        },
        "usdt_usd": {
            "connected": False,
            "ever_connected": False,
            "last_success": None,
            "last_error": None,
            "reconnect_count": 0,
        },
        "binance_open_interest": {
            "connected": False,
            "ever_connected": False,
            "last_success": None,
            "last_error": None,
            "reconnect_count": 0,
        },
        "binance_funding": {
            "connected": False,
            "ever_connected": False,
            "last_success": None,
            "last_error": None,
            "reconnect_count": 0,
        },
        "binance_daily_klines": {
            "connected": False,
            "ever_connected": False,
            "last_success": None,
            "last_error": None,
            "reconnect_count": 0,
        },
    }
    market_cache = {name: None for name in feed_health}

    update_feed_state(feed_health, market_cache, {}, {"binance_trades": "timeout"}, now=2.0)
    update_feed_state(feed_health, market_cache, {"binance_trades": [{"a": 3}]}, {}, now=3.0)

    assert feed_health["binance_trades"]["connected"] is True
    assert feed_health["binance_trades"]["reconnect_count"] == 1
    assert feed_health["binance_trades"]["last_success"] == 3.0
    assert market_cache["binance_trades"] == [{"a": 3}]


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


def test_aggregate_trade_candles_calculates_volume_and_cvd() -> None:
    trades = [
        {"time": 1000.1, "price": 100.0, "quantity": 0.4, "sell_aggressor": False},
        {"time": 1000.8, "price": 101.0, "quantity": 0.1, "sell_aggressor": True},
        {"time": 1001.1, "price": 99.0, "quantity": 0.6, "sell_aggressor": True},
    ]

    candles = aggregate_trade_candles(trades, now=1002.0, window_seconds=5, interval_seconds=1)

    assert [candle["Volume (BTC)"] for candle in candles] == [0.5, 0.6]
    assert [round(candle["Delta (BTC)"], 9) for candle in candles] == [0.3, -0.6]
    assert [round(candle["CVD (BTC)"], 9) for candle in candles] == [0.3, -0.3]
