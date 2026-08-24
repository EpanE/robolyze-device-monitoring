import datetime
import logging

import pytest

import app as app_module
import config
import geocode
import ssh_client

FAKE_PNG_BYTES = b"\x89PNG\r\n\x1a\nfake-image-bytes"

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
    "gps": {"ok": True, "reason": None, "lat": 3.404744, "lon": 101.371961,
            "last_valid_age_sec": 3.2, "last_sentence_age_sec": 1.0, "speed_kmh": 15.0},
    "tailscale": {"ok": True, "key_expiry_days": None},
}
HEALTHY_TAILSCALE = {"BackendState": "Running"}


class FakeTransport:
    def __init__(self, active):
        self._active = active

    def is_active(self):
        return self._active


class FakeClient:
    """Test double for a paramiko client. `alive` drives ssh_client.is_alive, which is
    what decides whether a refresh reuses this connection or opens a new one."""

    def __init__(self, alive=True):
        self._transport = FakeTransport(alive)
        self.closed = False

    def get_transport(self):
        return self._transport

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def reset_state(monkeypatch, tmp_path):
    app_module.state = app_module.new_state()
    # Redirected per test, not just in the tests that assert on it: a refresh records a
    # trend row, so without this every test writing to the repo's own logs/history.jsonl
    # would both litter the working tree and leak rows into the next test's assertions.
    monkeypatch.setattr(app_module.history, "HISTORY_PATH", tmp_path / "history.jsonl")
    # Refreshes now cache a live SSH session between calls, so a leftover one from the
    # previous test would make the next one skip its connect entirely.
    app_module.drop_ssh_session()
    monkeypatch.setattr(ssh_client, "connect", lambda *a, **k: FakeClient())
    monkeypatch.setattr(ssh_client, "grab_screenshot", lambda c: FAKE_PNG_BYTES)
    # Stubbed here, not per-test: any test that exercises a GPS fix without remembering to
    # patch this reaches the real Nominatim, and a suite run becomes a burst of requests
    # against the usage policy this app is otherwise careful about. Tests that care about
    # geocoding override it.
    monkeypatch.setattr(geocode, "reverse_geocode", lambda lat, lon, **k: "Testville, Selangor")
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
    monkeypatch.setattr(ssh_client, "fetch_health_snapshot", lambda c: (HEALTHY_HEALTH, 0.0))
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

    monkeypatch.setattr(ssh_client, "fetch_health_snapshot", boom)
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
    monkeypatch.setattr(ssh_client, "fetch_health_snapshot", lambda c: (HEALTHY_HEALTH, 0.0))
    monkeypatch.setattr(ssh_client, "fetch_tailscale_status", lambda c: HEALTHY_TAILSCALE)
    client.post("/api/refresh")

    resp = client.get("/report")
    assert resp.status_code == 200
    assert "Robolyze Daily Ops Update".encode() in resp.data
    assert "Confidence threshold set to 0.75".encode() in resp.data


def test_run_dir_fallback_shows_warning_banner(client, monkeypatch):
    fallback_health = {**HEALTHY_HEALTH, "run_dir_fallback": True}
    monkeypatch.setattr(ssh_client, "fetch_health_snapshot", lambda c: (fallback_health, 0.0))
    monkeypatch.setattr(ssh_client, "fetch_tailscale_status", lambda c: HEALTHY_TAILSCALE)
    client.post("/api/refresh")

    resp = client.get("/")
    assert b"fallback run-dir" in resp.data


def test_refresh_with_live_fix_populates_last_known_gps(client, monkeypatch):
    monkeypatch.setattr(ssh_client, "fetch_health_snapshot", lambda c: (HEALTHY_HEALTH, 0.0))
    monkeypatch.setattr(ssh_client, "fetch_tailscale_status", lambda c: HEALTHY_TAILSCALE)

    client.post("/api/refresh")

    assert app_module.state["last_known_gps"]["lat"] == 3.404744
    assert app_module.state["last_known_gps"]["lon"] == 101.371961
    assert app_module.state["last_known_gps"]["ts"] is not None


def test_refresh_without_live_fix_keeps_previous_last_known_gps(client, monkeypatch):
    monkeypatch.setattr(ssh_client, "fetch_health_snapshot", lambda c: (HEALTHY_HEALTH, 0.0))
    monkeypatch.setattr(ssh_client, "fetch_tailscale_status", lambda c: HEALTHY_TAILSCALE)
    client.post("/api/refresh")
    previous = app_module.state["last_known_gps"]

    stopped_health = {**HEALTHY_HEALTH, "gps": {"ok": None, "reason": "no GPS reader running"}}
    monkeypatch.setattr(ssh_client, "fetch_health_snapshot", lambda c: (stopped_health, 0.0))
    client.post("/api/refresh")

    assert app_module.state["last_known_gps"] == previous


def test_refresh_with_live_fix_populates_location_cache_and_shows_town(client, monkeypatch):
    monkeypatch.setattr(ssh_client, "fetch_health_snapshot", lambda c: (HEALTHY_HEALTH, 0.0))
    monkeypatch.setattr(ssh_client, "fetch_tailscale_status", lambda c: HEALTHY_TAILSCALE)
    monkeypatch.setattr(geocode, "reverse_geocode", lambda lat, lon, timeout=5: "Bestari Jaya, Selangor")

    client.post("/api/refresh")

    assert app_module.state["location_cache"]["town"] == "Bestari Jaya, Selangor"
    resp = client.get("/")
    assert "Bestari Jaya, Selangor".encode() in resp.data


def test_second_refresh_at_same_spot_does_not_regeocode(client, monkeypatch):
    monkeypatch.setattr(ssh_client, "fetch_health_snapshot", lambda c: (HEALTHY_HEALTH, 0.0))
    monkeypatch.setattr(ssh_client, "fetch_tailscale_status", lambda c: HEALTHY_TAILSCALE)
    call_count = {"n": 0}

    def fake_geocode(lat, lon, timeout=5):
        call_count["n"] += 1
        return "Bestari Jaya, Selangor"

    monkeypatch.setattr(geocode, "reverse_geocode", fake_geocode)

    client.post("/api/refresh")
    client.post("/api/refresh")

    assert call_count["n"] == 1


def test_refresh_captures_screenshot_into_state(client, monkeypatch):
    monkeypatch.setattr(ssh_client, "fetch_health_snapshot", lambda c: (HEALTHY_HEALTH, 0.0))
    monkeypatch.setattr(ssh_client, "fetch_tailscale_status", lambda c: HEALTHY_TAILSCALE)

    client.post("/api/refresh")

    assert app_module.state["screenshot"]["bytes"] == FAKE_PNG_BYTES
    assert app_module.state["screenshot_error"] is None


