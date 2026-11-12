"""A record of when the detection pipeline was down, so the dashboard can answer "since when, and why?".

The Pipeline card shows the state as of now, and health.json lives on /run (tmpfs): a reboot
wipes it, so by the time anyone looks the evidence of what the service was doing is gone.
This keeps it on the laptop, alongside what the dashboard itself saw.

Same shape as `clarity_log.py` and deliberately built on it — only *transitions* are stored,
time the dashboard could not see the Pi is written down as a `gap`, and the file is flat
JSONL. What differs is the state: running / pipeline_down / agent_stale. A stale snapshot is
recorded as `agent_stale` rather than skipped, because a dead health-agent *is* an outage,
and the only one whose last reading still says "ok".

Each pipeline_down row carries the hailo-detection unit's own account of how it died
(result, exit_code, restarts) — enough to tell a crash loop from a clean stop before anyone
opens the log viewer.
"""

import datetime
import json
import logging

from pathlib import Path

import clarity_log
import status

OUTAGE_LOG_PATH = Path(__file__).resolve().parent / "logs" / "outage_events.jsonl"

RETENTION = clarity_log.RETENTION
GAP_SEC = clarity_log.GAP_SEC

PROBLEM_STATES = ("pipeline_down", "agent_stale")

DETECTION_UNIT = "hailo-detection.service"
EVIDENCE_FIELDS = ("active_state", "sub_state", "result", "exit_code", "restarts", "uptime_sec")

log = logging.getLogger("dashboard")

load = clarity_log.load
last_seen = clarity_log.last_seen
format_duration = clarity_log.format_duration


def detection_evidence(health):
    service = health.get("service")
    unit = service.get(DETECTION_UNIT) if isinstance(service, dict) else None
    if not isinstance(unit, dict):
        return {}
    return {key: unit[key] for key in EVIDENCE_FIELDS if key in unit}


def derive_state(health, age_sec):
    """(state, evidence) for one snapshot.

    A stale snapshot outranks its own contents: whatever it says about the pipeline is
    frozen. `unknown` whenever pipeline.ok is not a real bool — never defaulted to running.
    """
    if not isinstance(health, dict):
        return "unknown", {}
    if status.is_stale(age_sec):
        return "agent_stale", {}
    pipeline = health.get("pipeline")
    ok = pipeline.get("ok") if isinstance(pipeline, dict) else None
    if ok is True:
        return "running", {}
    if ok is False:
        return "pipeline_down", detection_evidence(health)
    return "unknown", {}


def record(health, age_sec, now=None, path=None):
    """Append a row when the pipeline's state changed, or after a gap. Otherwise no-op."""
    if health is None:
        return

    path = Path(path) if path is not None else OUTAGE_LOG_PATH
    now = now or datetime.datetime.now()
    state, evidence = derive_state(health, age_sec)

    previous = last_seen(path)
    rows = load(path=path)
    prior = rows[-1] if rows else None
    after_gap = previous is not None and (now - previous).total_seconds() > GAP_SEC

    to_write = []
    if after_gap:
        to_write.append({"ts": clarity_log._iso(previous), "state": "gap",
                         "until": clarity_log._iso(now)})
    if after_gap or prior is None or prior.get("state") != state:
        row = {"ts": clarity_log._iso(now), "state": state,
               "prev_state": prior.get("state") if prior else None}
        if evidence:
            row["evidence"] = evidence
        to_write.append(row)

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if to_write:
            with path.open("a", encoding="utf-8") as handle:
                for entry in to_write:
                    handle.write(json.dumps(entry) + "\n")
        clarity_log._seen_path(path).write_text(clarity_log._iso(now), encoding="utf-8")
    except OSError:
        # The log is a nice-to-have. Losing it must never cost the refresh that produced it.
        log.warning("could not write %s; outage history is not being recorded", path,
                    exc_info=True)
        return

    if to_write:
        clarity_log._trim(path, now)


def _event(state, start, end, evidence, ongoing):
    seconds = max(0, int((end - start).total_seconds()))
    return {
        "state": state,
        "start": start,
        "end": end,
        "start_text": start.strftime("%H:%M"),
        "end_text": None if ongoing else end.strftime("%H:%M"),
        "duration_sec": seconds,
        "duration_text": format_duration(seconds),
        "evidence": evidence or {},
        "ongoing": ongoing,
    }


def build_events(rows, now, last_seen, problems_only=False):
    """Rows -> periods, grouped by the day they started, newest first.

    Each row opens a period that lasts until the next row. The final one runs to now only if
    the dashboard has been seeing snapshots; otherwise it ends at last_seen and the silence
    after it is shown as no data.
    """
    rows = [(r, clarity_log._when(r.get("ts"))) for r in rows]
    rows = sorted(((r, t) for r, t in rows if t), key=lambda item: item[1])

    events = []
    for i, (row, start) in enumerate(rows):
        state = row.get("state", "unknown")
        is_last = i == len(rows) - 1
        if state == "gap":
            end = clarity_log._when(row.get("until")) or (rows[i + 1][1] if not is_last else now)
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
            events.append(_event(state, start, end, row.get("evidence"), ongoing))

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
