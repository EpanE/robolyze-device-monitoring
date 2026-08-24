# Truck Fleet Monitoring — Complete Implementation Plan

**Based on:** Approved design handoff (2026-07-24), extended after the 2026-07-27 live SSH
capture (`reports/`) and the two-plane design discussion.
**Goal:** A **two-plane** monitoring system for the Duromac truck unit
(Raspberry Pi 5 + Hailo-8, running the Robolyze road-defect detector):

- **Plane A — Alerting (push):** a featherweight Pi health agent pushes health out over
  **plain cellular HTTPS** to Healthchecks.io (5 checks) → Telegram, with self-healing.
  A dead-man's-switch that survives Tailscale / inbound network being down. Answers
  *"something broke / the truck went down."*
- **Plane B — Dashboard (pull):** a **laptop-side** web dashboard the team runs locally,
  which SSH-pulls the agent's snapshot + a live screenshot over Tailscale. **No persistent
  server is added to the Pi.** Answers *"show me everything, live."*

**Design rule:** Plane B (UI / Tailscale) failure must **never** blind Plane A (alerting).
Alerting stays out-of-band over cellular; the dashboard is best-effort convenience.

**Estimated total effort:** ~6–8 working days of engineering + ~1–2 weeks of passive field tuning.

---

## Why this is needed (field evidence, `reports/`)

- **3 reboots in 2 days, ≥2 unclean (power-loss)** — nobody is alerted when the truck dies.
- **Two detection-app instances were running concurrently** (accidental manual re-launch on
  top of the systemd one) — double uploads, no cross-process dedup, load avg 6.24 on a 4-core.
- **Launch wrapper phases 1–2 retry forever with no timeout** — a parked/broken truck sits
  silently stuck with no external signal.
- **Tailscale has logged out before** — cutting off all remote access with no auto-recovery.
- None of this plan is deployed yet; the device is a pre-Phase-0 baseline on branch
  `sys-monitoring`.

**Load note:** the agent averages ~0.1–0.3% of one core (60s sleep loop). Killing the
duplicate pipeline frees ~a whole core. **Net Pi load after this work is lower than today.**

---

## Shared data model — one snapshot, three consumers

The agent builds **one health snapshot dict** every cycle and:
1. POSTs it to Healthchecks (Plane A alerting).
2. Writes it atomically to `/run/roadmon/health.json` (Plane B dashboard reads this).
3. Uses it to drive self-healing decisions.

This is the single source of truth. The laptop dashboard never recomputes anything on the
Pi — it just `ssh cat`s `health.json` plus one live screenshot grab.

---

## Phase 0 — Prerequisites & one-time setup (½ day, no code)

| # | Task | Output / acceptance |
|---|------|---------------------|
| 0.1 | Create Healthchecks.io account (free tier) + project "Duromac Fleet" | Project key available |
| 0.2 | Create 5 checks: `truck-alive` (60s/15m grace), `truck-pipeline` (60s/5m), `truck-camera` (60s/5m), `truck-gps` (60s/10m), `truck-tailscale` (60s/10m) | 5 ping URLs |
| 0.3 | Cron schedule on `truck-alive` for working hours (e.g. `Mon–Sat 07:00–19:00`) | Overnight silence never alerts |
| 0.4 | Telegram group (engineer + manager) + Healthchecks native Telegram integration on all 5 checks | Test ping → Telegram message |
| 0.5 | Add env vars to `.env.sample` / Pi `.env`: `HEALTHCHECKS_BASE_URL`, `HEALTH_AGENT_INTERVAL_SEC=60`, `HEALTH_PIPELINE_STALE_SEC=30`, `HEALTH_GPS_STALE_SEC=120`, `HEALTH_TAILSCALE_HEAL_CYCLES=3`, `HEALTH_DISK_ALERT_PCT=85`, `HEALTH_CAMERA_CHECK_INTERVAL_SEC=300`, `HEALTH_CAMERA_BLANK_MEAN_THRESH`, `HEALTH_CAMERA_OBSTRUCTED_STD_THRESH`, `TS_AUTHKEY=...` | `config/settings.py` reads them |
| 0.6 | Create `/run/roadmon/` via `tmpfiles.d` (`d /run/roadmon 0755 rasp rasp`) so it survives reboots | Directory exists after reboot |
| 0.7 | **Tailscale admin console: disable key expiry on this node** (zero-code layer of Workstream/Phase 3) | Node never logs out from expiry |
| 0.8 | **Kill the currently-running duplicate manual detection instance** (urgent — it double-uploads production data right now) | Only the systemd instance runs |
| 0.9 | `git log main..sys-monitoring` — build on existing branch work, don't duplicate it. **Repo path:** `/home/rasp/miniproj_epan/hailo-rpi5-examples` (matches running `hailo-detection.service`) — a second, unrelated checkout `~/Benchmark/hailo-rpi5-examples` (branch `main`, generic upstream Hailo examples) also exists on the box; don't confuse the two | Known starting state |