def test_index_links_the_screenshot_instead_of_inlining_it(client, monkeypatch):
    # Base64-inlining re-encoded the whole PNG (+33%) into every / response, including
    # each 30s auto-poll reload. A versioned URL lets the browser cache it instead.
    monkeypatch.setattr(ssh_client, "fetch_health_snapshot", lambda c: (HEALTHY_HEALTH, 0.0))
    monkeypatch.setattr(ssh_client, "fetch_tailscale_status", lambda c: HEALTHY_TAILSCALE)
    client.post("/api/refresh")

    body = client.get("/").get_data(as_text=True)

    assert "data:image/png;base64," not in body
    assert f"/api/screenshot?v={app_module.state['screenshot']['version']}" in body


def test_refresh_screenshot_failure_keeps_the_last_good_capture(client, monkeypatch):
    # Regression: a failed grab set state["screenshot"] = None, so the <img> rendered with
    # no src — a broken-image icon — exactly when the Pi's compositor was down and the last
    # known screen was most worth seeing.
    monkeypatch.setattr(ssh_client, "fetch_health_snapshot", lambda c: (HEALTHY_HEALTH, 0.0))
    monkeypatch.setattr(ssh_client, "fetch_tailscale_status", lambda c: HEALTHY_TAILSCALE)
    client.post("/api/refresh")
    good = app_module.state["screenshot"]
    assert good["bytes"] == FAKE_PNG_BYTES

    def boom(c):
        raise ssh_client.SSHCommandError("failed to create display")

    monkeypatch.setattr(ssh_client, "grab_screenshot", boom)
    # Age the stored capture past the refresh throttle, so this refresh actually attempts
    # a grab rather than skipping it.
    app_module.state["screenshot"] = {
        **app_module.state["screenshot"],
        "ts": datetime.datetime.now() - datetime.timedelta(
            seconds=app_module.SCREENSHOT_MIN_INTERVAL_SEC + 1
        ),
    }

    resp = client.post("/api/refresh")
    assert resp.status_code == 302
    # health/tailscale still succeed even though the screenshot failed
    assert app_module.state["health"] == HEALTHY_HEALTH
    # Bytes, not the whole record: the ts was deliberately aged above to defeat the
    # throttle, and it is the image that must survive a failed grab.
    assert app_module.state["screenshot"]["bytes"] == good["bytes"]
    assert "failed to create display" in app_module.state["screenshot_error"]

    body = client.get("/").get_data(as_text=True)
    assert "failed to create display" in body
    # ...and the retained image is labelled with its age rather than passing as current.
    assert "Captured:" in body


def test_grabbing_a_screenshot_stores_it_so_it_survives_a_reload(client, monkeypatch):
    # The grab route used to return bytes without touching state, so the next page load
    # silently reverted to the older screenshot from the last refresh.
    png_bytes = b"a-fresher-png"
    monkeypatch.setattr(ssh_client, "grab_screenshot", lambda c: png_bytes)

    resp = client.post("/api/screenshot")

    assert resp.status_code == 200
    assert resp.mimetype == "image/png"
    assert resp.data == png_bytes
    assert app_module.state["screenshot"]["bytes"] == png_bytes
    assert client.get("/api/screenshot").data == png_bytes


def test_grab_route_returns_error_on_failure(client, monkeypatch):
    def boom(c):
        raise ssh_client.SSHCommandError("grim: failed to create display")

    monkeypatch.setattr(ssh_client, "grab_screenshot", boom)

    resp = client.post("/api/screenshot")

    assert resp.status_code == 502
    assert b"failed to create display" in resp.data


def test_screenshot_route_serves_the_cached_image_without_touching_ssh(client, monkeypatch):
    def never(c):
        raise AssertionError("GET /api/screenshot must not open an SSH connection")

    monkeypatch.setattr(ssh_client, "grab_screenshot", never)
    app_module._store_screenshot(b"cached-png")

    resp = client.get("/api/screenshot")

    assert resp.status_code == 200
    assert resp.data == b"cached-png"


def test_screenshot_route_404s_when_nothing_has_been_captured(client):
    assert client.get("/api/screenshot").status_code == 404


def test_screenshot_version_changes_so_the_browser_refetches(client, monkeypatch):
    monkeypatch.setattr(ssh_client, "grab_screenshot", lambda c: b"one")
    client.post("/api/screenshot")
    first = app_module.state["screenshot"]["version"]

    monkeypatch.setattr(ssh_client, "grab_screenshot", lambda c: b"two")
    client.post("/api/screenshot")

    assert app_module.state["screenshot"]["version"] != first


def test_refresh_survives_raw_transport_error_without_500(client, monkeypatch):
    # Regression: the truck dropped off Tailscale mid-refresh and paramiko raised
    # ConnectionResetError, which is not SSHCommandError — it escaped the
    # per-section handlers and the whole /api/refresh returned HTTP 500.
    monkeypatch.setattr(ssh_client, "fetch_health_snapshot", lambda c: (HEALTHY_HEALTH, 0.0))
    monkeypatch.setattr(ssh_client, "fetch_tailscale_status", lambda c: HEALTHY_TAILSCALE)

    def reset(_client):
        raise ConnectionResetError(10054, "An existing connection was forcibly closed by the remote host")

    monkeypatch.setattr(ssh_client, "grab_screenshot", reset)

    resp = client.post("/api/refresh")

    assert resp.status_code == 302
    assert app_module.state["screenshot"] is None
    assert "forcibly closed" in app_module.state["screenshot_error"]
    assert app_module.state["health"] == HEALTHY_HEALTH


def test_concurrent_refresh_is_dropped_rather_than_queued(client, monkeypatch):
    # Each refresh holds a worker thread for three SSH round trips, so a
    # double-click used to stack whole SSH sessions and back up the waitress queue.
    monkeypatch.setattr(ssh_client, "fetch_health_snapshot", lambda c: (HEALTHY_HEALTH, 0.0))
    monkeypatch.setattr(ssh_client, "fetch_tailscale_status", lambda c: HEALTHY_TAILSCALE)

    calls = []
    monkeypatch.setattr(ssh_client, "connect", lambda *a, **k: calls.append(1) or FakeClient())

    app_module._ssh_lock.acquire()
    try:
        resp = client.post("/api/refresh")
    finally:
        app_module._ssh_lock.release()

    assert resp.status_code == 302
    assert calls == []  # no second SSH session opened while one is in flight


