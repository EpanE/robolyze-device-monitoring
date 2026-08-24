# Pi System Report — Duromac Truck Unit

**Captured:** 2026-07-27, live SSH session to `rasp@100.110.189.87` (Tailscale hostname `raspberrypi-2-with-hat`, Tailscale tag `robomydev@`).
**Method:** read-only commands only (no writes, no restarts, no config changes). Full unedited output in `raw-ssh-capture-1-system-and-code.txt` and `raw-ssh-capture-2-config-and-modules.txt` in this folder.

---

## 1. Hardware / OS

| Item | Value |
|---|---|
| Hostname | `raspberrypi` (Tailscale name: `raspberrypi-2-with-hat`) |
| Board | Raspberry Pi 5, AI HAT+ (Hailo-8, 26 TOPS) |
| Kernel | `6.12.62+rpt-rpi-2712` (SMP PREEMPT, aarch64) |
| OS | Debian GNU/Linux 12 (bookworm) |
| CPU temp at capture | 57.1°C, `throttled=0x0` (no throttling) |
| Load average | **6.24, 5.82, 6.11** — high for a 4-core board; see §5 (duplicate process) |
| RAM | 15Gi total, 2.1Gi used, 10Gi free, 13Gi available — healthy |
| Swap | 510Mi, 0 used |
| Root disk (`mmcblk0p2`) | 58G, 65% used, 20G free |
| Boot partition | 510M, 16% used |
| USB pendrive `KINGSTON` | mounted at `/media/rasp/KINGSTON`, 116G, 1.8M used — essentially empty (RECORD_SUBDIR path points here but nothing has landed on it yet, or it was recently wiped) |
| No SSD present | `.env` has `SSD_MOUNTPOINT=/mnt/ssd` but no `/mnt/ssd` mount exists — storage falls back to pendrive/SD per `config/settings.py` priority chain |

## 2. Hailo AI accelerator

| Item | Value |
|---|---|
| Device | `0001:01:00.0`, Board Name `Hailo-8`, Architecture `HAILO8` |
| Firmware | `4.20.0 (release,app,extended context switch buffer)` |
| HailoRT / hailo-all / hailofw / tappas-core | all `4.20.0` / `3.31.0` — internally consistent |
| Serial/Part/Product Number | `<N/A>` — not programmed on this unit (cosmetic, does not affect operation) |

## 3. Network

| Interface | Address | Notes |
|---|---|---|
| `eth0` | `192.168.1.100/24` (DHCP) | Truck LAN — camera lives at `192.168.1.102` on the same subnet |
| `tailscale0` | `100.110.189.87` | Remote-access overlay, used for this SSH session |
| `wlan0` | down/unavailable | Not in use |
| `docker0` | up, no carrier | Docker installed but nothing running on it currently |

Camera reachability check (`TCP connect 192.168.1.102:554`): **OK** at capture time.

Tailscale `status` shows ~30 other tailnet devices under the same `robomydev@` account (other trucks/dev machines/servers) — this Pi is currently the only one online among the `raspberrypi*` entries; `raspberrypi` (no suffix) and `raspberrypi-2` (no `-with-hat`) show as offline for a long time, suggesting they are retired/previous units, not currently-deployed duplicates.

## 4. GPS & camera peripherals

| Item | Value |
|---|---|
| GPS module | u-blox 7, USB, `/dev/ttyACM0` (symlinked `/dev/gps`), `dialout` group |
| Camera | RTSP at `192.168.1.102:554`, reachable at capture time |
| USB device list | u-blox GPS, Kingston pendrive, one unidentified device `0eef:0005 D-WAV Scientific Co., Ltd WS170120` (likely a touchscreen/digitizer controller for the Tkinter shutdown UI — not investigated further) |

## 5. Running processes — **two detection-app instances found running concurrently**

```
rasp   1525    (PPID 1)      started 12:33  CPU 99%  Hailo Detection App   — systemd-launched (hailo-detection.service)
rasp  237238   (PPID 237087) started 15:44  CPU 99%  Hailo Detection App   — launched from pts/2 (interactive terminal)
```

Both processes:
- Run from the same `cwd` (`~/miniproj_epan/hailo-rpi5-examples`)
- Are pinned near 99% of a core each (expected for the GStreamer/Hailo pipeline)
- Are simultaneously reading the same RTSP stream, the same GPS serial port, and sharing the one Hailo-8 device via `HAILO_MULTI_PROCESS_SERVICE=1`

**This explains the elevated load average (6.24 on what is effectively a 4-core SoC).** It also means:
- Two independent upload pipelines are POSTing to the Robolyze API and S3 for the *same* road segment — likely duplicate detections/uploads (each with its own `TrackDeduplicator`, so dedup does not cross processes).
- Two processes are opening `/dev/ttyACM0` independently — `utils/gps.py` is documented as safe for this ("no file locks... multiple processes can open the same serial port"), so this itself won't crash, but it does mean GPS data is being read and parsed twice for no benefit.
- Two recorder/local-save pipelines may be writing to the same output directories under `/media/rasp/KINGSTON/production/Duromac_roadmonitoring/...`.

The systemd-managed process (`1525`) is the "real" one (`hailo-detection.service`, `Restart=on-failure`). The second (`237238`, parent `237087`) was started manually from a terminal session (`who`/`last` show a `tty1` login at boot 12:10 and the same session still active) — almost certainly a developer/tester running `run_detection_fps.sh` or `detection.py` manually to check something, without first stopping the service instance. No evidence either process was intentionally left running as a redundancy measure — this looks like an accidental double-run.

