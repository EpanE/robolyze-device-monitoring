# hailo-rpi5-examples Codebase Report — Duromac Truck Unit

**Path on device:** `~/miniproj_epan/hailo-rpi5-examples/` (`/home/rasp/miniproj_epan/hailo-rpi5-examples`)
**Captured:** 2026-07-27, read-only SSH inspection. Full source dumps in `raw-ssh-capture-1-system-and-code.txt` (project tree, detection.py, utils/) and `raw-ssh-capture-2-config-and-modules.txt` (CLAUDE.md, config/settings.py, upload/*, detection/*, docs) in this folder.

This repo is a fork of `hailo-ai/hailo-rpi5-examples` heavily customized into "Robolyze" — a road-defect detection system (potholes, cracks, raveling, stagnant water) that runs the stock Hailo detection example plus a large amount of Duromac-specific application code layered on top (upload pipeline, GPS, recording, blur detection, shutdown UI, storage fallback).

## 1. Git state

| | |
|---|---|
| Origin | `https://github.com/Robo-my/Hailo-Road_Monitering.git` |
| Upstream | `https://github.com/hailo-ai/hailo-rpi5-examples` (tracked as `upstream`) |
| Current branch | **`sys-monitoring`** — working tree clean, nothing uncommitted |
| Other branches | `main`, `remotes/origin/feat/blur-detection`, `remotes/upstream/{main,dev}` |
| Latest commits (newest first) | `b5271fb` "Fix blur payload schema, add camera-drift recovery, extract pre-commit hook" · `f16f506` "Merge feat/blur-detection into main" · `ce0648f` "WIP: local pipeline/filter/config changes" · `327ed21` "Add blur (rain) soft-flagging" · plus a long history of pothole-confidence tuning, GPS fixes, ID-dedup fixes, storage-tier work |

**Note the branch name:** the device is already checked out on `sys-monitoring`, which strongly suggests prior monitoring-related work was started here (locally or by another contributor) — worth diffing against `main`/`origin` before starting fresh Phase 0 work, in case some scaffolding already exists on this branch that isn't reflected in the working tree state captured here (working tree = clean, so if there is `sys-monitoring`-specific work it's already committed; check `git log main..sys-monitoring` and `git diff main...sys-monitoring` before assuming this is a truly blank slate).

## 2. Project layout (top level, non-community/non-vendor)

```
basic_pipelines/
  detection.py                 — main entry point, wires everything together (see §4)
  robolyze_detection_app.py    — custom GStreamer/Hailo app: RTSP source building, reconnect logic, UI tap (see §5)
  detection_simple.py, pose_estimation.py, instance_segmentation.py, depth.py  — stock Hailo examples, unmodified, unused in production
config/
  settings.py                  — all configuration via env vars / .env (see §6)
core/recorder/
  ffmpeg_recorder.py           — RTSP → mp4 segment recording via ffmpeg subprocess
detection/
  filter.py, parser.py, visualizer.py, local_saver.py, local_save_pipeline.py, blur.py
upload/
  pipeline.py, s3_uploader.py, api_client.py, batcher.py, deduplicator.py   (see §7)
utils/
  gps.py, wait_for_speed.py, find_camera.sh, shutdown_ui.py, logger.py, directories.py, validators.py, calibrate_blur.py
doc/
  gps-troubleshooting.md, storage.md, storage_setup.md, install-raspberry-pi5.md, basic-pipelines.md
tests/  — pytest suite, split into laptop-safe and Hailo-RPi-only per CLAUDE.md
.claude/
  hooks/  — check-git-updates.sh, detect-device.sh (laptop vs Pi env tagging), pre-commit-tests.sh
  settings.json, settings.local.json
run_detection_fps.sh           — production launch wrapper, 4-phase gated start (see §8)
CLAUDE.md                      — extensive project-specific dev documentation (16KB, see §3)
.env / .env.sample             — full runtime config, secrets in .env (not copied into reports)
labels.json                    — class-index → label mapping
yolov11n_dataset9-9.hef        — currently deployed model (per run_detection_fps.sh); 3 older .hef files retained
logs/                          — 3,078 files, 305M, unbounded growth (see pi-system-report.md §9)
```

## 3. The project already has its own developer documentation (`CLAUDE.md`)

This is unusually thorough for a Pi-side hobby/field project — it documents the RTSP camera's static IP quirk, log file semantics (including exactly what "Frame heartbeat" absence means), env var reference, dual-tier upload architecture, blur soft-flagging design, and known log warnings with severity/action. **Any handoff or monitoring work should treat this file as the primary source of truth for intended behavior**, not just this report. Key excerpts folded into the sections below; full text in raw capture appendix 2.

## 4. `basic_pipelines/detection.py` — main pipeline entry point

- Standard Hailo GStreamer app_callback pattern: pulls the ROI, parses Hailo detections into plain dicts, splits into **confirmed** (≥`CONFIRMED_MIN_CONFIDENCE`, default 0.75) vs **training** (≥`TRAINING_DATA_MIN_CONFIDENCE`, default 0.30) tiers. Potholes get the lower training-floor threshold for the confirmed check too (`POTHOLE_LABELS` override in `_is_confirmed`).
- **A frame heartbeat already exists**: every ~5 real seconds (`time.monotonic()` gated), logs `Frame heartbeat frame=N fps=M` via the project logger — this fires unconditionally regardless of detections, specifically so absence-of-log-lines signals a frozen pipeline. **This is exactly the signal Phase 1/2 of `monitoring_implementation_plan.md` (`pipeline_state()`, `/run/roadmon/pipeline.alive`) needs** — right now it only goes to the rotating-less log file, nothing consumes it as a liveness signal, and nothing touches `/run/roadmon/`.
- GPS: attaches `{lat, lon}` to both tiers when a fix is available (`get_fix(require_valid=True, allow_stale=False)`, falling back to `allow_stale=True`); rate-limited warning (every 5s) when no fix. No persistence of fix/state to disk anywhere — see §9.
- Frame handling: single RGB→BGR conversion, then fanned out to local-save (if enabled), live UI overlay (if UI mode), and the uploader — comment in code explicitly notes this conversion happens once for all downstream consumers.
- Startup: loads `.env`, resolves storage paths via `config/settings.py` (`ensure_directories`), prints/logs any `settings.startup_alerts` (e.g. "requested storage target unavailable, falling back to X"), builds GPS reader, waits up to `GPS_WAIT_FOR_FIRST_FIX_SEC` for a fix before entering the main loop, then runs the GStreamer app.
- Shutdown path: ESC-key listener (TTY cbreak mode) and a Tkinter shutdown UI both call `app.request_shutdown()`; on stop, flushes the uploader with a timeout (`STOP_FLUSH_TIMEOUT_SEC`, default 3s) so in-flight uploads aren't silently dropped on a clean stop — but see §7 for what happens on an *unclean* stop (the common case per pi-system-report.md §7).
- Optional `UI_POWEROFF_CMD` — if the shutdown UI's poweroff button is used, runs a real `poweroff` shell command via `subprocess.run(shlex.split(...))`.

## 5. `basic_pipelines/robolyze_detection_app.py` — RTSP source & self-healing

- Builds the RTSP GStreamer source element manually (credential handling separated from the URL string so passwords never appear in printed/logged pipeline strings — `_redact_pipeline`).
- `rtspsrc ... protocols=tcp latency=300 do-rtsp-keep-alive=true timeout=10000000` — TCP-only RTSP, keep-alive enabled.
- **Already has RTSP-level self-healing**: on a GStreamer bus ERROR while `source_type == "rtsp"`:
  - `401 Unauthorized` → treated as unrecoverable, shuts down immediately (avoids a segfault-prone tight restart loop on bad creds — explicit comment in code).
  - Any other error → reconnect attempt, up to `self.max_restarts` (bounded, value not in this file — inherited from the upstream Hailo app class), with a `_rtsp_restart_pending` guard against overlapping restart attempts.
  - Once `max_restarts` is exhausted, the whole app shuts down — at which point **systemd's `Restart=on-failure` (`RestartSec=15`) takes over**, which re-runs the *entire* `run_detection_fps.sh` gating chain (internet check → camera reachability → GPS speed gate → 7s delay → relaunch). This is a reasonable two-layer design (in-process reconnect for transient RTSP hiccups, full restart-from-scratch via systemd for anything worse) but it means a full recovery from a bad patch of network can take several minutes, and **nothing currently reports this cycle to anyone** — it's silent unless someone tails the journal/log.

## 6. `config/settings.py` — configuration layer

- Loads `.env` once at import time (`load_env_file`, does not override already-set OS env vars), hand-rolled parser (not `python-dotenv`) that tolerates comments/quotes and deliberately mirrors the same key-by-key philosophy `run_detection_fps.sh` uses (see CLAUDE.md rationale: `.env` holds secrets and lines `python-dotenv` can choke on).
- Storage root selection: explicit `STORAGE_ROOT` override → SSD (by `SSD_UUID`, else `/mnt/ssd`) → pendrive (by `PENDRIVE_UUID`, else `/mnt/pendrive`) → SD/project-root fallback. Each external target is checked for actually being a live, writable mount (`_is_mount_ready` — `os.path.ismount` + `os.access`), not just path existence, and a human-readable `startup_alerts` entry is appended whenever a requested target isn't available, which `detection.py` prints and logs at startup.
- `DID_STATE_FILE` (default `<project_root>/.did_state`) persists a monotonically-increasing detection ID (`did`) high-water mark across restarts — atomic write via temp-file + `os.replace`. Confirmed present on device (`.did_state`, 16 bytes).
- Threshold/label config exactly matches CLAUDE.md's documented table (confirmed 0.75, training 0.30, pothole labels get the training floor, `manhole_drain` excluded from all uploads via `EXCLUDED_UPLOAD_LABELS`).

## 7. `upload/` — dual-tier upload pipeline

| File | Role |
|---|---|
| `pipeline.py` | Orchestrates everything: blur scoring, crack debounce, dedup, dual-tier routing, worker thread + bounded queue |
| `s3_uploader.py` | Thin boto3 wrapper, `put_object`, catches all exceptions and logs+returns `None` on failure |
| `api_client.py` | POSTs to Robolyze REST API, **3 retries with a flat 1s sleep between attempts**, then gives up and returns `False` |
| `batcher.py` | `CrackDebouncer` — coalesces crack detections within `CRACK_UPLOAD_DELAY` (default 5s) via a `threading.Timer`, keeping only the latest |
| `deduplicator.py` | `TrackDeduplicator` — per-process, in-memory, bounded (default 5000) set of seen track IDs + a spatial-quantized bbox key, LRU-evicted |

**Reliability gaps worth noting for the improvement handoff (not fixed here):**
- The upload queue (`self._upload_queue = queue.Queue(maxsize=upload_queue_size)`, default small — 8 in `detection.py`'s instantiation) is **purely in-memory**. A crash, an unclean shutdown (the common case per pi-system-report.md §7), or a queue-full drop all lose that detection's upload permanently — there is no disk-backed retry/spool.
- `S3Uploader.upload_bytes` has **no retry** at all (single attempt, log+return `None` on any exception) — contrast with `ApiClient.post`'s 3 retries. A transient S3 blip drops the object with only a log line.
- `TrackDeduplicator` state is per-process and in-memory — explains why running two detection-app instances simultaneously (pi-system-report.md §5) does not dedupe across them; each will happily upload the same physical detection independently.
- `_dropped_jobs` / drop-rate is tracked and log-rate-limited but, like the frame heartbat, only reaches a log file — nothing surfaces it as a health signal today.

## 8. `run_detection_fps.sh` — production launch wrapper

Four gated phases before the pipeline even starts, matching `hailo-detection.service`'s `ExecStart`:

1. **Wait for internet** — pings `8.8.8.8`, retries every 5s indefinitely (no timeout/give-up).
2. **Wait for camera** — parses host:port from `.env`'s `RTSP_URL`/`CAMERA_SOURCE`, TCP-probes it (`nc -z`); after 60s of failure (12 attempts × 5s) automatically runs `utils/find_camera.sh` once and logs its output inline — this is the fix for the 2026-07-24 camera-subnet-drift incident (see below). Also indefinite retry, no timeout/give-up.
3. **GPS speed gate** — `python utils/wait_for_speed.py`, blocks pipeline launch until GPS reports the vehicle moving ≥ `MIN_SPEED_KMH` (default 3 km/h), with its own internal GPS-hardware timeout (`GPS_SPEED_GATE_TIMEOUT_SEC`, default 120s) after which it *skips* the gate rather than blocking forever.
4. 7-second stabilisation delay, then `exec python basic_pipelines/detection.py --input "$CAMERA_URL" --hef-path .../yolov11n_dataset9-9.hef --labels-json labels.json --show-fps >>"$RUNTIME_LOG" 2>&1`.

Because phases 1–2 have **no timeout**, a truck that's parked with no internet or with the camera unplugged will sit silently retrying forever — exactly the scenario `monitoring_implementation_plan.md`'s `truck-alive` dead-man's-switch is meant to catch (right now there is no external signal at all that the truck is stuck in phase 1/2 vs. genuinely off).

`utils/find_camera.sh` (§ below) is a well-engineered, already-battle-tested piece of diagnostic tooling — it's read-only, always exits 0, and uses ARP/broadcast sniffing to locate the camera even when it's landed on the wrong subnet, specifically because ONVIF WS-Discovery's unicast reply can't route back across a stale subnet. Worth reusing as-is for the monitoring agent's `camera_state()` probe rather than re-implementing TCP-only reachability.

## 9. GPS module — `utils/gps.py` (current) vs `doc/gps-troubleshooting.md` (stale)

- **Current code** (`utils/gps.py`, last modified Mar 18): background daemon thread continuously reads NMEA sentences (`readline`, timeout=1), caches the latest parsed fix behind a `threading.Lock`; `get_fix()` is a pure cache read (never touches serial, never blocks). Also persists GPS module config to internal flash once at startup (`UBX-CFG-CFG`) so port/nav settings survive power cycles. Correctly rejects `(0.0, 0.0)` as an invalid fix (a documented pynmea2 quirk). Speed comes from `$GPRMC`; carried forward for `$GGA` sentences within the max-age window since GGA doesn't carry speed.
- **`doc/gps-troubleshooting.md` describes an older architecture** ("drain-on-read, no threads, no locks", `timeout=0` non-blocking) that **no longer matches the code** (which now uses a background thread with a lock and `timeout=1`). The (0,0)-rejection fix described in the doc *is* still accurate. **This doc is stale and should be corrected or flagged** — anyone debugging GPS issues in the field by reading this doc will get a wrong mental model of how reads behave (though the practical diagnostic steps — checking `/dev/ttyACM0`, baud rate, satellite fix via raw NMEA — remain valid regardless of the threading detail).
- No file/state is written anywhere for the GPS fix (no `/run/roadmon/gps_state.json` or equivalent) — confirms `monitoring_implementation_plan.md` Phase 3's `gps_state()` probe has nothing to read from today; it would need either a new write-out in `gps.py`/`detection.py`, or the health agent would need its own independent GPS read (undesirable given the single-antenna/single-port setup, though the module's docs claim multi-process-safe reads).

## 10. Storage / recording — `core/recorder/ffmpeg_recorder.py`, `detection/local_saver.py`

Not fully dumped in this pass (see raw capture appendix 2 for `ffmpeg_recorder.py`, `local_saver.py`, `local_save_pipeline.py`, `visualizer.py`, `blur.py` full source) — high-level roles per CLAUDE.md's own table:

| File | Role |
|---|---|
| `core/recorder/ffmpeg_recorder.py` | Spawns `ffmpeg` as a subprocess to record the RTSP stream to segmented mp4 files under the resolved storage root |
| `detection/local_saver.py` | JPEG + YOLO `.txt` save to disk (deprecated per CLAUDE.md — "S3 uploads already cover this; use only for offline debugging") |
| `detection/local_save_pipeline.py` | Async queue wrapper around the local saver so disk I/O doesn't block the GStreamer thread |
| `detection/visualizer.py` | Draws bounding boxes for the UI overlay and (off-thread) the labelled S3 upload image |
| `detection/blur.py` | `BlurDetector` ABC + `LaplacianBlurDetector` (variance-of-Laplacian over a road ROI), pluggable via `register_blur_detector`/`BLUR_METHOD` |

## 11. Config surface — env var count

`.env.sample` documents **~50 environment variables** across camera, Hailo runtime, upload routing/thresholds, dual-tier S3, blur detection, logging, recording, storage priority, local-save tuning, and GPS. This is the single source of truth for runtime behavior; any monitoring agent config (per `monitoring_implementation_plan.md` Phase 0.5 — `HEALTHCHECKS_BASE_URL`, `HEALTH_AGENT_INTERVAL_SEC`, etc.) should follow the exact same `.env` + `config/settings.py`-style loader pattern already established here, not a separate config mechanism.

## 12. Testing infrastructure already exists

Per `CLAUDE.md` and `tests/` directory listing: a real pytest suite split into laptop-safe (`test_local_detection_saver.py`, `test_local_save_pipeline.py`, `test_settings_storage_fallback.py`, `test_dual_tier_upload.py`, `test_regression.py`) and Hailo-RPi-only (`test_hailo_rpi5_examples.py`, `test_camera.py`, `test_gps.py`) tests, runnable via `make test-local` / `make test-all` or `run_tests.sh`. A `.claude/hooks/pre-commit-tests.sh` hook exists to run relevant tests automatically. **Any new health-agent code should follow this existing split** (mocked-probe unit tests laptop-side, hardware-dependent tests gated to the Pi) — this is precisely the testing strategy `monitoring_implementation_plan.md` Phase 1 already calls for, and the project has the scaffolding for it ready to reuse.

## 13. Known/self-documented issues (from CLAUDE.md's own tables)

| Issue | Severity (per project's own docs) | Status |
|---|---|---|
| `Training queue full (maxsize=N); dropped X job(s)` | Real — training-tier samples lost under detection bursts | Not fixed; tunable via queue size env var, root cause is S3 PUT latency vs. detection rate |
| `rtpjitterbuffer ... too big, reset skew` / `avdec_h264 decreasing timestamp` | Network — RTSP clock drift / out-of-order RTP | No code fix; only a problem if it correlates with frame-heartbeat stalls |
| `Python-dotenv could not parse statement...` | Real — malformed `.env` line | Manual fix when seen (this project doesn't even use python-dotenv in the hot path, see §6, but the message can still appear from a dependency) |
| Camera static-IP/subnet-drift | Real, already bit once (2026-07-24) | Mitigated (`find_camera.sh` + Phase 2 auto-diagnosis) but root cause (camera not on DHCP) still open per CLAUDE.md's own "Prevention" note |

---

*See `pi-system-report.md` for the live device/OS/process-level findings (including the duplicate running instance and the log-volume issue) that pair with the code-level findings above.*
