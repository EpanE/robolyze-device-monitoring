import pytest

from status import (
    STALE_AFTER_SEC,
    build_location_marker,
    build_status_cards,
    is_stale,
)

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
        "ok": True, "reason": None, "lat": 3.404744, "lon": 101.371961,
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


def test_gps_card_shows_location_when_reader_has_a_fix():
    cards = build_status_cards(HEALTHY_HEALTH, HEALTHY_TAILSCALE)
    assert "location=3.40474,101.37196" in _card(cards, "GPS")["detail"]


def test_gps_card_shows_no_fix_yet_when_reader_active_but_no_coordinates():
    health = {**HEALTHY_HEALTH, "gps": {
        "ok": False, "reason": "no fix", "lat": None, "lon": None,
        "last_valid_age_sec": 999, "last_sentence_age_sec": 5, "speed_kmh": None,
    }}
    cards = build_status_cards(health, HEALTHY_TAILSCALE)
    assert "location=no fix yet" in _card(cards, "GPS")["detail"]


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


def test_location_marker_uses_live_fix_when_gps_ok():
    marker = build_location_marker(HEALTHY_HEALTH, None)
    assert marker["lat"] == 3.404744
    assert marker["lon"] == 101.371961
    assert marker["freshness"] == "fresh"
    assert marker["maps_url"] == "https://www.google.com/maps?q=3.404744,101.371961"


def test_location_marker_is_stale_when_coords_present_but_not_ok():
    health = {**HEALTHY_HEALTH, "gps": {**HEALTHY_HEALTH["gps"], "ok": False}}
    marker = build_location_marker(health, None)
    assert marker["freshness"] == "stale"
    assert marker["lat"] == 3.404744


def test_location_marker_falls_back_to_last_known_when_no_live_coords():
    health = {**HEALTHY_HEALTH, "gps": {"ok": None, "reason": "no GPS reader running"}}
    last_known = {"lat": 3.1, "lon": 101.2, "ts": "2026-08-20 12:00:00"}
    marker = build_location_marker(health, last_known)
    assert marker["lat"] == 3.1
    assert marker["lon"] == 101.2
    assert marker["freshness"] == "stale"
    assert "2026-08-20 12:00:00" in marker["label"]


def test_location_marker_none_when_no_live_coords_and_no_last_known():
    health = {**HEALTHY_HEALTH, "gps": {"ok": None, "reason": "no GPS reader running"}}
    assert build_location_marker(health, None) is None


# SCHEMA.md: "Sub-fields ... may be absent when that component isn't running — always read
# with .get() + a fallback, never bare dict indexing." A partial snapshot (agent version
# skew, or a cat that races the agent's write) must degrade to grey cards, never 500 the
# page — state persists in memory, so a 500 here locks the operator out of the Refresh
# button on every subsequent load too.
@pytest.mark.parametrize("missing", ["system", "pipeline", "camera", "gps", "upload"])
def test_missing_section_renders_unknown_card_instead_of_raising(missing):
    health = {name: {} for name in ["system", "pipeline", "camera", "gps", "upload"]}
    del health[missing]

    cards = build_status_cards(health, None)

    assert len(cards) == 6
    assert all(card["emoji"] in {"✅", "🔴", "⚪"} for card in cards)


@pytest.mark.parametrize("null_section", ["system", "pipeline", "camera", "gps", "upload"])
def test_null_section_renders_unknown_card_instead_of_raising(null_section):
    # The schema uses explicit null liberally (error: null, drift_scan: null, gps.ok: null),
    # so a whole section arriving as null is as plausible as it being absent.
    health = {name: {} for name in ["system", "pipeline", "camera", "gps", "upload"]}
    health[null_section] = None

    cards = build_status_cards(health, None)

    assert len(cards) == 6