def test_geocode_failure_does_not_discard_a_good_health_snapshot(client, monkeypatch):
    # Regression: the health.json read and the GPS/geocode derivation shared one try block,
    # so a Nominatim failure nulled out a health snapshot that had already been fetched
    # successfully — and reported it in the banner as a health.json error.
    monkeypatch.setattr(ssh_client, "fetch_health_snapshot", lambda c: (HEALTHY_HEALTH, 0.0))
    monkeypatch.setattr(ssh_client, "fetch_tailscale_status", lambda c: HEALTHY_TAILSCALE)

    def boom(lat, lon, timeout=5):
        raise ValueError("Expecting value: line 1 column 1 (char 0)")

    monkeypatch.setattr(geocode, "reverse_geocode", boom)

    client.post("/api/refresh")

    assert app_module.state["health"] == HEALTHY_HEALTH
    assert app_module.state["health_error"] is None
    assert "Expecting value" in app_module.state["location_error"]


def test_location_error_is_shown_to_the_user(client, monkeypatch):
    # Set but never rendered: geocoding failures were silently swallowed and the town
    # label just quietly went stale.
    monkeypatch.setattr(ssh_client, "fetch_health_snapshot", lambda c: (HEALTHY_HEALTH, 0.0))
    monkeypatch.setattr(ssh_client, "fetch_tailscale_status", lambda c: HEALTHY_TAILSCALE)

    def boom(lat, lon, timeout=5):
        raise geocode.GeocodeError("nominatim unreachable")

    monkeypatch.setattr(geocode, "reverse_geocode", boom)
    client.post("/api/refresh")

    body = client.get("/").get_data(as_text=True)

    assert "nominatim unreachable" in body


def test_refresh_does_not_redirect_off_site(client, monkeypatch):
    # request.referrer is the raw caller-supplied Referer header. With no auth on any
    # route, a link from anywhere on the LAN could trigger the SSH connection and bounce
    # the operator to an attacker's page.
    monkeypatch.setattr(ssh_client, "fetch_health_snapshot", lambda c: (HEALTHY_HEALTH, 0.0))
    monkeypatch.setattr(ssh_client, "fetch_tailscale_status", lambda c: HEALTHY_TAILSCALE)

    resp = client.post("/api/refresh", headers={"Referer": "https://evil.example.com/x"})

    assert resp.status_code == 302
    assert "evil.example.com" not in resp.headers["Location"]


def test_refresh_still_returns_to_the_page_it_came_from(client, monkeypatch):
    monkeypatch.setattr(ssh_client, "fetch_health_snapshot", lambda c: (HEALTHY_HEALTH, 0.0))
    monkeypatch.setattr(ssh_client, "fetch_tailscale_status", lambda c: HEALTHY_TAILSCALE)

    resp = client.post("/api/refresh", headers={"Referer": "http://localhost/report"})

    assert resp.headers["Location"].endswith("/report")


def test_screenshot_version_survives_a_restart_without_reusing_a_cached_url(client, monkeypatch):
    # Regression: the version was a counter that new_state() reset to 0 on every process
    # start, so the first capture after a restart landed back on /api/screenshot?v=1 — a
    # URL the browser still had hard-cached (max-age=3600) from the previous run, showing
    # an hour-old screen.
    monkeypatch.setattr(ssh_client, "grab_screenshot", lambda c: b"before-restart")
    client.post("/api/screenshot")
    before = app_module.state["screenshot"]["version"]

    app_module.state = app_module.new_state()  # a fresh process

    monkeypatch.setattr(ssh_client, "grab_screenshot", lambda c: b"after-restart")
    client.post("/api/screenshot")

    assert app_module.state["screenshot"]["version"] != before


def test_recapturing_identical_bytes_keeps_the_same_url_so_the_cache_hit_is_correct(
    client, monkeypatch
):
    monkeypatch.setattr(ssh_client, "grab_screenshot", lambda c: b"same-bytes")
    client.post("/api/screenshot")
    first = app_module.state["screenshot"]["version"]

    client.post("/api/screenshot")

    assert app_module.state["screenshot"]["version"] == first


def test_unparseable_gps_coordinates_do_not_surface_as_a_geocode_failure(client, monkeypatch):
    # status.py already guards these fields with as_float; app.py did not, so raw NMEA text
    # reached math.radians() inside should_regeocode and TypeError'd there — reported to the
    # operator as a bogus "reverse geocode failed" banner.
    nmea_health = {**HEALTHY_HEALTH, "gps": {**HEALTHY_HEALTH["gps"], "lat": "N0324.28", "lon": "E10122.31"}}
    monkeypatch.setattr(ssh_client, "fetch_health_snapshot", lambda c: (nmea_health, 0.0))
    monkeypatch.setattr(ssh_client, "fetch_tailscale_status", lambda c: HEALTHY_TAILSCALE)

    def never(lat, lon, timeout=5):
        raise AssertionError("must not geocode a coordinate that isn't a number")

    monkeypatch.setattr(geocode, "reverse_geocode", never)

    client.post("/api/refresh")

    assert app_module.state["location_error"] is None
    assert app_module.state["last_known_gps"] is None
    assert app_module.state["health"] == nmea_health


def test_string_gps_coordinates_are_stored_as_numbers(client, monkeypatch):
    string_health = {**HEALTHY_HEALTH, "gps": {**HEALTHY_HEALTH["gps"], "lat": "3.404744", "lon": "101.371961"}}
    monkeypatch.setattr(ssh_client, "fetch_health_snapshot", lambda c: (string_health, 0.0))
    monkeypatch.setattr(ssh_client, "fetch_tailscale_status", lambda c: HEALTHY_TAILSCALE)
    monkeypatch.setattr(geocode, "reverse_geocode", lambda lat, lon, timeout=5: "Bestari Jaya, Selangor")

    client.post("/api/refresh")

    assert app_module.state["last_known_gps"]["lat"] == 3.404744
    assert app_module.state["last_known_gps"]["lon"] == 101.371961


def test_a_geocode_failure_backs_off_instead_of_retrying_every_refresh(client, monkeypatch):
    # Regression: a failure left location_cache unwritten, so should_regeocode's
    # `cached is None` branch fired again on the very next refresh — one Nominatim request
    # per 30s auto-poll tick for as long as the outage lasted.
    monkeypatch.setattr(ssh_client, "fetch_health_snapshot", lambda c: (HEALTHY_HEALTH, 0.0))
    monkeypatch.setattr(ssh_client, "fetch_tailscale_status", lambda c: HEALTHY_TAILSCALE)
    calls = {"n": 0}

    def boom(lat, lon, timeout=5):
        calls["n"] += 1
        raise geocode.GeocodeError("nominatim unreachable")

    monkeypatch.setattr(geocode, "reverse_geocode", boom)

    client.post("/api/refresh")
    client.post("/api/refresh")
    client.post("/api/refresh")

    assert calls["n"] == 1
    assert "nominatim unreachable" in app_module.state["location_error"]


