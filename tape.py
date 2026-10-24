"""Forward-outcome tracking for live setups and live forecast predictions.

The dashboard shows a setup the moment the order-flow signal and the candle
model agree, but it never answers the only question that matters: did those
setups actually reach their target or their stop?

The signal tape records every distinct setup as it appears and then follows the
observed perp trade price until the target is hit, the stop is hit, or the
horizon expires. It is intentionally independent of the paper position engine,
so a user who never clicks a button still gets an honest record of what the
rule would have produced.

The forecast tracker does the same for the candle model: every live forecast
call is logged with the candle it was based on and resolved against the next
candle once that candle closes.

Both trackers are pure functions over plain dictionaries so they can be tested
without a Streamlit session.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Sequence

TAPE_HORIZON_SECONDS = 900
TAPE_DEDUP_SECONDS = 60
TAPE_MAX_ROWS = 500
FORECAST_LOG_MAX_ROWS = 200


def _utc_stamp(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def make_setup_record(
    side: int,
    mark: float,
    entry: float,
    stop: float,
    target: float,
    stop_bps: float,
    now: float,
    cost_bps: float = 0.0,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one tape row for a setup that has just appeared."""
    record: dict[str, Any] = {
        "Recorded (UTC)": _utc_stamp(now),
        "Side": "LONG" if side > 0 else "SHORT",
        "side": side,
        "opened_ts": now,
        "entry": entry,
        "stop": stop,
        "target": target,
        "stop_bps": stop_bps,
        "cost_bps": cost_bps,
        "status": "OPEN",
        "exit": None,
        "exit_ts": None,
        "result_bps": None,
        "net_r": None,
        "mfe_bps": 0.0,
        "mae_bps": 0.0,
    }
    if context:
        for key, value in context.items():
            record[key] = value
    record["mark_at_signal"] = mark
    return record


def should_record_setup(
    tape: list[dict[str, Any]],
    side: int,
    now: float,
    dedup_seconds: int = TAPE_DEDUP_SECONDS,
) -> bool:
    """True when this side is a new setup rather than a repeat of the last one.

    A setup keeps firing on every refresh while the conditions hold, so the
    tape would otherwise fill with duplicates of a single signal.
    """
    if not tape:
        return True
    latest = tape[-1]
    if int(latest["side"]) != side:
        return True
    if latest["status"] == "OPEN":
        return False
    return (now - float(latest["exit_ts"] or latest["opened_ts"])) >= dedup_seconds


def update_tape(
    tape: list[dict[str, Any]],
    mark: float,
    now: float,
    horizon_seconds: int = TAPE_HORIZON_SECONDS,
) -> list[dict[str, Any]]:
    """Advance every open tape row against the latest observed price."""
    for entry in tape:
        if entry["status"] != "OPEN":
            continue
        side = int(entry["side"])
        reference = float(entry["entry"])
        if reference <= 0:
            continue
        move_bps = (mark - reference) / reference * 10_000 * side
        if move_bps > float(entry["mfe_bps"]):
            entry["mfe_bps"] = move_bps
        if move_bps < float(entry["mae_bps"]):
            entry["mae_bps"] = move_bps
        exit_price: float | None = None
        status = ""
        target_level = float(entry["target"])
        stop_level = float(entry["stop"])
        if side > 0:
            target_hit = mark >= target_level
            stop_hit = mark <= stop_level
        else:
            target_hit = mark <= target_level
            stop_hit = mark >= stop_level
        if target_hit:
            exit_price = target_level
            status = "TARGET"
        elif stop_hit:
            exit_price = stop_level
            status = "STOP"
        elif (now - float(entry["opened_ts"])) >= horizon_seconds:
            exit_price = mark
            status = "TIMEOUT"
        if exit_price is None:
            continue
        gross_bps = (exit_price - reference) / reference * 10_000 * side
        net_bps = gross_bps - float(entry.get("cost_bps", 0.0))
        stop_bps = float(entry["stop_bps"]) or 1.0
        entry["status"] = status
        entry["exit"] = exit_price
        entry["exit_ts"] = now
        entry["result_bps"] = gross_bps
        entry["net_bps"] = net_bps
        entry["net_r"] = net_bps / stop_bps
    return tape


