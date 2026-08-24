# Duromac Improvements Handoff — 2026-07-28

**Purpose:** feed a Claude session running **directly on the Pi**
(`~/miniproj_epan/hailo-rpi5-examples`, branch `sys-monitoring`) so it can pick up
implementation work without re-deriving this analysis. This document is read-only
output — no code was touched to produce it.

**Source baseline:** `reports/pi-system-report.md` and
`reports/hailo-rpi5-examples-codebase-report.md`, both captured 2026-07-27 via
read-only SSH. One day old — under the skill's staleness threshold, so no full
re-capture was done this pass. One live data point was re-checked this session
(2026-07-28 12:12 UTC+8, `ssh rasp@100.110.189.87 "echo ok && hostname && uptime"`):

```
raspberrypi
12:12:09 up 1:30, 2 users, load average: 6.53, 5.98, 5.93
```

Load average is still ~6.5 on a 4-core board — same signature as 2026-07-27's 6.24
— **despite an uptime of only 1h30m**, meaning whatever's driving the load
re-appears quickly after a reboot. This is consistent with, but does not by
itself confirm, the duplicate-process finding below (`ps aux` wasn't re-run this
session — see Finding 1's suggested verification step).

---

## Group 1 — Gaps vs. `monitoring_implementation_plan.md` (phase order)

### 1.1 Phase 0 — nothing deployed yet (confirmed baseline)
**What:** No `/run/roadmon/`, no `tmpfiles.d` entry for it, no `HEALTH_*` env vars
in `.env`/`.env.sample`, no Healthchecks.io or Telegram wiring anywhere in the repo
or environment.
**Where:** device filesystem root; `.env` / `.env.sample`.
**Why it matters:** every later phase depends on this. It's also why the
2026-07-25–27 crash history (3 reboots in 2 days, 2+ unclean) went completely
unalerted — Phase 1's dead-man's-switch would have caught it.
**Suggested direction:** work Phase 0's checklist (`monitoring_implementation_plan.md`
lines 51-67) in order. Items 0.1/0.3/0.4/0.7 are external-account/admin-console
actions a human must do (Healthchecks.io, Telegram, Tailscale admin console) — a
Pi-side coding session can't complete those itself, only prepare the env-var
plumbing (0.5) and the `tmpfiles.d` rule (0.6) that consume them once created.

### 1.2 Phase 1 — frame heartbeat exists but nothing consumes it
**What:** `basic_pipelines/detection.py` already logs `Frame heartbeat frame=N
fps=M` every ~5s (`time.monotonic()`-gated, unconditional). Nothing writes this to
`/run/roadmon/pipeline.alive`, and no `utils/health_agent.py` exists to read it.
**Where:** `basic_pipelines/detection.py` (heartbeat log call); target new file
`utils/health_agent.py`.
**Why it matters:** this is the exact liveness signal Phase 1/2's `pipeline_state()`
probe needs — it's a ~3-line wiring job, not new instrumentation. A frozen
(SIGSTOP'd or deadlocked) process currently produces no external signal at all.
**Suggested direction:** per plan Phase 1 (lines 70-114): build `health_agent.py`
as a standalone stdlib+`requests` script/service (not inside `detection.py`'s hot
path), touch `/run/roadmon/pipeline.alive` from the existing heartbeat callback
(the ~3-line edit the plan already scopes), and have the agent treat
`mtime < HEALTH_PIPELINE_STALE_SEC` **and** process-exists as healthy — a frozen
process with a stale mtime must fail, not just an absent process.

### 1.3 Phase 2 — camera-drift recovery already shipped, one layer earlier than planned
**What:** `utils/find_camera.sh` (ARP scan + passive L2 sniff, MAC-matched via
`CAMERA_MAC`) is real and already auto-invoked by `run_detection_fps.sh` after
~60s of camera unreachability — shipped in commit `b5271fb` (2026-07-24), which
predates and satisfies what Phase 2 originally scoped as new work.
**Where:** `utils/find_camera.sh`; call site in `run_detection_fps.sh` phase 2
(camera-wait loop).
**Why it matters:** don't re-implement this. The remaining Phase 2 gap is
**reporting** on it, not building it — `camera_state()` in the health agent should
surface TCP-reachability plus whatever `find_camera.sh`'s last run found, not
duplicate the scan logic.
**Suggested direction:** in `health_agent.py`, `camera_state()` does a TCP connect
to `192.168.1.102:554`; on failure, shell out to (or read a state file dropped by)
`find_camera.sh` and include its `drift_scan` result in the snapshot. Separately,
the plan's camera-clarity addition (blank/obstructed/blur via RTSP frame grab,
reusing `detection/blur.py`'s `LaplacianBlurDetector`/`BLUR_THRESHOLD`/`BLUR_ROI`
as-is) is still unbuilt — that's genuinely new work, distinct from drift recovery.

