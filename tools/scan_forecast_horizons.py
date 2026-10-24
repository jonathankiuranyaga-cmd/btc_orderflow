"""Does the forecast model predict *any* horizon, or only the one it was built for?

The app trains a logistic model to predict "does the next 1m bar close up".
That label was measured to be worth nothing: 51.88% accuracy against a 52.23%
majority baseline, with non-monotonic score deciles. This tool re-runs the same
model against labels at several horizons and reports, for each:

  * accuracy against the majority-class baseline
  * whether the score deciles are monotonic (a model with real information
    produces a clean gradient; a noise model produces a flat or broken one)
  * the top-decile minus bottom-decile mean forward return, which is the
    part a trader could actually monetise

Usage:
    python tools/scan_forecast_horizons.py --days 180
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from forecast import FEATURE_LOOKBACK, _features, _fit_logistic, _predict  # noqa: E402
from tools.event_study import fetch_klines  # noqa: E402


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def stdev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    average = mean(values)
    return math.sqrt(sum((v - average) ** 2 for v in values) / (len(values) - 1))


def spearman(xs: list[float], ys: list[float]) -> float:
    """Rank correlation, used to score decile monotonicity."""
    if len(xs) < 3:
        return 0.0

    def ranks(values: list[float]) -> list[float]:
        order = sorted(range(len(values)), key=lambda i: values[i])
        result = [0.0] * len(values)
        index = 0
        while index < len(order):
            stop = index
            while stop + 1 < len(order) and values[order[stop + 1]] == values[order[index]]:
                stop += 1
            average_rank = (index + stop) / 2 + 1
            for position in range(index, stop + 1):
                result[order[position]] = average_rank
            index = stop + 1
        return result

    rx, ry = ranks(xs), ranks(ys)
    mx, my = mean(rx), mean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    return num / den if den else 0.0


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan forecast labels across horizons.")
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--horizons", type=int, nargs="+", default=[1, 5, 15, 60, 240, 720])
    parser.add_argument("--max-samples", type=int, default=12000,
                        help="most recent samples used per horizon, to bound fit time")
    args = parser.parse_args()

    bars = fetch_klines(args.days, "1m")
    closes = [bar["close"] for bar in bars]
    print(f"bars: {len(bars)}  ({args.days}d)")

    features_cache = [_features(bars, index) for index in range(FEATURE_LOOKBACK, len(bars))]

    print("\n=== Forecast model accuracy by label horizon ===")
    print("  horizon |  baseline |  model  |  delta   | decile monotonicity | top-bottom spread")
    for horizon in args.horizons:
        limit = len(bars) - horizon
        if limit <= FEATURE_LOOKBACK + 500:
            continue
        rows = []
        labels = []
        forwards = []
        for index in range(FEATURE_LOOKBACK, limit):
            base = closes[index]
            if not base:
                continue
            rows.append(features_cache[index - FEATURE_LOOKBACK])
            labels.append(int(closes[index + horizon] > base))
            forwards.append((closes[index + horizon] / base - 1) * 10_000)
        if len(rows) < 1000:
            continue
        # The pure-python logistic fit is O(350 * n), so cap the sample. The
        # live app trains on 600 rolling labels and the backtest on 3000, so a
        # window of this size is more than representative.
        if len(rows) > args.max_samples:
            rows = rows[-args.max_samples :]
            labels = labels[-args.max_samples :]
            forwards = forwards[-args.max_samples :]
        split = int(len(rows) * 0.7)
        model = _fit_logistic(rows[:split], labels[:split])
        test_rows = rows[split:]
        test_labels = labels[split:]
        scores = [_predict(row, model) for row in test_rows]

        up_rate = sum(test_labels) / len(test_labels)
        baseline = max(up_rate, 1 - up_rate)
        correct = sum(1 for s, l in zip(scores, test_labels) if (s >= 0.5) == bool(l))
        accuracy = correct / len(test_labels)

        order = sorted(range(len(scores)), key=lambda i: scores[i])
        bucket = max(1, len(order) // 10)
        decile_hits = []
        for decile in range(10):
            chunk = order[decile * bucket : (decile + 1) * bucket] if decile < 9 else order[decile * bucket :]
            decile_hits.append(sum(test_labels[i] for i in chunk) / len(chunk))
        mono = spearman([float(d) for d in range(10)], decile_hits)

        forward = forwards[split:]
        fwd_order = sorted(range(len(forward)), key=lambda i: scores[i])
        top = fwd_order[-bucket:]
        bottom = fwd_order[:bucket]
        spread = mean([forward[i] for i in top]) - mean([forward[i] for i in bottom])

        flag = ""
        if accuracy > baseline and mono > 0.8:
            flag = "  <-- usable"
        print(
            f"  {horizon:>6}m |  {baseline:6.2%}  | {accuracy:6.2%} | "
            f"{accuracy - baseline:+6.2%} |  rho {mono:+.3f}          | {spread:+7.2f} bps{flag}"
        )
        print(
            "          decile hit rates: "
            + " ".join(f"{value:.1%}" for value in decile_hits)
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
