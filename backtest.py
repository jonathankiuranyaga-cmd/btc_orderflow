from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Sequence

from forecast import (
    FEATURE_LOOKBACK,
    MAX_ROLLING_LABELS,
    MIN_TRAIN_SAMPLES,
    _features,
    _fit_logistic,
    _predict,
)


def _utc_time(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _forecast_side(score: float, require_clear: bool) -> int:
    if score >= 0.56 or (not require_clear and score >= 0.52):
        return 1
    if score <= 0.44 or (not require_clear and score <= 0.48):
        return -1
    return 0


def run_backtest(
    rows: Sequence[Sequence[Any]],
    *,
    starting_balance: float = 10.0,
    test_bars: int = 200,
    risk_percent: float = 0.5,
    max_leverage: float = 1.0,
    stop_bps: float = 20.0,
    reward_risk: float = 2.0,
    fee_bps: float = 5.0,
    slippage_bps: float = 1.0,
    daily_loss_percent: float = 2.0,
    min_flow: float = 0.20,
    min_trades: int = 5,
    min_total_btc: float = 1.0,
    require_clear: bool = True,
    require_validation_edge: bool = True,
    max_hold_bars: int = 5,
    now_ms: int | None = None,
) -> dict[str, Any] | None:
    if now_ms is None:
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    candles = [
        {
            "open_time": int(row[0]),
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": float(row[5]),
            "close_time": int(row[6]),
            "trades": int(row[8]),
            "taker_buy_volume": float(row[9]),
        }
        for row in rows
        if len(row) >= 10 and int(row[6]) < now_ms
    ]
    if starting_balance <= 0 or stop_bps <= 0 or reward_risk <= 0 or max_hold_bars <= 0:
        raise ValueError("Balance, stop, target multiple, and holding period must be positive.")
    if len(candles) <= FEATURE_LOOKBACK + MIN_TRAIN_SAMPLES + test_bars:
        return None

    test_start = len(candles) - test_bars
    train_features: list[list[float]] = []
    train_labels: list[int] = []
    for index in range(FEATURE_LOOKBACK, test_start - 1):
        close = candles[index]["close"]
        next_close = candles[index + 1]["close"]
        if close != next_close:
            train_features.append(_features(candles, index))
            train_labels.append(int(next_close > close))
    if len(train_features) > MAX_ROLLING_LABELS:
        train_features = train_features[-MAX_ROLLING_LABELS:]
        train_labels = train_labels[-MAX_ROLLING_LABELS:]
    if len(train_labels) < MIN_TRAIN_SAMPLES:
        return None

    validation_split = int(len(train_labels) * 0.8)
    validation_model = _fit_logistic(train_features[:validation_split], train_labels[:validation_split])
    training_validation_predictions = [
        _predict(features, validation_model) >= 0.5
        for features in train_features[validation_split:]
    ]
    training_validation_labels = train_labels[validation_split:]
    training_validation_accuracy = sum(
        prediction == bool(label)
        for prediction, label in zip(training_validation_predictions, training_validation_labels)
    ) / len(training_validation_labels)
    training_up_rate = sum(training_validation_labels) / len(training_validation_labels)
    training_validation_baseline = max(training_up_rate, 1 - training_up_rate)
    entry_gate_passed = training_validation_accuracy >= training_validation_baseline

    model = _fit_logistic(train_features, train_labels)
    balance = starting_balance
    trades: list[dict[str, Any]] = []
    equity_curve = [starting_balance]
    position: dict[str, Any] | None = None
    cooldown_until_bar = -1
    daily_pnl: dict[str, float] = {}
    test_predictions: list[bool] = []
    test_labels: list[bool] = []
    test_scores: list[float] = []

    def close_trade(raw_exit: float, exit_bar_index: int, reason: str) -> None:
        nonlocal balance, position, cooldown_until_bar
        if position is None:
            return
        side = int(position["side"])
        exit_fill = raw_exit * (1 - side * slippage_bps / 10_000)
        exit_fee = exit_fill * position["quantity"] * fee_bps / 10_000
        net_pnl = (
            side * (exit_fill - position["entry"]) * position["quantity"]
            - position["entry_fee"]
            - exit_fee
        )
        balance += net_pnl
        exit_candle = candles[exit_bar_index]
        day = datetime.fromtimestamp(exit_candle["close_time"] / 1000, timezone.utc).date().isoformat()
        daily_pnl[day] = daily_pnl.get(day, 0.0) + net_pnl
        trades.insert(
            0,
            {
                "Entry (UTC)": _utc_time(position["entry_time"]),
                "Exit (UTC)": _utc_time(exit_candle["close_time"]),
                "Side": position["label"],
                "Entry (USDT)": position["entry"],
                "Exit (USDT)": exit_fill,
                "Quantity (BTC)": position["quantity"],
                "Fees (USDT)": position["entry_fee"] + exit_fee,
                "Net PnL (USDT)": net_pnl,
                "Exit reason": reason,
            },
        )
        equity_curve.append(balance)
        if net_pnl < 0:
            cooldown_until_bar = exit_bar_index + 5
        position = None

    for entry_index in range(test_start, len(candles)):
        candle = candles[entry_index]
        signal_index = entry_index - 1
        signal_candle = candles[signal_index]
        up_score = _predict(_features(candles, signal_index), model)
        actual_up = candle["close"] > signal_candle["close"]
        test_predictions.append(up_score >= 0.5)
        test_labels.append(actual_up)
        test_scores.append(up_score)
        closed_this_bar = False

        if position is not None:
            side = int(position["side"])
            stop_hit = candle["low"] <= position["stop"] if side > 0 else candle["high"] >= position["stop"]
            target_hit = candle["high"] >= position["target"] if side > 0 else candle["low"] <= position["target"]
            if stop_hit:
                gap_exit = candle["open"] < position["stop"] if side > 0 else candle["open"] > position["stop"]
                close_trade(candle["open"] if gap_exit else position["stop"], entry_index, "Stop")
                closed_this_bar = True
            elif target_hit:
                close_trade(position["target"], entry_index, "Target")
                closed_this_bar = True
            elif entry_index - int(position["entry_bar"]) + 1 >= max_hold_bars:
                close_trade(candle["close"], entry_index, "Time exit")
                closed_this_bar = True

        if position is not None or closed_this_bar or entry_index - 1 < test_start - 1:
            continue
        if require_validation_edge and not entry_gate_passed:
            continue

        forecast_side = _forecast_side(up_score, require_clear)

        volume = signal_candle["volume"]
        buy_volume = signal_candle["taker_buy_volume"]
        delta_fraction = (2 * buy_volume - volume) / volume if volume > 0 else 0.0
        if signal_candle["trades"] < min_trades or volume < min_total_btc:
            continue
        flow_side = 1 if delta_fraction >= min_flow else -1 if delta_fraction <= -min_flow else 0
        if forecast_side == 0 or flow_side != forecast_side:
            continue

        entry_day = datetime.fromtimestamp(candle["open_time"] / 1000, timezone.utc).date().isoformat()
        if entry_index < cooldown_until_bar or daily_pnl.get(entry_day, 0.0) <= -starting_balance * daily_loss_percent / 100:
            continue

        side = forecast_side
        entry = candle["open"] * (1 + side * slippage_bps / 10_000)
        stop_distance = entry * stop_bps / 10_000
        risk_budget = balance * risk_percent / 100
        loss_per_btc = stop_distance + entry * (2 * fee_bps + 2 * slippage_bps) / 10_000
        quantity = min(risk_budget / loss_per_btc, balance * max_leverage / entry) if loss_per_btc > 0 else 0.0
        if quantity <= 0:
            continue
        position = {
            "side": side,
            "label": "LONG" if side > 0 else "SHORT",
            "entry": entry,
            "quantity": quantity,
            "entry_fee": entry * quantity * fee_bps / 10_000,
            "entry_time": candle["open_time"],
            "entry_bar": entry_index,
            "stop": entry - side * stop_distance,
            "target": entry + side * stop_distance * reward_risk,
        }

        stop_hit = candle["low"] <= position["stop"] if side > 0 else candle["high"] >= position["stop"]
        target_hit = candle["high"] >= position["target"] if side > 0 else candle["low"] <= position["target"]
        if stop_hit:
            close_trade(position["stop"], entry_index, "Stop")
        elif target_hit:
            close_trade(position["target"], entry_index, "Target")

    if position is not None:
        close_trade(candles[-1]["close"], len(candles) - 1, "End of test")

    wins = sum(float(trade["Net PnL (USDT)"]) > 0 for trade in trades)
    losses = sum(float(trade["Net PnL (USDT)"]) < 0 for trade in trades)
    gross_profit = sum(float(trade["Net PnL (USDT)"]) for trade in trades if float(trade["Net PnL (USDT)"]) > 0)
    gross_loss = -sum(float(trade["Net PnL (USDT)"]) for trade in trades if float(trade["Net PnL (USDT)"]) < 0)
    peak = starting_balance
    max_drawdown = 0.0
    for value in equity_curve:
        peak = max(peak, value)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - value) / peak * 100)
    validation_accuracy = (
        sum(prediction == label for prediction, label in zip(test_predictions, test_labels)) / len(test_labels)
        if test_labels
        else 0.0
    )
    up_rate = sum(test_labels) / len(test_labels) if test_labels else 0.0
    confidence_scan = []
    confidence_scan_counts = []
    for cutoff in (0.56, 0.60, 0.65, 0.70, 0.75, 0.80, 0.90):
        selected = [
            (score >= cutoff, label)
            for score, label in zip(test_scores, test_labels)
            if score >= cutoff or score <= 1 - cutoff
        ]
        correct = sum(prediction == label for prediction, label in selected)
        confidence_scan.append(
            {
                "Score cutoff": f"{cutoff:.0%}",
                "Test signals": len(selected),
                "Test coverage": f"{len(selected) / len(test_scores):.2%}" if test_scores else "0.00%",
                "Hit rate": f"{correct / len(selected):.1%}" if selected else "No signals",
            }
        )
        confidence_scan_counts.append({"cutoff": cutoff, "signals": len(selected), "correct": correct})

    return {
        "starting_balance": starting_balance,
        "ending_balance": balance,
        "net_pnl": balance - starting_balance,
        "return_percent": (balance / starting_balance - 1) * 100,
        "trade_count": len(trades),
        "wins": wins,
        "losses": losses,
        "win_rate": wins / len(trades) * 100 if trades else 0.0,
        "profit_factor": gross_profit / gross_loss if gross_loss > 0 else float("inf"),
        "max_drawdown_percent": max_drawdown,
        "model_accuracy": validation_accuracy,
        "majority_baseline": max(up_rate, 1 - up_rate) if test_labels else 0.0,
        "training_validation_accuracy": training_validation_accuracy,
        "training_validation_baseline": training_validation_baseline,
        "training_validation_samples": len(training_validation_labels),
        "entry_gate_passed": entry_gate_passed,
        "require_validation_edge": require_validation_edge,
        "training_bars": test_start,
        "model_training_samples": len(train_labels),
        "test_bars": len(candles) - test_start,
        "confidence_scan": confidence_scan,
        "confidence_scan_counts": confidence_scan_counts,
        "test_start": _utc_time(candles[test_start]["open_time"]),
        "test_end": _utc_time(candles[-1]["close_time"]),
        "trades": trades,
    }