def summarize_tape(tape: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate the resolved tape rows into headline numbers."""
    resolved = [entry for entry in tape if entry["status"] != "OPEN"]
    open_rows = [entry for entry in tape if entry["status"] == "OPEN"]
    targets = sum(1 for entry in resolved if entry["status"] == "TARGET")
    stops = sum(1 for entry in resolved if entry["status"] == "STOP")
    timeouts = sum(1 for entry in resolved if entry["status"] == "TIMEOUT")
    total = len(resolved)
    net_rs = [float(entry["net_r"]) for entry in resolved if entry.get("net_r") is not None]
    total_r = sum(net_rs)
    return {
        "setups": len(tape),
        "resolved": total,
        "open": len(open_rows),
        "targets": targets,
        "stops": stops,
        "timeouts": timeouts,
        "target_rate": (targets / total) if total else 0.0,
        "stop_rate": (stops / total) if total else 0.0,
        "total_r": total_r,
        "avg_r": (total_r / total) if total else 0.0,
        "expectancy_r": (total_r / total) if total else 0.0,
        "avg_mfe_bps": (
            sum(float(entry["mfe_bps"]) for entry in resolved) / total if total else 0.0
        ),
        "avg_mae_bps": (
            sum(float(entry["mae_bps"]) for entry in resolved) / total if total else 0.0
        ),
        "best_r": max(net_rs) if net_rs else 0.0,
        "worst_r": min(net_rs) if net_rs else 0.0,
    }


def log_forecast_prediction(
    log: list[dict[str, Any]],
    direction: str,
    reference_close: float,
    reference_open_ms: int,
    now: float,
    max_rows: int = FORECAST_LOG_MAX_ROWS,
) -> list[dict[str, Any]]:
    """Record a live forecast call, skipping repeats of the same candle."""
    if direction not in ("UP", "DOWN"):
        return log
    if log:
        latest = log[-1]
        if latest["direction"] == direction and latest["reference_open_ms"] == reference_open_ms:
            return log
    log.append(
        {
            "predicted_at": now,
            "direction": direction,
            "reference_close": reference_close,
            "reference_open_ms": reference_open_ms,
            "resolved": False,
            "actual": None,
            "hit": None,
        }
    )
    if len(log) > max_rows:
        log = log[-max_rows:]
    return log


def resolve_forecast_log(
    log: list[dict[str, Any]], klines: Sequence[Sequence[Any]], interval_ms: int = 60_000
) -> list[dict[str, Any]]:
    """Resolve logged forecasts against the candle that followed them."""
    closes = {int(row[0]): float(row[4]) for row in klines if len(row) >= 5}
    for entry in log:
        if entry["resolved"]:
            continue
        next_open = int(entry["reference_open_ms"]) + interval_ms
        close = closes.get(next_open)
        if close is None:
            continue
        actual_up = close > float(entry["reference_close"])
        entry["actual"] = "UP" if actual_up else "DOWN"
        entry["hit"] = entry["actual"] == entry["direction"]
        entry["resolved"] = True
    return log


def summarize_forecast_log(log: list[dict[str, Any]]) -> dict[str, Any]:
    """Hit rate of the forecasts that have actually resolved this session."""
    resolved = [entry for entry in log if entry["resolved"]]
    hits = sum(1 for entry in resolved if entry["hit"])
    pending = len(log) - len(resolved)
    return {
        "logged": len(log),
        "resolved": len(resolved),
        "pending": pending,
        "hits": hits,
        "hit_rate": (hits / len(resolved)) if resolved else 0.0,
    }
