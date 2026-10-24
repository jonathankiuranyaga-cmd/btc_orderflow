"""Which timeframe actually carries structure in the order flow?

The event study asks "does a thresholded footprint event predict a move". This
asks the more powerful question: "how strong is the relationship between
order flow and forward returns, as a function of how long the flow is
measured over and how far ahead we look". It uses every bar rather than only
the events that pass a threshold, so it has far more statistical power.

It also measures flow persistence, which is the mechanism that would make
order flow tradable: if aggressive buying is persistent (an institutional
campaign), then strong buying should predict *more buying* and therefore
drift, even when the immediate return response is negligible.

Usage:
    python tools/diagnose_timeframes.py --days 30
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.event_study import fetch_klines  # noqa: E402


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def stdev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    average = mean(values)
    return math.sqrt(sum((v - average) ** 2 for v in values) / (len(values) - 1))


def pearson(xs: list[float], ys: list[float]) -> float:
    if len(xs) < 3:
        return 0.0
    mx, my = mean(xs), mean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den = math.sqrt(sum((x - mx) ** 2 for x in xs) * sum((y - my) ** 2 for y in ys))
    return num / den if den else 0.0


def autocorrelation(values: list[float], lag: int) -> float:
    if len(values) <= lag + 2:
        return 0.0
    average = mean(values)
    num = sum((values[i] - average) * (values[i - lag] - average) for i in range(lag, len(values)))
    den = sum((value - average) ** 2 for value in values)
    return num / den if den else 0.0


def main() -> int:
    parser = argparse.ArgumentParser(description="Timeframe structure in BTC order flow.")
    parser.add_argument("--days", type=int, default=30)
    args = parser.parse_args()

    bars = fetch_klines(args.days, "1m")
    closes = [bar["close"] for bar in bars]
    deltas: list[float] = []
    for bar in bars:
        total = bar["buy"] + bar["sell"]
        deltas.append((bar["buy"] - bar["sell"]) / total if total else 0.0)
    print(f"bars: {len(bars)}")

    print("\n=== Return autocorrelation by horizon (aggregated closes) ===")
    for minutes in (1, 2, 3, 5, 10, 15, 30, 60, 240):
        step = minutes
        rets = []
        for i in range(step, len(closes), step):
            base = closes[i - step]
            if base:
                rets.append((closes[i] / base - 1) * 10_000)
        if len(rets) < 30:
            continue
        ac1 = autocorrelation(rets, 1)
        se = 1.0 / math.sqrt(len(rets))
        flag = " *" if abs(ac1) > 2 * se else ""
        print(
            f"  {minutes:>3}m: lag-1 autocorr {ac1:+.4f}  "
            f"(se {se:.4f}, n={len(rets)}){flag}"
        )

    print("\n=== Order-flow (taker delta) autocorrelation, 1m bars ===")
    for lag in (1, 2, 3, 5, 10, 20, 60):
        ac = autocorrelation(deltas, lag)
        se = 1.0 / math.sqrt(len(deltas))
        flag = " *" if abs(ac) > 2 * se else ""
        print(f"  lag {lag:>2}m: {ac:+.4f}  (se {se:.4f}){flag}")

    print("\n=== Flow window x forward horizon: correlation and top-decile edge ===")
    print("   corr = corr(mean delta over W mins, return over H mins)")
    print("   top10 = mean forward bps in the strongest-signal decile (the tradeable slice)")
    print("   cost  = 12 bps round trip; any top10 below +12 bps is unprofitable\n")
    cost = 12.0
    windows = [1, 3, 5, 15, 30]
    horizons = [1, 3, 5, 15, 30, 60]

    for horizon in horizons:
        print(f"  --- horizon {horizon}m ---")
        for window in windows:
            if horizon < window:
                continue
            signal: list[float] = []
            forward: list[float] = []
            step = max(1, horizon)
            for end in range(window, len(bars) - horizon, step):
                flow = mean(deltas[end - window : end])
                base = closes[end]
                if not base:
                    continue
                # Forward return starts *after* the signal window closes. Using
                # closes[end - horizon] here would overlap the signal window
                # and measure contemporaneous OFI/price correlation instead of
                # prediction, which is a look-ahead that flatters the result.
                signal.append(flow)
                forward.append((closes[end + horizon] / base - 1) * 10_000)
            if len(signal) < 50:
                continue
            corr = pearson(signal, forward)
            se = 1.0 / math.sqrt(len(signal))
            order = sorted(range(len(signal)), key=lambda i: signal[i])
            decile = max(1, len(order) // 10)
            top = order[-decile:]
            bottom = order[:decile]
            top_mean = mean([forward[i] for i in top])
            bottom_mean = mean([forward[i] for i in bottom])
            edge = top_mean - bottom_mean
            verdict = "EDGE" if abs(edge) > cost else ""
            print(
                f"    W={window:>2}m  corr {corr:+.4f} (t {corr / se:+5.1f})  "
                f"top10 {top_mean:+6.2f} bps  bottom10 {bottom_mean:+6.2f} bps  "
                f"spread {edge:+6.2f} bps  n={len(signal):>5}  {verdict}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