def test_a_successful_geocode_clears_the_backoff_clock(client, monkeypatch):
    monkeypatch.setattr(ssh_client, "fetch_health_snapshot", lambda c: (HEALTHY_HEALTH, 0.0))
    monkeypatch.setattr(ssh_client, "fetch_tailscale_status", lambda c: HEALTHY_TAILSCALE)
    monkeypatch.setattr(geocode, "reverse_geocode", lambda lat, lon, timeout=5: "Bestari Jaya, Selangor")

    client.post("/api/refresh")

    assert app_module.state["location_error_ts"] is None


def test_polling_refresh_returns_204_instead_of_a_page_it_would_discard(client, monkeypatch):
    # fetch() follows a 302 by default, so the poll loop downloaded the whole page body
    # only to throw it away and have location.reload() fetch the same page again.
    monkeypatch.setattr(ssh_client, "fetch_health_snapshot", lambda c: (HEALTHY_HEALTH, 0.0))
    monkeypatch.setattr(ssh_client, "fetch_tailscale_status", lambda c: HEALTHY_TAILSCALE)

    resp = client.post("/api/refresh", headers={"X-Requested-With": "fetch"})

    assert resp.status_code == 204
    assert resp.data == b""
    assert app_module.state["health"] == HEALTHY_HEALTH


def test_a_plain_form_post_still_gets_the_post_redirect_get(client, monkeypatch):
    monkeypatch.setattr(ssh_client, "fetch_health_snapshot", lambda c: (HEALTHY_HEALTH, 0.0))
    monkeypatch.setattr(ssh_client, "fetch_tailscale_status", lambda c: HEALTHY_TAILSCALE)

    assert client.post("/api/refresh").status_code == 302


def test_a_dropped_poll_is_reported_as_409_rather_than_looking_like_success(client):
    # Regression: the lock-held path returned a bare 302, indistinguishable from a real
    # refresh, so the poll loop reloaded and showed stale state under a fresh-looking page.
    app_module._ssh_lock.acquire()
    try:
        resp = client.post("/api/refresh", headers={"X-Requested-With": "fetch"})
    finally:
        app_module._ssh_lock.release()

    assert resp.status_code == 409
    assert b"in flight" in resp.data


def test_a_dropped_form_refresh_tells_the_operator_it_was_skipped(client):
    app_module._ssh_lock.acquire()
    try:
        resp = client.post("/api/refresh", headers={"Referer": "http://localhost/report"})
    finally:
        app_module._ssh_lock.release()

    assert resp.status_code == 302
    assert "refresh=skipped" in resp.headers["Location"]

    body = client.get("/report?refresh=skipped").get_data(as_text=True)
    assert "Refresh skipped" in body


def test_a_swallowed_health_failure_keeps_its_traceback_in_the_log(client, monkeypatch, caplog):
    # state["health_error"] holds only str(exc) for the banner, and the next refresh
    # overwrites it. The log is the only place the traceback survives.
    def boom(c):
        raise ssh_client.SSHCommandError("health.json read failed: No such file or directory")

    monkeypatch.setattr(ssh_client, "fetch_health_snapshot", boom)
    monkeypatch.setattr(ssh_client, "fetch_tailscale_status", lambda c: HEALTHY_TAILSCALE)

    with caplog.at_level(logging.DEBUG, logger="dashboard"):
        client.post("/api/refresh")

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("health.json read failed" in r.getMessage() for r in errors)
    assert any(r.exc_info for r in errors)


def test_the_ssh_password_never_reaches_the_log(client, monkeypatch, caplog):
    # The connect traceback quotes the source line `ssh_client.connect(..., config.SSH_PASSWORD)`
    # — the attribute name. This pins that it stays that way.
    monkeypatch.setattr(config, "SSH_PASSWORD", "s3cr3t-must-not-be-logged")

    def refuse(*a, **k):
        raise OSError("[Errno 111] Connection refused")

    monkeypatch.setattr(ssh_client, "connect", refuse)

    with caplog.at_level(logging.DEBUG, logger="dashboard"):
        client.post("/api/refresh")

    assert "Connection refused" in caplog.text
    assert "s3cr3t" not in caplog.text


def test_every_banner_error_has_a_matching_log_entry(client, monkeypatch, caplog):
    monkeypatch.setattr(ssh_client, "fetch_health_snapshot", lambda c: (HEALTHY_HEALTH, 0.0))

    def tailscale_boom(c):
        raise ssh_client.SSHCommandError("tailscale status failed: command not found")

    def screenshot_boom(c):
        raise ssh_client.SSHCommandError("failed to create display")

    def geocode_boom(lat, lon, timeout=5):
        raise geocode.GeocodeError("nominatim unreachable")

    monkeypatch.setattr(ssh_client, "fetch_tailscale_status", tailscale_boom)
    monkeypatch.setattr(ssh_client, "grab_screenshot", screenshot_boom)
    monkeypatch.setattr(geocode, "reverse_geocode", geocode_boom)

    with caplog.at_level(logging.DEBUG, logger="dashboard"):
        client.post("/api/refresh")

    logged = caplog.text
    assert "nominatim unreachable" in logged
    assert "command not found" in logged
    assert "failed to create display" in logged


def test_a_clean_refresh_logs_nothing_at_error_level(client, monkeypatch, caplog):
    # A log that cries wolf on every healthy refresh is one nobody reads.
    monkeypatch.setattr(ssh_client, "fetch_health_snapshot", lambda c: (HEALTHY_HEALTH, 0.0))
    monkeypatch.setattr(ssh_client, "fetch_tailscale_status", lambda c: HEALTHY_TAILSCALE)
    monkeypatch.setattr(geocode, "reverse_geocode", lambda lat, lon, timeout=5: "Bestari Jaya, Selangor")

    with caplog.at_level(logging.DEBUG, logger="dashboard"):
        client.post("/api/refresh")

    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []


def test_a_failed_manual_screenshot_is_logged_too(client, monkeypatch, caplog):
    def boom(c):
        raise ssh_client.SSHCommandError("grim: failed to create display")

    monkeypatch.setattr(ssh_client, "grab_screenshot", boom)

    with caplog.at_level(logging.DEBUG, logger="dashboard"):
        resp = client.post("/api/screenshot")

    assert resp.status_code == 502
    assert "failed to create display" in caplog.text


