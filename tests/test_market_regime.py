"""Tests for the market-regime read."""

from market_regime import BAND_FAIR, BAND_GOOD, BAND_POOR, calculate_market_regime, describe_market_regime

NOW = 1_000_000.0


def _trade(time_offset: float, price: float, sell_aggressor: bool = False) -> dict:
    return {"time": NOW + time_offset, "price": price, "quantity": 1.0, "sell_aggressor": sell_aggressor}


def test_calculate_market_regime_empty_inputs() -> None:
    regime = calculate_market_regime([], {"bids": [], "asks": []}, 100_000.0, NOW, 5.0, 1.0)

    assert regime["band"] == BAND_POOR
    assert regime["volatility_bps"] == 0.0
    assert regime["participation_per_sec"] == 0.0
    assert regime["spread_bps"] == 0.0
    assert regime["trade_count"] == 0
    assert regime["median_move_clears_cost"] is False


def test_calculate_market_regime_spread_only() -> None:
    book = {"bids": [(99_990.0, 1.0)], "asks": [(100_010.0, 1.0)]}
    regime = calculate_market_regime([], book, 100_000.0, NOW, 5.0, 1.0)

    # 20 USDT spread on 100_000 = 2 bps
    assert regime["spread_bps"] == 2.0
    assert regime["band"] == BAND_POOR


def test_calculate_market_regime_good_band() -> None:
    trades = [_trade(i * 0.25 - 30, 100_000.0 + i * 50.0) for i in range(120)]
    book = {"bids": [(99_999.0, 1.0)], "asks": [(100_001.0, 1.0)]}
    regime = calculate_market_regime(trades, book, 100_000.0, NOW, 1.0, 0.5)

    assert regime["band"] == BAND_GOOD
    assert regime["median_move_clears_cost"] is True
    assert regime["participation_per_sec"] > 0


def test_calculate_market_regime_poor_band_high_cost() -> None:
    trades = [_trade(i * 0.5 - 30, 100_000.0 + i * 0.5) for i in range(60)]
    book = {"bids": [(99_999.0, 1.0)], "asks": [(100_001.0, 1.0)]}
    regime = calculate_market_regime(trades, book, 100_000.0, NOW, 20.0, 20.0)

    assert regime["band"] == BAND_POOR
    assert regime["median_move_clears_cost"] is False


def test_describe_market_regime_returns_text() -> None:
    assert "Scalping regime" in describe_market_regime({"band": BAND_GOOD})
    assert "Scalping regime" in describe_market_regime({"band": BAND_FAIR})
    assert "Scalping regime" in describe_market_regime({"band": BAND_POOR})
    assert "Scalping regime" in describe_market_regime({"band": "Unknown"})


def test_calculate_market_regime_move_to_cost_below_one() -> None:
    trades = [_trade(i * 0.5 - 30, 100_000.0 + i * 0.5) for i in range(60)]
    book = {"bids": [(99_999.0, 1.0)], "asks": [(100_001.0, 1.0)]}
    regime = calculate_market_regime(trades, book, 100_000.0, NOW, 5.0, 1.0)

    assert regime["move_to_cost"] < 1.0
    assert regime["band"] == BAND_POOR
