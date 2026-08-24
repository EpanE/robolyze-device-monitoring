---
name: robolyze-rm-duromac-improvements
description: Use when asked to review the Duromac truck Pi (hailo-rpi5-examples, host 100.110.189.87) against monitoring_implementation_plan.md, to find improvement/reliability gaps in that codebase or its field-observed runtime state, or to produce/refresh the improvements handoff document — read-only analysis only, this skill never edits code (local or remote).
---

# Robolyze RM — Duromac Improvements Handoff

## Purpose

Produces a **handoff markdown document** listing where the Duromac truck's monitoring
implementation (`monitoring_implementation_plan.md`, this repo) and its underlying
`hailo-rpi5-examples` codebase (on the Pi, `~/miniproj_epan/hailo-rpi5-examples/`) need
improvement — so a human or a later coding session can act on it. **This skill only reads,
analyzes, and writes the handoff document. It never edits code, never SSHes with write
intent, never runs a mutating command on the Pi.** Turning a listed improvement into actual
code is a separate, later task.

## Hard constraint

- Local edits: none. Do not touch anything under `~/miniproj_epan/hailo-rpi5-examples/` or
  any file outside `reports/` and this skill's own output.
- Remote SSH (`rasp@100.110.189.87`, password `robo2024`): **read-only commands only** — the
  same class of commands used to produce `reports/pi-system-report.md` and
  `reports/hailo-rpi5-examples-codebase-report.md` (`cat`, `ls`, `find`, `git log`/`status`,
  `systemctl status`, `ps`, `df`, `journalctl` — never `--write`, never `systemctl restart`,
  never editing a remote file, never `git commit`/`push` on the device).
- If a finding requires verifying something destructively (e.g. "does the queue actually
  drop under load?"), describe it as a recommended test in the handoff instead of running it.

## Behavior on invocation

1. Read the three baseline documents in this repo:
   - `monitoring_implementation_plan.md` — the target design (Phases 0–4).
   - `reports/pi-system-report.md` — device/OS/process/service state as last captured.
   - `reports/hailo-rpi5-examples-codebase-report.md` — codebase-level findings as last
     captured.
2. Check staleness: if these reports are more than a few days old, or the user says the
   device has changed since, refresh them first — re-run a read-only SSH capture pass (same
   pattern as the two reports above: `hostname`/`uname`/`df`/`free`, `git log`/`status`,
   `systemctl status hailo-detection`, `ps aux | grep -i hailo`, `ls /run/roadmon/`, log
   directory size) and update the two report files before analyzing. Otherwise, work from the
   existing reports without re-connecting.
3. Compare current state against each phase of `monitoring_implementation_plan.md`:
   - Phase 0 (Healthchecks.io, `.env` vars, `/run/roadmon/`) — present or not.
   - Phase 1 (`utils/health_agent.py`, `truck-alive`, systemd unit, shutdown hook) — present
     or not; note that `detection.py` already emits a `Frame heartbeat` log line every ~5s
     that Phase 1's `pipeline_state()` probe is meant to consume — check whether that link has
     been made yet.
   - Phase 2 (pipeline/camera checks) — note `utils/find_camera.sh` already exists and is
     reusable for `camera_state()`'s drift-scan rather than reimplementing it.
   - Phase 3 (GPS/Tailscale checks, self-healing, `detection.service` auto-restart) — note
     `hailo-detection.service` already has `Restart=on-failure`/`RestartSec=15`, and
     `utils/gps.py` has no state file for `gps_state()` to read yet.
   - Phase 4 (field tuning, runbook) — not applicable until 0–3 exist.
4. Also flag **non-plan reliability/quality issues** found in the codebase or field state that
   the plan doesn't cover but affect whether the plan will work cleanly — see "Known baseline
   findings" below for the starting set as of 2026-07-27; re-verify each still applies before
   restating it, since the device changes over time.
5. Write the handoff to `reports/duromac-improvements-handoff-{YYYY-MM-DD}.md` (use the
   current date). If a handoff from today already exists, overwrite it rather than
   accumulating same-day duplicates.

## Handoff document format

For each finding: **what**, **where** (file/service/log line), **why it matters** (tie back
to a plan phase or a concrete failure scenario), and **suggested direction** (not a diff —
enough for someone else to implement it). Group by:

1. Gaps vs. `monitoring_implementation_plan.md` phases (in phase order).
2. Reliability/quality issues independent of the plan (upload/log/process-level).
3. Stale or misleading documentation found in the repo.

Rank each group roughly by impact (data loss / silent failure first, cosmetic last). Do not
include a findings entry unless it's been checked against current reports/state — no
speculative items.

## Known baseline findings (as of 2026-07-27 — re-verify before reusing)

Seed reference only; confirm each still holds against current reports before including it in
a fresh handoff.

| Area | Finding |
|---|---|
| Phase 1 link-up | `Frame heartbeat` log line exists in `detection.py` but nothing writes `/run/roadmon/pipeline.alive`; no Healthchecks/`HEALTH_*` env vars anywhere |
| Phase 2 reuse | `utils/find_camera.sh` is read-only, already handles the camera-subnet-drift failure mode — wire it into `camera_state()` rather than rewriting |
| Phase 3 GPS state | `utils/gps.py` has no on-disk state file; `gps_state()` has nothing to read without either a new write-out or a second GPS reader |
| Phase 3 self-heal | `hailo-detection.service` already has `Restart=on-failure`/`RestartSec=15` — Phase 3's "detection.service auto-restart" checklist item may already be satisfied, confirm before treating as new work |
| Duplicate process | Two `Hailo Detection App` instances were observed running concurrently (one systemd-managed, one manual/interactive) — competing for camera/GPS/Hailo device and double-uploading; recommend an explicit single-instance guard (e.g. pidfile/lock) independent of the monitoring work |
| Log volume | `utils/logger.py` creates a new undeleted log file per process start; 3,078 files / 305M observed with no rotation/retention — recommend `RotatingFileHandler` or a cleanup cron/systemd-tmpfiles rule |
| Upload reliability | Upload queue (`upload/pipeline.py`) is in-memory only, small default depth; `S3Uploader.upload_bytes` has zero retry (vs. `ApiClient.post`'s 3 retries) — detections lost on crash, unclean shutdown, or queue-full with only a log line, no surfaced metric |
| Stale doc | `doc/gps-troubleshooting.md` describes a no-thread/no-lock GPS reader architecture that no longer matches `utils/gps.py` (now a locked background thread) — correct or flag |
| Boot history | Device shows 3 reboots in 2 days, 2+ tagged `crash` (unclean) — direct field evidence supporting Phase 1's dead-man's-switch priority, not just a nice-to-have |

## Cross-references

- `monitoring_implementation_plan.md` (repo root) — the target design this skill measures
  against.
- `reports/pi-system-report.md`, `reports/hailo-rpi5-examples-codebase-report.md` — the
  baseline this skill reads (and refreshes when stale).
- `reports/raw-ssh-capture-*.txt` — full unedited command output backing the two reports
  above, for spot-checking a finding's source.