# --- snapshot staleness -------------------------------------------------------------


def test_refresh_records_snapshot_age(client, monkeypatch):
    monkeypatch.setattr(ssh_client, "fetch_health_snapshot", lambda c: (HEALTHY_HEALTH, 42.0))
    monkeypatch.setattr(ssh_client, "fetch_tailscale_status", lambda c: HEALTHY_TAILSCALE)

    client.post("/api/refresh")

    assert app_module.state["health_age_sec"] == 42.0


def test_stale_snapshot_renders_a_banner_and_grey_cards(client, monkeypatch):
    monkeypatch.setattr(ssh_client, "fetch_health_snapshot", lambda c: (HEALTHY_HEALTH, 900.0))
    monkeypatch.setattr(ssh_client, "fetch_tailscale_status", lambda c: HEALTHY_TAILSCALE)

    client.post("/api/refresh")
    resp = client.get("/")

    assert b"health-agent" in resp.data
    assert "⚪".encode() in resp.data
    # The healthy snapshot's own values must not be what the operator reads.
    assert b"44.6" not in resp.data


def test_fresh_snapshot_renders_no_stale_banner(client, monkeypatch):
    monkeypatch.setattr(ssh_client, "fetch_health_snapshot", lambda c: (HEALTHY_HEALTH, 12.0))
    monkeypatch.setattr(ssh_client, "fetch_tailscale_status", lambda c: HEALTHY_TAILSCALE)

    client.post("/api/refresh")
    resp = client.get("/")

    assert b"health-agent" not in resp.data
    assert b"44.6" in resp.data


def test_failed_health_read_clears_the_stale_age(client, monkeypatch):
    # Otherwise the age from the last good refresh outlives the snapshot it described and
    # keeps colouring a page that no longer has any snapshot at all.
    monkeypatch.setattr(ssh_client, "fetch_health_snapshot", lambda c: (HEALTHY_HEALTH, 42.0))
    monkeypatch.setattr(ssh_client, "fetch_tailscale_status", lambda c: HEALTHY_TAILSCALE)
    client.post("/api/refresh")

    def boom(c):
        raise ssh_client.SSHCommandError("No such file or directory")

    monkeypatch.setattr(ssh_client, "fetch_health_snapshot", boom)
    client.post("/api/refresh")

    assert app_module.state["health_age_sec"] is None


def test_report_refuses_to_generate_from_a_stale_snapshot(client, monkeypatch):
    # A WhatsApp report reading "✅ System running" off a frozen file is worse than no
    # report: it is signed-off, forwarded, and believed.
    monkeypatch.setattr(ssh_client, "fetch_health_snapshot", lambda c: (HEALTHY_HEALTH, 900.0))
    monkeypatch.setattr(ssh_client, "fetch_tailscale_status", lambda c: HEALTHY_TAILSCALE)
    client.post("/api/refresh")

    resp = client.get("/report")

    assert b"System running" not in resp.data


# --- last-known GPS timestamps ------------------------------------------------------
# The stamp on this record is what the map prints as "as of ...". It must be when the fix
# was taken, not when we happened to read a file still quoting it.


def test_last_known_gps_is_stamped_from_the_fix_age_not_the_read_time():
    health = {"gps": {"ok": True, "lat": 3.4, "lon": 101.3, "last_valid_age_sec": 600.0}}
    now = datetime.datetime(2026, 8, 24, 12, 0, 0)

    app_module._update_location(health, now=now)

    assert app_module.state["last_known_gps"]["ts"] == "2026-08-24 11:50:00"


def test_an_old_fix_republished_every_refresh_is_not_restamped_as_current():
    # The Pi keeps emitting the same coordinates with ok=false long after the fix went
    # stale. Restamping on each read made an hours-old position read "as of a moment ago".
    now = datetime.datetime(2026, 8, 24, 12, 0, 0)
    # The coordinates never change and the age keeps climbing, exactly as the Pi reports
    # it: both reads describe the same 10:00 fix, so both must resolve to 10:00.
    first = {"gps": {"ok": False, "lat": 3.4, "lon": 101.3, "last_valid_age_sec": 7200.0}}
    later = {"gps": {"ok": False, "lat": 3.4, "lon": 101.3, "last_valid_age_sec": 7500.0}}

    app_module._update_location(first, now=now)
    app_module._update_location(later, now=now + datetime.timedelta(minutes=5))

    assert app_module.state["last_known_gps"]["ts"] == "2026-08-24 10:00:00"


def test_a_fix_with_no_age_and_no_ok_flag_does_not_overwrite_a_known_stamp():
    known = {"gps": {"ok": True, "lat": 3.4, "lon": 101.3, "last_valid_age_sec": 0.0}}
    unknown = {"gps": {"ok": None, "lat": 3.4, "lon": 101.3, "last_valid_age_sec": None}}
    now = datetime.datetime(2026, 8, 24, 12, 0, 0)

    app_module._update_location(known, now=now)
    app_module._update_location(unknown, now=now + datetime.timedelta(hours=1))

    assert app_module.state["last_known_gps"]["ts"] == "2026-08-24 12:00:00"


def test_a_fix_with_no_age_still_records_coordinates_when_nothing_is_known_yet():
    # Better a position with an honest "time unknown" than no marker at all.
    health = {"gps": {"ok": None, "lat": 3.4, "lon": 101.3, "last_valid_age_sec": None}}

    app_module._update_location(health, now=datetime.datetime(2026, 8, 24, 12, 0, 0))

    assert app_module.state["last_known_gps"]["lat"] == 3.4
    assert app_module.state["last_known_gps"]["ts"] is None


# --- screenshot publishing ----------------------------------------------------------
# The bytes and the version that names them are cached hard (max-age=3600) under a URL
# built from that version, so they must never be able to come apart.


def test_stored_screenshot_is_one_record_so_bytes_and_version_cannot_diverge():
    app_module._store_screenshot(b"first")
    record = app_module.state["screenshot"]

    app_module._store_screenshot(b"second")

    # The record handed out earlier is the one it always was: publishing swaps the whole
    # record in a single assignment rather than mutating three fields in sequence, so a
    # concurrent reader can only ever see one capture or the other, never a mix.
    assert record["bytes"] == b"first"
    assert app_module.state["screenshot"]["bytes"] == b"second"
    assert record["version"] != app_module.state["screenshot"]["version"]


