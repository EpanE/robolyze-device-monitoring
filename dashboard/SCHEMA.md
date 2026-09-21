# `health.json` schema this dashboard renders

Written every 60s by `utils/health_agent.py` (`health-agent.service`) on the Pi to
`/run/roadmon/health.json`. Last verified against a **live** snapshot on 2026-09-09 — the
2026-08-20 version of this file (Phases 0-3, commits `f41d4c4`/`482a65b`/`b1b8f8d`) had already
drifted from the real Pi. **If you touch `health_agent.py`, update this file in the same
change** — a stale schema doc here is worse than none, because other sessions trust it verbatim.

```json
{
  "agent_pid": 59184, "boot_id": "eb911389-...", "host": "raspberrypi",
  "iso": "2026-09-09T17:00:40+0800", "ts": 1788944440.88, "run_dir": "/run/roadmon",
  "run_dir_fallback": false,
  "system": {
    "ok": true, "cpu_temp_c": 57.9, "throttled": "0x0",
    "disk_path": "/home/rasp", "disk_total_gb": 61.7, "disk_free_gb": 19.0,
    "disk_used_pct": 64.0, "disk_alert": false, "disk_alert_pct": 85,
    "load_avg": [6.63, 6.83, 6.87], "mem_available_kb": 12181648, "mem_total_kb": 16604144,
    "uptime_sec": 6450.8
  },
  "pipeline": {
    "ok": true, "instances": 1, "instance_pids": [54871], "duplicate_instances": false,
    "heartbeat_age_sec": 3.9, "heartbeat_file": "/run/roadmon/pipeline.alive",
    "fps": 28.51, "frame": 146293, "confirmed_min_confidence": 0.75,
    "stale_after_sec": 30, "writer_alive": true, "writer_pid": 54871
  },
  "upload": {
    "ok": true, "queue_depth": 0, "queue_maxsize": 8, "dropped_total": 0,
    "training_enabled": 1, "training_queue_depth": 0, "training_queue_maxsize": 64,
    "training_dropped_total": 0
  },
  "camera": {
    "ok": true, "tcp_ok": true, "tcp_connect_ms": 1.1, "host": "192.168.1.102",
    "port": 554, "url": "rtsp://192.168.1.102/live/mainstream",
    "clarity": {
      "checked": true, "clear": true, "blur_score": 142.83, "blur_spread": [128.67, 157.88],
      "blank": false, "blurry": false, "obstructed": false, "samples": 3,
      "mean": 101.5, "std": 25.5, "frame_size": [2688, 1944], "grab_sec": 3.59, "age_sec": 165.0
    },
    "drift_scan": null
  },
  "gps": {
    "ok": true, "device_present": true, "port": "/dev/gps", "state_file": "/run/roadmon/gps_state.json",
    "nmea_status": "status=A", "lat": 3.378066, "lon": 101.3980975,
    "last_valid_age_sec": 0.0, "last_sentence_age_sec": 0.0, "sentences_per_sec": 9.05,
    "speed_kmh": 0.11, "stale_after_sec": 120, "reopen_total": 1,
    "writer_pid": 54871, "writer_age_sec": 4.9
  },
  "tailscale": {
    "ok": true, "checked": true, "online": true, "backend_state": "Running",
    "hostname": "raspberrypi", "key_expiry_days": null,
    "tailscale_ips": ["100.110.189.87", "fd7a:115c:a1e0::b736:bd57"],
    "version": "1.102.2-t6cac91817-g6ff0ddc72",
    "health": ["Some peers are advertising routes but --accept-routes is false"]
  },
  "self_heal": {"enabled": true, "self_heals": 0, "tailscale_bad_cycles": 0, "actions": []},
  "service": {
    "hailo-detection.service": {"checked": true, "ok": true, "active_state": "active", "sub_state": "running",
      "result": "success", "exit_code": "0", "exit_status": 0, "restarts": 1, "uptime_sec": 4897.9, "active_since_ts": 1788939543.0},
    "health-agent.service": {"checked": true, "ok": true, "active_state": "active", "sub_state": "running",
      "result": "success", "exit_code": "0", "exit_status": 0, "restarts": 0, "uptime_sec": 4785.9, "active_since_ts": 1788939655.0}
  }
}
```

## Removed since 2026-08-20 — do not rely on these

- **`pipeline.error` / `upload.error` no longer exist** on the current agent build. `status.py`'s
  `.get("error") or "not running"` / `"no data"` fallback already degrades gracefully to a
  generic (less specific) message — this is a documentation correction, not a functional bug.
  Older agent images may still emit these keys; the dashboard supports both.
- **`gps.reason` no longer exists.** `_gps_card` prefers it verbatim if present (older agents),
  otherwise constructs a message from `gps.device_present`/`gps.nmea_status`.

## New since 2026-08-20

