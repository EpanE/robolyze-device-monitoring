"""A record of when the camera was blurry, so the dashboard can answer "since when?".

The Camera card shows clarity as of now, and `history.py` keeps only numbers for ~24h. Neither
can answer "was it blurry yesterday afternoon, and for how long" — which is what decides
whether a lens needs cleaning or a mount needs fixing.

Only *transitions* are stored (clear -> blurry -> clear), not a row per poll. The Pi
re-checks clarity every ~5 minutes while the dashboard polls every 30s, so per-poll rows
would be mostly duplicates and 30 days of them would bury the events.

Recorded by the dashboard, so it only knows what the dashboard saw. Time the laptop was off,
or the Pi unreachable, is written down as a `gap` rather than left to read as "still
clear" — a healthy-looking bar across an outage is the one thing this log must not draw.
Flat JSONL like history.py, for the same reason: readable with `tail`, survives a restart.
"""

import datetime
import json
import logging

from pathlib import Path

import status

CLARITY_LOG_PATH = Path(__file__).resolve().parent / "logs" / "clarity_events.jsonl"

RETENTION = datetime.timedelta(days=30)

# Longer than the slowest normal rhythm (a 30s poll, a ~5 min clarity check) but short
# enough that a reboot or a dropped Tailscale link shows up as a gap, not a blip.
GAP_SEC = 600

PROBLEM_STATES = ("blurry", "blank", "obstructed")

log = logging.getLogger("dashboard")


def derive_state(health):
    """(state, blur_score) for one snapshot.

    Precedence blank > obstructed > blurry > clear: a blank frame also scores as blurry,
    and the more specific cause is the useful one. `unknown` whenever the clarity result
    cannot be trusted — never defaulted to clear.
    """
    camera = health.get("camera") if isinstance(health, dict) else None
    if not isinstance(camera, dict):
        return "unknown", None
    clarity = camera.get("clarity")
    if not isinstance(clarity, dict) or not clarity.get("checked") or camera.get("ok") is False:
        return "unknown", None

    score = status.as_float(clarity.get("blur_score"))
    for state in ("blank", "obstructed", "blurry"):
        if clarity.get(state):
            return state, score
    if clarity.get("clear"):
        return "clear", score
    return "unknown", score


def _seen_path(path):
    return path.with_suffix(".seen")


def _iso(moment):
    return moment.isoformat(timespec="seconds")


