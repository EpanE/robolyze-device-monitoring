# `health.json` schema this dashboard renders

Written every 60s by `utils/health_agent.py` (`health-agent.service`) on the Pi to
`/run/roadmon/health.json`, shipped 2026-08-20 (Phases 0-3, commits `f41d4c4`/`482a65b`/`b1b8f8d`
on `sys-monitoring`). This replaces the pre-Phase-1 invented schema this file used to describe.

```json
{
  "run_dir_fallback": false,
  "system": {"ok": true, "cpu_temp_c": 44.6, "throttled": "0x0",
             "disk_total_gb": 61.7, "disk_free_gb": 19.5, "disk_used_pct": 63.3,
             "disk_alert": false, "disk_alert_pct": 85},
  "pipeline": {"ok": true, "instances": 1, "duplicate_instances": false,
               "heartbeat_age_sec": 2.1, "fps": 8.4, "confirmed_min_confidence": 0.75,
               "error": null},
  "upload": {"ok": true, "queue_depth": 0, "dropped_total": 0, "error": null},
  "camera": {"ok": true, "tcp_ok": true, "tcp_connect_ms": 0.8,
             "clarity": {"checked": true, "clear": true, "blur_score": 17.7,
                          "blur_spread": [16.46, 18.87], "age_sec": 240.0},
             "drift_scan": null},
  "gps": {"ok": null, "reason": "no GPS reader running (state file absent)",
          "last_valid_age_sec": null, "last_sentence_age_sec": null, "speed_kmh": null},
  "tailscale": {"ok": true, "key_expiry_days": null}
}
```

## Rendering rules

- **`ok` is three-valued**: `true` / `false` / `null`. `null` means *not determined*, not an
  error — render grey/unknown, never red or green. Normal case: `gps.ok == null` whenever the
  pipeline is stopped (GPS port is exclusive; `pipeline.ok` already reports the stopped state,
  so a red GPS card too would double-alarm one fault).
- **`camera.clarity` is advisory only** — it never colours `camera.ok` (TCP reachability
  alone). Thresholds are barely calibrated (a known-clear lens has scored 1-in-6 "blurry" false
  positives); treat `clear: false` as "worth a look," not "broken." `blur_spread` is shown next
  to it — a wide spread means scene motion, not a dirty lens.
- **`tailscale.key_expiry_days: null` means expiry is disabled** — the desired state. Never
  render as unknown/expired. (`ok`/`key_expiry_days` for Tailscale live inside `health.json`;
  `BackendState` for the live Tailscale card is fetched independently via
  `tailscale status --json` so that card still works even if `health.json` is stale/missing.)
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
  banner raised, report withheld. Only the Tailscale card, fetched over its own command,
  is exempt.
- Trust `ok` / `disk_alert` as computed on the Pi — the dashboard never re-derives thresholds
  from raw fields, so there's exactly one place (`health_agent.py`) that decides what counts as
  healthy.
- Sub-fields inside `pipeline`/`upload`/`camera.clarity` may be absent when that component isn't
  running — always read with `.get()` + a fallback, never bare dict indexing.

`tailscale status --json`'s own output is used as-is for the Tailscale card's `BackendState` —
fetched over a separate SSH command from `health.json`, so the card still renders even when
`health.json` is missing or the agent has crashed.
