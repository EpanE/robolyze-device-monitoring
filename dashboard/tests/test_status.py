import pytest

from status import (
    STALE_AFTER_SEC,
    build_location_marker,
    build_status_cards,
    is_stale,
    pipeline_down_while_connected,
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
    },
    "upload": {"ok": True, "queue_depth": 0, "dropped_total": 0},
    "camera": {
        "ok": True, "tcp_ok": True, "tcp_connect_ms": 0.8,
        "clarity": {"checked": True, "clear": True, "blur_score": 17.7,
                     "blur_spread": [16.46, 18.87], "age_sec": 240.0},
        "drift_scan": None,
    },
    "gps": {
        "ok": True, "device_present": True, "port": "/dev/gps", "nmea_status": "status=A",
        "lat": 3.404744, "lon": 101.371961,
        "last_valid_age_sec": 3.2, "last_sentence_age_sec": 1.0, "speed_kmh": 15.0,
        "reopen_total": 1, "writer_age_sec": 4.9,
    },
    "tailscale": {
        "ok": True, "checked": True, "online": True, "backend_state": "Running",
        "hostname": "raspberrypi", "key_expiry_days": None,
        "tailscale_ips": ["100.110.189.87"], "version": "1.102.2-t6cac91817-g6ff0ddc72",
        "health": [],
    },
    "self_heal": {"enabled": True, "self_heals": 0, "tailscale_bad_cycles": 0, "actions": []},
    "service": {
        "hailo-detection.service": {"ok": True, "active_state": "active", "restarts": 1},
        "health-agent.service": {"ok": True, "active_state": "active", "restarts": 0},
    },
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


def test_camera_clarity_badge_red_when_not_clear_even_though_card_stays_green():
    health = {**HEALTHY_HEALTH, "camera": {
        "ok": True, "tcp_ok": True, "tcp_connect_ms": 1.2,
        "clarity": {"checked": True, "clear": False, "blur_score": 9.1,
                     "blur_spread": [5.0, 12.0], "age_sec": 90.0},
        "drift_scan": None,
    }}
    cards = build_status_cards(health, HEALTHY_TAILSCALE)
    card = _card(cards, "Camera")
    assert card["emoji"] == "✅"
    assert card["badge"]["emoji"] == "🔴"
    assert card["badge"]["css_class"] == "status-bad"
    assert "not clear" in card["badge"]["text"]


def test_camera_clarity_badge_green_when_clear():
    cards = build_status_cards(HEALTHY_HEALTH, HEALTHY_TAILSCALE)
    card = _card(cards, "Camera")
    assert card["badge"]["emoji"] == "✅"
    assert card["badge"]["css_class"] == "status-ok"
    assert "clear" in card["badge"]["text"]


def test_camera_clarity_badge_absent_when_not_checked():
    health = {**HEALTHY_HEALTH, "camera": {
        "ok": True, "tcp_ok": True, "tcp_connect_ms": 0.8,
        "clarity": {"checked": False}, "drift_scan": None,
    }}
    cards = build_status_cards(health, HEALTHY_TAILSCALE)
    assert _card(cards, "Camera")["badge"] is None


def test_camera_clarity_badge_absent_when_clarity_block_missing():
    health = {**HEALTHY_HEALTH, "camera": {"ok": True, "tcp_ok": True, "tcp_connect_ms": 0.8}}
    cards = build_status_cards(health, HEALTHY_TAILSCALE)
    assert _card(cards, "Camera")["badge"] is None


def test_clarity_badge_distinguishes_blank():
    health = {**HEALTHY_HEALTH, "camera": {**HEALTHY_HEALTH["camera"], "clarity": {
        "checked": True, "clear": False, "blank": True, "obstructed": False, "blurry": False,
        "blur_score": 1.0,
    }}}
    badge = _card(build_status_cards(health, HEALTHY_TAILSCALE), "Camera")["badge"]
    assert "BLANK" in badge["text"]


def test_clarity_badge_distinguishes_obstructed():
    health = {**HEALTHY_HEALTH, "camera": {**HEALTHY_HEALTH["camera"], "clarity": {
        "checked": True, "clear": False, "blank": False, "obstructed": True, "blurry": False,
        "blur_score": 1.0,
    }}}
    badge = _card(build_status_cards(health, HEALTHY_TAILSCALE), "Camera")["badge"]
    assert "OBSTRUCTED" in badge["text"]


