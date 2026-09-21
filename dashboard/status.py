# health.json is rewritten every 60s by health-agent.service. Three missed cycles is the
# line between "the write we happened to race" and "nothing is writing this file". Past it
# the snapshot's own contents are worthless — a dead agent leaves its last good one behind,
# and every field in it keeps reporting a healthy truck indefinitely.
STALE_AFTER_SEC = 180


def is_stale(age_sec) -> bool:
    """age_sec is None when the age could not be established (an older Pi image, say).
    That is not evidence of staleness, so it renders the snapshot as-is."""
    return age_sec is not None and age_sec > STALE_AFTER_SEC


def _section(health: dict, name: str) -> dict:
    # SCHEMA.md: components that aren't running may omit their section entirely, and the
    # schema uses explicit null liberally. Bare indexing here used to 500 the whole page —
    # and since `state` persists in memory, every later page load 500'd too, leaving the
    # operator unable to even reach the Refresh button.
    section = health.get(name)
    return section if isinstance(section, dict) else {}


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


def _card(name, ok, detail, rows=None, badge=None):
    return {
        "name": name, "emoji": _symbol(ok), "css_class": _css_class(ok),
        "ok": ok, "detail": detail, "rows": rows or [], "badge": badge,
    }


def _row(label, value, alert=False) -> dict:
    return {"label": label, "value": value, "alert": bool(alert)}


def _fmt_load(load_avg) -> str | None:
    if not isinstance(load_avg, (list, tuple)) or len(load_avg) < 3:
        return None
    return f"{load_avg[0]:.2f}/{load_avg[1]:.2f}/{load_avg[2]:.2f}"


def _mem_used_pct(mem_total_kb, mem_available_kb):
    if not mem_total_kb:
        return None
    return max(0.0, (mem_total_kb - (mem_available_kb or 0)) / mem_total_kb * 100)


def _fmt_duration_sec(seconds) -> str | None:
    if seconds is None:
        return None
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    return f"{hours}h{rem // 60}m" if hours else f"{rem // 60}m"


def _service_summary(service: dict) -> str:
    parts, alert = [], False
    for name, info in service.items():
        if not isinstance(info, dict):
            continue
        short = name.removesuffix(".service")
        parts.append(f"{short}={info.get('active_state') or 'unknown'}(restarts={info.get('restarts')})")
        if info.get("ok") is False:
            alert = True
    if not parts:
        return ""
    summary = "services: " + ", ".join(parts)
    return summary + " — SERVICE ALERT" if alert else summary


def _describe_action(action) -> str:
    # Shape of self_heal.actions entries isn't pinned down by the Pi (empty in every
    # snapshot seen so far) — degrade to str() rather than assume a dict shape and raise.
    if isinstance(action, dict):
        return str(action.get("type") or action.get("action") or action.get("reason") or action)[:80]
    return str(action)[:80]


def _self_heal_badge(self_heal: dict) -> dict | None:
    if not self_heal:
        return None
    if self_heal.get("enabled") is False:
        return {"emoji": "🔴", "css_class": "status-bad", "text": "Self-heal: disabled"}
    self_heals = self_heal.get("self_heals") or 0
    bad_cycles = self_heal.get("tailscale_bad_cycles") or 0
    if not self_heals and not bad_cycles:
        return {"emoji": "✅", "css_class": "status-ok", "text": "Self-heal: 0 actions"}
    actions = self_heal.get("actions") or []
    last = _describe_action(actions[-1]) if actions else None
    text = f"Self-heal: {self_heals} action(s)" + (f" — last: {last}" if last else "")
    if bad_cycles:
        text += f", tailscale_bad_cycles={bad_cycles}"
    return {"emoji": "⚠️", "css_class": "status-warn", "text": text}


def _system_rows(system: dict, load, mem_pct, uptime, service: dict) -> list:
    rows = []
    if system.get("cpu_temp_c") is not None:
        rows.append(_row("Temperature", f"{system.get('cpu_temp_c')}°C"))
    if system.get("disk_used_pct") is not None:
        rows.append(_row(
            "Disk used",
            f"{system.get('disk_used_pct')}% ({system.get('disk_free_gb')}/{system.get('disk_total_gb')} GB free)",
            alert=system.get("disk_alert"),
        ))
    if load:
        rows.append(_row("Load average", load))
    if mem_pct is not None:
        rows.append(_row("Memory used", f"{mem_pct:.0f}%"))
    if uptime:
        rows.append(_row("Uptime", uptime))
    if system.get("throttled") is not None:
        rows.append(_row("Throttled", str(system.get("throttled"))))
    for name, info in service.items():
        if not isinstance(info, dict):
            continue
        short = name.removesuffix(".service")
        rows.append(_row(
            short, f"{info.get('active_state') or 'unknown'} ({info.get('restarts')} restarts)",
            alert=info.get("ok") is False,
        ))
    return rows


