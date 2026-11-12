import datetime
import json

import pytest

import outage_log

T0 = datetime.datetime(2026, 10, 28, 12, 0, 0)


def minutes(n):
    return datetime.timedelta(minutes=n)


def health(pipeline_ok=True, restarts=1, exit_code="0", result="success", heartbeat=2.1):
    return {
        "pipeline": {"ok": pipeline_ok, "heartbeat_age_sec": heartbeat, "duplicate_instances": False},
        "service": {
            "hailo-detection.service": {
                "ok": pipeline_ok, "active_state": "active" if pipeline_ok else "failed",
                "sub_state": "running" if pipeline_ok else "failed", "result": result,
                "exit_code": exit_code, "restarts": restarts, "uptime_sec": 120.0,
            },
        },
    }


@pytest.fixture
def path(tmp_path):
    return tmp_path / "outage_events.jsonl"


def rows(path):
    return outage_log.load(path=path)


# --- deriving the state -------------------------------------------------------------


def test_running_when_the_pipeline_is_ok():
    assert outage_log.derive_state(health(True), 10)[0] == "running"


def test_pipeline_down_when_the_pipeline_is_not_ok():
    assert outage_log.derive_state(health(False), 10)[0] == "pipeline_down"


def test_agent_stale_outranks_everything_when_the_snapshot_is_old():
    # A frozen snapshot saying "ok" is the thing that must never read as running.
    assert outage_log.derive_state(health(True), 999)[0] == "agent_stale"


@pytest.mark.parametrize("h", [{}, {"pipeline": {}}, {"pipeline": {"ok": None}}, None])
def test_unknown_when_pipeline_ok_is_not_a_bool(h):
    assert outage_log.derive_state(h, 10)[0] == "unknown"


def test_pipeline_down_carries_the_service_evidence():
    _, evidence = outage_log.derive_state(health(False, restarts=7, exit_code="1", result="exit-code"), 10)

    assert evidence["restarts"] == 7
    assert evidence["exit_code"] == "1"
    assert evidence["result"] == "exit-code"
    assert evidence["active_state"] == "failed"


def test_evidence_is_empty_when_the_service_block_is_missing():
    h = health(False)
    del h["service"]

    assert outage_log.derive_state(h, 10)[1] == {}


# --- recording ----------------------------------------------------------------------


def test_first_snapshot_writes_a_row(path):
    outage_log.record(health(True), 10, now=T0, path=path)

    [row] = rows(path)
    assert row["state"] == "running"
    assert row["prev_state"] is None


def test_unchanged_state_writes_nothing_more(path):
    outage_log.record(health(True), 10, now=T0, path=path)
    outage_log.record(health(True), 10, now=T0 + minutes(1), path=path)

    assert len(rows(path)) == 1


def test_a_transition_writes_a_row_with_evidence(path):
    outage_log.record(health(True), 10, now=T0, path=path)
    outage_log.record(health(False, restarts=4, exit_code="1"), 10, now=T0 + minutes(1), path=path)

    down = rows(path)[-1]
    assert down["state"] == "pipeline_down"
    assert down["prev_state"] == "running"
    assert down["evidence"]["restarts"] == 4


def test_a_stale_snapshot_is_recorded_as_agent_stale_not_skipped(path):
    outage_log.record(health(True), 10, now=T0, path=path)
    outage_log.record(health(True), 400, now=T0 + minutes(5), path=path)

    assert rows(path)[-1]["state"] == "agent_stale"


def test_no_snapshot_at_all_records_nothing(path):
    outage_log.record(None, None, now=T0, path=path)

    assert rows(path) == []


def test_a_long_silence_is_written_as_a_gap(path):
    outage_log.record(health(True), 10, now=T0, path=path)
    outage_log.record(health(True), 10, now=T0 + minutes(30), path=path)

    states = [r["state"] for r in rows(path)]
    assert states == ["running", "gap", "running"]


def test_an_unwritable_path_does_not_raise(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")

    outage_log.record(health(True), 10, now=T0, path=blocker / "sub" / "log.jsonl")


def test_rows_past_retention_are_trimmed_keeping_the_newest_old_one(path):
    old = T0 - datetime.timedelta(days=40)
    older = T0 - datetime.timedelta(days=45)
    path.write_text(
        json.dumps({"ts": older.isoformat(timespec="seconds"), "state": "running"}) + "\n"
        + json.dumps({"ts": old.isoformat(timespec="seconds"), "state": "pipeline_down"}) + "\n",
        encoding="utf-8",
    )

    outage_log.record(health(True), 10, now=T0, path=path)

    kept = [r["state"] for r in rows(path)]
    assert kept == ["pipeline_down", "running"]


def test_corrupt_lines_are_skipped_on_load(path):
    path.write_text('{"ts": "2026-10-28T12:00:00", "state": "running"}\n{half\n', encoding="utf-8")

    assert [r["state"] for r in rows(path)] == ["running"]


# --- building events ----------------------------------------------------------------


def test_events_group_by_day_newest_first_with_durations():
    r = [
        {"ts": (T0 - minutes(60)).isoformat(), "state": "running"},
        {"ts": (T0 - minutes(30)).isoformat(), "state": "pipeline_down",
         "evidence": {"restarts": 3, "exit_code": "1"}},
        {"ts": (T0 - minutes(10)).isoformat(), "state": "running"},
    ]

    [day] = outage_log.build_events(r, now=T0, last_seen=T0)

    states = [e["state"] for e in day["events"]]
    assert states == ["running", "pipeline_down", "running"]
    down = day["events"][1]
    assert down["duration_text"] == "20 min"
    assert down["evidence"]["restarts"] == 3
    assert day["events"][0]["ongoing"] is True


def test_problems_only_drops_running_periods():
    r = [
        {"ts": (T0 - minutes(60)).isoformat(), "state": "running"},
        {"ts": (T0 - minutes(30)).isoformat(), "state": "pipeline_down"},
    ]

    [day] = outage_log.build_events(r, now=T0, last_seen=T0, problems_only=True)

    assert [e["state"] for e in day["events"]] == ["pipeline_down"]


def test_silence_after_the_last_row_shows_as_a_gap_not_an_ongoing_period():
    r = [{"ts": (T0 - minutes(120)).isoformat(), "state": "running"}]

    [day] = outage_log.build_events(r, now=T0, last_seen=T0 - minutes(60))

    assert day["events"][0]["state"] == "gap"
    assert day["events"][0]["ongoing"] is True