**Gate to Phase 1:** manual `curl` to each ping URL turns the check green; a forced miss
turns it red in Telegram.

---

## Phase 1 — Health agent core + `truck-alive` + dup guard (1.5 days)

**Deliverables**
- `utils/health_agent.py` (~250–350 lines, stdlib + `requests`, zero hardware deps).
- `systemd/health-agent.service` (`Restart=always`, `After=network-online.target`).
- `systemd/roadmon-shutdown.service` (oneshot, `Before=shutdown.target`) — sends
  `truck-alive/fail` with `{"reason":"clean-shutdown"}` (distinguishes graceful off from crash).
- `tests/test_health_agent.py` — mocked probes + mocked `requests`, laptop-safe (follow the
  existing `tests/` laptop-vs-Pi split).

**Agent structure (build in this order):**
1. **Snapshot collector** — pure functions, each returns a dict and never raises:
   - `system_vitals()` — CPU temp, `vcgencmd get_throttled`, disk-free (SSD→pendrive→SD),
     RAM, uptime; **disk-free % flag** (Phase 4).
   - `pipeline_state()` — mtime of `/run/roadmon/pipeline.alive` AND process exists
     (frozen process w/ stale mtime must fail); **also counts detection instances → flags >1**.
   - `camera_state()` — TCP connect `192.168.1.102:554` (stub OK this phase).
   - `gps_state()` — read `/run/roadmon/gps_state.json` (stub OK this phase).
   - `tailscale_state()` — `tailscale status --json` (stub OK this phase).
   - `upload_state()` — queue depths + dropped counts.
2. **Ping sender** — POST full JSON snapshot as body; per-check URL; 10s timeout; failures
   logged, never crash the loop. **Plain HTTPS over cellular — never route via Tailscale.**
3. **Snapshot writer** — atomic write to `/run/roadmon/health.json` (temp-file + `os.replace`,
   mirroring the existing `.did_state` pattern in `config/settings.py`).
4. **Main loop** — every 60s: collect → write file → always ping `truck-alive` →
   conditionally ping component checks.
5. **Clean-shutdown hook** — `systemd/roadmon-shutdown.service` as above.

**Duplicate-instance guard (new):**
- In `basic_pipelines/detection.py` (or early in `run_detection_fps.sh`), acquire a **`flock`**
  on `/run/roadmon/detection.lock`. If already held → log
  `"detection already running, refusing second instance"` and exit non-zero.
- Use `flock` (kernel auto-releases on process death), **not** a PID file — no stale-lock
  problem across the unclean reboots this device actually sees.
- Root cause is a manual launch bypassing systemd, so the guard lives in **app startup**,
  not the unit file. `pipeline_state()` also flags >1 instance so any recurrence is visible.

**Field test (gate to Phase 2):**
- Deploy agent, enable service. Verify 60s pings arrive with full bodies in Healthchecks and
  `/run/roadmon/health.json` updates.
- Pull the plug → `truck-alive` red + Telegram after grace; last body readable.
- Reboot → agent auto-starts, check green, recovery message.
- `systemctl kill health-agent` → auto-restart within seconds (`Restart=always`).
- Launch `detection.py` manually while service runs → second instance refuses via flock.

