"""Diagnose why the 1-minute forecast scores below the majority-class baseline.

Decisive test: bucket every held-out bar by the model's up_score and measure
the realised next-bar up rate in each bucket. If the model carries signal the
buckets will be monotonic (low scores -> mostly down, high scores -> mostly
up). If every bucket sits near 50% the model is noise, and if the buckets are
monotonic in the *opposite* direction the sign convention is inverted.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from forecast import FEATURE_LOOKBACK, _features, _fit_logistic, _predict  # noqa: E402
from tools.event_study import fetch_klines  # noqa: E402


def autocorrelation(returns: list[float], lag: int) -> float:
    mean = sum(returns) / len(returns)
    num = sum((returns[i] - mean) * (returns[i - lag] - mean) for i in range(lag, len(returns)))
    den = sum((value - mean) ** 2 for value in returns)
    return num / den if den else 0.0


def main() -> int:
    bars = fetch_klines(30, "1m")
    closes = [bar["close"] for bar in bars]
    print(f"bars: {len(bars)}")

    returns = [
        (closes[i] / closes[i - 1] - 1) * 10_000 if closes[i - 1] else 0.0
        for i in range(1, len(closes))
    ]
    print("\n=== Return autocorrelation (1m closes) ===")
    for lag in (1, 2, 3, 5, 10):
        print(f"  lag {lag:>2}: {autocorrelation(returns, lag):+.4f}")

    up_moves = [r for r in returns if r > 0]
    down_moves = [r for r in returns if r < 0]
    print(f"\n  up-minute share  : {len(up_moves) / len(returns):.2%}")
    print(f"  up_rate baseline : {max(len(up_moves), len(down_moves)) / len(returns):.2%}")

    up_follows_up = sum(1 for i in range(len(returns) - 1) if returns[i] > 0 and returns[i + 1] > 0)
    down_follows_down = sum(1 for i in range(len(returns) - 1) if returns[i] < 0 and returns[i + 1] < 0)
    n = len(returns) - 1
    print(f"  P(next up | up)   : {up_follows_up / len(up_moves):.2%}")
    print(f"  P(next up | down) : {1 - down_follows_down / len(down_moves):.2%}")

    split = int(len(bars) * 0.7)
    feature_rows: list[list[float]] = []
    labels: list[int] = []
    for index in range(FEATURE_LOOKBACK, len(bars) - 1):
        feature_rows.append(_features(bars, index))
        labels.append(int(bars[index + 1]["close"] > bars[index]["close"]))

    model = _fit_logistic(feature_rows[:split], labels[:split])
    print("\n=== Fitted weights (normalised units) ===")
    names = ["mom1", "mom3", "mom5", "intrabar_pos", "rel_volume"]
    for name, weight in zip(names, model[3]):
        print(f"  {name:>12}: {weight:+.4f}")

    test_features = feature_rows[split:]
    test_labels = labels[split:]
    scores = [_predict(row, model) for row in test_features]
    forward_bps = [
        (bars[index + 1]["close"] / bars[index]["close"] - 1) * 10_000
        for index in range(split + FEATURE_LOOKBACK, len(bars) - 1)
    ]

    correct = sum(1 for score, label in zip(scores, test_labels) if (score >= 0.5) == bool(label))
    inverted = sum(1 for score, label in zip(scores, test_labels) if (score < 0.5) == bool(label))
    up_rate = sum(test_labels) / len(test_labels)
    print("\n=== Held-out accuracy (last 30% of 30d) ===")
    print(f"  samples            : {len(test_labels)}")
    print(f"  up_rate (baseline) : {max(up_rate, 1 - up_rate):.2%}")
    print(f"  model as-is        : {correct / len(test_labels):.2%}")
    print(f"  model inverted     : {inverted / len(test_labels):.2%}")

    print("\n=== Score decile -> realised next-bar up rate ===")
    order = sorted(range(len(scores)), key=lambda index: scores[index])
    bucket = len(order) // 10
    for decile in range(10):
        chunk = order[decile * bucket : (decile + 1) * bucket] if decile < 9 else order[decile * bucket :]
        hit = sum(test_labels[index] for index in chunk) / len(chunk)
        avg_score = sum(scores[index] for index in chunk) / len(chunk)
        avg_fwd = sum(forward_bps[index] for index in chunk) / len(chunk)
        print(
            f"  decile {decile + 1:>2}  score {avg_score:.3f}  next-up {hit:6.2%}  "
            f"mean fwd {avg_fwd:+6.2f} bps"
        )

    print("\n=== Taker-delta decile -> realised next-bar up rate (order flow) ===")
    deltas = []
    for index in range(FEATURE_LOOKBACK, len(bars) - 1):
        bar = bars[index]
        total = bar["buy"] + bar["sell"]
        deltas.append((bar["buy"] - bar["sell"]) / total if total else 0.0)
    test_deltas = deltas[split:]
    order = sorted(range(len(test_deltas)), key=lambda index: test_deltas[index])
    bucket = len(order) // 10
    for decile in range(10):
        chunk = order[decile * bucket : (decile + 1) * bucket] if decile < 9 else order[decile * bucket :]
        hit = sum(test_labels[index] for index in chunk) / len(chunk)
        avg_delta = sum(test_deltas[index] for index in chunk) / len(chunk)
        avg_fwd = sum(forward_bps[index] for index in chunk) / len(chunk)
        print(
            f"  decile {decile + 1:>2}  delta {avg_delta:+.3f}  next-up {hit:6.2%}  "
            f"mean fwd {avg_fwd:+6.2f} bps"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
