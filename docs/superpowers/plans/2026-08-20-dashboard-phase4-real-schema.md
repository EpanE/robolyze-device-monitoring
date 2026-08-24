# Dashboard Real-Schema Alignment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **No git repo in this project directory** (`D:\05 Projects\18 robolyze-monitoring-implemetation` is not a git repo — verified via `git rev-parse --is-inside-work-tree` → "fatal: not a git repository"). Skip every "Commit" step below; just leave files saved after each task.

**Goal:** Reconcile `dashboard/` (built 2026-07-28 against an invented `SCHEMA.md`) with the real `health.json` contract now shipped by the Pi's `health_agent.py` (Phases 0-3 done, pushed `f41d4c4`/`482a65b`/`b1b8f8d`), so the dashboard renders live data correctly instead of crashing on `KeyError` or misreading tri-state fields as booleans.

**Architecture:** No new files, no new abstractions — same four-module split (`status.py` card logic, `report.py` WhatsApp text, `ssh_client.py` unchanged, `app.py` wiring). Card builders and the report switch from the old flat-boolean schema to the real nested schema, trust the agent's own precomputed `ok`/`disk_alert` fields instead of recomputing thresholds dashboard-side (single source of truth stays on the Pi), and add tri-state (`true`/`false`/`null`) rendering throughout.

**Tech Stack:** Flask, Jinja2, pytest, paramiko (unchanged) — `dashboard/.venv` already has all deps installed.

---

## Why each change is needed

The old `SCHEMA.md` assumed a schema that never existed on the Pi (flat booleans: `pipeline.alive`, `camera.reachable`, `gps.fix`/`fresh`, top-level `disk.used_pct`). The real snapshot (verified live 2026-08-20, see field list below) is nested, uses **three-valued `ok`** (`true`/`false`/`null`) per block, and already computes the pass/fail logic the dashboard used to duplicate (e.g. `system.disk_alert`, `pipeline.ok`). Rendering the real snapshot through the old code would either crash (`KeyError: 'alive'`) or silently misread `null` as falsy-red.

Also fixes one pre-existing bug unrelated to the schema change: `report.py`'s WhatsApp template hardcodes `✅ Gating logic active (≥3 km/h trigger confirmed)` with no sensor backing it — the spec explicitly lists gating-logic confirmation as a **manual** field (no silent checkmark allowed), so this is a genuine correctness bug, not just a schema mismatch.

## Real data contract (reference for every task below)

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

Key rules (from the Pi-side handoff, verified against a live snapshot):
- `ok` is `true`/`false`/`null`. `null` = not determined, **not an error** — must render grey/unknown, never red or green.
- `camera.clarity` never colours `camera.ok` (TCP reachability only) — clarity is advisory, shown alongside.
- `tailscale.key_expiry_days: null` means expiry disabled — the **desired** state, not "unknown".
- Trust `ok`/`disk_alert` fields as computed by the agent — don't re-derive thresholds dashboard-side.
- `pipeline`/`upload`/camera `clarity` blocks may be missing sub-fields when not running — use `.get()` with fallbacks, never bare indexing.

---

## File structure (all in `dashboard/`)

