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
    assert "🔴 Camera feed clarity not checked" in report


def test_camera_checked_but_not_clear_flags_only_clarity_line():
    health = {**HEALTHY, "camera": {"clarity": {"checked": True, "clear": False}}}
    report = build_report(health, DEFAULT_CONFIDENCE, datetime.date(2026, 7, 28))
    assert "✅ Duromac feed inspected" in report
    assert "🔴 Camera feed is NOT clear — check lens" in report


def test_camera_missing_clarity_block_treated_as_not_inspected():
    health = {**HEALTHY, "camera": {}}
    report = build_report(health, DEFAULT_CONFIDENCE, datetime.date(2026, 7, 28))
    assert "🔴 Duromac feed inspected" in report
    assert "🔴 Camera feed clarity not checked" in report


def test_camera_clarity_emoji_and_text_never_contradict():
    # The bug: emoji correctly flipped red on unclear/unchecked, but the sentence
    # was a hardcoded literal that always said "is clear" regardless of state.
    cases = [
        (True, True, "✅ Camera feed is clear"),
        (True, False, "🔴 Camera feed is NOT clear — check lens"),
        (False, True, "🔴 Camera feed clarity not checked"),
        (False, False, "🔴 Camera feed clarity not checked"),
    ]
    for checked, clear, expected in cases:
        health = {**HEALTHY, "camera": {"clarity": {"checked": checked, "clear": clear}}}
        report = build_report(health, DEFAULT_CONFIDENCE, datetime.date(2026, 7, 28))
        assert expected in report, f"checked={checked} clear={clear}: expected {expected!r}"
        # No other emoji may prefix this specific sentence text.
        other_emoji = "🔴" if expected.startswith("✅") else "✅"
        assert f"{other_emoji} {expected[2:]}" not in report


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


def test_gating_logic_is_always_a_green_tick():
    # No sensor drives this line — it can only change if the code itself changes, so it's
    # a hardcoded checkmark rather than a manual "confirm yourself" placeholder.
    report = build_report(HEALTHY, DEFAULT_CONFIDENCE, datetime.date(2026, 7, 28))
    assert "✅ Gating logic active" in report
    assert "__ Gating logic active" not in report


def test_manual_fields_and_whatsapp_formatting_preserved_verbatim():
    report = build_report(HEALTHY, DEFAULT_CONFIDENCE, datetime.date(2026, 7, 28))
    assert "*📍 Location : _{Location}_*" in report
    assert "*🚛 Driver : _{Driver}_*" in report
    assert "⏸️ Sync script run - stoped" in report
    assert "⏸️ recording/ - pause when needed or prompt" in report
    assert "⏸️ Recording - pause when needed or prompt" in report
    assert "🛣️ *Robolyze Daily Ops Update*" in report
    assert "▶️ = running" in report


def test_town_prefills_location_line_when_provided():
    report = build_report(HEALTHY, DEFAULT_CONFIDENCE, datetime.date(2026, 7, 28), town="Bestari Jaya, Selangor")
    assert "*📍 Location : _Bestari Jaya, Selangor_*" in report
    assert "{Location}" not in report


def test_town_defaults_to_manual_placeholder_when_not_provided():
    report = build_report(HEALTHY, DEFAULT_CONFIDENCE, datetime.date(2026, 7, 28), town=None)
    assert "*📍 Location : _{Location}_*" in report


def test_driver_name_prefills_the_driver_line_when_provided():
    report = build_report(
        HEALTHY, DEFAULT_CONFIDENCE, datetime.date(2026, 7, 28), driver_name="Alice"
    )
    assert "*🚛 Driver : _Alice_*" in report
    assert "{Driver}" not in report


def test_driver_name_defaults_to_manual_placeholder_when_not_provided():
    report = build_report(HEALTHY, DEFAULT_CONFIDENCE, datetime.date(2026, 7, 28), driver_name=None)
    assert "*🚛 Driver : _{Driver}_*" in report


def test_driver_name_defaults_to_manual_placeholder_when_empty():
    report = build_report(HEALTHY, DEFAULT_CONFIDENCE, datetime.date(2026, 7, 28), driver_name="")
    assert "*🚛 Driver : _{Driver}_*" in report


def test_null_confidence_falls_back_to_the_configured_default():
    # .get(key, default) only fires when the key is ABSENT, but the schema uses explicit
    # null (see `error: null`), so the report rendered "Confidence threshold set to None".
    health = {"pipeline": {"ok": True, "confirmed_min_confidence": None},
              "upload": {}, "system": {}, "camera": {}}

    report_text = build_report(health, "0.75", datetime.date(2026, 8, 21))

    assert "set to 0.75" in report_text
    assert "None" not in report_text
