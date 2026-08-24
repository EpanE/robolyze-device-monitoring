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
    clarity = _section(camera, "clarity")
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


def as_float(value):
    # The Pi is the authority on what the fix is, but a GPS layer emitting raw NMEA text
    # would otherwise blow up the f-string below with "Unknown format code 'f'".
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _gps_card(gps: dict) -> dict:
    if gps.get("ok") is None:
        detail = gps.get("reason") or "not determined"
    else:
        lat, lon = as_float(gps.get("lat")), as_float(gps.get("lon"))
        location = f"{lat:.5f},{lon:.5f}" if lat is not None and lon is not None else "no fix yet"
        detail = (
            f"location={location}, "
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


def _stale_cards(tailscale, age_sec, key_expiry_days):
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
        _tailscale_card(tailscale, key_expiry_days),
    ]


def build_status_cards(health: dict | None, tailscale: dict | None, age_sec=None) -> list:
    # Tailscale is fetched over its own independent SSH command, so its card
    # must not be gated on health.json (a separate command) being present.
    if health is None:
        return _no_data_cards(tailscale)

    key_expiry_days = _section(health, "tailscale").get("key_expiry_days")

    # Before any card reads a field: past the staleness line every one of them would
    # otherwise render the last thing a dead agent wrote as the truck's current state.
    if is_stale(age_sec):
        return _stale_cards(tailscale, age_sec, key_expiry_days)

    return [
        _system_card(_section(health, "system")),
        _pipeline_card(_section(health, "pipeline")),
        _camera_card(_section(health, "camera")),
        _gps_card(_section(health, "gps")),
        _upload_card(_section(health, "upload")),
        _tailscale_card(tailscale, key_expiry_days),
    ]
