from datetime import datetime, timezone

from app import (
    absorption_heuristic,
    calculate_consecutive_losses,
    calculate_conviction,
    calculate_drawdown_pct,
    calculate_unrealized,
    describe_aggressor_strength,
    get_signal,
    summarize_book,
    summarize_flow,
)


def test_summarize_flow_basic():
    now = datetime.now(timezone.utc).timestamp()
    trades = [
        {"time": now - 10, "quantity": 1.0, "sell_aggressor": False},
        {"time": now - 20, "quantity": 2.0, "sell_aggressor": True},
        {"time": now - 30, "quantity": 0.5, "sell_aggressor": False},
    ]
    result = summarize_flow(trades)
    assert result["buy_qty"] == 1.5
    assert result["sell_qty"] == 2.0
    assert result["delta"] == -0.5
    assert result["trade_count"] == 3
    assert result["total"] == 3.5
    assert abs(result["delta_fraction"] - (-0.5 / 3.5)) < 1e-9


def test_summarize_flow_empty():
    assert summarize_flow([])["trade_count"] == 0
    assert summarize_flow([])["delta_fraction"] == 0.0


def test_summarize_book_basic():
    book = {
        "bids": [["100.0", "1.0"], ["99.0", "2.0"]],
        "asks": [["101.0", "1.5"], ["102.0", "0.5"]],
    }
    result = summarize_book(book, levels=5)
    assert result["bid_qty"] == 3.0
    assert result["ask_qty"] == 2.0
    assert abs(result["imbalance"] - (1.0 / 5.0)) < 1e-9
    assert result["mid"] == 100.5
    assert result["spread"] == 1.0


def test_summarize_book_empty_guard():
    result = summarize_book({"bids": [], "asks": []}, levels=5)
    assert result["bid_qty"] == 0.0
    assert result["ask_qty"] == 0.0
    assert result["imbalance"] == 0.0
    assert result["mid"] == 0.0
    assert result["spread"] == 0.0


def test_get_signal_long_flat_short():
    flow = {"delta_fraction": 0.0}
    book = {"imbalance": 0.0}
    assert get_signal(flow, book, 0.2, 0.15) == "FLAT"

    flow = {"delta_fraction": 0.5}
    book = {"imbalance": 0.5}
    assert get_signal(flow, book, 0.2, 0.15) == "LONG"

    flow = {"delta_fraction": -0.5}
    book = {"imbalance": -0.5}
    assert get_signal(flow, book, 0.2, 0.15) == "SHORT"


def test_calculate_unrealized_long_profit():
    position = {"side": 1, "entry": 100.0, "quantity": 1.0, "entry_fee": 0.5}
    pnl = calculate_unrealized(position, 101.0, fee_bps=5.0, slippage_bps=1.0)
    assert pnl > 0


def test_calculate_drawdown_pct():
    curve = [{"eq": 100.0}, {"eq": 110.0}, {"eq": 90.0}, {"eq": 105.0}]
    dd = calculate_drawdown_pct(curve)
    assert abs(dd - ((110.0 - 90.0) / 110.0 * 100.0)) < 1e-9


def test_calculate_drawdown_pct_empty():
    assert calculate_drawdown_pct([]) == 0.0


def test_calculate_consecutive_losses():
    trades = [
        {"Net PnL (USDT)": 10.0},
        {"Net PnL (USDT)": -5.0},
        {"Net PnL (USDT)": -3.0},
        {"Net PnL (USDT)": 2.0},
        {"Net PnL (USDT)": -1.0},
        {"Net PnL (USDT)": -2.0},
    ]
    assert calculate_consecutive_losses(trades) == 2


def test_describe_aggressor_strength_strong_buyer():
    flow = {"trade_count": 10, "total": 5.0, "delta_fraction": 0.5}
    assert describe_aggressor_strength(flow, 0.2, 5, 1.0) == "Strong buyer aggression"


def test_describe_aggressor_strength_strong_seller():
    flow = {"trade_count": 10, "total": 5.0, "delta_fraction": -0.5}
    assert describe_aggressor_strength(flow, 0.2, 5, 1.0) == "Strong seller aggression"