def test_clarity_badge_distinguishes_blurry():
    health = {**HEALTHY_HEALTH, "camera": {**HEALTHY_HEALTH["camera"], "clarity": {
        "checked": True, "clear": False, "blank": False, "obstructed": False, "blurry": True,
        "blur_score": 1.0,
    }}}
    badge = _card(build_status_cards(health, HEALTHY_TAILSCALE), "Camera")["badge"]
    assert "BLURRY" in badge["text"]


def test_clarity_badge_priority_blank_beats_obstructed_and_blurry_when_all_true():
    health = {**HEALTHY_HEALTH, "camera": {**HEALTHY_HEALTH["camera"], "clarity": {
        "checked": True, "clear": False, "blank": True, "obstructed": True, "blurry": True,
        "blur_score": 1.0,
    }}}
    badge = _card(build_status_cards(health, HEALTHY_TAILSCALE), "Camera")["badge"]
    assert "BLANK" in badge["text"]


def test_cards_without_a_badge_source_have_no_badge():
    cards = build_status_cards(HEALTHY_HEALTH, HEALTHY_TAILSCALE)
    for name in ("Pipeline", "GPS", "Upload"):
        assert _card(cards, name)["badge"] is None


def test_system_card_shows_zero_action_self_heal_badge_when_healthy():
    cards = build_status_cards(HEALTHY_HEALTH, HEALTHY_TAILSCALE)
    badge = _card(cards, "System")["badge"]
    assert badge["css_class"] == "status-ok"
    assert "0 actions" in badge["text"]


def test_tailscale_card_has_no_badge_when_health_json_warnings_empty():
    cards = build_status_cards(HEALTHY_HEALTH, HEALTHY_TAILSCALE)
    assert _card(cards, "Tailscale")["badge"] is None


# --- system: load/mem/uptime/services -------------------------------------------------


def test_system_card_includes_load_avg_mem_and_uptime_when_present():
    health = {**HEALTHY_HEALTH, "system": {
        **HEALTHY_HEALTH["system"],
        "load_avg": [1.5, 2.5, 3.5], "mem_total_kb": 1000, "mem_available_kb": 250,
        "uptime_sec": 4000,
    }}
    detail = _card(build_status_cards(health, HEALTHY_TAILSCALE), "System")["detail"]
    assert "load_avg=1.50/2.50/3.50" in detail
    assert "mem=75% used" in detail
    assert "uptime=1h6m" in detail


def test_system_card_omits_load_mem_uptime_segments_when_absent():
    detail = _card(build_status_cards(HEALTHY_HEALTH, HEALTHY_TAILSCALE), "System")["detail"]
    assert "load_avg=" not in detail
    assert "mem=" not in detail
    assert "uptime=" not in detail


def test_system_card_service_summary_lists_restarts_per_unit():
    detail = _card(build_status_cards(HEALTHY_HEALTH, HEALTHY_TAILSCALE), "System")["detail"]
    assert "hailo-detection=active(restarts=1)" in detail
    assert "health-agent=active(restarts=0)" in detail
    assert "SERVICE ALERT" not in detail


def test_system_card_service_alert_appended_when_a_service_is_not_ok():
    health = {**HEALTHY_HEALTH, "service": {
        **HEALTHY_HEALTH["service"],
        "hailo-detection.service": {"ok": False, "active_state": "failed", "restarts": 4},
    }}
    card = _card(build_status_cards(health, HEALTHY_TAILSCALE), "System")
    assert "SERVICE ALERT" in card["detail"]
    assert card["emoji"] == "✅"  # never recolors System — system.ok is still true


def test_system_card_no_service_segment_when_service_section_absent():
    health = {k: v for k, v in HEALTHY_HEALTH.items() if k != "service"}
    detail = _card(build_status_cards(health, HEALTHY_TAILSCALE), "System")["detail"]
    assert "services:" not in detail


# --- self-heal badge -------------------------------------------------------------------


def test_self_heal_badge_warn_when_actions_have_fired_shows_last_action():
    health = {**HEALTHY_HEALTH, "self_heal": {
        "enabled": True, "self_heals": 2, "tailscale_bad_cycles": 0,
        "actions": [{"type": "restarted camera"}, {"type": "restarted tailscale"}],
    }}
    badge = _card(build_status_cards(health, HEALTHY_TAILSCALE), "System")["badge"]
    assert badge["css_class"] == "status-warn"
    assert "2 action(s)" in badge["text"]
    assert "restarted tailscale" in badge["text"]


def test_self_heal_badge_bad_when_disabled():
    health = {**HEALTHY_HEALTH, "self_heal": {"enabled": False, "self_heals": 0,
                                                "tailscale_bad_cycles": 0, "actions": []}}
    badge = _card(build_status_cards(health, HEALTHY_TAILSCALE), "System")["badge"]
    assert badge["css_class"] == "status-bad"
    assert "disabled" in badge["text"]


