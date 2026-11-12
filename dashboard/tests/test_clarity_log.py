import datetime
import json

import pytest

import clarity_log

T0 = datetime.datetime(2026, 10, 21, 12, 0, 0)


def minutes(n):
    return datetime.timedelta(minutes=n)


def health(state="clear", score=142.8, age_sec=30, camera_ok=True, checked=True):
    clarity = {
        "checked": checked, "clear": state == "clear", "blur_score": score,
        "blank": state == "blank", "blurry": state == "blurry",
        "obstructed": state == "obstructed", "age_sec": age_sec,
    }
    return {"camera": {"ok": camera_ok, "clarity": clarity}}


@pytest.fixture
def path(tmp_path):
    return tmp_path / "clarity_events.jsonl"


def rows(path):
    return clarity_log.load(path=path)


# --- deriving the state -------------------------------------------------------------


@pytest.mark.parametrize("state", ["clear", "blurry", "blank", "obstructed"])
def test_state_comes_from_the_clarity_flags(state):
    assert clarity_log.derive_state(health(state))[0] == state


def test_blank_outranks_obstructed_outranks_blurry():
    h = health("blurry")
    h["camera"]["clarity"]["obstructed"] = True
    assert clarity_log.derive_state(h)[0] == "obstructed"
    h["camera"]["clarity"]["blank"] = True
    assert clarity_log.derive_state(h)[0] == "blank"


@pytest.mark.parametrize("h", [
    {},
    {"camera": {}},
    {"camera": {"clarity": None}},
    health(checked=False),
    health("clear", camera_ok=False),
])
def test_state_is_unknown_when_clarity_cannot_be_trusted(h):
    # Camera down or never checked is not "clear" — saying so would paint a healthy bar
    # across exactly the time nothing was looking.
    assert clarity_log.derive_state(h)[0] == "unknown"


def test_blur_score_is_carried_through():
    assert clarity_log.derive_state(health("blurry", score=12.5)) == ("blurry", 12.5)


# --- recording ----------------------------------------------------------------------


def test_first_sample_is_recorded(path):
    clarity_log.record(health("clear"), 5.0, now=T0, path=path)

    [row] = rows(path)
    assert row["state"] == "clear"
    assert row["blur_score"] == 142.8


def test_row_is_stamped_with_when_the_check_ran_not_when_it_was_seen(path):
    clarity_log.record(health("blurry", age_sec=120), 5.0, now=T0, path=path)

    [row] = rows(path)
    assert row["ts"] == (T0 - datetime.timedelta(seconds=120)).isoformat(timespec="seconds")


def test_unchanged_state_writes_nothing(path):
    # 30s polls re-read the same cached result for ~5 minutes; logging each would bury the
    # transitions that are the whole point.
    clarity_log.record(health("clear"), 5.0, now=T0, path=path)
    clarity_log.record(health("clear"), 5.0, now=T0 + minutes(1), path=path)
    clarity_log.record(health("clear", score=150), 5.0, now=T0 + minutes(2), path=path)

    assert len(rows(path)) == 1


def test_a_transition_is_recorded_with_the_previous_state(path):
    clarity_log.record(health("clear"), 5.0, now=T0, path=path)
    clarity_log.record(health("blurry", score=20.0), 5.0, now=T0 + minutes(5), path=path)

    last = rows(path)[-1]
    assert (last["state"], last["prev_state"], last["blur_score"]) == ("blurry", "clear", 20.0)


def test_stale_snapshot_records_nothing(path):
    clarity_log.record(health("blurry"), 600.0, now=T0, path=path)

    assert rows(path) == []


def test_missing_health_records_nothing(path):
    clarity_log.record(None, 5.0, now=T0, path=path)

    assert rows(path) == []


def test_a_gap_is_marked_and_the_state_rewritten_after_it(path):
    clarity_log.record(health("clear"), 5.0, now=T0, path=path)
    # Dashboard off / Pi unreachable for an hour, then same state again: the hour is not
    # "clear", it is unknown, and the log has to say so.
    clarity_log.record(health("clear"), 5.0, now=T0 + minutes(60), path=path)

    got = rows(path)
    assert [r["state"] for r in got] == ["clear", "gap", "clear"]
    assert got[1]["ts"] == T0.isoformat(timespec="seconds")
    assert got[1]["until"] == (T0 + minutes(60)).isoformat(timespec="seconds")


