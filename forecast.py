from __future__ import annotations

from datetime import datetime, timezone
from math import exp, log, sqrt
from typing import Any, Sequence

FEATURE_LOOKBACK = 20
MIN_TRAIN_SAMPLES = 100
MIN_VALIDATION_SAMPLES = 50
MIN_FEATURE_ROWS = 150
FEATURE_COUNT = 5
MAX_ROLLING_LABELS = 600


def _sigmoid(value: float) -> float:
    if value >= 0:
        return 1 / (1 + exp(-min(value, 40)))
    exp_value = exp(max(value, -40))
    return exp_value / (1 + exp_value)


def _features(candles: list[dict[str, float]], index: int) -> list[float]:
    candle = candles[index]
    close = candle["close"]
    start = index - FEATURE_LOOKBACK + 1
    if start < 0:
        start = 0
    avg_volume = sum(item["volume"] for item in candles[start : index + 1]) / max(index - start + 1, 1)
    candle_range = max(candle["high"] - candle["low"], close * 1e-8)
    prev1 = candles[index - 1]["close"]
    prev3 = candles[index - 3]["close"] if index - 3 >= 0 else prev1
    prev5 = candles[index - 5]["close"] if index - 5 >= 0 else prev3
    return [
        (close / prev1 - 1) * 10_000,
        (close / prev3 - 1) * 10_000,
        (close / prev5 - 1) * 10_000,
        (close - candle["open"]) / candle_range,
        log(max(candle["volume"], 1e-12) / max(avg_volume, 1e-12)),
    ]


def _fit_logistic(
    rows: list[list[float]], labels: list[int]
) -> tuple[list[float], list[float], float, list[float]]:
    means = [sum(row[column] for row in rows) / len(rows) for column in range(FEATURE_COUNT)]
    scales = []
    for column in range(FEATURE_COUNT):
        variance = sum((row[column] - means[column]) ** 2 for row in rows) / len(rows)
        scales.append(sqrt(variance) or 1.0)
    normalized = [
        [max(-8.0, min(8.0, (row[j] - means[j]) / scales[j])) for j in range(FEATURE_COUNT)]
        for row in rows
    ]
    positive_rate = (sum(labels) + 1) / (len(labels) + 2)
    bias = log(positive_rate / (1 - positive_rate))
    weights = [0.0] * FEATURE_COUNT

    for _ in range(350):
        bias_gradient = 0.0
        weight_gradients = [0.0] * FEATURE_COUNT
        for row, label in zip(normalized, labels):
            error = _sigmoid(bias + sum(weight * value for weight, value in zip(weights, row))) - label
            bias_gradient += error
            for column, value in enumerate(row):
                weight_gradients[column] += error * value
        sample_count = len(labels)
        bias -= 0.12 * bias_gradient / sample_count
        for column in range(FEATURE_COUNT):
            gradient = weight_gradients[column] / sample_count + 0.02 * weights[column]
            weights[column] -= 0.12 * gradient

    return means, scales, bias, weights


def _predict(
    features: list[float], model: tuple[list[float], list[float], float, list[float]]
) -> float:
    means, scales, bias, weights = model
    normalized = [max(-8.0, min(8.0, (features[j] - means[j]) / scales[j])) for j in range(FEATURE_COUNT)]
    return _sigmoid(bias + sum(weight * value for weight, value in zip(weights, normalized)))


def forecast_next_minute(
    rows: Sequence[Sequence[Any]], now_ms: int | None = None
) -> dict[str, float | int | str] | None:
    if now_ms is None:
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    candles = [
        {
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": float(row[5]),
        }
        for row in rows
        if len(row) >= 7 and int(row[6]) < now_ms
    ]
    if len(candles) < FEATURE_LOOKBACK + MIN_TRAIN_SAMPLES + MIN_VALIDATION_SAMPLES + 1:
        return None

    feature_rows: list[list[float]] = []
    labels: list[int] = []

    for index in range(FEATURE_LOOKBACK, len(candles) - 1):
        current_close = candles[index]["close"]
        next_close = candles[index + 1]["close"]
        if next_close == current_close:
            continue
        feature_rows.append(_features(candles, index))
        labels.append(int(next_close > current_close))

    if len(feature_rows) == 0:
        return None

    if len(feature_rows) > MAX_ROLLING_LABELS:
        feature_rows = feature_rows[-MAX_ROLLING_LABELS:]
        labels = labels[-MAX_ROLLING_LABELS:]

    split = int(len(feature_rows) * 0.8)

    if split < MIN_TRAIN_SAMPLES or len(feature_rows) - split < MIN_VALIDATION_SAMPLES or len(feature_rows) < MIN_FEATURE_ROWS:
        return None

    validation_model = _fit_logistic(feature_rows[:split], labels[:split])
    validation_predictions = [
        _predict(row, validation_model) >= 0.5 for row in feature_rows[split:]
    ]
    validation_labels = labels[split:]
    validation_accuracy = sum(
        prediction == bool(label) for prediction, label in zip(validation_predictions, validation_labels)
    ) / len(validation_labels)
    up_rate = sum(validation_labels) / len(validation_labels)
    baseline_accuracy = max(up_rate, 1 - up_rate)

    full_model = _fit_logistic(feature_rows, labels)
    up_score = _predict(_features(candles, len(candles) - 1), full_model)
    if up_score >= 0.56:
        direction = "UP_CLEAR"
    elif up_score >= 0.52:
        direction = "UP_WEAK"
    elif up_score <= 0.44:
        direction = "DOWN_CLEAR"
    elif up_score <= 0.48:
        direction = "DOWN_WEAK"
    else:
        direction = "NO CLEAR EDGE"
    return {
        "direction": direction,
        "up_score": up_score,
        "validation_accuracy": validation_accuracy,
        "baseline_accuracy": baseline_accuracy,
        "training_samples": len(feature_rows),
        "validation_samples": len(validation_labels),
        "latest_close": candles[-1]["close"],
        "below_baseline": validation_accuracy < baseline_accuracy - 1e-9,
    }