def test_self_heal_badge_absent_when_section_missing():
    health = {k: v for k, v in HEALTHY_HEALTH.items() if k != "self_heal"}
    badge = _card(build_status_cards(health, HEALTHY_TAILSCALE), "System")["badge"]
    assert badge is None


def test_self_heal_action_with_unexpected_shape_does_not_raise():
    health = {**HEALTHY_HEALTH, "self_heal": {
        "enabled": True, "self_heals": 1, "tailscale_bad_cycles": 0,
        "actions": ["restarted tailscale"],
    }}
    badge = _card(build_status_cards(health, HEALTHY_TAILSCALE), "System")["badge"]
    assert "restarted tailscale" in badge["text"]

    health2 = {**HEALTHY_HEALTH, "self_heal": {
        "enabled": True, "self_heals": 1, "tailscale_bad_cycles": 0,
        "actions": [{"foo": "bar"}],
    }}
    badge2 = _card(build_status_cards(health2, HEALTHY_TAILSCALE), "System")["badge"]
    assert badge2 is not None  # must not raise


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


def test_gps_ok_null_still_prefers_legacy_reason_field_when_present():
    health = {**HEALTHY_HEALTH, "gps": {"ok": None, "reason": "legacy reason string",
                                          "device_present": True, "nmea_status": "status=V"}}
    detail = _card(build_status_cards(health, HEALTHY_TAILSCALE), "GPS")["detail"]
    assert detail.startswith("legacy reason string")


def test_gps_ok_null_uses_device_present_and_nmea_status_when_reason_absent():
    health = {**HEALTHY_HEALTH, "gps": {"ok": None, "device_present": True, "nmea_status": "status=V"}}
    detail = _card(build_status_cards(health, HEALTHY_TAILSCALE), "GPS")["detail"]
    assert "device present" in detail
    assert "status=V" in detail


def test_gps_ok_null_falls_back_to_generic_when_device_present_and_nmea_both_absent():
    health = {**HEALTHY_HEALTH, "gps": {"ok": None}}
    detail = _card(build_status_cards(health, HEALTHY_TAILSCALE), "GPS")["detail"]
    assert "not determined" in detail


def test_gps_detail_includes_reopen_total_and_writer_age_when_present():
    detail = _card(build_status_cards(HEALTHY_HEALTH, HEALTHY_TAILSCALE), "GPS")["detail"]
    assert "reopens=1" in detail
    assert "writer_age=4.9s" in detail


def test_gps_detail_omits_reopen_total_and_writer_age_when_absent():
    health = {**HEALTHY_HEALTH, "gps": {
        "ok": True, "lat": 3.4, "lon": 101.3,
        "last_valid_age_sec": 1.0, "last_sentence_age_sec": 1.0, "speed_kmh": 1.0,
    }}
    detail = _card(build_status_cards(health, HEALTHY_TAILSCALE), "GPS")["detail"]
    assert "reopens=" not in detail
    assert "writer_age=" not in detail


def test_upload_not_ok_flags_upload_card():
    health = {**HEALTHY_HEALTH, "upload": {"ok": False, "error": "no pipeline heartbeat to read upload stats from"}}
    cards = build_status_cards(health, HEALTHY_TAILSCALE)
    card = _card(cards, "Upload")
    assert card["emoji"] == "🔴"
    assert "no pipeline heartbeat" in card["detail"]


def test_upload_card_shows_training_queue_when_present():
    health = {**HEALTHY_HEALTH, "upload": {
        "ok": True, "queue_depth": 0, "dropped_total": 0,
        "training_enabled": 1, "training_queue_depth": 3, "training_dropped_total": 2,
    }}
    detail = _card(build_status_cards(health, HEALTHY_TAILSCALE), "Upload")["detail"]
    assert "training: queue_depth=3, dropped_total=2" in detail


def test_upload_card_omits_training_segment_when_absent():
    detail = _card(build_status_cards(HEALTHY_HEALTH, HEALTHY_TAILSCALE), "Upload")["detail"]
    assert "training:" not in detail


def test_upload_card_flags_training_disabled():
    health = {**HEALTHY_HEALTH, "upload": {
        "ok": True, "queue_depth": 0, "dropped_total": 0,
        "training_enabled": 0, "training_queue_depth": 0, "training_dropped_total": 0,
    }}
    detail = _card(build_status_cards(health, HEALTHY_TAILSCALE), "Upload")["detail"]
    assert "(disabled)" in detail


