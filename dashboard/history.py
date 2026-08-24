"""A rolling record of past snapshots, so the dashboard can answer "since when?".

Everything else here renders the truck as it is right now. That answers "is it broken"
but never "when did it break", which is the question actually asked after the fact — did
the disk fill overnight, has fps been sagging all week, did the upload queue start backing
up before or after the reboot. One JSON line per refresh is enough to draw that.

Deliberately a flat file, not a database: the whole point is that it survives a laptop
restart and can be read with `tail`, and a dashboard for one truck does not need more.
"""

import datetime
import json
import logging

from pathlib import Path

import status

HISTORY_PATH = Path(__file__).resolve().parent / "logs" / "history.jsonl"

# ~24h at the 30s auto-poll cadence. Bounded for the same reason the log file is: the
# Pi-side logger grew to 3,078 files / 305M in the field by never deleting anything.
MAX_ENTRIES = 2880

log = logging.getLogger("dashboard")

# What a trend is actually read from, and how to present it. Everything here is a number
# that means something over time — `ok` flags are not, they are the current-state cards'
# job.
TRACKED = (
    ("fps", "Pipeline fps", ("pipeline", "fps")),
    ("heartbeat_age_sec", "Heartbeat age (s)", ("pipeline", "heartbeat_age_sec")),
    ("cpu_temp_c", "CPU temp (°C)", ("system", "cpu_temp_c")),
    ("disk_used_pct", "Disk used (%)", ("system", "disk_used_pct")),
    ("queue_depth", "Upload queue", ("upload", "queue_depth")),
    ("dropped_total", "Dropped total", ("upload", "dropped_total")),
    ("speed_kmh", "Speed (km/h)", ("gps", "speed_kmh")),
)


def _reading(health, section, field):
    values = health.get(section)
    if not isinstance(values, dict):
        return None
    return status.as_float(values.get(field))


def record(health, age_sec, ts=None, path=None):
    """Append one row. No-ops when there is nothing trustworthy to record.

    A stale snapshot is skipped rather than stored: its numbers are frozen, and writing
    them every 30s would draw a flat, healthy-looking line across exactly the outage the
    staleness check exists to make visible.
    """
    if health is None or status.is_stale(age_sec):
        return

    path = Path(path) if path is not None else HISTORY_PATH
    entry = {"ts": (ts or datetime.datetime.now()).isoformat(timespec="seconds")}
    for key, _label, (section, field) in TRACKED:
        # null rather than omitted, and the row is kept even if every reading is null:
        # a gap is a fact about the truck, and dropping the row would let the chart draw
        # a straight line across an outage as though nothing had happened.
        entry[key] = _reading(health, section, field)
    entry["snapshot_age_sec"] = status.as_float(age_sec)

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")
    except OSError:
        # A trend is a nice-to-have. Losing it must never cost the refresh that produced it.
        log.warning("could not append to %s; trend history is not being recorded", path,
                    exc_info=True)
        return

    _trim_if_needed(path)


def _trim_if_needed(path):
    """Rewrite the file down to MAX_ENTRIES once it has drifted well past it.

    On a threshold rather than every append: rewriting the whole file each refresh would
    cost more than the trend is worth, and the slack is only a few hundred KB.
    """
    try:
        with path.open("r", encoding="utf-8") as handle:
            lines = handle.readlines()
        if len(lines) <= MAX_ENTRIES * 2:
            return
        with path.open("w", encoding="utf-8") as handle:
            handle.writelines(lines[-MAX_ENTRIES:])
    except OSError:
        log.warning("could not trim %s", path, exc_info=True)


def load(path=None, limit=MAX_ENTRIES):
    """Entries oldest-first, newest last. Missing file reads as no history."""
    path = Path(path) if path is not None else HISTORY_PATH
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []

    entries = []
    for line in lines[-limit:] if limit else lines:
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            # A half-written line from a kill -9 mid-append. Skipping it costs one sample;
            # letting it raise would cost the whole trend.
            continue
    return entries


def series(entries, key):
    return [entry.get(key) for entry in entries]


def sparkline_points(values, width, height):
    """Map a series onto SVG coordinates, y inverted so high values sit high.

    Gaps (None) are dropped rather than plotted as zero — a stopped pipeline is not a
    pipeline running at 0 fps, and drawing it as one is a lie the chart would tell
    convincingly.
    """
    total = len(values)
    if total < 2:
        return []

    step = width / (total - 1)
    present = [(i, v) for i, v in enumerate(values) if isinstance(v, (int, float))]
    if len(present) < 2:
        return []

    low = min(v for _, v in present)
    high = max(v for _, v in present)
    span = high - low
    if span == 0:
        # A flat series has no range to scale against; mid-height is the honest picture.
        return [(round(i * step, 2), round(height / 2, 2)) for i, _ in present]

    return [
        (round(i * step, 2), round(height - ((v - low) / span) * height, 2))
        for i, v in present
    ]


SPARK_WIDTH = 240
SPARK_HEIGHT = 40


def build_trends(entries, width=SPARK_WIDTH, height=SPARK_HEIGHT):
    """One entry per tracked field that actually has readings, ready for the template."""
    trends = []
    for key, label, _source in TRACKED:
        values = series(entries, key)
        points = sparkline_points(values, width, height)
        if not points:
            continue
        readings = [v for v in values if isinstance(v, (int, float))]
        trends.append({
            "key": key,
            "label": label,
            "latest": readings[-1],
            "low": min(readings),
            "high": max(readings),
            "samples": len(readings),
            "points": " ".join(f"{x},{y}" for x, y in points),
            "width": width,
            "height": height,
        })
    return trends