def test_screenshot_requested_under_its_own_version_is_cacheable(client, monkeypatch):
    monkeypatch.setattr(ssh_client, "grab_screenshot", lambda c: b"png-one")
    client.post("/api/screenshot")
    version = app_module.state["screenshot"]["version"]

    resp = client.get(f"/api/screenshot?v={version}")

    assert resp.data == b"png-one"
    assert "max-age=3600" in resp.headers["Cache-Control"]


def test_screenshot_requested_under_a_stale_version_is_never_cached(client, monkeypatch):
    # The page was rendered with v=A; by the time the browser asked, a refresh had already
    # published B. Serving B is right — caching B under A's URL for an hour is not.
    monkeypatch.setattr(ssh_client, "grab_screenshot", lambda c: b"png-one")
    client.post("/api/screenshot")
    stale_version = app_module.state["screenshot"]["version"]
    monkeypatch.setattr(ssh_client, "grab_screenshot", lambda c: b"png-two")
    client.post("/api/screenshot")

    resp = client.get(f"/api/screenshot?v={stale_version}")

    assert resp.data == b"png-two"
    assert "no-store" in resp.headers["Cache-Control"]


def test_screenshot_requested_without_a_version_is_never_cached(client, monkeypatch):
    monkeypatch.setattr(ssh_client, "grab_screenshot", lambda c: b"png-one")
    client.post("/api/screenshot")

    resp = client.get("/api/screenshot")

    assert resp.data == b"png-one"
    assert "no-store" in resp.headers["Cache-Control"]


def test_manual_grab_reports_the_version_it_stored(client, monkeypatch):
    # Without this the page had nothing to name the new bytes with and fell back to
    # ?v=<timestamp> — a URL that never matches the stored version, so every manually
    # grabbed screenshot was uncacheable and refetched on each reload.
    monkeypatch.setattr(ssh_client, "grab_screenshot", lambda c: b"png-one")

    resp = client.post("/api/screenshot")

    assert resp.headers["X-Screenshot-Version"] == app_module.state["screenshot"]["version"]


# --- startup wiring -----------------------------------------------------------------


def test_create_app_returns_the_configured_flask_app(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "validate", lambda: None)
    monkeypatch.setattr(app_module.logging_setup, "setup_logging",
                        lambda **kw: tmp_path / "dashboard.log")

    assert app_module.create_app() is app_module.app


def test_create_app_validates_config(monkeypatch, tmp_path):
    # The whole point: served under `waitress-serve app:create_app`, an empty .env used to
    # sail past every check and only surface as a paramiko error on the first refresh.
    monkeypatch.setattr(app_module.logging_setup, "setup_logging",
                        lambda **kw: tmp_path / "dashboard.log")

    def missing():
        raise config.ConfigError("Missing from .env: SSH_PASSWORD")

    monkeypatch.setattr(config, "validate", missing)

    with pytest.raises(config.ConfigError, match="SSH_PASSWORD"):
        app_module.create_app()


def test_create_app_configures_logging_before_validating(monkeypatch, tmp_path):
    # Order matters: a bad .env must land in the log file, not only flash past in a
    # terminal nobody opened (run.bat is double-clicked).
    events = []
    monkeypatch.setattr(app_module.logging_setup, "setup_logging",
                        lambda **kw: events.append("logging") or (tmp_path / "d.log"))
    monkeypatch.setattr(config, "validate", lambda: events.append("validate"))

    app_module.create_app()

    assert events == ["logging", "validate"]


# --- access control -----------------------------------------------------------------
# Every POST route here opens an SSH session to the truck using the credentials in .env,
# and until now anything that could reach the port could trigger one.


def test_routes_are_open_when_no_password_is_configured(client, monkeypatch):
    # The field workflow is double-click run.bat on a trusted LAN; requiring a password
    # nobody set would just lock the operator out of their own dashboard.
    monkeypatch.setattr(config, "DASHBOARD_PASSWORD", "")
    assert client.get("/").status_code == 200


def test_configured_password_makes_routes_require_auth(client, monkeypatch):
    monkeypatch.setattr(config, "DASHBOARD_PASSWORD", "s3cret")

    resp = client.get("/")

    assert resp.status_code == 401
    assert "Basic" in resp.headers["WWW-Authenticate"]


def test_correct_password_is_accepted(client, monkeypatch):
    monkeypatch.setattr(config, "DASHBOARD_PASSWORD", "s3cret")

    resp = client.get("/", auth=("ops", "s3cret"))

    assert resp.status_code == 200


def test_wrong_password_is_rejected(client, monkeypatch):
    monkeypatch.setattr(config, "DASHBOARD_PASSWORD", "s3cret")

    resp = client.get("/", auth=("ops", "guess"))

    assert resp.status_code == 401


def test_auth_also_guards_the_ssh_routes(client, monkeypatch):
    monkeypatch.setattr(config, "DASHBOARD_PASSWORD", "s3cret")

    assert client.post("/api/refresh").status_code == 401
    assert client.post("/api/screenshot").status_code == 401


def test_cross_origin_post_is_rejected(client, monkeypatch):
    # A page on any other site the operator has open can submit a form here. It cannot
    # read the response, but it can make the dashboard open an SSH session to the truck.
    monkeypatch.setattr(ssh_client, "fetch_health_snapshot", lambda c: (HEALTHY_HEALTH, 0.0))
    monkeypatch.setattr(ssh_client, "fetch_tailscale_status", lambda c: HEALTHY_TAILSCALE)

    resp = client.post("/api/refresh", headers={"Origin": "http://evil.example"})

    assert resp.status_code == 403
    assert app_module.state["health"] is None


def test_same_origin_post_is_allowed(client, monkeypatch):
    monkeypatch.setattr(ssh_client, "fetch_health_snapshot", lambda c: (HEALTHY_HEALTH, 0.0))
    monkeypatch.setattr(ssh_client, "fetch_tailscale_status", lambda c: HEALTHY_TAILSCALE)

    resp = client.post("/api/refresh", headers={"Origin": "http://localhost"})

    assert resp.status_code == 302
    assert app_module.state["health"] == HEALTHY_HEALTH


def test_post_without_an_origin_header_is_allowed(client, monkeypatch):
    # curl and the launcher's own smoke checks send no Origin. Browsers have sent it on
    # every cross-origin POST for years, so its absence is not the attack this blocks.
    monkeypatch.setattr(ssh_client, "fetch_health_snapshot", lambda c: (HEALTHY_HEALTH, 0.0))
    monkeypatch.setattr(ssh_client, "fetch_tailscale_status", lambda c: HEALTHY_TAILSCALE)

    assert client.post("/api/refresh").status_code == 302


