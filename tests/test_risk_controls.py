from app import calculate_consecutive_losses, calculate_drawdown_pct


def test_calculate_consecutive_losses_counts_streak() -> None:
    trades = [
        {"Net PnL (USDT)": 5.0},
        {"Net PnL (USDT)": -1.0},
        {"Net PnL (USDT)": -2.5},
        {"Net PnL (USDT)": -3.0},
        {"Net PnL (USDT)": 2.0},
        {"Net PnL (USDT)": -1.0},
    ]

    assert calculate_consecutive_losses(trades) == 3


def test_calculate_drawdown_pct_tracks_peak_to_trough() -> None:
    equity_curve = [
        {"eq": 1000.0},
        {"eq": 1100.0},
        {"eq": 950.0},
        {"eq": 900.0},
        {"eq": 980.0},
    ]

    assert calculate_drawdown_pct(equity_curve) == 18.181818181818183