def _system_card(system: dict, self_heal: dict, service: dict) -> dict:
    detail = (
        f"{system.get('cpu_temp_c')}°C, disk {system.get('disk_used_pct')}% used "
        f"({system.get('disk_free_gb')}/{system.get('disk_total_gb')} GB free), "
        f"throttled={system.get('throttled')}"
    )
    if system.get("disk_alert"):
        detail += " — DISK ALERT"
    load = _fmt_load(system.get("load_avg"))
    if load:
        detail += f" | load_avg={load}"
    mem_pct = _mem_used_pct(system.get("mem_total_kb"), system.get("mem_available_kb"))
    if mem_pct is not None:
        detail += f" | mem={mem_pct:.0f}% used"
    uptime = _fmt_duration_sec(system.get("uptime_sec"))
    if uptime:
        detail += f" | uptime={uptime}"
    services = _service_summary(service)
    if services:
        detail += f" | {services}"
    rows = _system_rows(system, load, mem_pct, uptime, service)
    return _card("System", system.get("ok"), detail, rows=rows, badge=_self_heal_badge(self_heal))


def _pipeline_card(pipeline: dict) -> dict:
    rows = []
    if pipeline.get("ok"):
        detail = f"fps={pipeline.get('fps')}, heartbeat_age={pipeline.get('heartbeat_age_sec')}s"
        rows = [
            _row("FPS", str(pipeline.get("fps"))),
            _row("Heartbeat age", f"{pipeline.get('heartbeat_age_sec')}s"),
        ]
    else:
        detail = pipeline.get("error") or "not running"
    if pipeline.get("duplicate_instances"):
        detail += f" — DUPLICATE INSTANCES ({pipeline.get('instances')})"
    return _card("Pipeline", pipeline.get("ok"), detail, rows=rows)


def _upload_card(upload: dict) -> dict:
    rows = []
    if "queue_depth" in upload:
        detail = f"queue_depth={upload.get('queue_depth')}, dropped_total={upload.get('dropped_total')}"
        rows = [
            _row("Queue depth", str(upload.get("queue_depth"))),
            _row("Dropped total", str(upload.get("dropped_total"))),
        ]
    else:
        detail = upload.get("error") or "no data"
    if "training_queue_depth" in upload:
        detail += (
            f" | training: queue_depth={upload.get('training_queue_depth')}, "
            f"dropped_total={upload.get('training_dropped_total')}"
        )
        if not upload.get("training_enabled"):
            detail += " (disabled)"
        if rows:
            rows.append(_row("Training queue depth", str(upload.get("training_queue_depth"))))
            rows.append(_row("Training dropped total", str(upload.get("training_dropped_total"))))
    return _card("Upload", upload.get("ok"), detail, rows=rows)


_CLARITY_ISSUES = [
    ("blank", "BLANK", "no signal / black frame — check camera power & feed"),
    ("obstructed", "OBSTRUCTED", "lens blocked — check for dirt/debris/covering"),
    ("blurry", "BLURRY", "out of focus — check lens focus/mounting"),
]


def _clarity_badge(clarity: dict) -> dict | None:
    # Separate from camera.ok on purpose (see _camera_card) — this is the visible signal
    # clarity never gets to send by coloring the card itself.
    if not clarity.get("checked"):
        return None
    if clarity.get("clear"):
        return {
            "emoji": "✅", "css_class": "status-ok",
            "text": f"Clarity: clear (blur={clarity.get('blur_score')})",
        }
    # Priority order: blank (camera may not be producing a usable frame at all) beats
    # obstructed (dirty/covered lens) beats blurry (focus) when more than one is true.
    for field, label, hint in _CLARITY_ISSUES:
        if clarity.get(field):
            return {
                "emoji": "🔴", "css_class": "status-bad",
                "text": f"Clarity: {label} — {hint} (blur={clarity.get('blur_score')})",
            }
    # None of the specific flags is set (older agent, or a borderline reading) — the
    # original generic behaviour.
    return {
        "emoji": "🔴", "css_class": "status-bad",
        "text": f"Clarity: not clear (blur={clarity.get('blur_score')})",
    }