- **`self_heal`** — the auto-recovery subsystem's own counters. `self_heals` is a cumulative
  count of recovery actions taken this boot; `actions` is a list of recent actions (shape not
  pinned down yet — read defensively). Surfaced as a badge on the System card: green "0 actions"
  when nothing has fired, amber when it has (informational — a self-heal firing means the system
  *recovered*, not that it's currently broken), red if `enabled: false`.
- **`service`** — per-`systemd`-unit health, keyed by unit name. `ok`/`active_state`/`restarts`
  are the fields the dashboard reads; a climbing `restarts` count is an early reliability signal.
  Folded into the System card's detail line as `services: <short-name>=<active_state>(restarts=N), ...`,
  with a trailing `— SERVICE ALERT` if any unit's own `ok` is `false`. This never recolors the
  System card itself (same "trust the Pi's `system.ok`, never re-derive" rule as `disk_alert`).
- **`system.load_avg` / `mem_available_kb` / `mem_total_kb` / `uptime_sec`** — standard Linux ops
  signals, shown in the System card detail as `load_avg=1m/5m/15m`, `mem=N% used`, `uptime=XhYm`.
- **`camera.clarity.blank` / `.blurry` / `.obstructed`** — three *different* failure modes with
  different operator actions (no signal vs. lens blocked vs. out of focus). The clarity badge
  distinguishes them by label (`BLANK`/`OBSTRUCTED`/`BLURRY`), checked in that priority order
  when more than one is true (blank is the most severe — the camera may not be producing a usable
  frame at all).
- **`gps.device_present` / `nmea_status` / `reopen_total` / `writer_age_sec` /
  `sentences_per_sec` / `stale_after_sec`** — `device_present`/`nmea_status` feed the
  `gps.ok == null` fallback message; `reopen_total` (device flakiness — climbing count means the
  GPS device keeps needing to be reopened) and `writer_age_sec` are appended to the normal detail
  line whenever present.
- **`upload.training_enabled` / `training_queue_depth` / `training_queue_maxsize` /
  `training_dropped_total`** — the training-data upload path, parallel to the main detection
  upload queue. Surfaced in the Upload card detail as `training: queue_depth=N, dropped_total=N`
  (with `(disabled)` appended if `training_enabled` is falsy). No derived "queue nearly full"
  alert is added — the Pi hasn't given us a threshold boolean for this queue.
- **`tailscale.health`** — health.json's own Tailscale section now embeds `backend_state` and a
  `health` warnings array (Tailscale's own self-reported advisories, e.g. "peers advertising
  routes but --accept-routes is false"). **This does NOT become the source of truth for the
  Tailscale card's `ok`/`BackendState`** — that stays the live `tailscale status --json` SSH call
  (`ssh_client.fetch_tailscale_status`) specifically so the card still renders when `health.json`
  is stale or missing. The `health` array is surfaced as an additional amber advisory badge on
  the Tailscale card when non-empty.

## Rendering rules

- **`ok` is three-valued**: `true` / `false` / `null`. `null` means *not determined*, not an
  error — render grey/unknown, never red or green. Normal case: `gps.ok == null` whenever the
  pipeline is stopped (GPS port is exclusive; `pipeline.ok` already reports the stopped state,
  so a red GPS card too would double-alarm one fault).
- **`camera.clarity` is advisory only** — it never colours `camera.ok` (TCP reachability alone).
  Thresholds are barely calibrated (a known-clear lens has scored 1-in-6 "blurry" false
  positives); treat `clear: false` as "worth a look," not "broken." `blank`/`obstructed`/`blurry`
  distinguish failure modes for the badge text/emoji; `blur_spread` is shown next to
  `blur_score` — a wide spread means scene motion, not a dirty lens.
- **`tailscale.key_expiry_days: null` means expiry is disabled** — the desired state. Never
  render as unknown/expired. `ok`/`key_expiry_days`/`BackendState`-equivalent text for the
  Tailscale card's coloring come from the live `tailscale status --json` fetch, not
  `health.json`, so that card still works even if `health.json` is stale/missing.
  `tailscale.health` (the warnings array), by contrast, is *only* readable from `health.json` —
  it's advisory-only and shown as a badge, never affects `ok`.
- **`self_heal`/`service` are advisory/informational only** — they never recolor the System card;
  System's color stays driven solely by `system.ok` (and `disk_alert` text-only), same as before.
- **`clarity.age_sec`** is how stale the cached clarity result is (runs every ~300s, not every
  60s cycle) — always show it.
- **`run_dir_fallback: true`** means the Pi's tmpfiles rule is missing and the agent fell back
  to `<repo>/.roadmon` — the dashboard shows a warning banner (`templates/base.html`); the rest
  of the snapshot is still valid.
- **The file's age is part of the contract.** Nothing inside the JSON says when it was
  written, and a dead `health-agent.service` leaves its last snapshot on disk reading
  back as a perfectly healthy truck. The dashboard reads `stat -c %Y` alongside the file
  (and `date +%s`, so the age is computed on the Pi's clock rather than the laptop's) and
  treats anything older than 180s — three missed 60s cycles — as stale: health cards grey,
  banner raised, report withheld. Only the Tailscale card, fetched over its own command, and the
  `key_expiry_days`/`tailscale.health` values threaded through from the last-known `health.json`,
  are exempt.
- Trust `ok` / `disk_alert` / per-service `ok` as computed on the Pi — the dashboard never
  re-derives thresholds from raw fields, so there's exactly one place (`health_agent.py`) that
  decides what counts as healthy.
- Sub-fields inside `pipeline`/`upload`/`camera.clarity`/`gps`/`self_heal`/`service` may be
  absent when that component isn't running, or on an older agent build — always read with
  `.get()` + a fallback, never bare dict indexing.

`tailscale status --json`'s own output is used as-is for the Tailscale card's `BackendState`
(and hence `ok`) — fetched over a separate SSH command from `health.json`, so the card still
renders even when `health.json` is missing or the agent has crashed.