---

## Phase 2 — Pipeline + camera checks (1 day)

**Deliverables**
- Edit `basic_pipelines/detection.py` (~3 lines): in the **existing** ~5s frame-heartbeat
  callback, `touch /run/roadmon/pipeline.alive` and include current fps. (The heartbeat log
  line already exists; this materializes it as a liveness file.)
- Agent: real `pipeline_state()` (mtime < `HEALTH_PIPELINE_STALE_SEC` = healthy; confirm the
  process exists — a frozen process with stale mtime must fail).
- Agent: real `camera_state()`; **on TCP failure**, optionally invoke `utils/find_camera.sh`
  (reuse as-is — read-only, always exits 0, ARP/broadcast sniff) and include `drift_scan`
  result (catches the known 192.168.x subnet-drift failure mode).
  - **Confirmed shipped on the Pi (commit `b5271fb`, 2026-07-24)** — `find_camera.sh` is real,
    not hypothetical: ARP scan + passive L2 sniff, positively identified via `CAMERA_MAC` in
    `.env`. Already auto-wired one layer earlier than this plan assumed —
    `run_detection_fps.sh`'s startup retry loop fires it automatically after ~60s of camera
    unreachability (root-caused the 3-hour drift outage this was built for). Phase 2's job here
    is to **reuse/report on this confirmed-real tool** during steady-state agent monitoring, not
    build it. Dependency to flag: needs passwordless `sudo` + `nmap`/`tcpdump` on the Pi;
    degrades gracefully (still exits 0) if either is missing.