### 1.4 Phase 3 — GPS has no state file; Tailscale self-heal unbuilt; systemd auto-restart already satisfied
**What:** three sub-findings:
- `utils/gps.py` caches the latest fix in-memory (thread + lock) but never
  persists it to disk — `gps_state()` has nothing to read from today.
- No Tailscale self-heal logic exists (`BackendState == "NeedsLogin"` →
  `tailscale up --authkey=...` fallback, or `tailscaled` restart after N stale
  cycles).
- `hailo-detection.service` **already has** `Restart=on-failure`, `RestartSec=15`
  — the plan's "(Recommended) detection.service already exists... keep it" line
  (plan line 177) is confirming existing state, not asking for new work. Don't
  treat this as an open task.
**Where:** `utils/gps.py` (needs a write-out, e.g. atomic write to
`/run/roadmon/gps_state.json` mirroring `config/settings.py`'s `DID_STATE_FILE`
pattern); target new Tailscale-check code in `health_agent.py`;
`/etc/systemd/system/hailo-detection.service` (no change needed, verify only).
**Why it matters:** GPS and Tailscale checks are both still fully stubbed —
without the state file, `gps_state()` can't be implemented as a pure cache read
the way the plan intends (it would otherwise need its own independent serial
read, which the plan explicitly wants to avoid).
**Suggested direction:** add the JSON write in `gps.py`'s existing fix-update path
(same lock-protected section that updates the in-memory cache), atomic
temp-file + `os.replace` like `DID_STATE_FILE`. For Tailscale, implement per plan
lines 168-176 — note `TS_AUTHKEY` is a reusable credential accepted as
acceptable-to-start per the locked design decision, OAuth-minted keys are a later
hardening option, not a blocker.

### 1.5 Phase 4 — not applicable yet
No dashboard or disk-alert work exists on the Pi side (dashboard is laptop-side
per the two-plane design; disk-alert is a `system_vitals()` addition that depends
on Phase 1's agent skeleton existing first). Nothing to report here until Phase
1-3 land.

---

## Group 2 — Reliability/quality issues independent of the plan

### 2.1 Duplicate detection-app instances (high impact — active data duplication)
**What:** 2026-07-27 capture found two `Hailo Detection App` processes running
concurrently: PID 1525 (systemd-managed, `hailo-detection.service`) and PID
237238 (manually launched from an interactive `pts/2` session, same cwd). Both
read the same RTSP stream and GPS port, and — critically — each has its **own**
in-memory `TrackDeduplicator`, so detections are independently uploaded twice
with no cross-process dedup.
**Where:** `basic_pipelines/detection.py` startup; `upload/deduplicator.py`
(per-process, in-memory, not shared).
**Why it matters:** this is live production data duplication happening right
now, independent of any monitoring work — every pothole/crack this truck detects
while both instances run gets uploaded twice. It also explains the elevated load
average (6.24–6.53 on effectively 4 cores) seen in both the 2026-07-27 capture
and this session's live check.
**Suggested direction:** `monitoring_implementation_plan.md` Phase 1 already
scopes the fix — a `flock` on `/run/roadmon/detection.lock` acquired early in
`detection.py` (or `run_detection_fps.sh`), refusing a second instance with a
clear log line if the lock is already held. Use `flock` specifically (not a PID
file) since it auto-releases on crash, which matters given this device's history
of unclean shutdowns. **Immediate action independent of that:** identify and kill
the stray manual instance now — `ps aux | grep -i detection` on the Pi to confirm
whether PID 237238 (or its successor after the reboots since 07-27) is still
running, then `kill` only the non-systemd one. This was queued for verification
this session but not completed (no scriptable password auth from the laptop side
this pass) — a Pi-local session can just run it directly.

### 2.2 Unbounded log growth (medium-high impact — eventual disk-fill)
**What:** `utils/logger.py` creates a new `edge_<timestamp>.log` per process
start with no `RotatingFileHandler`, no size cap, no retention. 2026-07-27
capture: 3,078 files, 305M in `logs/`, growing without bound (compounded by the
duplicate-instance issue above — two processes means two log sets per boot).
**Where:** `utils/logger.py`.
**Why it matters:** per the locked design decision, logs are intentionally kept
(not rotated/deleted) — accepted risk. The only guard is a disk-free alert
(Phase 4, `HEALTH_DISK_ALERT_PCT=85`), which doesn't exist yet (Phase 0/1 not
built). Until that alert exists, there is currently **zero warning** before the
58G root partition fills, on a device that reboots unpredictably and isn't
locally accessible for cleanup except over Tailscale.
**Suggested direction:** no code change to `logger.py` itself (decision:
keep-all). Priority is getting Phase 1's `system_vitals()` disk-free check built
so there's at least a Telegram warning before the card fills. Manual cleanup of
`logs/` remains a periodic field task outside this plan's scope.