def last_seen(path=None):
    """When a fresh snapshot was last recorded, or None. Kept on disk so a restart of the
    dashboard still notices the time it spent off."""
    path = Path(path) if path is not None else CLARITY_LOG_PATH
    try:
        return datetime.datetime.fromisoformat(_seen_path(path).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def record(health, age_sec, now=None, path=None):
    """Append a row when the camera's state changed, or after a gap. Otherwise no-op.

    Skips stale snapshots for the same reason history.record does: frozen readings written
    as if live would paper over the outage.
    """
    if health is None or status.is_stale(age_sec):
        return

    path = Path(path) if path is not None else CLARITY_LOG_PATH
    now = now or datetime.datetime.now()
    state, score = derive_state(health)

    checked_at = now
    clarity_age = None
    if state != "unknown":
        clarity_age = status.as_float(health["camera"]["clarity"].get("age_sec"))
    if clarity_age and clarity_age > 0:
        # Stamp when the check ran, not when this poll noticed it.
        checked_at = now - datetime.timedelta(seconds=clarity_age)

    previous = last_seen(path)
    rows = load(path=path)
    prior = rows[-1] if rows else None
    after_gap = previous is not None and (now - previous).total_seconds() > GAP_SEC

    to_write = []
    if after_gap:
        to_write.append({"ts": _iso(previous), "state": "gap", "until": _iso(now)})
    if after_gap or prior is None or prior.get("state") != state:
        to_write.append({
            "ts": _iso(max(checked_at, previous) if after_gap else checked_at),
            "state": state,
            "blur_score": score,
            "prev_state": prior.get("state") if prior else None,
        })

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if to_write:
            with path.open("a", encoding="utf-8") as handle:
                for entry in to_write:
                    handle.write(json.dumps(entry) + "\n")
        _seen_path(path).write_text(_iso(now), encoding="utf-8")
    except OSError:
        # The log is a nice-to-have. Losing it must never cost the refresh that produced it.
        log.warning("could not write %s; clarity history is not being recorded", path,
                    exc_info=True)
        return

    if to_write:
        _trim(path, now)


def _trim(path, now):
    """Drop rows past retention, keeping the newest one before the cutoff.

    That one row is what says what state the camera was in when the visible window opens;
    without it the oldest period would have no start.
    """
    cutoff = _iso(now - RETENTION)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        parsed = [(_parse(line), line) for line in lines if line.strip()]
        old = [i for i, (entry, _) in enumerate(parsed) if entry and entry.get("ts", "") < cutoff]
        if len(old) <= 1:
            return
        drop = set(old[:-1])
        kept = [line for i, (_, line) in enumerate(parsed) if i not in drop]
        path.write_text("\n".join(kept) + "\n", encoding="utf-8")
    except OSError:
        log.warning("could not trim %s", path, exc_info=True)


def _parse(line):
    try:
        entry = json.loads(line)
    except json.JSONDecodeError:
        # A half-written line from a kill -9 mid-append costs one row, not the whole log.
        return None
    return entry if isinstance(entry, dict) else None


def load(path=None):
    """Rows oldest-first. Missing file reads as no history."""
    path = Path(path) if path is not None else CLARITY_LOG_PATH
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    return [entry for entry in map(_parse, lines) if entry]


def format_duration(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} s"
    if seconds < 3600:
        return f"{seconds // 60} min"
    if seconds < 86400:
        hours, minutes = seconds // 3600, seconds % 3600 // 60
        return f"{hours} h {minutes} min" if minutes else f"{hours} h"
    days, hours = seconds // 86400, seconds % 86400 // 3600
    return f"{days} d {hours} h" if hours else f"{days} d"


def _when(value):
    try:
        return datetime.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _event(state, start, end, score, ongoing):
    seconds = max(0, int((end - start).total_seconds()))
    return {
        "state": state,
        "start": start,
        "end": end,
        "start_text": start.strftime("%H:%M"),
        "end_text": None if ongoing else end.strftime("%H:%M"),
        "duration_sec": seconds,
        "duration_text": format_duration(seconds),
        "blur_score": score,
        "ongoing": ongoing,
    }


def build_events(rows, now, last_seen, problems_only=False):
    """Rows -> periods, grouped by the day they started, newest first.

    Each row opens a period that lasts until the next row. The final one runs to now only
    if the dashboard has been seeing snapshots; otherwise it ends at last_seen and the
    silence after it is shown as no data.
    """
    rows = [(r, _when(r.get("ts"))) for r in rows]
    rows = sorted(((r, t) for r, t in rows if t), key=lambda item: item[1])

    events = []
    for i, (row, start) in enumerate(rows):
        state = row.get("state", "unknown")
        is_last = i == len(rows) - 1
        if state == "gap":
            end = _when(row.get("until")) or (rows[i + 1][1] if not is_last else now)
            events.append(_event("gap", start, end, None, False))
            continue
        if not is_last:
            end, ongoing = rows[i + 1][1], False
        elif last_seen and (now - last_seen).total_seconds() > GAP_SEC:
            end, ongoing = last_seen, False
            events.append(_event("gap", last_seen, now, None, True))
        else:
            end, ongoing = now, True
        if end > start or ongoing:
            events.append(_event(state, start, end, row.get("blur_score"), ongoing))

    if problems_only:
        events = [e for e in events if e["state"] in PROBLEM_STATES]

    events.sort(key=lambda e: e["start"], reverse=True)
    days = []
    for event in events:
        day = event["start"].date().isoformat()
        if not days or days[-1]["day"] != day:
            days.append({"day": day, "events": []})
        days[-1]["events"].append(event)
    return days