- **Camera clarity check (extends `camera_state()`):** a slower secondary timer inside the
  agent's main loop (cycle-count modulo, e.g. every 5th–10th 60s cycle — `HEALTH_CAMERA_CHECK_INTERVAL_SEC`,
  default ~300–600s) triggers a one-shot RTSP frame grab from `rtsp://192.168.1.102/live/mainstream`
  (same URL/protocol as production, TCP-only) via an **independent short-lived connection inside
  `health_agent.py`** — zero edits to `detection.py`'s hot path. On a successful grab, run three
  checks: mean-brightness (blank/black frame — `HEALTH_CAMERA_BLANK_MEAN_THRESH`),
  variance/edge-density (obstructed/lens-covered — `HEALTH_CAMERA_OBSTRUCTED_STD_THRESH`), and
  the existing `LaplacianBlurDetector` + ROI **reused as-is from `detection/blur.py`** (blurry/dirty
  lens — don't re-declare `BLUR_THRESHOLD`/`BLUR_ROI`). Cache `{clear: bool, blank, obstructed,
  blurry, last_checked_ts}` in the agent's in-memory state between checks; include it in every
  snapshot's `camera` block regardless of whether this cycle actually ran the grab.

**Field tests (gate to Phase 3):**
- Kill `detection.py` mid-run → `truck-pipeline` red in ~5–6 min while `truck-alive` stays
  green; body shows `heartbeat_age_s` climbing.
- `SIGSTOP` the pipeline (freeze, not crash) → same red — the freeze-detection proof.
- Unplug camera ethernet → `truck-camera` red; body shows `tcp_ok=false` + drift-scan result.
- Cover the lens → obstructed check goes red within one check interval; disconnect/black the
  camera → blank check goes red; smear the lens → blur check goes red (same manual test method
  as `utils/calibrate_blur.py`'s existing calibration workflow).

---

## Phase 3 — GPS + Tailscale (both layers) + self-healing (1 day)

**Deliverables**
- Edit `utils/gps.py` / `wait_for_speed.py` (or `detection.py`) to write
  `/run/roadmon/gps_state.json` (`{fix, lat, lon, speed_kmh, ts}`) on each GPS tick.
  (Opportunistically fix the stale `doc/gps-troubleshooting.md` while here — it describes an
  old no-thread architecture that no longer matches the threaded code.)
- Agent: GPS check = device node exists AND fix fresher than `HEALTH_GPS_STALE_SEC`.
- Agent: Tailscale check = surface `BackendState` + `key_expiry_days` in every snapshot.
- **Self-heal, two layers (restart ≠ re-login):**
  1. **Admin (done in 0.7):** key expiry disabled → never logs out from expiry.
  2. **Agent fallback:** if `BackendState == "NeedsLogin"` (or key near expiry) →
     `tailscale up --authkey=$TS_AUTHKEY`; if `not Running` for `HEALTH_TAILSCALE_HEAL_CYCLES`
     consecutive cycles → `systemctl restart tailscaled`. Increment a `self_heals` counter in
     the snapshot (the heal is visible in the flight recorder).
  - **Security note:** `TS_AUTHKEY` is a reusable credential in `.env` (which already holds
    secrets). Prefer a Tailscale OAuth client + tag to mint keys if rotation becomes a
    concern; a long-lived reusable authkey is acceptable to start.
- (Recommended) `systemd/detection.service` already exists as `hailo-detection.service`
  (`Restart=on-failure`, `RestartSec=15`) — closes the "pipeline crash → nobody restarts it"
  gap. Keep it.

**Field tests:**
- Unplug GPS → `truck-gps` red in ~10–11 min.
- `tailscale logout` → agent re-ups via authkey within `HEAL_CYCLES`; `self_heals=1` in next
  body; no alert if healed within grace.
- `systemctl stop tailscaled` → agent restarts it within 3 cycles.

---

## Phase 4 — Disk-fill safety + laptop dashboard (Plane B) (1–1.5 days)

**Disk-fill safety (logs kept as-is):**
- Per decision, **logs are NOT rotated or deleted — all logs retained** (`utils/logger.py`
  unchanged). Only guard against the card filling:
  - Agent `system_vitals()` flags disk-free below `HEALTH_DISK_ALERT_PCT` (default 85) in the
    `truck-alive` snapshot.
  - Accepted risk: the 58G card will eventually need manual attention; the alert buys lead
    time to act before it's full.

**Laptop-side dashboard (Plane B):**
- Team-run **local web dashboard** (Flask/FastAPI binding `localhost`, opens in browser).
  **Nothing persistent runs on the Pi.**
- Lives in **`dashboard/`** inside this monitoring-implementation project dir (alongside
  `reports/` and this plan doc) — not the main robolyze/hailo-rpi5-examples repo, not a
  separate standalone repo. Data path: SSH over Tailscale to `rasp@100.110.189.87` and:
  - `cat /run/roadmon/health.json` → render status (disk/temp/fps, pipeline, camera, GPS,
    upload, tailscale) — **no recompute on Pi**.
  - `tailscale status --json` → render tailnet state.
  - **Screenshot on demand:** `raspi2png`/`grim` against the Pi framebuffer (the unit has a
    display for the Tkinter shutdown UI), pipe image back, show inline. On-demand only —
    no constant capture.
- Auth: Tailscale creds + SSH key; document setup in the dashboard repo README.
- Transient SSH load only; no resident Pi process.

**Dashboard UI:**
- **Tabs:** "Live Status" (default) and "Daily Report" — not one long scrolling page.
- **Live Status tab:** status cards for System (CPU/disk/temp/uptime), Pipeline (fps/heartbeat
  age), Camera (reachability + clarity check breakdown from Phase 2), GPS (fix/speed/last-seen),
  Upload (queue depth/drops), Tailscale (BackendState/key expiry/self-heals) — each card uses the
  **same emoji legend as the Daily Ops Report** (▶️/⏸️/✅/🔴) so the dashboard and the report read
  consistently. Screenshot panel below the cards, "Grab Screenshot" on-demand button. "Refresh
  Now" button + an opt-in auto-poll toggle (30s interval, off by default) — matches the existing
  on-demand-SSH-pull design rather than replacing it.
- **Daily Report tab:** read-only display of the auto-filled values, plus the report textarea
  described below with a "Copy to Clipboard" button. "Regenerate" re-fetches `health.json` and
  refreshes only the auto-filled portions.
- **Style:** polished/clean — cards, consistent spacing/typography, color-coded status. Small
  internal tool, not over-engineered, but presentable enough to show to management.

**Daily Ops Report (management update):**
- Dashboard adds a report view/button that renders the team's existing management-facing
  Markdown update (`🛣️ Robolyze Daily Ops Update`, sections: Data Sync & Cleanup, System Health
  Check, Camera Check, Dashboard Update, Recording Status).
- **Auto-filled** straight from the already-fetched `health.json` / config — no extra SSH round
  trip:
  - *System running* ← `pipeline_state()`.
  - *Detections uploaded to client dashboard* ← `upload_state()` (queue depth 0 / no drops).
  - *Pendrive's storage no longer full* ← disk-free % vs `HEALTH_DISK_ALERT_PCT`.
  - *Confidence threshold set to 0.75* ← one new field added to the snapshot dict: the
    confidence-threshold config value the pipeline already reads from `config/settings.py`.
  - *Duromac feed inspected* ← agent successfully completed a camera clarity check this cycle
    (no RTSP/read error) — proof the automated inspection ran.
  - *Camera feed is clear* ← composite of the camera clarity check added in Phase 2 (blank +
    obstructed + blurry, all passing).
- **Left manual** (editable blank/placeholder fields, ops person fills same as today — no sensor
  source exists for these): Location, Driver, Sync-script run status, `recording/` pause status,
  Gating-logic-active (≥3 km/h) confirmation.
- **Output is WhatsApp, not Telegram** (corrected — this report is posted to the team's WhatsApp,
  unrelated to Plane A's Telegram alerting). No new bot/integration: the dashboard renders a
  **raw-text textarea**, not a Markdown/HTML preview — the report's `*asterisk*` bold and emoji
  are WhatsApp's own literal formatting syntax, not decorative Markdown, so they must survive
  verbatim. Rendering `*bold*` into actual HTML bold would strip the asterisks WhatsApp needs and
  silently break the report. Auto-filled values are inserted into the template text as-is; manual
  fields stay as `__` placeholders the ops person edits directly in the textarea; a "Copy to
  Clipboard" button copies the exact raw contents for pasting into WhatsApp.
- **Scope note:** camera clarity only feeds this dashboard report, not Plane A/Healthchecks
  alerting — threshold false-positives (e.g. glare misread as blank) are a UX annoyance in a
  once-daily report, not a truck-down page. Wiring it into the `truck-camera` Healthchecks
  check/Telegram alert is explicitly **not** part of this change; revisit as a future option once
  thresholds prove reliable after Phase 6 field tuning.

**Field tests:**
- From a team laptop, run the dashboard → status renders from `health.json`, live screenshot
  loads, tailscale status shows — all over Tailscale, no Pi server running.
- Lower `HEALTH_DISK_ALERT_PCT` (or simulate low disk) → flag appears in `truck-alive` body.
- Generate the Daily Ops Report → the six auto-filled lines match live `health.json`/config
  values; the four manual lines render as empty/edit-ready placeholders (never a silent ✅).

---

## Phase 5 — External pinger (Plane A supplement) (½ day)

- Runs on the **existing always-on Robolyze backend** (not a laptop — laptops aren't always on).
- Small cron/job pings the Pi's Tailscale IP (`100.110.189.87`) every N minutes, records
  last-seen / reachability, reports to the same Telegram (or Healthchecks) channel.
- **Honest scope:** this adds *live reachability* ("can I reach it right now"). It **cannot
  remote-power-on** the truck — that stays a driver action / future out-of-band-hardware
  project. Offline *detection* is already handled by the dead-man switch + working-hours
  schedule; the pinger is the "reachable now for the dashboard" signal.

**Field test:** stop Tailscale on the Pi → backend pinger reports unreachable.

---

## Phase 6 — Field tuning & sign-off (1–2 weeks passive)

1. Run a full week of real driving. Watch for:
   - False `truck-alive` alarms from cellular dead zones / tunnels → lengthen grace.
   - Component false reds (GPS flapping in urban canyons) → adjust `HEALTH_GPS_STALE_SEC`.
2. Verify the **reading rule** holds: `truck-alive` red = truck off (one event);
   `truck-alive` green + component red = real component failure.
3. Exercise the decision tree on a real end-of-day power-off; confirm it's classified as
   informational (clean-shutdown hook fired), not a failure.
4. Write a one-page runbook for the Telegram group: what each red means, what to open (the
   dashboard), when to call the driver.
5. Manager demo: induce a live failure end-to-end (pull camera → Telegram alert → open
   dashboard → diagnose without manual SSH). Set latency expectations honestly (component
   ~5–11 min, whole-truck ~15 min).

**Definition of done:** one week with zero false alarms, every induced failure alerting
within its expected window, dashboard usable by the team, and both engineer + manager
receiving and understanding alerts.

---

## Deferred (explicitly out of scope for this effort)

| Item | Why deferred |
|---|---|
| **Upload durability** — in-memory queue loses on crash; `S3Uploader` has no retry (vs API's 3) | Real, but hot-path / higher-risk, different domain. Ship monitoring first; let `upload_state()` quantify the real drop rate, then prioritize with evidence. |
| **Camera on DHCP** — root cause of subnet drift | `find_camera.sh` + drift scan already mitigate. Network-infra ticket of its own. |
| **Out-of-band power/state hardware** — always-on LTE module reporting truck power independent of the Pi | Only way to get true remote power-on visibility; separate hardware project. Park it. |
| **Log rotation/retention** | Decision: keep all logs. Only the disk alert (Phase 4) guards against a full card. |

---

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| Cellular dead zones cause `truck-alive` false alarms | 15 min grace to start; tune upward in Phase 6; working-hours cron schedule |
| `/run/roadmon` missing after OS update | tmpfiles.d config (0.6); agent also `mkdir -p` defensively |
| Agent bug crashes the loop | Every probe wrapped in try/except returning an error state; `Restart=always`; agent death is self-announcing (no pings → `truck-alive` red) |
| Dashboard/Tailscale down blinds the team | By design, alerting (Plane A) is out-of-band over cellular and independent of Tailscale; dashboard is best-effort only |
| Tailscale logs out | Admin key-expiry disabled (0.7) + agent authkey auto-reup (Phase 3) |
| `TS_AUTHKEY` leaks from `.env` | `.env` already holds secrets; move to OAuth-client-minted keys if rotation matters |
| Healthchecks.io outage | Rare; silence from HC ≠ truck failure. External pinger (Phase 5) is an independent second signal |
| `detection.py` edits break the pipeline | Changes are ~3 lines inside an existing callback + a flock guard; test on bench rig before truck deploy |
| Camera clarity thresholds (blank/obstructed/blur) misfire | Same calibration problem as existing `BLUR_THRESHOLD` — reuse `utils/calibrate_blur.py`'s workflow; start conservative, tune during Phase 6 field week; kept out of Plane A alerting so false positives are only a report annoyance |
| Second RTSP client (camera clarity check) adds camera/network load | Slow cadence (5–10 min, `HEALTH_CAMERA_CHECK_INTERVAL_SEC`) confirmed with user; field evidence camera tolerates a second concurrent client |
| Card fills (logs kept unbounded) | Disk alert (Phase 4) gives lead time; manual cleanup remains a field task |

---

## Rollout checklist (copy into your tracker)

- [ ] 0.1–0.9 Healthchecks + Telegram + env + tmpfiles + tailscale-expiry-disable + kill-dup + branch-diff
- [ ] `health_agent.py` core + unit tests green on laptop
- [ ] `health-agent.service` + shutdown hook deployed; power-pull test passes
- [ ] flock dup guard; second-instance-refused test passes
- [ ] `detection.py` heartbeat-file edit; freeze test passes
- [ ] Camera probe + drift scan; unplug test passes
- [ ] GPS state file + check; unplug test passes (+ fix stale gps doc)
- [ ] Tailscale check + both-layer self-heal; logout + stop-tailscaled tests pass
- [ ] Disk alert flag
- [ ] Laptop dashboard: health.json + screenshot + tailscale render over Tailscale
- [ ] External pinger on Robolyze backend; unreachable test passes
- [ ] 1–2 week field tuning, grace periods finalized
- [ ] Runbook written, manager demo done