### 2.3 Upload path has no retry/durability below the API layer (medium impact — silent data loss on failure)
**What:** `S3Uploader.upload_bytes` (`upload/s3_uploader.py`) has **zero retry**
— single attempt, catches all exceptions, logs and returns `None`. Contrast with
`ApiClient.post` (`upload/api_client.py`), which retries 3× with a flat 1s sleep.
The upload queue itself (`upload/pipeline.py`, `queue.Queue(maxsize=8)` in
`detection.py`'s instantiation) is purely in-memory — a crash, unclean shutdown,
or a full queue drops that detection's upload permanently, with only a
rate-limited log line (`_dropped_jobs`) that nothing currently surfaces as a
health signal.
**Where:** `upload/s3_uploader.py`, `upload/pipeline.py`.
**Why it matters:** this is explicitly flagged as **deferred** in
`monitoring_implementation_plan.md` ("Upload durability" — real but hot-path/
higher-risk, different domain; ship monitoring first, let `upload_state()`
quantify the real drop rate before prioritizing with evidence). Listing it here
per the skill's mandate to flag non-plan reliability issues, not as a request to
build it now.
**Suggested direction:** once Phase 1's `upload_state()` (queue depth + dropped
count) is live and reporting for a while, use that data to decide whether an S3
retry (mirroring `ApiClient.post`'s pattern) or a disk-backed spool queue is
worth the complexity. Don't build it speculatively ahead of that evidence — this
matches the plan's own reasoning for deferring it.

### 2.4 Camera clarity check — genuinely new work, not yet started (medium impact — closes a manual reporting gap)
**What:** the Daily Ops Report's "Camera feed is clear" line is currently a
manual human check. The plan's Phase 2 addition (independent short-lived RTSP
client inside `health_agent.py`, blank/obstructed/blur checks, slow 5-10 min
cadence) is fully designed but unbuilt.
**Where:** target `utils/health_agent.py` (new code); reuses
`detection/blur.py`'s existing `LaplacianBlurDetector` and its calibrated
`BLUR_THRESHOLD`/`BLUR_ROI` as-is — do not recalibrate.
**Why it matters:** lower urgency than 2.1-2.3 since it only feeds a once-daily
report, not alerting — false positives here are a report annoyance, not a
truck-down page (explicit scope decision).
**Suggested direction:** build after Phase 1's agent skeleton exists, since it
lives in the same process. Follow the existing manual calibration workflow
(`utils/calibrate_blur.py`) for threshold tuning during Phase 6 field testing.

---

## Group 3 — Stale or misleading documentation

### 3.1 `doc/gps-troubleshooting.md` describes an outdated GPS architecture
**What:** the doc describes a "drain-on-read, no threads, no locks" GPS reader
with `timeout=0` non-blocking reads. Current `utils/gps.py` (last modified Mar
18) uses a background daemon thread with a lock and `timeout=1` — a materially
different architecture. The doc's `(0.0, 0.0)`-fix-rejection guidance is still
accurate; only the threading/blocking model section is wrong.
**Where:** `doc/gps-troubleshooting.md` vs. `utils/gps.py`.
**Why it matters:** low severity but real — anyone debugging a GPS issue in the
field by reading this doc gets a wrong mental model of how reads block/don't
block, though the practical diagnostic steps (checking `/dev/ttyACM0`, baud
rate, raw NMEA) remain valid regardless.
**Suggested direction:** `monitoring_implementation_plan.md` Phase 3 already
flags this as an opportunistic fix while touching `gps.py` for the state-file
work (1.4 above) — bundle the doc correction into that same change rather than
treating it as a separate task.

---

## Cross-reference

- Full plan: `monitoring_implementation_plan.md` (this repo, laptop-side).
- Baseline reports this handoff is built from: `reports/pi-system-report.md`,
  `reports/hailo-rpi5-examples-codebase-report.md`.
- Raw unedited SSH capture backing those reports: `reports/raw-ssh-capture-1-system-and-code.txt`,
  `reports/raw-ssh-capture-2-config-and-modules.txt`.
- Shipped fix already covering part of Group 1.3: commit `b5271fb` on
  `sys-monitoring`, 2026-07-24.

**Not independently re-verified this pass** (would require either a fresh
read-only SSH capture round or a Pi-local session running the commands directly):
whether PID 237238's duplicate instance (or a successor) is still alive right
now, current `logs/` file count/size, current disk-free %. The live `uptime`
check this session (load avg 6.53) is consistent with but does not confirm the
duplicate-instance finding — recommend a Pi-local session run
`ps aux | grep -i detection` as its first action to confirm before acting on 2.1.