def test_tailscale_card_shows_warnings_badge_when_health_json_has_warnings():
    health = {**HEALTHY_HEALTH, "tailscale": {
        **HEALTHY_HEALTH["tailscale"], "health": ["route warning"],
    }}
    badge = _card(build_status_cards(health, HEALTHY_TAILSCALE), "Tailscale")["badge"]
    assert badge["css_class"] == "status-warn"
    assert "route warning" in badge["text"]


def test_tailscale_ok_still_driven_by_live_backend_state_not_health_json_warnings():
    health = {**HEALTHY_HEALTH, "tailscale": {
        **HEALTHY_HEALTH["tailscale"], "health": ["some advisory"],
    }}
    card = _card(build_status_cards(health, HEALTHY_TAILSCALE), "Tailscale")
    assert card["emoji"] == "✅"  # HEALTHY_TAILSCALE (live fetch) says Running regardless


def test_stale_snapshot_still_shows_tailscale_warnings_badge_from_last_known_health():
    health = {**HEALTHY_HEALTH, "tailscale": {
        **HEALTHY_HEALTH["tailscale"], "health": ["route warning"],
    }}
    cards = build_status_cards(health, HEALTHY_TAILSCALE, age_sec=600.0)
    badge = _card(cards, "Tailscale")["badge"]
    assert badge is not None
    assert "route warning" in badge["text"]


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

    gps_card = _card(build_status_cards(health, None), "GPS")

    assert gps_card["name"] == "GPS"
    assert "3.40474" in gps_card["detail"]


def test_unparseable_lat_lon_falls_back_to_no_fix():
    health = {
        "system": {}, "pipeline": {}, "camera": {}, "upload": {},
        "gps": {"ok": True, "lat": "no-fix", "lon": None},
    }

    gps_card = _card(build_status_cards(health, None), "GPS")

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


# --- structured rows --------------------------------------------------------------------


def test_system_rows_include_temperature_disk_and_service_rows():
    rows = _card(build_status_cards(HEALTHY_HEALTH, HEALTHY_TAILSCALE), "System")["rows"]
    by_label = {r["label"]: r for r in rows}
    assert by_label["Temperature"]["value"] == "44.6°C"
    assert "63.3%" in by_label["Disk used"]["value"]
    assert by_label["Disk used"]["alert"] is False
    assert by_label["hailo-detection"]["value"] == "active (1 restarts)"


def test_system_disk_alert_row_carries_alert_flag():
    health = {**HEALTHY_HEALTH, "system": {**HEALTHY_HEALTH["system"], "disk_alert": True}}
    rows = _card(build_status_cards(health, HEALTHY_TAILSCALE), "System")["rows"]
    assert next(r for r in rows if r["label"] == "Disk used")["alert"] is True


def test_system_failed_service_row_carries_alert_flag():
    health = {**HEALTHY_HEALTH, "service": {
        **HEALTHY_HEALTH["service"],
        "hailo-detection.service": {"ok": False, "active_state": "failed", "restarts": 4},
    }}
    rows = _card(build_status_cards(health, HEALTHY_TAILSCALE), "System")["rows"]
    assert next(r for r in rows if r["label"] == "hailo-detection")["alert"] is True


def test_pipeline_rows_present_only_when_ok():
    ok_rows = _card(build_status_cards(HEALTHY_HEALTH, HEALTHY_TAILSCALE), "Pipeline")["rows"]
    assert {r["label"] for r in ok_rows} == {"FPS", "Heartbeat age"}

    health = {**HEALTHY_HEALTH, "pipeline": {"ok": False, "error": "heartbeat file missing"}}
    bad_rows = _card(build_status_cards(health, HEALTHY_TAILSCALE), "Pipeline")["rows"]
    assert bad_rows == []


def test_camera_clarity_not_clear_row_carries_alert_flag():
    health = {**HEALTHY_HEALTH, "camera": {
        "ok": True, "tcp_ok": True, "tcp_connect_ms": 1.2,
        "clarity": {"checked": True, "clear": False, "blur_score": 9.1,
                     "blur_spread": [5.0, 12.0], "age_sec": 90.0},
        "drift_scan": None,
    }}
    rows = _card(build_status_cards(health, HEALTHY_TAILSCALE), "Camera")["rows"]
    assert next(r for r in rows if r["label"] == "Clarity")["alert"] is True