def test_cross_origin_get_is_still_allowed(client, monkeypatch):
    # GETs change nothing here, and blocking them would break an <img> embed for no gain.
    monkeypatch.setattr(config, "DASHBOARD_PASSWORD", "")
    assert client.get("/", headers={"Origin": "http://evil.example"}).status_code == 200


def test_create_app_warns_when_bound_wide_open_without_a_password(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(app_module.logging_setup, "setup_logging",
                        lambda **kw: tmp_path / "d.log")
    monkeypatch.setattr(config, "validate", lambda: None)
    monkeypatch.setattr(config, "HOST", "0.0.0.0")
    monkeypatch.setattr(config, "DASHBOARD_PASSWORD", "")

    with caplog.at_level(logging.WARNING):
        app_module.create_app()

    assert "DASHBOARD_PASSWORD" in caplog.text


def test_create_app_is_quiet_when_bound_to_loopback(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(app_module.logging_setup, "setup_logging",
                        lambda **kw: tmp_path / "d.log")
    monkeypatch.setattr(config, "validate", lambda: None)
    monkeypatch.setattr(config, "HOST", "127.0.0.1")
    monkeypatch.setattr(config, "DASHBOARD_PASSWORD", "")

    with caplog.at_level(logging.WARNING):
        app_module.create_app()

    assert "DASHBOARD_PASSWORD" not in caplog.text


# --- redirect targets ---------------------------------------------------------------


@pytest.mark.parametrize("referrer", [
    "http://evil.example/landing",     # absolute, another host
    "//evil.example/landing",          # protocol-relative
    r"/\evil.example/landing",        # browsers normalise this to protocol-relative too
    "javascript:alert(1)",             # no netloc at all, so the old netloc check passed it
])
def test_refresh_never_redirects_off_site(client, monkeypatch, referrer):
    monkeypatch.setattr(ssh_client, "fetch_health_snapshot", lambda c: (HEALTHY_HEALTH, 0.0))
    monkeypatch.setattr(ssh_client, "fetch_tailscale_status", lambda c: HEALTHY_TAILSCALE)

    resp = client.post("/api/refresh", headers={"Referer": referrer})

    assert "evil.example" not in resp.headers["Location"]
    assert not resp.headers["Location"].startswith("javascript:")


def test_refresh_returns_to_the_page_it_came_from(client, monkeypatch):
    monkeypatch.setattr(ssh_client, "fetch_health_snapshot", lambda c: (HEALTHY_HEALTH, 0.0))
    monkeypatch.setattr(ssh_client, "fetch_tailscale_status", lambda c: HEALTHY_TAILSCALE)

    resp = client.post("/api/refresh", headers={"Referer": "http://localhost/report"})

    assert resp.headers["Location"].endswith("/report")


# --- SSH connection reuse -----------------------------------------------------------
# Every 30s auto-poll tick used to pay a fresh TCP handshake plus paramiko password auth,
# which is the expensive part of a refresh — the commands themselves are tiny.


def test_refresh_reuses_a_live_connection(client, monkeypatch):
    connects = []

    def counting_connect(*a, **k):
        connects.append(1)
        return FakeClient()

    monkeypatch.setattr(ssh_client, "connect", counting_connect)
    monkeypatch.setattr(ssh_client, "fetch_health_snapshot", lambda c: (HEALTHY_HEALTH, 0.0))
    monkeypatch.setattr(ssh_client, "fetch_tailscale_status", lambda c: HEALTHY_TAILSCALE)

    client.post("/api/refresh")
    client.post("/api/refresh")
    client.post("/api/refresh")

    assert len(connects) == 1


def test_refresh_reconnects_when_the_link_died(client, monkeypatch):
    connects = []

    def counting_connect(*a, **k):
        connects.append(1)
        return FakeClient(alive=len(connects) > 1)

    monkeypatch.setattr(ssh_client, "connect", counting_connect)
    monkeypatch.setattr(ssh_client, "fetch_health_snapshot", lambda c: (HEALTHY_HEALTH, 0.0))
    monkeypatch.setattr(ssh_client, "fetch_tailscale_status", lambda c: HEALTHY_TAILSCALE)

    client.post("/api/refresh")   # first connect, transport goes inactive afterwards
    client.post("/api/refresh")   # must not try to reuse a dead transport

    assert len(connects) == 2


def test_a_failed_command_drops_the_connection_so_the_next_refresh_reconnects(client, monkeypatch):
    # A command failing on a link that looks alive is the ambiguous case. Reconnecting
    # costs one handshake; reusing a wedged session costs every refresh after it.
    connects = []

    def counting_connect(*a, **k):
        connects.append(1)
        return FakeClient()

    def boom(c):
        raise ssh_client.SSHCommandError("SSH connection lost: socket closed")

    monkeypatch.setattr(ssh_client, "connect", counting_connect)
    monkeypatch.setattr(ssh_client, "fetch_health_snapshot", boom)
    monkeypatch.setattr(ssh_client, "fetch_tailscale_status", lambda c: HEALTHY_TAILSCALE)

    client.post("/api/refresh")
    client.post("/api/refresh")

    assert len(connects) == 2


def test_a_connect_failure_does_not_cache_a_broken_client(client, monkeypatch):
    def refuse(*a, **k):
        raise OSError("No route to host")

    monkeypatch.setattr(ssh_client, "connect", refuse)

    client.post("/api/refresh")
    resp = client.get("/")

    assert b"No route to host" in resp.data
    assert app_module._ssh_session is None


# --- screenshot throttling ----------------------------------------------------------


def test_refresh_skips_the_screenshot_while_a_recent_one_exists(client, monkeypatch):
    # A full-res PNG on every 30s tick is the bulk of what this app pulls over a cellular
    # link, for a screen nobody is necessarily watching.
    grabs = []
    monkeypatch.setattr(ssh_client, "grab_screenshot", lambda c: grabs.append(1) or b"png")
    monkeypatch.setattr(ssh_client, "fetch_health_snapshot", lambda c: (HEALTHY_HEALTH, 0.0))
    monkeypatch.setattr(ssh_client, "fetch_tailscale_status", lambda c: HEALTHY_TAILSCALE)

    client.post("/api/refresh")
    client.post("/api/refresh")

    assert len(grabs) == 1


def test_refresh_recaptures_once_the_screenshot_has_aged_out(client, monkeypatch):
    grabs = []
    monkeypatch.setattr(ssh_client, "grab_screenshot", lambda c: grabs.append(1) or b"png")
    monkeypatch.setattr(ssh_client, "fetch_health_snapshot", lambda c: (HEALTHY_HEALTH, 0.0))
    monkeypatch.setattr(ssh_client, "fetch_tailscale_status", lambda c: HEALTHY_TAILSCALE)

    client.post("/api/refresh")
    aged = datetime.datetime.now() - datetime.timedelta(
        seconds=app_module.SCREENSHOT_MIN_INTERVAL_SEC + 1
    )
    app_module.state["screenshot"] = {**app_module.state["screenshot"], "ts": aged}
    client.post("/api/refresh")

    assert len(grabs) == 2


def test_the_manual_grab_button_is_never_throttled(client, monkeypatch):
    grabs = []
    monkeypatch.setattr(ssh_client, "grab_screenshot", lambda c: grabs.append(1) or b"png")

    client.post("/api/screenshot")
    client.post("/api/screenshot")

    assert len(grabs) == 2


# --- incremental poll ---------------------------------------------------------------
# The auto-poll used to finish with location.reload(): a full page fetch every 30s that
# tore down and rebuilt the Leaflet map, losing pan, zoom and scroll position.


def test_status_route_returns_rendered_fragments(client, monkeypatch):
    monkeypatch.setattr(ssh_client, "fetch_health_snapshot", lambda c: (HEALTHY_HEALTH, 0.0))
    monkeypatch.setattr(ssh_client, "fetch_tailscale_status", lambda c: HEALTHY_TAILSCALE)
    client.post("/api/refresh")

    payload = client.get("/api/status").get_json()

    # Rendered server-side from the same partials the full page uses, rather than
    # rebuilt in JavaScript — one source of truth for what a card looks like.
    assert "Pipeline" in payload["cards_html"]
    assert "44.6" in payload["cards_html"]
    assert payload["last_refreshed"] is not None


def test_status_route_reports_banners(client, monkeypatch):
    monkeypatch.setattr(ssh_client, "fetch_health_snapshot", lambda c: (HEALTHY_HEALTH, 900.0))
    monkeypatch.setattr(ssh_client, "fetch_tailscale_status", lambda c: HEALTHY_TAILSCALE)
    client.post("/api/refresh")

    payload = client.get("/api/status").get_json()

    assert "health-agent" in payload["banners_html"]


def test_status_route_carries_the_marker(client, monkeypatch):
    monkeypatch.setattr(ssh_client, "fetch_health_snapshot", lambda c: (HEALTHY_HEALTH, 0.0))
    monkeypatch.setattr(ssh_client, "fetch_tailscale_status", lambda c: HEALTHY_TAILSCALE)
    client.post("/api/refresh")

    payload = client.get("/api/status").get_json()

    assert payload["marker"]["lat"] == 3.404744
    assert payload["marker"]["freshness"] == "fresh"


def test_status_route_carries_the_screenshot_version(client, monkeypatch):
    monkeypatch.setattr(ssh_client, "grab_screenshot", lambda c: b"png-one")
    client.post("/api/screenshot")

    payload = client.get("/api/status").get_json()

    assert payload["screenshot"]["version"] == app_module.state["screenshot"]["version"]


def test_status_route_is_empty_before_any_refresh(client):
    payload = client.get("/api/status").get_json()

    assert payload["marker"] is None
    assert payload["screenshot"] is None
    assert payload["last_refreshed"] is None
    assert "No data" in payload["cards_html"]


def test_status_route_needs_no_ssh(client, monkeypatch):
    def never(*a, **k):
        raise AssertionError("GET /api/status must not open an SSH connection")

    monkeypatch.setattr(ssh_client, "connect", never)
    assert client.get("/api/status").status_code == 200


def test_the_poll_loop_no_longer_reloads_the_page(client):
    body = client.get("/").get_data(as_text=True)
    assert "location.reload()" not in body


# --- trend history ------------------------------------------------------------------


@pytest.fixture
def history_path():
    """The isolated history file reset_state already pointed the app at."""
    return app_module.history.HISTORY_PATH


def test_refresh_records_a_history_row(client, monkeypatch, history_path):
    monkeypatch.setattr(ssh_client, "fetch_health_snapshot", lambda c: (HEALTHY_HEALTH, 5.0))
    monkeypatch.setattr(ssh_client, "fetch_tailscale_status", lambda c: HEALTHY_TAILSCALE)

    client.post("/api/refresh")

    assert len(app_module.history.load(path=history_path)) == 1


def test_a_failed_refresh_records_nothing(client, monkeypatch, history_path):
    def boom(c):
        raise ssh_client.SSHCommandError("No such file or directory")

    monkeypatch.setattr(ssh_client, "fetch_health_snapshot", boom)
    monkeypatch.setattr(ssh_client, "fetch_tailscale_status", lambda c: HEALTHY_TAILSCALE)

    client.post("/api/refresh")

    assert app_module.history.load(path=history_path) == []


def test_a_history_write_failure_does_not_break_the_refresh(client, monkeypatch, history_path):
    # A trend is a nice-to-have; the live status is not.
    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(ssh_client, "fetch_health_snapshot", lambda c: (HEALTHY_HEALTH, 5.0))
    monkeypatch.setattr(ssh_client, "fetch_tailscale_status", lambda c: HEALTHY_TAILSCALE)
    monkeypatch.setattr(app_module.history, "record", boom)

    resp = client.post("/api/refresh")

    assert resp.status_code == 302
    assert app_module.state["health"] == HEALTHY_HEALTH


def test_the_page_draws_a_sparkline_once_there_is_history(client, monkeypatch, history_path):
    monkeypatch.setattr(ssh_client, "fetch_health_snapshot", lambda c: (HEALTHY_HEALTH, 5.0))
    monkeypatch.setattr(ssh_client, "fetch_tailscale_status", lambda c: HEALTHY_TAILSCALE)
    client.post("/api/refresh")
    client.post("/api/refresh")

    body = client.get("/").get_data(as_text=True)

    assert "<polyline" in body
    assert "Pipeline fps" in body


def test_the_page_says_so_when_there_is_no_history_yet(client, history_path):
    body = client.get("/").get_data(as_text=True)

    assert "<polyline" not in body
    assert "Trends appear" in body


def test_status_route_carries_the_trends(client, monkeypatch, history_path):
    # Otherwise the sparklines are the one part of the page that stays frozen at whatever
    # the last full page load drew, while everything around them updates.
    monkeypatch.setattr(ssh_client, "fetch_health_snapshot", lambda c: (HEALTHY_HEALTH, 5.0))
    monkeypatch.setattr(ssh_client, "fetch_tailscale_status", lambda c: HEALTHY_TAILSCALE)
    client.post("/api/refresh")
    client.post("/api/refresh")

    payload = client.get("/api/status").get_json()

    assert "<polyline" in payload["trends_html"]
