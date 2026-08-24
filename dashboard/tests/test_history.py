import datetime
import json

import pytest

import history

HEALTH = {
    "system": {"ok": True, "cpu_temp_c": 44.6, "disk_used_pct": 63.3, "disk_alert": False},
    "pipeline": {"ok": True, "fps": 8.4, "heartbeat_age_sec": 2.1},
    "upload": {"ok": True, "queue_depth": 0, "dropped_total": 0},
    "gps": {"ok": True, "speed_kmh": 15.0},
}


@pytest.fixture
def path(tmp_path):
    return tmp_path / "history.jsonl"


def test_record_appends_one_line_per_refresh(path):
    history.record(HEALTH, 12.0, path=path)
    history.record(HEALTH, 13.0, path=path)

    assert len(path.read_text().strip().splitlines()) == 2


def test_record_captures_the_fields_a_trend_is_read_from(path):
    history.record(HEALTH, 12.0, path=path, ts=datetime.datetime(2026, 8, 24, 12, 0, 0))

    entry = json.loads(path.read_text().strip())

    assert entry["ts"] == "2026-08-24T12:00:00"
    assert entry["fps"] == 8.4
    assert entry["cpu_temp_c"] == 44.6
    assert entry["disk_used_pct"] == 63.3
    assert entry["queue_depth"] == 0
    assert entry["dropped_total"] == 0
    assert entry["snapshot_age_sec"] == 12.0


def test_record_writes_nulls_rather_than_dropping_a_cycle(path):
    # A stopped pipeline omits its section. The gap is itself the signal — dropping the
    # row instead would draw a continuous line straight across an outage.
    history.record({"system": {"cpu_temp_c": 44.6}}, 12.0, path=path)

    entry = json.loads(path.read_text().strip())

    assert entry["fps"] is None
    assert entry["cpu_temp_c"] == 44.6


def test_record_ignores_a_missing_snapshot(path):
    history.record(None, None, path=path)
    assert not path.exists()


def test_record_ignores_a_stale_snapshot(path):
    # Frozen values repeated every 30s would draw a flat line that looks like a healthy,
    # steady truck — the exact misreading the staleness work exists to prevent.
    history.record(HEALTH, 9999.0, path=path)
    assert not path.exists()


def test_load_returns_entries_oldest_first(path):
    history.record(HEALTH, 1.0, path=path, ts=datetime.datetime(2026, 8, 24, 12, 0, 0))
    history.record(HEALTH, 2.0, path=path, ts=datetime.datetime(2026, 8, 24, 12, 0, 30))

    entries = history.load(path=path)

    assert [e["snapshot_age_sec"] for e in entries] == [1.0, 2.0]


def test_load_on_a_missing_file_is_empty_not_an_error(path):
    assert history.load(path=path) == []


def test_load_skips_a_corrupt_line(path):
    # A half-written line from a kill -9 mid-append must not take the whole trend with it.
    history.record(HEALTH, 1.0, path=path)
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"ts": "2026-08-24T12:00:0\n')
    history.record(HEALTH, 2.0, path=path)

    assert len(history.load(path=path)) == 2


def test_load_returns_only_the_most_recent_window(path):
    for i in range(10):
        history.record({**HEALTH, "upload": {"queue_depth": i}}, 1.0, path=path)

    entries = history.load(path=path, limit=3)

    assert [e["queue_depth"] for e in entries] == [7, 8, 9]


def test_the_file_is_trimmed_rather_than_growing_forever(path, monkeypatch):
    monkeypatch.setattr(history, "MAX_ENTRIES", 5)

    for _ in range(30):
        history.record(HEALTH, 1.0, path=path)

    # Trimmed on a threshold, not on every append — rewriting the file each refresh would
    # cost more than the trend is worth.
    assert len(path.read_text().strip().splitlines()) <= history.MAX_ENTRIES * 2


def test_trimming_keeps_the_newest_entries(path, monkeypatch):
    monkeypatch.setattr(history, "MAX_ENTRIES", 5)

    for i in range(40):
        history.record({**HEALTH, "upload": {"queue_depth": i}}, 1.0, path=path)

    entries = history.load(path=path)

    assert entries[-1]["queue_depth"] == 39


def test_series_pulls_one_field_across_entries():
    entries = [{"fps": 8.0}, {"fps": None}, {"fps": 9.0}]
    assert history.series(entries, "fps") == [8.0, None, 9.0]


# --- sparklines ---------------------------------------------------------------------


def test_sparkline_maps_the_low_value_to_the_bottom_and_the_high_to_the_top():
    points = history.sparkline_points([0.0, 10.0], width=100, height=20)

    assert points[0] == (0.0, 20.0)
    assert points[1] == (100.0, 0.0)


def test_sparkline_skips_gaps():
    # A None is a cycle with no reading. It must break the line, not be drawn as zero.
    points = history.sparkline_points([1.0, None, 3.0], width=100, height=20)

    assert len(points) == 2
    assert [p[0] for p in points] == [0.0, 100.0]


def test_sparkline_of_a_flat_series_is_a_middle_line():
    # Zero range would divide by zero; a flat line at mid-height is the honest picture.
    points = history.sparkline_points([5.0, 5.0, 5.0], width=100, height=20)

    assert [p[1] for p in points] == [10.0, 10.0, 10.0]


def test_sparkline_needs_at_least_two_points():
    assert history.sparkline_points([], width=100, height=20) == []
    assert history.sparkline_points([1.0], width=100, height=20) == []
    assert history.sparkline_points([None, None], width=100, height=20) == []


def test_build_trends_summarises_each_series(path):
    for value in (1.0, 5.0, 3.0):
        history.record({**HEALTH, "pipeline": {"fps": value}}, 1.0, path=path)

    trends = history.build_trends(history.load(path=path))
    fps = next(t for t in trends if t["key"] == "fps")

    assert fps["latest"] == 3.0
    assert fps["low"] == 1.0
    assert fps["high"] == 5.0
    assert fps["points"]


def test_build_trends_omits_a_series_with_no_readings(path):
    history.record({"system": {"cpu_temp_c": 44.6}}, 1.0, path=path)
    history.record({"system": {"cpu_temp_c": 45.0}}, 1.0, path=path)

    trends = history.build_trends(history.load(path=path))

    assert [t["key"] for t in trends] == ["cpu_temp_c"]


def test_build_trends_of_nothing_is_empty():
    assert history.build_trends([]) == []