def test_camera_clarity_row_says_not_checked_yet_when_unchecked():
    health = {**HEALTHY_HEALTH, "camera": {
        "ok": True, "tcp_ok": True, "tcp_connect_ms": 0.8,
        "clarity": {"checked": False}, "drift_scan": None,
    }}
    rows = _card(build_status_cards(health, HEALTHY_TAILSCALE), "Camera")["rows"]
    assert next(r for r in rows if r["label"] == "Clarity")["value"] == "not checked yet"


def test_gps_rows_empty_when_ok_is_none():
    health = {**HEALTHY_HEALTH, "gps": {"ok": None, "reason": "no GPS reader running"}}
    assert _card(build_status_cards(health, HEALTHY_TAILSCALE), "GPS")["rows"] == []


def test_gps_rows_present_when_ok_is_not_none():
    rows = _card(build_status_cards(HEALTHY_HEALTH, HEALTHY_TAILSCALE), "GPS")["rows"]
    labels = {r["label"] for r in rows}
    assert {"Location", "Fix age", "Sentence age", "Speed", "Reopens", "Writer age"} == labels


def test_upload_rows_empty_when_no_data():
    health = {**HEALTHY_HEALTH, "upload": {"ok": False, "error": "no pipeline heartbeat"}}
    assert _card(build_status_cards(health, HEALTHY_TAILSCALE), "Upload")["rows"] == []


def test_tailscale_rows_present_even_when_not_ok():
    cards = build_status_cards(HEALTHY_HEALTH, {"BackendState": "NeedsLogin"})
    rows = _card(cards, "Tailscale")["rows"]
    assert {r["label"] for r in rows} == {"Backend state", "Key expiry"}


def test_stale_and_no_data_cards_have_no_rows():
    stale_cards = build_status_cards(HEALTHY_HEALTH, HEALTHY_TAILSCALE, age_sec=600.0)
    for name in ("System", "Pipeline", "Camera", "GPS", "Upload"):
        assert _card(stale_cards, name)["rows"] == []

    no_data_cards = build_status_cards(None, None)
    assert all(card["rows"] == [] for card in no_data_cards)


# --- severity ordering -------------------------------------------------------------------


def test_cards_sort_not_ok_first_then_unknown_then_ok():
    health = {
        **HEALTHY_HEALTH,
        "camera": {"ok": False, "tcp_ok": False, "tcp_connect_ms": None, "clarity": {"checked": False}},
        "gps": {"ok": None, "reason": "no GPS reader running"},
    }
    cards = build_status_cards(health, HEALTHY_TAILSCALE)
    assert [c["name"] for c in cards] == ["Camera", "GPS", "System", "Pipeline", "Upload", "Tailscale"]


def test_cards_with_equal_severity_keep_original_relative_order():
    health = {
        **HEALTHY_HEALTH,
        "camera": {"ok": False, "tcp_ok": False, "tcp_connect_ms": None, "clarity": {"checked": False}},
        "upload": {"ok": False, "error": "no pipeline heartbeat"},
    }
    cards = build_status_cards(health, HEALTHY_TAILSCALE)
    not_ok_names = [c["name"] for c in cards if c["ok"] is False]
    assert not_ok_names == ["Camera", "Upload"]


# --- pipeline_down_while_connected --------------------------------------------------------


def test_pipeline_down_while_connected_true_when_pipeline_explicitly_not_ok():
    health = {**HEALTHY_HEALTH, "pipeline": {"ok": False, "error": "not running"}}
    cards = build_status_cards(health, HEALTHY_TAILSCALE)
    assert pipeline_down_while_connected(cards, stale=False) is True


def test_pipeline_down_while_connected_false_when_pipeline_healthy():
    cards = build_status_cards(HEALTHY_HEALTH, HEALTHY_TAILSCALE)
    assert pipeline_down_while_connected(cards, stale=False) is False


def test_pipeline_down_while_connected_false_when_pipeline_ok_is_unknown():
    # ok=None means "not determined yet" (missing section, older agent) — must stay
    # neutral, same tri-state rule every other card already follows.
    health = {**HEALTHY_HEALTH, "pipeline": {}}
    cards = build_status_cards(health, HEALTHY_TAILSCALE)
    assert pipeline_down_while_connected(cards, stale=False) is False


def test_pipeline_down_while_connected_false_when_stale():
    # A stale snapshot already gets its own loud banner — this one must not pile on.
    health = {**HEALTHY_HEALTH, "pipeline": {"ok": False, "error": "not running"}}
    cards = build_status_cards(health, HEALTHY_TAILSCALE, age_sec=600.0)
    assert pipeline_down_while_connected(cards, stale=True) is False