## 6. systemd services

| Unit | State | Notes |
|---|---|---|
| `hailo-detection.service` | enabled, active | `ExecStart=/bin/bash .../run_detection_fps.sh`, `Restart=on-failure`, `RestartSec=15`, `User=rasp`, `After=network-online.target hailort.service` — see full unit text below |
| `hailort.service` | enabled | Hailo runtime service |
| No other custom units | — | **No health/monitoring/watchdog service exists yet** — confirms `monitoring_implementation_plan.md` Phase 0–3 has not been deployed to this device at all |

```ini
[Unit]
Description=Hailo RTSP detection (show FPS)
Wants=network-online.target hailort.service
After=network-online.target hailort.service

[Service]
Type=simple
User=rasp
WorkingDirectory=/home/rasp/miniproj_epan/hailo-rpi5-examples
ExecStart=/bin/bash /home/rasp/miniproj_epan/hailo-rpi5-examples/run_detection_fps.sh
Restart=on-failure
RestartSec=15
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

No crontab for `rasp` or `root`.

## 7. Boot/crash history

`last -n 10` shows **three reboots in the last two days**, at least two tagged `crash` (unclean shutdown, i.e. power loss rather than a graceful `shutdown -h`):

```
reboot   Mon Jul 27 11:16   still running        (current session)
rasp tty1  Sun Jul 26 02:41 - crash (1+08:35)     <- unclean
reboot   Sun Jul 26 02:16   still running
rasp tty1  Sun Jul 26 02:40 - crash  (-00:23)     <- unclean, 23 min after prior boot
reboot   Sun Jul 26 02:16   still running
rasp tty1  Sat Jul 25 16:55 - crash  (09:21)      <- unclean
```

This is exactly the truck-power-cycling pattern `monitoring_implementation_plan.md`'s dead-man's-switch is designed for — direct field evidence that Phase 1 (`truck-alive` check + clean-shutdown hook) is needed, not speculative.

## 8. `/run/roadmon/`, tmpfiles.d — not present

- `/run/roadmon/` does not exist.
- No `tmpfiles.d` config for it.
- No `HEALTH_*` env vars in `.env`/`.env.sample`.
- No Healthchecks.io / Telegram integration anywhere in the repo or environment.

Confirms: **none of `monitoring_implementation_plan.md` Phase 0–3 has been implemented on this device.** This is a pre-Phase-0 baseline.

## 9. Log volume — no rotation, no cleanup

- `logs/` directory: **3,078 files, 305M**, growing without bound.
- `utils/logger.py` creates one new `edge_<timestamp>.log` file **per process start** — no `RotatingFileHandler`, no max size, no retention policy, nothing ever deletes old ones.
- `run_detection_fps.sh` additionally writes one `runtime_<TS>.log` and one `gst_<TS>.log` per launch into the same directory.
- File timestamps in `logs/` show clusters of restarts seconds apart on several dates (e.g. 2026-04-04 has ~150 log files within a single afternoon), consistent with crash-restart loops during earlier development/field debugging.
- `hailort.log` / `hailort.1.log` (Hailo runtime's own rotating log) total ~1.5MB — these *do* rotate (HailoRT's own mechanism), unlike the app's own logs.

At current growth rate (a fresh log set per boot/restart, no cap) this will eventually fill the 58G root partition on a device that reboots unpredictably (§7) and is not remotely accessible for cleanup except over Tailscale.

## 10. Storage / recording

- `RECORD=1`, target `production/Duromac_roadmonitoring/recording` under whichever storage root wins the SSD → pendrive → SD priority chain (`config/settings.py`).
- No `/mnt/ssd` present → falls back to the Kingston pendrive (`/media/rasp/KINGSTON`), which is mounted but nearly empty (1.8M used of 116G) at capture time — either recording just started fresh, the drive was recently swapped/wiped, or recording is not actually writing there (not independently verified beyond the empty `df -h` figure; worth a live check).
- `SAVE_DETECTIONS_LOCAL` is unset (defaults off) — no local per-detection JPEG/TXT save active; all detections flow through the S3/API upload pipeline only.

## 11. Pending OS packages

Not captured in detail in this pass beyond `apt list --upgradable` being queried — see raw capture appendix 2 for the exact output if package-freshness matters later.

## 12. Camera identity (from project's own `CLAUDE.md`, verified consistent with live TCP check)

| | |
|---|---|
| Address | `192.168.1.102/24`, gateway `192.168.1.1`, static (not DHCP) |
| MAC | `aa:f9:68:15:1e:ec` |
| Model | General ADA32N1, firmware `20251204.7369` |
| Stream | `rtsp://192.168.1.102/live/mainstream`, h264, 2688×1944 @ 25fps |
| Web UI | `http://192.168.1.102:8080` |

Documented known failure mode (already happened once, 2026-07-24): the camera's IP is static in its own flash and does **not** follow a truck-LAN renumbering, causing a silent multi-hour stall until `utils/find_camera.sh` was built. Still not on DHCP — the underlying prevention step (DHCP reservation + switch camera to DHCP) is explicitly flagged as not yet done in the project's own docs.

---

*Continued in `hailo-rpi5-examples-codebase-report.md` for application-level (code) findings.*
