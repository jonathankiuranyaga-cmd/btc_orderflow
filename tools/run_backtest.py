"""Headless backtest runner for the paper strategy.

Usage:
    python tools/run_backtest.py --days 30 --balance 10
    python tools/run_backtest.py --days 7 --balance 10 --mode holdout
    python tools/run_backtest.py --days 30 --balance 10 --no-gate --no-clear

Defaults mirror the dashboard sidebar so the numbers line up with what the app
shows when the same settings are used.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest import run_backtest, run_walk_forward  # noqa: E402

BINANCE_BASE = "https://fapi.binance.com"
SYMBOL = "BTCUSDT"

# Dashboard sidebar defaults.
DEFAULT_OPTIONS = {
    "risk_percent": 0.5,
    "max_leverage": 1.0,
    "stop_bps": 20.0,
    "reward_risk": 2.0,
    "fee_bps": 5.0,
    "slippage_bps": 1.0,
    "daily_loss_percent": 2.0,
    "min_flow": 0.20,
    "min_trades": 5,
    "min_total_btc": 1.0,
    "require_clear": True,
    "require_validation_edge": True,
    "max_hold_bars": 5,
}


def fetch_klines(days: int) -> list[list]:
    import json

    import requests

    cache = Path(__file__).resolve().parent.parent / ".cache" / f"klines_{days}d.json"
    if cache.exists():
        age_hours = (datetime.now(timezone.utc).timestamp() - cache.stat().st_mtime) / 3600
        if age_hours < 1:
            with cache.open(encoding="utf-8") as handle:
                return json.load(handle)

    session = requests.Session()
    end_time = int(datetime.now(timezone.utc).timestamp() * 1000)
    cursor = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
    rows: list[list] = []
    while cursor < end_time:
        batch = session.get(
            f"{BINANCE_BASE}/fapi/v1/klines",
            params={
                "symbol": SYMBOL,
                "interval": "1m",
                "startTime": cursor,
                "endTime": end_time,
                "limit": 1000,
            },
            timeout=20,
        ).json()
        if not batch:
            break
        rows.extend(batch)
        next_cursor = int(batch[-1][0]) + 60_000
        if next_cursor <= cursor or len(batch) < 1000:
            break
        cursor = next_cursor

    cache.parent.mkdir(parents=True, exist_ok=True)
    with cache.open("w", encoding="utf-8") as handle:
        json.dump(rows, handle)
    return rows


def fmt_pct(value: float) -> str:
    return f"{value:+.2f}%"


def print_result(result: dict | None, label: str) -> bool:
    if result is None:
        print(f"\n=== {label} ===\nNo result: not enough closed candles for this configuration.")
        return False

    print(f"\n=== {label} ===")
    print(f"Starting balance      : {result['starting_balance']:.4f} USDT")
    print(f"Ending balance        : {result['ending_balance']:.4f} USDT")
    print(f"Net PnL               : {result['net_pnl']:+.4f} USDT")
    print(f"Return                : {fmt_pct(result['return_percent'])}")
    print(f"Trades                : {result['trade_count']}  (W {result['wins']} / L {result['losses']})")
    print(f"Win rate              : {result['win_rate']:.1f}%")
    profit_factor = result["profit_factor"]
    print(f"Profit factor         : {profit_factor:.3f}" if profit_factor != float("inf") else "Profit factor         : inf")
    print(f"Max drawdown          : {result['max_drawdown_percent']:.2f}%")
    print(f"Model accuracy (test) : {result['model_accuracy']:.2%}  vs baseline {result['majority_baseline']:.2%}")
    print(
        f"Gate (pre-test)       : accuracy {result['training_validation_accuracy']:.2%} "
        f"vs baseline {result['training_validation_baseline']:.2%}"
        + (
            f" on {result['training_validation_samples']} samples"
            if "training_validation_samples" in result
            else ""
        )
    )
    if "folds" in result:
        print(f"Folds passing gate    : {result['gate_passing_folds']}/{result['fold_count']}")
        print(f"Profitable folds      : {result['profitable_folds']}/{result['fold_count']}")
        print("\nFold table:")
        for row in result["folds"]:
            print(
                f"  {row['Fold']}  {row['Test start (UTC)']} -> {row['Test end (UTC)']}  "
                f"gate {row['Gate']:7s} ({row['Gate accuracy']} vs {row['Gate baseline']})  "
                f"test {row['Test accuracy']} vs {row['Test baseline']}  "
                f"trades {row['Trades']:3d}  win {row['Win rate']:>6s}  "
                f"pnl {row['Net PnL (USDT)']:+.4f}  return {row['Return']}"
            )
    else:
        print(f"Entry gate passed     : {result['entry_gate_passed']}")
    print(f"Test window           : {result['test_start']} -> {result['test_end']}")
    print(f"Test bars             : {result['test_bars']}   Fit samples: {result['model_training_samples']}")

    print("\nDirectional hit rate by score cutoff (held-out candles only):")
    for row in result["confidence_scan"]:
        print(
            f"  cutoff {row['Score cutoff']:>4s}  signals {row['Test signals']:>5d}  "
            f"coverage {row['Test coverage']:>7s}  hit rate {row['Hit rate']}"
        )

    trades = result.get("trades") or []
    if trades:
        reasons: dict[str, list[float]] = {}
        for trade in trades:
            reasons.setdefault(trade["Exit reason"], []).append(float(trade["Net PnL (USDT)"]))
        print("\nExit-reason breakdown:")
        for reason, pnls in sorted(reasons.items()):
            wins = sum(1 for pnl in pnls if pnl > 0)
            print(
                f"  {reason:<12} n={len(pnls):>3d}  wins {wins:>3d} ({wins / len(pnls):>5.1%})  "
                f"total {sum(pnls):+.4f}  avg {sum(pnls) / len(pnls):+.4f} USDT"
            )

        print(f"\nFirst 15 trades (newest first):")
        header = f"  {'Entry (UTC)':<18} {'Side':<6} {'Entry':>10} {'Exit':>10} {'Qty':>10} {'Fees':>8} {'PnL':>9}  Reason"
        print(header)
        for trade in trades[:15]:
            print(
                f"  {trade['Entry (UTC)']:<18} {trade['Side']:<6} {trade['Entry (USDT)']:>10.2f} "
                f"{trade['Exit (USDT)']:>10.2f} {trade['Quantity (BTC)']:>10.6f} "
                f"{trade['Fees (USDT)']:>8.4f} {trade['Net PnL (USDT)']:>9.4f}  {trade['Exit reason']}"
            )
        total_fees = sum(float(trade["Fees (USDT)"]) for trade in trades)
        gross = sum(float(trade["Net PnL (USDT)"]) for trade in trades)
        print(f"\n  Total fees paid: {total_fees:.4f} USDT   Sum of trade PnL: {gross:+.4f} USDT")
    else:
        print("\nNo trades were taken.")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the paper strategy backtest headlessly.")
    parser.add_argument("--days", type=int, default=30, choices=[7, 30], help="history window")
    parser.add_argument("--balance", type=float, default=10.0, help="starting balance in USDT")
    parser.add_argument("--mode", choices=["walk", "holdout", "both"], default="both")
    parser.add_argument("--no-gate", action="store_true", help="disable the pre-test validation edge gate")
    parser.add_argument("--no-clear", action="store_true", help="accept weak forecasts too")
    parser.add_argument("--max-hold", type=int, default=5, choices=[3, 5, 10, 20])
    parser.add_argument("--stop-bps", type=float, default=20.0)
    parser.add_argument("--reward-risk", type=float, default=2.0)
    parser.add_argument("--fee-bps", type=float, default=5.0)
    parser.add_argument("--slippage-bps", type=float, default=1.0)
    parser.add_argument("--risk-percent", type=float, default=0.5)
    parser.add_argument("--min-flow", type=float, default=0.20)
    args = parser.parse_args()

    options = dict(DEFAULT_OPTIONS)
    options["require_validation_edge"] = not args.no_gate
    options["require_clear"] = not args.no_clear
    options["max_hold_bars"] = args.max_hold
    options["stop_bps"] = args.stop_bps
    options["reward_risk"] = args.reward_risk
    options["fee_bps"] = args.fee_bps
    options["slippage_bps"] = args.slippage_bps
    options["risk_percent"] = args.risk_percent
    options["min_flow"] = args.min_flow

    print(f"Fetching {args.days} days of BTCUSDT 1m candles...")
    rows = fetch_klines(args.days)
    closed = [row for row in rows if len(row) > 6 and int(row[6]) < int(datetime.now(timezone.utc).timestamp() * 1000)]
    print(f"Fetched {len(rows)} rows, {len(closed)} closed candles.")

    ran = False
    if args.mode in ("walk", "both"):
        result = run_walk_forward(
            rows,
            starting_balance=args.balance,
            fold_count=5,
            initial_train_fraction=0.5,
            **options,
        )
        ran = print_result(result, f"5-fold walk-forward · {args.days}d · {args.balance:g} USDT") or ran

    if args.mode in ("holdout", "both"):
        test_bars = max(100, int(len(closed) * 0.2))
        result = run_backtest(rows, starting_balance=args.balance, test_bars=test_bars, **options)
        ran = print_result(result, f"single 80/20 holdout · {args.days}d · {args.balance:g} USDT") or ran

    return 0 if ran else 1


if __name__ == "__main__":
    raise SystemExit(main())
