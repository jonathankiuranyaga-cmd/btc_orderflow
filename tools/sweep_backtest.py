"""Sweep backtest configurations and report the ones that hold up out of sample.

Every configuration is evaluated with the same 5-fold walk-forward so the
reported return is out-of-sample. A configuration is only interesting when it
is profitable in most folds, not just one.

Usage:
    python tools/sweep_backtest.py --days 30
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest import run_walk_forward  # noqa: E402
from tools.run_backtest import DEFAULT_OPTIONS, fetch_klines  # noqa: E402

SWEEP = [
    # label, overrides
    ("baseline (gate on)", {}),
    ("baseline (gate off)", {"require_validation_edge": False}),
    ("RR 1.0", {"reward_risk": 1.0, "require_validation_edge": False}),
    ("RR 1.5", {"reward_risk": 1.5, "require_validation_edge": False}),
    ("RR 3.0", {"reward_risk": 3.0, "require_validation_edge": False}),
    ("stop 10 bps, RR 1.0", {"stop_bps": 10.0, "reward_risk": 1.0, "require_validation_edge": False}),
    ("stop 10 bps, RR 1.5", {"stop_bps": 10.0, "reward_risk": 1.5, "require_validation_edge": False}),
    ("stop 40 bps, RR 1.0", {"stop_bps": 40.0, "reward_risk": 1.0, "require_validation_edge": False}),
    ("max hold 10", {"max_hold_bars": 10, "require_validation_edge": False}),
    ("max hold 20", {"max_hold_bars": 20, "require_validation_edge": False}),
    ("max hold 20, RR 1.5", {"max_hold_bars": 20, "reward_risk": 1.5, "require_validation_edge": False}),
    ("max hold 20, stop 40, RR 1.5", {"max_hold_bars": 20, "stop_bps": 40.0, "reward_risk": 1.5, "require_validation_edge": False}),
    ("max hold 20, RR 1.0, flow 0.10", {"max_hold_bars": 20, "reward_risk": 1.0, "min_flow": 0.10, "require_validation_edge": False}),
    ("max hold 20, RR 1.0, flow 0.40", {"max_hold_bars": 20, "reward_risk": 1.0, "min_flow": 0.40, "require_validation_edge": False}),
    ("no fees, no slippage", {"fee_bps": 0.0, "slippage_bps": 0.0, "require_validation_edge": False}),
    ("no fees, no slip, max hold 20, RR 1.5", {"fee_bps": 0.0, "slippage_bps": 0.0, "max_hold_bars": 20, "reward_risk": 1.5, "require_validation_edge": False}),
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=30, choices=[7, 30])
    parser.add_argument("--balance", type=float, default=10.0)
    args = parser.parse_args()

    rows = fetch_klines(args.days)
    print(f"{args.days}d candles: {len(rows)} rows\n")

    header = (
        f"{'configuration':<38} {'return':>9} {'trades':>7} {'win%':>7} {'PF':>7} "
        f"{'maxDD':>7} {'prof folds':>11} {'fees':>8}"
    )
    print(header)
    print("-" * len(header))

    for label, overrides in SWEEP:
        options = dict(DEFAULT_OPTIONS)
        options.update(overrides)
        result = run_walk_forward(
            rows,
            starting_balance=args.balance,
            fold_count=5,
            initial_train_fraction=0.5,
            **options,
        )
        if result is None:
            print(f"{label:<38} {'n/a':>9}")
            continue
        trades = result.get("trades") or []
        fees = sum(float(trade["Fees (USDT)"]) for trade in trades)
        profit_factor = result["profit_factor"]
        pf_text = f"{profit_factor:.2f}" if profit_factor != float("inf") else "inf"
        print(
            f"{label:<38} {result['return_percent']:>+8.2f}% {result['trade_count']:>7d} "
            f"{result['win_rate']:>6.1f}% {pf_text:>7} {result['max_drawdown_percent']:>6.2f}% "
            f"{result['profitable_folds']:>6d}/{result['fold_count']:<4d} {fees:>8.4f}"
        )

    print("\nAll returns are out-of-sample walk-forward results on untouched test folds.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