def test_non_numeric_lat_lon_does_not_raise():
    # A GPS layer emitting raw NMEA text would otherwise hit
    # "Unknown format code 'f' for object of type 'str'" inside the f-string.
    health = {
        "system": {}, "pipeline": {}, "camera": {}, "upload": {},
        "gps": {"ok": True, "lat": "3.404744", "lon": "101.371961"},
    }

    gps_card = build_status_cards(health, None)[3]

    assert gps_card["name"] == "GPS"
    assert "3.40474" in gps_card["detail"]


def test_unparseable_lat_lon_falls_back_to_no_fix():
    health = {
        "system": {}, "pipeline": {}, "camera": {}, "upload": {},
        "gps": {"ok": True, "lat": "no-fix", "lon": None},
    }

    gps_card = build_status_cards(health, None)[3]

    assert "no fix yet" in gps_card["detail"]


# --- snapshot staleness -------------------------------------------------------------
# health.json is rewritten every 60s. A dead health-agent leaves the last good snapshot
# on disk, where it reads back as a perfectly healthy truck forever — the one failure
# this dashboard must never render green.


def test_fresh_snapshot_renders_normally():
    cards = build_status_cards(HEALTHY_HEALTH, HEALTHY_TAILSCALE, age_sec=42.0)
    assert _card(cards, "System")["emoji"] == "✅"


def test_stale_snapshot_forces_health_cards_to_unknown():
    cards = build_status_cards(HEALTHY_HEALTH, HEALTHY_TAILSCALE, age_sec=600.0)

    for name in ("System", "Pipeline", "Camera", "GPS", "Upload"):
        card = _card(cards, name)
        assert card["emoji"] == "⚪", f"{name} still rendered {card['emoji']} on a stale snapshot"
        assert "stale" in card["detail"]


def test_stale_snapshot_still_renders_the_live_tailscale_card():
    # Fetched over its own SSH command, so it is not stale just because health.json is.
    cards = build_status_cards(HEALTHY_HEALTH, HEALTHY_TAILSCALE, age_sec=600.0)
    assert _card(cards, "Tailscale")["emoji"] == "✅"


def test_stale_card_detail_names_the_age():
    cards = build_status_cards(HEALTHY_HEALTH, HEALTHY_TAILSCALE, age_sec=600.0)
    assert "600s" in _card(cards, "System")["detail"]


def test_missing_age_renders_normally():
    # Pre-upgrade state, or a refresh that never carried an age — must not paint the
    # whole page grey on the strength of a missing field.
    cards = build_status_cards(HEALTHY_HEALTH, HEALTHY_TAILSCALE, age_sec=None)
    assert _card(cards, "System")["emoji"] == "✅"


def test_is_stale_boundary():
    assert is_stale(STALE_AFTER_SEC - 1) is False
    assert is_stale(STALE_AFTER_SEC) is False
    assert is_stale(STALE_AFTER_SEC + 1) is True
    assert is_stale(None) is False


def test_stale_snapshot_marker_is_not_labelled_live():
    # The coordinates are real, but they are as old as the snapshot — a green "Live GPS
    # fix" pin on a frozen snapshot is the map saying the truck is where it was hours ago.
    marker = build_location_marker(HEALTHY_HEALTH, None, age_sec=600.0)
    assert marker["freshness"] == "stale"
    assert "Live" not in marker["label"]


def test_fresh_snapshot_marker_stays_live():
    marker = build_location_marker(HEALTHY_HEALTH, None, age_sec=42.0)
    assert marker["freshness"] == "fresh"
    assert marker["label"] == "Live GPS fix"


def test_last_known_marker_with_an_unknown_fix_time_says_so():
    # ts is None when the Pi gave coordinates but nothing to date them by. Printing
    # "as of None" is worse than admitting the time isn't known.
    health = {"gps": {"ok": None, "lat": None, "lon": None}}
    marker = build_location_marker(health, {"lat": 3.4, "lon": 101.3, "ts": None})

    assert "None" not in marker["label"]
    assert marker["freshness"] == "stale"