def test_short_silence_is_not_a_gap(path):
    clarity_log.record(health("clear"), 5.0, now=T0, path=path)
    clarity_log.record(health("clear"), 5.0, now=T0 + minutes(5), path=path)

    assert len(rows(path)) == 1


def test_gap_detection_survives_a_restart(path):
    # Last-seen lives on disk, not in a module variable.
    clarity_log.record(health("clear"), 5.0, now=T0, path=path)
    clarity_log.record(health("clear"), 5.0, now=T0 + minutes(120), path=path)

    assert "gap" in [r["state"] for r in rows(path)]


def test_write_failure_is_swallowed(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")

    clarity_log.record(health("clear"), 5.0, now=T0, path=blocker / "sub" / "log.jsonl")


def test_truncated_line_is_skipped(path):
    clarity_log.record(health("clear"), 5.0, now=T0, path=path)
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"ts": "2026-10-2')

    assert len(rows(path)) == 1


def test_missing_file_reads_as_empty(path):
    assert rows(path) == []


# --- retention ----------------------------------------------------------------------


def test_rows_older_than_retention_are_trimmed_keeping_a_baseline(path):
    old = T0 - datetime.timedelta(days=40)
    older = T0 - datetime.timedelta(days=45)
    with path.open("w", encoding="utf-8") as handle:
        for ts, state in ((older, "blurry"), (old, "clear")):
            handle.write(json.dumps({"ts": ts.isoformat(timespec="seconds"), "state": state}) + "\n")

    clarity_log.record(health("blurry"), 5.0, now=T0, path=path)

    states = [r["state"] for r in rows(path)]
    # Newest pre-cutoff row stays so the first visible period still has a known state.
    assert states == ["clear", "blurry"]


# --- building the event list --------------------------------------------------------


def row(at, state, score=None, until=None):
    r = {"ts": at.isoformat(timespec="seconds"), "state": state, "blur_score": score}
    if until:
        r["until"] = until.isoformat(timespec="seconds")
    return r


def flat(days):
    return [e for day in days for e in day["events"]]


def test_periods_run_until_the_next_row_and_newest_comes_first():
    data = [row(T0, "clear", 140), row(T0 + minutes(35), "blurry", 20)]

    events = flat(clarity_log.build_events(data, now=T0 + minutes(40), last_seen=T0 + minutes(40)))

    assert [e["state"] for e in events] == ["blurry", "clear"]
    assert events[1]["duration_sec"] == 35 * 60
    assert events[0]["ongoing"] is True
    assert events[1]["ongoing"] is False


def test_gap_row_becomes_a_no_data_period():
    data = [row(T0, "clear"), row(T0, "gap", until=T0 + minutes(60)), row(T0 + minutes(60), "clear")]

    events = flat(clarity_log.build_events(data, now=T0 + minutes(61), last_seen=T0 + minutes(61)))

    gap = [e for e in events if e["state"] == "gap"][0]
    assert gap["duration_sec"] == 60 * 60


def test_silence_since_last_seen_shows_as_trailing_no_data():
    data = [row(T0, "clear")]

    events = flat(clarity_log.build_events(data, now=T0 + minutes(90), last_seen=T0 + minutes(5)))

    assert events[0]["state"] == "gap"
    assert events[1]["state"] == "clear"
    assert events[1]["ongoing"] is False


def test_events_are_grouped_by_day_newest_day_first():
    day2 = T0 + datetime.timedelta(days=1)
    data = [row(T0, "clear"), row(day2, "blurry", 20)]

    days = clarity_log.build_events(data, now=day2 + minutes(5), last_seen=day2 + minutes(5))

    assert [d["day"] for d in days] == [day2.date().isoformat(), T0.date().isoformat()]


def test_problems_filter_drops_clear_and_unknown():
    data = [row(T0, "clear"), row(T0 + minutes(10), "blurry", 20),
            row(T0 + minutes(20), "unknown"), row(T0 + minutes(30), "obstructed")]

    events = flat(clarity_log.build_events(
        data, now=T0 + minutes(40), last_seen=T0 + minutes(40), problems_only=True))

    assert [e["state"] for e in events] == ["obstructed", "blurry"]


def test_no_rows_gives_no_days():
    assert clarity_log.build_events([], now=T0, last_seen=None) == []


@pytest.mark.parametrize("sec,text", [(45, "45 s"), (35 * 60, "35 min"), (3 * 3600 + 5 * 60, "3 h 5 min"),
                                      (2 * 86400 + 3600, "2 d 1 h")])
def test_duration_formatting(sec, text):
    assert clarity_log.format_duration(sec) == text