def run_walk_forward(
    rows: Sequence[Sequence[Any]],
    *,
    starting_balance: float = 10.0,
    fold_count: int = 5,
    initial_train_fraction: float = 0.5,
    now_ms: int | None = None,
    **backtest_options: Any,
) -> dict[str, Any] | None:
    if fold_count < 2 or not 0 < initial_train_fraction < 1:
        raise ValueError("Walk-forward needs at least two folds and a training fraction between zero and one.")
    if now_ms is None:
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    closed_rows = [row for row in rows if len(row) > 6 and int(row[6]) < now_ms]
    initial_train_bars = int(len(closed_rows) * initial_train_fraction)
    remaining_bars = len(closed_rows) - initial_train_bars
    if initial_train_bars <= FEATURE_LOOKBACK + MIN_TRAIN_SAMPLES or remaining_bars < fold_count * 100:
        return None

    fold_results: list[dict[str, Any]] = []
    fold_table: list[dict[str, Any]] = []
    previous_end = initial_train_bars
    for fold_index in range(fold_count):
        fold_end = (
            len(closed_rows)
            if fold_index == fold_count - 1
            else initial_train_bars + int(remaining_bars * (fold_index + 1) / fold_count)
        )
        fold_test_bars = fold_end - previous_end
        result = run_backtest(
            closed_rows[:fold_end],
            starting_balance=starting_balance,
            test_bars=fold_test_bars,
            now_ms=now_ms,
            **backtest_options,
        )
        if result is None:
            return None
        fold_results.append(result)
        fold_table.append(
            {
                "Fold": fold_index + 1,
                "Test start (UTC)": result["test_start"],
                "Test end (UTC)": result["test_end"],
                "Fit samples": result["model_training_samples"],
                "Gate": "PASS" if result["entry_gate_passed"] else "BLOCKED",
                "Gate accuracy": f"{result['training_validation_accuracy']:.1%}",
                "Gate baseline": f"{result['training_validation_baseline']:.1%}",
                "Test accuracy": f"{result['model_accuracy']:.1%}",
                "Test baseline": f"{result['majority_baseline']:.1%}",
                "Trades": result["trade_count"],
                "Win rate": f"{result['win_rate']:.1f}%",
                "Net PnL (USDT)": result["net_pnl"],
                "Return": f"{result['return_percent']:+.2f}%",
            }
        )
        previous_end = fold_end

    total_test_bars = sum(int(result["test_bars"]) for result in fold_results)
    total_trades = [trade for result in fold_results for trade in result["trades"]]
    total_trades.sort(key=lambda trade: trade["Entry (UTC)"], reverse=True)
    wins = sum(float(trade["Net PnL (USDT)"]) > 0 for trade in total_trades)
    losses = sum(float(trade["Net PnL (USDT)"]) < 0 for trade in total_trades)
    gross_profit = sum(float(trade["Net PnL (USDT)"]) for trade in total_trades if float(trade["Net PnL (USDT)"]) > 0)
    gross_loss = -sum(float(trade["Net PnL (USDT)"]) for trade in total_trades if float(trade["Net PnL (USDT)"]) < 0)
    weighted_return = sum(float(result["return_percent"]) * int(result["test_bars"]) for result in fold_results) / total_test_bars
    weighted_model_accuracy = sum(float(result["model_accuracy"]) * int(result["test_bars"]) for result in fold_results) / total_test_bars
    weighted_test_baseline = sum(float(result["majority_baseline"]) * int(result["test_bars"]) for result in fold_results) / total_test_bars
    validation_samples = sum(int(result["training_validation_samples"]) for result in fold_results)
    weighted_gate_accuracy = sum(float(result["training_validation_accuracy"]) * int(result["training_validation_samples"]) for result in fold_results) / validation_samples
    weighted_gate_baseline = sum(float(result["training_validation_baseline"]) * int(result["training_validation_samples"]) for result in fold_results) / validation_samples
    aggregated_scan = []
    for scan_index, cutoff in enumerate(fold_results[0]["confidence_scan_counts"]):
        signals = sum(int(result["confidence_scan_counts"][scan_index]["signals"]) for result in fold_results)
        correct = sum(int(result["confidence_scan_counts"][scan_index]["correct"]) for result in fold_results)
        score_cutoff = float(cutoff["cutoff"])
        aggregated_scan.append(
            {
                "Score cutoff": f"{score_cutoff:.0%}",
                "Test signals": signals,
                "Test coverage": f"{signals / total_test_bars:.2%}",
                "Hit rate": f"{correct / signals:.1%}" if signals else "No signals",
            }
        )
    fold_returns = [float(result["return_percent"]) for result in fold_results]
    return {
        "starting_balance": starting_balance,
        "ending_balance": starting_balance * (1 + weighted_return / 100),
        "net_pnl": starting_balance * weighted_return / 100,
        "return_percent": weighted_return,
        "trade_count": len(total_trades),
        "wins": wins,
        "losses": losses,
        "win_rate": wins / len(total_trades) * 100 if total_trades else 0.0,
        "profit_factor": gross_profit / gross_loss if gross_loss > 0 else float("inf"),
        "max_drawdown_percent": max(float(result["max_drawdown_percent"]) for result in fold_results),
        "model_accuracy": weighted_model_accuracy,
        "majority_baseline": weighted_test_baseline,
        "training_validation_accuracy": weighted_gate_accuracy,
        "training_validation_baseline": weighted_gate_baseline,
        "entry_gate_passed": all(bool(result["entry_gate_passed"]) for result in fold_results),
        "gate_passing_folds": sum(bool(result["entry_gate_passed"]) for result in fold_results),
        "profitable_folds": sum(value > 0 for value in fold_returns),
        "fold_count": len(fold_results),
        "require_validation_edge": bool(backtest_options.get("require_validation_edge", True)),
        "test_bars": total_test_bars,
        "model_training_samples": sum(int(result["model_training_samples"]) for result in fold_results),
        "training_bars": sum(int(result["training_bars"]) for result in fold_results),
        "confidence_scan": aggregated_scan,
        "folds": fold_table,
        "trades": total_trades,
        "test_start": fold_results[0]["test_start"],
        "test_end": fold_results[-1]["test_end"],
    }