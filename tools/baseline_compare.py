"""Benchmark the paper strategy against random-direction and buy-and-hold baselines.

The strategy only makes money if it beats a random coin flip that uses the same
entry filter, sizing, and costs. This runs that benchmark over the same
candles and the same 5-fold walk-forward windows.

Usage:
    python tools/baseline_compare.py --days 30 --balance 10
"""

from __future__ import annotations

import argparse
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest import run_walk_forward  # noqa: E402
from tools.run_backtest import DEFAULT_OPTIONS, fetch_klines  # noqa: E402


def random_side_backtest(rows, *, starting_balance, fold_count, initial_train_fraction, options, rng):
    """Walk-forward replay where the model side is replaced by a coin flip.

    The flow filter, sizing, stops, targets, hold window, fees, slippage and
    daily loss limit are identical to the real strategy, so the only difference
    is the direction. Any edge in the strategy must show up as a positive
    difference against the average of these runs.
    """
    import backtest as bt

    real_forecast_side = bt._forecast_side

    def fake_forecast_side(score: float, require_clear: bool) -> int:
        return rng.choice((-1, 0, 1))

    bt._forecast_side = fake_forecast_side
    try:
        return run_walk_forward(
            rows,
            starting_balance=starting_balance,
            fold_count=fold_count,
            initial_train_fraction=initial_train_fraction,
            **options,
        )
    finally:
        bt._forecast_side = real_forecast_side


def buy_and_hold(rows, starting_balance: float) -> dict[str, float]:
    closed = [row for row in rows if len(row) > 6 and int(row[6]) < int(datetime.now(timezone.utc).timestamp() * 1000)]
    if len(closed) < 2:
        return {"return_percent": 0.0}
    entry = float(closed[0][1])
    exit_price = float(closed[-1][4])
    return {"return_percent": (exit_price / entry - 1) * 100}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=30, choices=[7, 30])
    parser.add_argument("--balance", type=float, default=10.0)
    parser.add_argument("--runs", type=int, default=20)
    args = parser.parse_args()

    rows = fetch_klines(args.days)
    options = dict(DEFAULT_OPTIONS)
    options["require_validation_edge"] = False

    strategy = run_walk_forward(
        rows, starting_balance=args.balance, fold_count=5, initial_train_fraction=0.5, **options
    )
    strategy_return = float(strategy["return_percent"]) if strategy else 0.0
    strategy_trades = int(strategy["trade_count"]) if strategy else 0

    rng = random.Random(20261019)
    random_returns: list[float] = []
    random_trades: list[int] = []
    for _ in range(args.runs):
        result = random_side_backtest(
            rows,
            starting_balance=args.balance,
            fold_count=5,
            initial_train_fraction=0.5,
            options=options,
            rng=rng,
        )
        if result:
            random_returns.append(float(result["return_percent"]))
            random_trades.append(int(result["trade_count"]))

    bnh = buy_and_hold(rows, args.balance)
    avg_random = sum(random_returns) / len(random_returns) if random_returns else 0.0
    wins = sum(1 for value in random_returns if value > 0)
    avg_random_trades = sum(random_trades) / len(random_trades) if random_trades else 0

    print(f"Window                 : {args.days} days, {args.balance:g} USDT starting balance")
    print(f"Strategy (model side)  : {strategy_return:+.2f}%  ({strategy_trades} trades)")
    print(f"Random side, average   : {avg_random:+.2f}%  ({len(random_returns)} runs, {wins} profitable, "
          f"{avg_random_trades:.0f} trades avg)")
    print(f"Random side, best/worst: {max(random_returns):+.2f}% / {min(random_returns):+.2f}%" if random_returns else "")
    print(f"Buy and hold           : {bnh['return_percent']:+.2f}%")
    print(f"Strategy minus random  : {strategy_return - avg_random:+.2f} percentage points")
    if strategy_trades and avg_random_trades:
        print(
            f"Per trade: strategy {strategy_return / strategy_trades:+.5f}%  "
            f"random {avg_random / avg_random_trades:+.5f}%  "
            f"(strategy trades {strategy_trades / avg_random_trades:.2f}x fewer)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