def _camera_card(camera: dict) -> dict:
    # camera.ok is TCP reachability ONLY — clarity is advisory and must never flip this.
    detail = f"tcp_ok={camera.get('tcp_ok')} ({camera.get('tcp_connect_ms')}ms)"
    rows = [_row(
        "TCP reachable", f"{camera.get('tcp_ok')} ({camera.get('tcp_connect_ms')}ms)",
        alert=camera.get("tcp_ok") is False,
    )]
    clarity = _section(camera, "clarity")
    if clarity.get("checked"):
        detail += (
            f" | clarity: clear={clarity.get('clear')}"
            f" (blur={clarity.get('blur_score')}, spread={clarity.get('blur_spread')}, "
            f"{clarity.get('age_sec')}s ago)"
        )
        rows.append(_row(
            "Clarity", f"{'clear' if clarity.get('clear') else 'not clear'} (blur={clarity.get('blur_score')})",
            alert=clarity.get("clear") is False,
        ))
    else:
        rows.append(_row("Clarity", "not checked yet"))
    drift = camera.get("drift_scan")
    if drift:
        detail += f" | drift_scan: located={drift.get('located')}"
        rows.append(_row("Drift scan", f"located={drift.get('located')}"))
    return _card("Camera", camera.get("ok"), detail, rows=rows, badge=_clarity_badge(clarity))


def as_float(value):
    # The Pi is the authority on what the fix is, but a GPS layer emitting raw NMEA text
    # would otherwise blow up the f-string below with "Unknown format code 'f'".
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _gps_unknown_reason(gps: dict) -> str:
    # `gps.reason` is no longer emitted by the current agent — prefer it verbatim if an
    # older agent still sends it, otherwise construct something useful from what IS
    # still present instead of a hardcoded "not determined" stub.
    reason = gps.get("reason")
    if reason:
        return reason
    if gps.get("device_present") is False:
        return f"GPS device not present ({gps.get('port') or 'no port configured'})"
    if gps.get("device_present") is True:
        nmea = gps.get("nmea_status")
        return (
            f"GPS device present but status not determined (nmea_status={nmea})"
            if nmea else "GPS device present but status not determined"
        )
    return "not determined"


def _gps_card(gps: dict) -> dict:
    rows = []
    if gps.get("ok") is None:
        detail = _gps_unknown_reason(gps)
    else:
        lat, lon = as_float(gps.get("lat")), as_float(gps.get("lon"))
        location = f"{lat:.5f},{lon:.5f}" if lat is not None and lon is not None else "no fix yet"
        detail = (
            f"location={location}, "
            f"fix_age={gps.get('last_valid_age_sec')}s, "
            f"sentence_age={gps.get('last_sentence_age_sec')}s, "
            f"speed={gps.get('speed_kmh')}km/h"
        )
        rows = [
            _row("Location", location),
            _row("Fix age", f"{gps.get('last_valid_age_sec')}s"),
            _row("Sentence age", f"{gps.get('last_sentence_age_sec')}s"),
            _row("Speed", f"{gps.get('speed_kmh')}km/h"),
        ]
    if gps.get("reopen_total") is not None:
        detail += f", reopens={gps.get('reopen_total')}"
        if rows:
            rows.append(_row("Reopens", str(gps.get("reopen_total"))))
    if gps.get("writer_age_sec") is not None:
        detail += f", writer_age={gps.get('writer_age_sec')}s"
        if rows:
            rows.append(_row("Writer age", f"{gps.get('writer_age_sec')}s"))
    return _card("GPS", gps.get("ok"), detail, rows=rows)


def _tailscale_warnings_badge(warnings: list | None) -> dict | None:
    # From health.json's own tailscale.health array — advisory only, deliberately NOT
    # part of `ok` below: `ok` stays tied to the live `tailscale status --json` fetch
    # so this card keeps working when health.json is stale/missing (see SCHEMA.md).
    if not warnings:
        return None
    text = warnings[0] if len(warnings) == 1 else f"{warnings[0]} (+{len(warnings) - 1} more)"
    return {"emoji": "⚠️", "css_class": "status-warn", "text": f"Advisory: {text}"}


def _tailscale_card(tailscale: dict | None, key_expiry_days, warnings: list | None = None) -> dict:
    if not tailscale:
        return _card("Tailscale", None, "No data — refresh to fetch tailscale status")
    backend_state = tailscale.get("BackendState")
    ok = backend_state == "Running"
    detail = f"BackendState={backend_state}"
    detail += ", key expiry disabled" if key_expiry_days is None else f", key expires in {key_expiry_days}d"
    rows = [
        _row("Backend state", str(backend_state)),
        _row("Key expiry", "disabled" if key_expiry_days is None else f"{key_expiry_days}d"),
    ]
    return _card("Tailscale", ok, detail, rows=rows, badge=_tailscale_warnings_badge(warnings))