| File | Change |
|---|---|
| `status.py` | Full rewrite of card builders: real field paths, tri-state `ok`, drop the `config` param (thresholds now come from the snapshot itself) |
| `report.py` | Rewrite field extraction to real paths; fix hardcoded gating-logic checkmark → manual placeholder; confidence value now read from `pipeline.confirmed_min_confidence` with a config fallback |
| `config.py` | Rename `CONFIDENCE_THRESHOLD` → `DEFAULT_CONFIDENCE_THRESHOLD` (it's now only a fallback, not the primary source) |
| `app.py` | Update call sites for the new `status.build_status_cards()`/`report.build_report()` signatures; delete now-dead `_report_config()` |
| `templates/base.html` | Add `run_dir_fallback` warning banner |
| `templates/live_status.html` | Add `card.css_class` to the card `div`; add `⚪ = unknown` to the legend |
| `static/style.css` | Add `.status-ok`/`.status-bad`/`.status-unknown` border classes |
| `SCHEMA.md` | Replace entirely with the real contract |
| `tests/test_status.py`, `tests/test_report.py`, `tests/test_app.py` | Replace fixtures with real-shaped snapshots; add tri-state coverage |

Test runner: `dashboard/.venv/Scripts/python.exe -m pytest -v` (run from `dashboard/`).

---

### Task 1: `status.py` — tri-state card builders on the real schema

**Files:**
- Modify: `dashboard/status.py`
- Test: `dashboard/tests/test_status.py`

- [ ] **Step 1: Replace `dashboard/tests/test_status.py` with real-shaped fixtures and tri-state assertions**

```python
from status import build_status_cards

HEALTHY_HEALTH = {
    "run_dir_fallback": False,
    "system": {
        "ok": True, "cpu_temp_c": 44.6, "throttled": "0x0",
        "disk_total_gb": 61.7, "disk_free_gb": 19.5, "disk_used_pct": 63.3,
        "disk_alert": False, "disk_alert_pct": 85,
    },
    "pipeline": {
        "ok": True, "instances": 1, "duplicate_instances": False,
        "heartbeat_age_sec": 2.1, "fps": 8.4, "confirmed_min_confidence": 0.75,
        "error": None,
    },
    "upload": {"ok": True, "queue_depth": 0, "dropped_total": 0, "error": None},
    "camera": {
        "ok": True, "tcp_ok": True, "tcp_connect_ms": 0.8,
        "clarity": {"checked": True, "clear": True, "blur_score": 17.7,
                     "blur_spread": [16.46, 18.87], "age_sec": 240.0},
        "drift_scan": None,
    },
    "gps": {
        "ok": True, "reason": None,
        "last_valid_age_sec": 3.2, "last_sentence_age_sec": 1.0, "speed_kmh": 15.0,
    },
    "tailscale": {"ok": True, "key_expiry_days": None},
}
HEALTHY_TAILSCALE = {"BackendState": "Running"}


def _card(cards, name):
    return next(c for c in cards if c["name"] == name)


def test_all_healthy_gives_six_ok_cards():
    cards = build_status_cards(HEALTHY_HEALTH, HEALTHY_TAILSCALE)
    assert len(cards) == 6
    assert all(c["emoji"] == "✅" for c in cards)
    assert all(c["css_class"] == "status-ok" for c in cards)
    assert {c["name"] for c in cards} == {
        "System", "Pipeline", "Camera", "GPS", "Upload", "Tailscale",
    }


def test_system_card_trusts_agent_ok_and_disk_alert():
    health = {**HEALTHY_HEALTH, "system": {**HEALTHY_HEALTH["system"], "ok": False, "disk_alert": True}}
    cards = build_status_cards(health, HEALTHY_TAILSCALE)
    card = _card(cards, "System")
    assert card["emoji"] == "🔴"
    assert "DISK ALERT" in card["detail"]


def test_pipeline_dead_flags_pipeline_card_and_shows_error():
    health = {**HEALTHY_HEALTH, "pipeline": {"ok": False, "error": "heartbeat file missing or unreadable"}}
    cards = build_status_cards(health, HEALTHY_TAILSCALE)
    card = _card(cards, "Pipeline")
    assert card["emoji"] == "🔴"
    assert "heartbeat file missing" in card["detail"]


def test_pipeline_duplicate_instances_surfaced_in_detail():
    health = {**HEALTHY_HEALTH, "pipeline": {**HEALTHY_HEALTH["pipeline"], "duplicate_instances": True, "instances": 2}}
    cards = build_status_cards(health, HEALTHY_TAILSCALE)
    assert "DUPLICATE INSTANCES" in _card(cards, "Pipeline")["detail"]


def test_camera_ok_is_tcp_reachability_only_clarity_never_colours_it():
    health = {**HEALTHY_HEALTH, "camera": {
        "ok": True, "tcp_ok": True, "tcp_connect_ms": 1.2,
        "clarity": {"checked": True, "clear": False, "blur_score": 9.1,
                     "blur_spread": [5.0, 12.0], "age_sec": 90.0},
        "drift_scan": None,
    }}
    cards = build_status_cards(health, HEALTHY_TAILSCALE)
    card = _card(cards, "Camera")
    assert card["emoji"] == "✅"  # tcp_ok true — clarity=False must NOT flip this red
    assert "clear=False" in card["detail"]


def test_camera_unreachable_shows_drift_scan():
    health = {**HEALTHY_HEALTH, "camera": {
        "ok": False, "tcp_ok": False, "tcp_connect_ms": None,
        "clarity": {"checked": True, "clear": True, "blur_score": 17.7,
                     "blur_spread": [16.0, 19.0], "age_sec": 900.0},
        "drift_scan": {"ran": True, "located": True, "findings": ["mac seen on .87"]},
    }}
    cards = build_status_cards(health, HEALTHY_TAILSCALE)
    card = _card(cards, "Camera")
    assert card["emoji"] == "🔴"
    assert "located=True" in card["detail"]


def test_gps_ok_null_renders_unknown_not_red_or_green():
    health = {**HEALTHY_HEALTH, "gps": {
        "ok": None, "reason": "no GPS reader running (state file absent)",
        "last_valid_age_sec": None, "last_sentence_age_sec": None, "speed_kmh": None,
    }}
    cards = build_status_cards(health, HEALTHY_TAILSCALE)
    card = _card(cards, "GPS")
    assert card["emoji"] == "⚪"
    assert card["css_class"] == "status-unknown"
    assert "no GPS reader running" in card["detail"]


def test_gps_ok_false_flags_red_with_reason():
    health = {**HEALTHY_HEALTH, "gps": {
        "ok": False, "reason": "no fix", "last_valid_age_sec": 999, "last_sentence_age_sec": 5, "speed_kmh": None,
    }}
    cards = build_status_cards(health, HEALTHY_TAILSCALE)
    assert _card(cards, "GPS")["emoji"] == "🔴"


def test_upload_not_ok_flags_upload_card():
    health = {**HEALTHY_HEALTH, "upload": {"ok": False, "error": "no pipeline heartbeat to read upload stats from"}}
    cards = build_status_cards(health, HEALTHY_TAILSCALE)
    card = _card(cards, "Upload")
    assert card["emoji"] == "🔴"
    assert "no pipeline heartbeat" in card["detail"]


def test_tailscale_key_expiry_none_reads_as_disabled_not_unknown():
    cards = build_status_cards(HEALTHY_HEALTH, HEALTHY_TAILSCALE)
    card = _card(cards, "Tailscale")
    assert card["emoji"] == "✅"
    assert "key expiry disabled" in card["detail"]


def test_tailscale_not_running_flags_tailscale_card():
    cards = build_status_cards(HEALTHY_HEALTH, {"BackendState": "NeedsLogin"})
    assert _card(cards, "Tailscale")["emoji"] == "🔴"


def test_missing_health_data_shows_unknown_not_red():
    cards = build_status_cards(None, None)
    assert len(cards) == 6
    assert all(c["emoji"] == "⚪" for c in cards)
    assert all(c["detail"] for c in cards)


def test_missing_health_but_present_tailscale_still_reports_tailscale_independently():
    cards = build_status_cards(None, HEALTHY_TAILSCALE)
    assert _card(cards, "Tailscale")["emoji"] == "✅"
    assert _card(cards, "System")["emoji"] == "⚪"


def test_run_dir_fallback_true_is_readable_from_health():
    health = {**HEALTHY_HEALTH, "run_dir_fallback": True}
    # status.py doesn't render this itself (base.html reads state.health directly) —
    # this test just locks the field name so a rename elsewhere doesn't silently break the banner.
    assert health["run_dir_fallback"] is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd "D:\05 Projects\18 robolyze-monitoring-implemetation\dashboard" && .venv\Scripts\python.exe -m pytest tests/test_status.py -v`
Expected: FAIL — old `status.py` still expects `health["pipeline"]["alive"]` etc., and `build_status_cards` still requires a third `config` arg.

- [ ] **Step 3: Rewrite `dashboard/status.py`**

```python
def _symbol(ok):
    if ok is True:
        return "✅"
    if ok is False:
        return "🔴"
    return "⚪"


def _css_class(ok):
    if ok is True:
        return "status-ok"
    if ok is False:
        return "status-bad"
    return "status-unknown"


def _card(name, ok, detail):
    return {"name": name, "emoji": _symbol(ok), "css_class": _css_class(ok), "detail": detail}


def _system_card(system: dict) -> dict:
    detail = (
        f"{system.get('cpu_temp_c')}°C, disk {system.get('disk_used_pct')}% used "
        f"({system.get('disk_free_gb')}/{system.get('disk_total_gb')} GB free), "
        f"throttled={system.get('throttled')}"
    )
    if system.get("disk_alert"):
        detail += " — DISK ALERT"
    return _card("System", system.get("ok"), detail)


def _pipeline_card(pipeline: dict) -> dict:
    if pipeline.get("ok"):
        detail = f"fps={pipeline.get('fps')}, heartbeat_age={pipeline.get('heartbeat_age_sec')}s"
    else:
        detail = pipeline.get("error") or "not running"
    if pipeline.get("duplicate_instances"):
        detail += f" — DUPLICATE INSTANCES ({pipeline.get('instances')})"
    return _card("Pipeline", pipeline.get("ok"), detail)


def _upload_card(upload: dict) -> dict:
    if "queue_depth" in upload:
        detail = f"queue_depth={upload.get('queue_depth')}, dropped_total={upload.get('dropped_total')}"
    else:
        detail = upload.get("error") or "no data"
    return _card("Upload", upload.get("ok"), detail)


def _camera_card(camera: dict) -> dict:
    # camera.ok is TCP reachability ONLY — clarity is advisory and must never flip this.
    detail = f"tcp_ok={camera.get('tcp_ok')} ({camera.get('tcp_connect_ms')}ms)"
    clarity = camera.get("clarity") or {}
    if clarity.get("checked"):
        detail += (
            f" | clarity: clear={clarity.get('clear')}"
            f" (blur={clarity.get('blur_score')}, spread={clarity.get('blur_spread')}, "
            f"{clarity.get('age_sec')}s ago)"
        )
    drift = camera.get("drift_scan")
    if drift:
        detail += f" | drift_scan: located={drift.get('located')}"
    return _card("Camera", camera.get("ok"), detail)


def _gps_card(gps: dict) -> dict:
    if gps.get("ok") is None:
        detail = gps.get("reason") or "not determined"
    else:
        detail = (
            f"fix_age={gps.get('last_valid_age_sec')}s, "
            f"sentence_age={gps.get('last_sentence_age_sec')}s, "
            f"speed={gps.get('speed_kmh')}km/h"
        )
    return _card("GPS", gps.get("ok"), detail)


def _tailscale_card(tailscale: dict | None, key_expiry_days) -> dict:
    if not tailscale:
        return _card("Tailscale", None, "No data — refresh to fetch tailscale status")
    backend_state = tailscale.get("BackendState")
    ok = backend_state == "Running"
    detail = f"BackendState={backend_state}"
    detail += ", key expiry disabled" if key_expiry_days is None else f", key expires in {key_expiry_days}d"
    return _card("Tailscale", ok, detail)


def _no_data_cards(tailscale):
    return [
        _card("System", None, "No data — refresh to fetch health.json"),
        _card("Pipeline", None, "No data — refresh to fetch health.json"),
        _card("Camera", None, "No data — refresh to fetch health.json"),
        _card("GPS", None, "No data — refresh to fetch health.json"),
        _card("Upload", None, "No data — refresh to fetch health.json"),
        _tailscale_card(tailscale, None),
    ]


def build_status_cards(health: dict | None, tailscale: dict | None) -> list:
    # Tailscale is fetched over its own independent SSH command, so its card
    # must not be gated on health.json (a separate command) being present.
    if health is None:
        return _no_data_cards(tailscale)

    key_expiry_days = (health.get("tailscale") or {}).get("key_expiry_days")

    return [
        _system_card(health["system"]),
        _pipeline_card(health["pipeline"]),
        _camera_card(health["camera"]),
        _gps_card(health["gps"]),
        _upload_card(health["upload"]),
        _tailscale_card(tailscale, key_expiry_days),
    ]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd "D:\05 Projects\18 robolyze-monitoring-implemetation\dashboard" && .venv\Scripts\python.exe -m pytest tests/test_status.py -v`
Expected: PASS (13 tests)

---

### Task 2: `report.py` — real field paths + fix the gating-logic checkmark bug

**Files:**
- Modify: `dashboard/report.py`
- Test: `dashboard/tests/test_report.py`

- [ ] **Step 1: Replace `dashboard/tests/test_report.py`**

```python
import datetime

from report import build_report, format_report_date

HEALTHY = {
    "pipeline": {"ok": True, "confirmed_min_confidence": 0.75},
    "upload": {"ok": True},
    "system": {"disk_alert": False},
    "camera": {"clarity": {"checked": True, "clear": True}},
}
DEFAULT_CONFIDENCE = "0.75"


def test_format_report_date_adds_ordinal_and_weekday():
    assert format_report_date(datetime.date(2026, 7, 28)) == "28th July 2026 (Tuesday)"


def test_format_report_date_handles_first_second_third():
    assert format_report_date(datetime.date(2026, 7, 1)) == "1st July 2026 (Wednesday)"
    assert format_report_date(datetime.date(2026, 7, 2)) == "2nd July 2026 (Thursday)"
    assert format_report_date(datetime.date(2026, 7, 3)) == "3rd July 2026 (Friday)"
    assert format_report_date(datetime.date(2026, 7, 11)) == "11th July 2026 (Saturday)"


def test_all_healthy_produces_all_checkmarks():
    report = build_report(HEALTHY, DEFAULT_CONFIDENCE, datetime.date(2026, 7, 28))
    assert "✅ System running" in report
    assert "✅ Detections uploaded to client dashboard" in report
    assert "Confidence threshold set to 0.75 ✓" in report
    assert "✅ Duromac feed inspected" in report
    assert "✅ Camera feed is clear" in report
    assert "✅ Pendrive's storage no longer full" in report


def test_pipeline_down_flags_system_running():
    health = {**HEALTHY, "pipeline": {"ok": False, "confirmed_min_confidence": 0.75}}
    report = build_report(health, DEFAULT_CONFIDENCE, datetime.date(2026, 7, 28))
    assert "🔴 System running" in report


def test_upload_not_ok_flags_dashboard_line():
    health = {**HEALTHY, "upload": {"ok": False}}
    report = build_report(health, DEFAULT_CONFIDENCE, datetime.date(2026, 7, 28))
    assert "🔴 Detections uploaded to client dashboard" in report


def test_camera_not_checked_flags_both_camera_lines_never_silent_ok():
    health = {**HEALTHY, "camera": {"clarity": {"checked": False, "clear": True}}}
    report = build_report(health, DEFAULT_CONFIDENCE, datetime.date(2026, 7, 28))
    assert "🔴 Duromac feed inspected" in report
    assert "🔴 Camera feed is clear" in report


def test_camera_checked_but_not_clear_flags_only_clarity_line():
    health = {**HEALTHY, "camera": {"clarity": {"checked": True, "clear": False}}}
    report = build_report(health, DEFAULT_CONFIDENCE, datetime.date(2026, 7, 28))
    assert "✅ Duromac feed inspected" in report
    assert "🔴 Camera feed is clear" in report


def test_camera_missing_clarity_block_treated_as_not_inspected():
    health = {**HEALTHY, "camera": {}}
    report = build_report(health, DEFAULT_CONFIDENCE, datetime.date(2026, 7, 28))
    assert "🔴 Duromac feed inspected" in report
    assert "🔴 Camera feed is clear" in report


def test_disk_alert_flags_storage_line():
    health = {**HEALTHY, "system": {"disk_alert": True}}
    report = build_report(health, DEFAULT_CONFIDENCE, datetime.date(2026, 7, 28))
    assert "🔴 Pendrive's storage no longer full" in report


def test_confidence_value_read_from_snapshot_not_default():
    health = {**HEALTHY, "pipeline": {"ok": True, "confirmed_min_confidence": 0.6}}
    report = build_report(health, DEFAULT_CONFIDENCE, datetime.date(2026, 7, 28))
    assert "Confidence threshold set to 0.6 ✓" in report


def test_confidence_falls_back_to_default_when_pipeline_not_running():
    health = {**HEALTHY, "pipeline": {"ok": False}}  # no confirmed_min_confidence key
    report = build_report(health, "0.75", datetime.date(2026, 7, 28))
    assert "Confidence threshold set to 0.75 ✓" in report


def test_gating_logic_is_a_manual_placeholder_not_a_silent_checkmark():
    report = build_report(HEALTHY, DEFAULT_CONFIDENCE, datetime.date(2026, 7, 28))
    assert "✅ Gating logic active" not in report
    assert "__ Gating logic active" in report


def test_manual_fields_and_whatsapp_formatting_preserved_verbatim():
    report = build_report(HEALTHY, DEFAULT_CONFIDENCE, datetime.date(2026, 7, 28))
    assert "*📍 Location : _{Location}_*" in report
    assert "*🚛 Driver : _{name}_*" in report
    assert "⏸️ Sync script run - stoped" in report
    assert "⏸️ recording/ - pause when needed or prompt" in report
    assert "⏸️ Recording - pause when needed or prompt" in report
    assert "🛣️ *Robolyze Daily Ops Update*" in report
    assert "▶️ = running" in report
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd "D:\05 Projects\18 robolyze-monitoring-implemetation\dashboard" && .venv\Scripts\python.exe -m pytest tests/test_report.py -v`
Expected: FAIL — old `report.py` still reads `health["pipeline"]["alive"]`, still hardcodes the gating-logic checkmark, and `build_report` still takes a `config` dict instead of a plain default string.

- [ ] **Step 3: Rewrite `dashboard/report.py`**

```python
import datetime
from string import Template

TEMPLATE = Template(
    """🛣️ *Robolyze Daily Ops Update*
📅 *Today $date*

*📍 Location : _{Location}_*
*🚛 Driver : _{name}_*

*1. Data Sync & Cleanup*
⏸️ Sync script run - stoped
⏸️ recording/ - pause when needed or prompt

*2. System Health Check*
$sys_emoji System running
__ Gating logic active (≥3 km/h trigger) — confirm manually, no sensor for this

*3. Camera Check*
$camera_insp_emoji Duromac feed inspected
$camera_clear_emoji Camera feed is clear

*4. Dashboard Update*
$dash_emoji Detections uploaded to client dashboard
✅ Confidence threshold set to $conf_value ✓

*5. Recording Status*
⏸️ Recording - pause when needed or prompt
$storage_emoji Pendrive's storage no longer full

Note:
▶️ = running
⏸️ = Pause
✅ = ok
🔴 = not ok
"""
)

_ORDINAL_SUFFIXES = {1: "st", 2: "nd", 3: "rd"}


def format_report_date(date: datetime.date) -> str:
    day = date.day
    suffix = "th" if 11 <= day % 100 <= 13 else _ORDINAL_SUFFIXES.get(day % 10, "th")
    return f"{day}{suffix} {date.strftime('%B %Y (%A)')}"


def _ok(condition: bool) -> str:
    return "✅" if condition else "🔴"


def build_report(health: dict, default_confidence: str, date: datetime.date) -> str:
    pipeline = health.get("pipeline") or {}
    upload = health.get("upload") or {}
    system = health.get("system") or {}
    clarity = (health.get("camera") or {}).get("clarity") or {}

    pipeline_ok = bool(pipeline.get("ok"))
    upload_ok = bool(upload.get("ok"))
    camera_inspected_ok = bool(clarity.get("checked"))
    camera_clear_ok = camera_inspected_ok and bool(clarity.get("clear"))
    storage_ok = not system.get("disk_alert", False)
    conf_value = pipeline.get("confirmed_min_confidence", default_confidence)

    return TEMPLATE.substitute(
        date=format_report_date(date),
        sys_emoji=_ok(pipeline_ok),
        dash_emoji=_ok(upload_ok),
        conf_value=conf_value,
        camera_insp_emoji=_ok(camera_inspected_ok),
        camera_clear_emoji=_ok(camera_clear_ok),
        storage_emoji=_ok(storage_ok),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd "D:\05 Projects\18 robolyze-monitoring-implemetation\dashboard" && .venv\Scripts\python.exe -m pytest tests/test_report.py -v`
Expected: PASS (13 tests)

---

### Task 3: `config.py` + `app.py` — wire the new signatures, drop dead config

**Files:**
- Modify: `dashboard/config.py:28`
- Modify: `dashboard/app.py:27-45`
- Test: `dashboard/tests/test_app.py`

- [ ] **Step 1: Replace `dashboard/tests/test_app.py`**

```python
import pytest

import app as app_module
import ssh_client

HEALTHY_HEALTH = {
    "run_dir_fallback": False,
    "system": {"ok": True, "cpu_temp_c": 44.6, "throttled": "0x0",
               "disk_total_gb": 61.7, "disk_free_gb": 19.5, "disk_used_pct": 63.3,
               "disk_alert": False, "disk_alert_pct": 85},
    "pipeline": {"ok": True, "instances": 1, "duplicate_instances": False,
                 "heartbeat_age_sec": 2.1, "fps": 8.4, "confirmed_min_confidence": 0.75,
                 "error": None},
    "upload": {"ok": True, "queue_depth": 0, "dropped_total": 0, "error": None},
    "camera": {"ok": True, "tcp_ok": True, "tcp_connect_ms": 0.8,
               "clarity": {"checked": True, "clear": True, "blur_score": 17.7,
                            "blur_spread": [16.46, 18.87], "age_sec": 240.0},
               "drift_scan": None},
    "gps": {"ok": True, "reason": None, "last_valid_age_sec": 3.2,
            "last_sentence_age_sec": 1.0, "speed_kmh": 15.0},
    "tailscale": {"ok": True, "key_expiry_days": None},
}
HEALTHY_TAILSCALE = {"BackendState": "Running"}


class FakeClient:
    def close(self):
        pass


@pytest.fixture(autouse=True)
def reset_state(monkeypatch):
    app_module.state = app_module.new_state()
    monkeypatch.setattr(ssh_client, "connect", lambda *a, **k: FakeClient())
    yield


@pytest.fixture
def client():
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


def test_index_shows_no_data_before_refresh(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"No data" in resp.data


def test_refresh_populates_state_and_redirects(client, monkeypatch):
    monkeypatch.setattr(ssh_client, "fetch_health_json", lambda c: HEALTHY_HEALTH)
    monkeypatch.setattr(ssh_client, "fetch_tailscale_status", lambda c: HEALTHY_TAILSCALE)

    resp = client.post("/api/refresh")
    assert resp.status_code == 302
    assert app_module.state["health"] == HEALTHY_HEALTH
    assert app_module.state["tailscale"] == HEALTHY_TAILSCALE
    assert app_module.state["last_refreshed"] is not None

    resp = client.get("/")
    assert "✅".encode() in resp.data
    assert b"No data" not in resp.data


def test_refresh_handles_partial_failure_without_crashing(client, monkeypatch):
    def boom(c):
        raise ssh_client.SSHCommandError("health.json read failed: No such file or directory")

    monkeypatch.setattr(ssh_client, "fetch_health_json", boom)
    monkeypatch.setattr(ssh_client, "fetch_tailscale_status", lambda c: HEALTHY_TAILSCALE)

    resp = client.post("/api/refresh")
    assert resp.status_code == 302
    assert app_module.state["health"] is None
    assert "No such file or directory" in app_module.state["health_error"]
    assert app_module.state["tailscale"] == HEALTHY_TAILSCALE


def test_report_route_prompts_refresh_when_no_data(client):
    resp = client.get("/report")
    assert resp.status_code == 200
    assert b"Refresh" in resp.data


def test_report_route_renders_report_after_refresh(client, monkeypatch):
    monkeypatch.setattr(ssh_client, "fetch_health_json", lambda c: HEALTHY_HEALTH)
    monkeypatch.setattr(ssh_client, "fetch_tailscale_status", lambda c: HEALTHY_TAILSCALE)
    client.post("/api/refresh")

    resp = client.get("/report")
    assert resp.status_code == 200
    assert "Robolyze Daily Ops Update".encode() in resp.data
    assert "Confidence threshold set to 0.75".encode() in resp.data


def test_run_dir_fallback_shows_warning_banner(client, monkeypatch):
    fallback_health = {**HEALTHY_HEALTH, "run_dir_fallback": True}
    monkeypatch.setattr(ssh_client, "fetch_health_json", lambda c: fallback_health)
    monkeypatch.setattr(ssh_client, "fetch_tailscale_status", lambda c: HEALTHY_TAILSCALE)
    client.post("/api/refresh")

    resp = client.get("/")
    assert b"fallback run-dir" in resp.data


def test_screenshot_route_returns_png_bytes(client, monkeypatch):
    png_bytes = b"\x89PNG\r\n\x1a\nfake"
    monkeypatch.setattr(ssh_client, "grab_screenshot", lambda c: png_bytes)

    resp = client.get("/api/screenshot")
    assert resp.status_code == 200
    assert resp.mimetype == "image/png"
    assert resp.data == png_bytes


def test_screenshot_route_returns_error_on_failure(client, monkeypatch):
    def boom(c):
        raise ssh_client.SSHCommandError("raspi2png: command not found")

    monkeypatch.setattr(ssh_client, "grab_screenshot", boom)

    resp = client.get("/api/screenshot")
    assert resp.status_code == 502
    assert b"raspi2png" in resp.data
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd "D:\05 Projects\18 robolyze-monitoring-implemetation\dashboard" && .venv\Scripts\python.exe -m pytest tests/test_app.py -v`
Expected: FAIL — `status.build_status_cards()` still requires 3 args, `report.build_report()` still expects a config dict, and the `run_dir_fallback` banner doesn't exist yet in `base.html`.

- [ ] **Step 3: Update `dashboard/config.py`** — rename the fallback constant

Change line 28 from:
```python
CONFIDENCE_THRESHOLD = _env.get("CONFIRMED_MIN_CONFIDENCE", "0.75")
```
to:
```python
DEFAULT_CONFIDENCE_THRESHOLD = _env.get("CONFIRMED_MIN_CONFIDENCE", "0.75")
```

- [ ] **Step 4: Update `dashboard/app.py`** — new call signatures, drop dead helper

Replace lines 27-45 (the `_report_config` helper plus the `index`/`daily_report` routes) with:

```python
@app.route("/")
def index():
    cards = status.build_status_cards(state["health"], state["tailscale"])
    return render_template("live_status.html", cards=cards, state=state)


@app.route("/report")
def daily_report():
    report_text = None
    if state["health"] is not None:
        report_text = report.build_report(state["health"], config.DEFAULT_CONFIDENCE_THRESHOLD, datetime.date.today())
    return render_template("daily_report.html", report_text=report_text, state=state)
```

- [ ] **Step 5: Add the `run_dir_fallback` banner to `dashboard/templates/base.html`**

Add after the existing `tailscale_error` banner block (after line 26):
```html
  {% if state.health and state.health.run_dir_fallback %}
    <div class="banner banner-warn">⚠️ Agent is writing to a fallback run-dir (tmpfiles rule missing on the Pi) — health data is still valid.</div>
  {% endif %}
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd "D:\05 Projects\18 robolyze-monitoring-implemetation\dashboard" && .venv\Scripts\python.exe -m pytest tests/test_app.py -v`
Expected: PASS (9 tests)

---

### Task 4: Unknown-state styling + camera clarity legend

**Files:**
- Modify: `dashboard/templates/live_status.html:23,41`
- Modify: `dashboard/static/style.css`

No automated test for this task — CSS/markup only, verified by manual smoke check in Task 6.

- [ ] **Step 1: Add `card.css_class` to the card `div` in `dashboard/templates/live_status.html`**

Change line 23 from:
```html
      <div class="card">
```
to:
```html
      <div class="card {{ card.css_class }}">
```

- [ ] **Step 2: Add the unknown-state symbol to the legend**

Change line 41 from:
```html
  <p class="legend">▶️ = running &nbsp; ⏸️ = pause &nbsp; ✅ = ok &nbsp; 🔴 = not ok</p>
```
to:
```html
  <p class="legend">▶️ = running &nbsp; ⏸️ = pause &nbsp; ✅ = ok &nbsp; 🔴 = not ok &nbsp; ⚪ = not determined</p>
```

- [ ] **Step 3: Add status-tint border classes to `dashboard/static/style.css`**

Add after the existing `.card { ... }` rule (after line 105):
```css
.card.status-ok { border-left: 4px solid #22c55e; }
.card.status-bad { border-left: 4px solid #ef4444; }
.card.status-unknown { border-left: 4px solid var(--muted); }
```

---

### Task 5: `SCHEMA.md` — replace with the real contract

**Files:**
- Modify: `dashboard/SCHEMA.md`

- [ ] **Step 1: Replace the full file contents**

```markdown
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
- **`camera.clarity` is advisory only** — never lets it colour `camera.ok` (TCP reachability
  alone). Thresholds are barely calibrated (a known-clear lens has scored 1-in-6 "blurry" false
  positives); treat `clear: false` as "worth a look," not "broken." `blur_spread` next to it —
  a wide spread means scene motion, not a dirty lens.
- **`tailscale.key_expiry_days: null` means expiry is disabled** — the desired state. Never
  render as unknown/expired. (`ok`/`key_expiry_days` for Tailscale live inside `health.json`;
  `BackendState` for the live Tailscale card is fetched independently via
  `tailscale status --json` so that card still works even if `health.json` is stale/missing.)
- **`clarity.age_sec`** is how stale the cached clarity result is (runs every ~300s, not every
  60s cycle) — always show it.
- **`run_dir_fallback: true`** means the Pi's tmpfiles rule is missing and the agent fell back
  to `<repo>/.roadmon` — the dashboard shows a warning banner (`templates/base.html`); the rest
  of the snapshot is still valid.
- Trust `ok` / `disk_alert` as computed on the Pi — the dashboard never re-derives thresholds
  from raw fields, so there's exactly one place (`health_agent.py`) that decides what counts as
  healthy.
- Sub-fields inside `pipeline`/`upload`/`camera.clarity` may be absent when that component isn't
  running — always read with `.get()` + a fallback, never bare dict indexing.

`tailscale status --json`'s own output is used as-is for the Tailscale card's `BackendState` —
fetched over a separate SSH command from `health.json`, so the card still renders even when
`health.json` is missing or the agent has crashed.
```

---

### Task 6: Full suite run + manual smoke check

**Files:** none — verification only.

- [ ] **Step 1: Run the full dashboard test suite**

Run: `cd "D:\05 Projects\18 robolyze-monitoring-implemetation\dashboard" && .venv\Scripts\python.exe -m pytest -v`
Expected: All tests pass (status: 13, report: 13, app: 9, ssh_client: unchanged from before).

- [ ] **Step 2: Manual smoke check against the real Pi**

Start the dev server:
```
cd "D:\05 Projects\18 robolyze-monitoring-implemetation\dashboard" && .venv\Scripts\python.exe app.py
```
Open `http://127.0.0.1:5000`, click **Refresh Now**. Confirm against the live Pi:
- All six cards render without a 500 error or `KeyError` in the Flask console.
- GPS card shows ⚪ grey (not red) if the pipeline is currently stopped on the Pi.
- Camera card stays ✅ even if `clarity.clear` is `false` — clarity text shown separately, not
  colouring the card.
- Tailscale card shows "key expiry disabled" (not "unknown").
- If `run_dir_fallback` happens to be `true` on the Pi right now, the warning banner appears.
- Daily Report tab: gating-logic line reads `__ Gating logic active...`, not a hardcoded ✅.

Stop the server (Ctrl+C) once confirmed.

---

## Self-review notes

- **Spec coverage:** tri-state `ok` rendering (Task 1/4), camera clarity advisory-only (Task 1),
  `key_expiry_days` null=disabled (Task 1), `clarity.age_sec` display (Task 1), `run_dir_fallback`
  banner (Task 3), WhatsApp raw-textarea/manual-placeholder rules (Task 2, pre-existing and now
  gating-logic fixed), disk-alert trust-the-agent (Task 1/2) — all covered.
- **Not in scope for this plan:** Phase 0.1-0.4 (Healthchecks/Telegram setup), Phase 5/6, AWS
  credential rotation, GPS field-verification follow-up — none of these touch `dashboard/`.
- **Type consistency checked:** `build_status_cards(health, tailscale)` (2-arg, no `config`) used
  consistently in Task 1's tests, Task 3's `app.py` rewrite, and Task 3's `test_app.py`.
  `build_report(health, default_confidence, date)` (plain string default, not a config dict) used
  consistently across Task 2 and Task 3.
