from tape import (
    log_forecast_prediction,
    make_setup_record,
    resolve_forecast_log,
    should_record_setup,
    summarize_forecast_log,
    summarize_tape,
    update_tape,
)

ENTRY = 100.0
STOP = 99.0
TARGET = 102.0


def _record(side=1, now=0.0, cost_bps=0.0):
    # Stop and target must sit on the correct side of the entry for the side
    # being tested, exactly as the live setup builder places them.
    stop = ENTRY - 1.0 if side > 0 else ENTRY + 1.0
    target = ENTRY + 2.0 if side > 0 else ENTRY - 2.0
    return make_setup_record(
        side=side,
        mark=ENTRY,
        entry=ENTRY,
        stop=stop,
        target=target,
        stop_bps=100.0,
        now=now,
        cost_bps=cost_bps,
    )


def test_update_tape_marks_target():
    tape = [_record()]
    update_tape(tape, mark=102.0, now=10.0)
    assert tape[0]["status"] == "TARGET"
    assert tape[0]["exit"] == 102.0
    assert abs(tape[0]["result_bps"] - 200.0) < 1e-9
    assert abs(tape[0]["net_r"] - 2.0) < 1e-9


def test_update_tape_marks_stop():
    tape = [_record()]
    update_tape(tape, mark=99.0, now=10.0)
    assert tape[0]["status"] == "STOP"
    assert abs(tape[0]["result_bps"] + 100.0) < 1e-9
    assert abs(tape[0]["net_r"] + 1.0) < 1e-9


def test_update_tape_short_side_uses_inverse():
    tape = [_record(side=-1)]
    update_tape(tape, mark=98.0, now=10.0)
    assert tape[0]["status"] == "TARGET"
    assert abs(tape[0]["result_bps"] - 200.0) < 1e-9


def test_update_tape_times_out():
    tape = [_record(now=0.0)]
    update_tape(tape, mark=100.5, now=1000.0)
    assert tape[0]["status"] == "TIMEOUT"
    assert abs(tape[0]["result_bps"] - 50.0) < 1e-9


def test_update_tape_tracks_excursions():
    tape = [_record()]
    update_tape(tape, mark=101.0, now=10.0)
    update_tape(tape, mark=99.5, now=20.0)
    assert abs(tape[0]["mfe_bps"] - 100.0) < 1e-9
    assert abs(tape[0]["mae_bps"] + 50.0) < 1e-9
    assert tape[0]["status"] == "OPEN"


def test_update_tape_applies_round_trip_cost():
    tape = [_record(cost_bps=12.0)]
    update_tape(tape, mark=102.0, now=10.0)
    assert abs(tape[0]["net_bps"] - 188.0) < 1e-9
    assert abs(tape[0]["net_r"] - 1.88) < 1e-9


def test_should_record_setup_first_time():
    assert should_record_setup([], side=1, now=0.0) is True


def test_should_record_setup_blocks_duplicate_while_open():
    tape = [_record(now=0.0)]
    assert should_record_setup(tape, side=1, now=5.0) is False


def test_should_record_setup_allows_side_change():
    tape = [_record(now=0.0)]
    assert should_record_setup(tape, side=-1, now=5.0) is True


def test_should_record_setup_respects_dedup_window():
    tape = [_record(now=0.0)]
    update_tape(tape, mark=102.0, now=1.0)
    assert should_record_setup(tape, side=1, now=10.0) is False
    assert should_record_setup(tape, side=1, now=200.0) is True


def test_summarize_tape_counts():
    tape = [_record()]
    update_tape(tape, mark=102.0, now=1.0)
    tape.append(_record(side=-1, now=2.0))
    update_tape(tape, mark=101.5, now=3.0)
    summary = summarize_tape(tape)
    assert summary["setups"] == 2
    assert summary["resolved"] == 2
    assert summary["targets"] == 1
    assert summary["stops"] == 1
    assert abs(summary["target_rate"] - 0.5) < 1e-9
    assert abs(summary["total_r"] - 1.0) < 1e-9
    assert abs(summary["expectancy_r"] - 0.5) < 1e-9


def test_summarize_tape_empty():
    summary = summarize_tape([])
    assert summary["setups"] == 0
    assert summary["target_rate"] == 0.0
    assert summary["total_r"] == 0.0


def test_log_forecast_prediction_skips_repeat():
    log = log_forecast_prediction([], "UP", 100.0, 0, now=1.0)
    log = log_forecast_prediction(log, "UP", 100.0, 0, now=2.0)
    assert len(log) == 1


def test_log_forecast_prediction_new_candle():
    log = log_forecast_prediction([], "UP", 100.0, 0, now=1.0)
    log = log_forecast_prediction(log, "UP", 101.0, 60_000, now=2.0)
    assert len(log) == 2


def test_log_forecast_prediction_ignores_neutral():
    log = log_forecast_prediction([], "NO CLEAR EDGE", 100.0, 0, now=1.0)
    assert log == []


def test_resolve_forecast_log_scores_next_candle():
    log = log_forecast_prediction([], "UP", 100.0, 0, now=1.0)
    klines = [[0, 100, 101, 99, 100.5, 10, 59_999], [60_000, 100, 102, 100, 101.0, 10, 119_999]]
    log = resolve_forecast_log(log, klines)
    assert log[0]["resolved"] is True
    assert log[0]["actual"] == "UP"
    assert log[0]["hit"] is True


def test_resolve_forecast_log_marks_miss():
    log = log_forecast_prediction([], "UP", 100.0, 0, now=1.0)
    klines = [[0, 100, 101, 99, 100.5, 10, 59_999], [60_000, 100, 101, 98, 99.0, 10, 119_999]]
    log = resolve_forecast_log(log, klines)
    assert log[0]["actual"] == "DOWN"
    assert log[0]["hit"] is False


def test_resolve_forecast_log_leaves_pending_when_candle_missing():
    log = log_forecast_prediction([], "UP", 100.0, 0, now=1.0)
    log = resolve_forecast_log(log, [])
    assert log[0]["resolved"] is False


def test_summarize_forecast_log():
    log = log_forecast_prediction([], "UP", 100.0, 0, now=1.0)
    klines = [[0, 100, 101, 99, 100.5, 10, 59_999], [60_000, 100, 102, 100, 101.0, 10, 119_999]]
    log = resolve_forecast_log(log, klines)
    summary = summarize_forecast_log(log)
    assert summary["logged"] == 1
    assert summary["resolved"] == 1
    assert summary["hits"] == 1
    assert abs(summary["hit_rate"] - 1.0) < 1e-9