def _no_data_cards(tailscale):
    return [
        _card("System", None, "No data — refresh to fetch health.json"),
        _card("Pipeline", None, "No data — refresh to fetch health.json"),
        _card("Camera", None, "No data — refresh to fetch health.json"),
        _card("GPS", None, "No data — refresh to fetch health.json"),
        _card("Upload", None, "No data — refresh to fetch health.json"),
        _tailscale_card(tailscale, None),
    ]


def build_location_marker(
    health: dict | None, last_known: dict | None, age_sec=None
) -> dict | None:
    gps = _section(health or {}, "gps")
    lat, lon = as_float(gps.get("lat")), as_float(gps.get("lon"))
    stale_snapshot = is_stale(age_sec)

    if lat is not None and lon is not None:
        freshness = "fresh" if gps.get("ok") and not stale_snapshot else "stale"
        # These coordinates are exactly as old as the snapshot carrying them; a green
        # "Live GPS fix" pin on a frozen file is the map asserting the truck is still there.
        label = (
            f"GPS fix from a stale snapshot ({age_sec:.0f}s old)"
            if stale_snapshot else "Live GPS fix"
        )
    elif last_known:
        lat, lon = last_known["lat"], last_known["lon"]
        freshness = "stale"
        # ts is None when the Pi gave coordinates with nothing to date them by; printing
        # "as of None" reads like a bug and tells the operator less than saying so.
        when = f"as of {last_known['ts']}" if last_known.get("ts") else "fix time unknown"
        label = f"Last known location ({when}) — GPS currently unknown"
    else:
        return None

    return {
        "lat": lat,
        "lon": lon,
        "freshness": freshness,
        "label": label,
        "maps_url": f"https://www.google.com/maps?q={lat},{lon}",
    }


def _stale_cards(tailscale, age_sec, key_expiry_days, tailscale_warnings=None):
    detail = (
        f"Snapshot is stale — {age_sec:.0f}s old, written every 60s. "
        "health-agent.service is not running on the Pi; these values are frozen."
    )
    return [
        _card("System", None, detail),
        _card("Pipeline", None, detail),
        _card("Camera", None, detail),
        _card("GPS", None, detail),
        _card("Upload", None, detail),
        # Live: its own SSH command, so it says nothing about health.json's age.
        _tailscale_card(tailscale, key_expiry_days, tailscale_warnings),
    ]


def _severity(card: dict) -> int:
    ok = card["ok"]
    return 0 if ok is False else 1 if ok is None else 2


def pipeline_down_while_connected(cards: list, stale: bool) -> bool:
    # Mirrors the stale-snapshot banner's reasoning one level down: a dead health-agent
    # already gets a loud banner (see _banners.html), but a dead *pipeline* while the
    # agent and SSH are both fine only turns the card red — easy to miss on a page scan.
    # Only fires on an explicit ok=False, never on None: None means "not determined yet"
    # and every other card already treats that as neutral, not bad.
    if stale:
        return False
    pipeline = next((c for c in cards if c["name"] == "Pipeline"), None)
    return pipeline is not None and pipeline["ok"] is False


def _by_severity(cards: list) -> list:
    # sorted() is stable — cards with equal severity keep their original relative
    # order (System, Pipeline, Camera, GPS, Upload, Tailscale), so this only ever
    # moves problems to the top, never reshuffles ties.
    return sorted(cards, key=_severity)


def build_status_cards(health: dict | None, tailscale: dict | None, age_sec=None) -> list:
    # Tailscale is fetched over its own independent SSH command, so its card
    # must not be gated on health.json (a separate command) being present.
    if health is None:
        return _by_severity(_no_data_cards(tailscale))

    tailscale_section = _section(health, "tailscale")
    key_expiry_days = tailscale_section.get("key_expiry_days")
    tailscale_warnings = tailscale_section.get("health")

    # Before any card reads a field: past the staleness line every one of them would
    # otherwise render the last thing a dead agent wrote as the truck's current state.
    if is_stale(age_sec):
        return _by_severity(_stale_cards(tailscale, age_sec, key_expiry_days, tailscale_warnings))

    return _by_severity([
        _system_card(_section(health, "system"), _section(health, "self_heal"), _section(health, "service")),
        _pipeline_card(_section(health, "pipeline")),
        _camera_card(_section(health, "camera")),
        _gps_card(_section(health, "gps")),
        _upload_card(_section(health, "upload")),
        _tailscale_card(tailscale, key_expiry_days, tailscale_warnings),
    ])