def test_describe_aggressor_strength_insufficient():
    flow = {"trade_count": 2, "total": 0.5, "delta_fraction": 0.5}
    assert describe_aggressor_strength(flow, 0.2, 5, 1.0) == "Insufficient 60s flow"


def test_describe_aggressor_strength_below_threshold():
    flow = {"trade_count": 10, "total": 5.0, "delta_fraction": 0.1}
    assert describe_aggressor_strength(flow, 0.2, 5, 1.0) == "Buyers lead, below strong threshold"


def test_absorption_heuristic_no_alert_when_weak_flow():
    now = datetime.now(timezone.utc).timestamp()
    recent = [{"time": now - 5, "price": 100.0}]
    flow = {"delta": 0.1, "delta_fraction": 0.1}
    assert absorption_heuristic(recent, flow) is None


def test_absorption_heuristic_flags_buyer_absorption():
    now = datetime.now(timezone.utc).timestamp()
    # strong buyer delta but flat price
    recent = [
        {"time": now - 30, "price": 100.00},
        {"time": now - 20, "price": 100.01},
        {"time": now - 10, "price": 100.00},
        {"time": now - 1, "price": 100.01},
    ]
    flow = {"delta": 1.0, "delta_fraction": 0.8}
    alert = absorption_heuristic(recent, flow)
    assert alert is not None
    assert "buyer" in alert["message"].lower()


def test_absorption_heuristic_no_alert_when_price_moves():
    now = datetime.now(timezone.utc).timestamp()
    recent = [
        {"time": now - 30, "price": 100.00},
        {"time": now - 20, "price": 100.50},
        {"time": now - 10, "price": 101.00},
        {"time": now - 1, "price": 101.50},
    ]
    flow = {"delta": 1.0, "delta_fraction": 0.8}
    assert absorption_heuristic(recent, flow) is None


def test_calculate_conviction_requires_actionable_setup():
    flow = {"delta_fraction": 0.5, "trade_count": 30}
    book = {"imbalance": 0.5}
    result = calculate_conviction(
        "FLAT",
        flow,
        book,
        "UP_CLEAR",
        0.8,
        {"active": False, "confidence": 0.0},
        min_flow=0.2,
        min_book=0.15,
        flow_ok=True,
        stale_data=False,
    )
    assert result["score"] == 0
    assert result["components"] == {}


def test_calculate_conviction_strong_agreement_scores_high():
    flow = {"delta_fraction": 0.6, "trade_count": 40}
    book = {"imbalance": 0.5}
    result = calculate_conviction(
        "LONG",
        flow,
        book,
        "UP_CLEAR",
        0.85,
        {"active": True, "confidence": 0.9},
        min_flow=0.2,
        min_book=0.15,
        flow_ok=True,
        stale_data=False,
    )
    assert result["score"] >= 75
    assert result["components"]["Model agreement"] == 1.0


def test_calculate_conviction_model_conflict_reduces_score():
    flow = {"delta_fraction": 0.6, "trade_count": 40}
    book = {"imbalance": 0.5}
    agreeing = calculate_conviction(
        "LONG",
        flow,
        book,
        "UP_CLEAR",
        0.85,
        {"active": True, "confidence": 0.9},
        min_flow=0.2,
        min_book=0.15,
        flow_ok=True,
        stale_data=False,
    )
    conflicting = calculate_conviction(
        "LONG",
        flow,
        book,
        "DOWN_CLEAR",
        0.85,
        {"active": True, "confidence": 0.9},
        min_flow=0.2,
        min_book=0.15,
        flow_ok=True,
        stale_data=False,
    )
    assert conflicting["score"] < agreeing["score"]


def test_calculate_conviction_stale_data_scores_zero():
    flow = {"delta_fraction": 0.6, "trade_count": 40}
    book = {"imbalance": 0.5}
    result = calculate_conviction(
        "LONG",
        flow,
        book,
        "UP_CLEAR",
        0.85,
        {"active": True, "confidence": 0.9},
        min_flow=0.2,
        min_book=0.15,
        flow_ok=True,
        stale_data=True,
    )
    assert result["score"] == 0
