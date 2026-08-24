import pytest

import geocode


class FakeResponse:
    def __init__(self, json_data, status_code=200):
        self._json_data = json_data
        self.status_code = status_code

    def raise_for_status(self):
        pass

    def json(self):
        return self._json_data


def test_should_regeocode_true_when_no_cache():
    assert geocode.should_regeocode(None, 3.4, 101.3, now_ts=1000.0) is True


def test_should_regeocode_false_when_close_and_within_interval():
    cached = {"lat": 3.4, "lon": 101.3, "ts": 1000.0, "town": "Bestari Jaya, Selangor"}
    assert geocode.should_regeocode(cached, 3.4001, 101.3001, now_ts=1100.0) is False


def test_should_regeocode_true_when_moved_beyond_threshold():
    cached = {"lat": 3.4, "lon": 101.3, "ts": 1000.0, "town": "Bestari Jaya, Selangor"}
    # ~0.6km north
    assert geocode.should_regeocode(cached, 3.4054, 101.3, now_ts=1010.0) is True


def test_should_regeocode_true_when_interval_elapsed():
    cached = {"lat": 3.4, "lon": 101.3, "ts": 1000.0, "town": "Bestari Jaya, Selangor"}
    assert geocode.should_regeocode(cached, 3.4, 101.3, now_ts=1000.0 + 300) is True


def test_reverse_geocode_requests_suburb_level_zoom(monkeypatch):
    # Regression: zoom=10 is too coarse and makes Nominatim substitute the enclosing
    # district into the `town` field itself (e.g. "Kuala Selangor" instead of "Bestari
    # Jaya") — confirmed against the live API. zoom=14 ("suburb" level per Nominatim's
    # own docs) is the level that returns the specific town/suburb name.
    captured = {}

    def fake_get(url, params, headers, timeout):
        captured["params"] = params
        return FakeResponse({"address": {"town": "Bestari Jaya", "state": "Selangor"}})

    monkeypatch.setattr(geocode.requests, "get", fake_get)
    geocode.reverse_geocode(3.4, 101.3)
    assert captured["params"]["zoom"] == 14


def test_reverse_geocode_success_combines_town_and_state(monkeypatch):
    monkeypatch.setattr(
        geocode.requests, "get",
        lambda *a, **k: FakeResponse({"address": {"town": "Bestari Jaya", "state": "Selangor"}}),
    )
    assert geocode.reverse_geocode(3.4, 101.3) == "Bestari Jaya, Selangor"


def test_reverse_geocode_falls_back_through_address_fields(monkeypatch):
    monkeypatch.setattr(
        geocode.requests, "get",
        lambda *a, **k: FakeResponse({"address": {"village": "Kampung Test", "state": "Selangor"}}),
    )
    assert geocode.reverse_geocode(3.4, 101.3) == "Kampung Test, Selangor"


def test_reverse_geocode_prefers_suburb_over_the_larger_enclosing_town(monkeypatch):
    # Real case: truck physically in Bestari Jaya (tagged `suburb` in OSM), but the
    # enclosing district's `town` field is "Kuala Selangor" — suburb is more specific
    # and should win.
    monkeypatch.setattr(
        geocode.requests, "get",
        lambda *a, **k: FakeResponse(
            {"address": {"suburb": "Bestari Jaya", "town": "Kuala Selangor", "state": "Selangor"}}
        ),
    )
    assert geocode.reverse_geocode(3.4, 101.3) == "Bestari Jaya, Selangor"


def test_reverse_geocode_prefers_neighbourhood_over_suburb(monkeypatch):
    monkeypatch.setattr(
        geocode.requests, "get",
        lambda *a, **k: FakeResponse(
            {"address": {"neighbourhood": "Taman Test", "suburb": "Bestari Jaya", "state": "Selangor"}}
        ),
    )
    assert geocode.reverse_geocode(3.4, 101.3) == "Taman Test, Selangor"


def test_reverse_geocode_raises_on_request_exception(monkeypatch):
    def boom(*a, **k):
        raise geocode.requests.RequestException("timed out")

    monkeypatch.setattr(geocode.requests, "get", boom)
    with pytest.raises(geocode.GeocodeError, match="timed out"):
        geocode.reverse_geocode(3.4, 101.3)


def test_reverse_geocode_raises_when_no_usable_address_field(monkeypatch):
    monkeypatch.setattr(
        geocode.requests, "get",
        lambda *a, **k: FakeResponse({"address": {"country": "Malaysia"}}),
    )
    with pytest.raises(geocode.GeocodeError, match="no town/city/village"):
        geocode.reverse_geocode(3.4, 101.3)


def test_non_json_body_raises_geocode_error(monkeypatch):
    # Nominatim serves an HTML page (not JSON) on rate-limit, and a captive portal will
    # do the same. resp.json() used to sit outside the try, so the raw requests exception
    # escaped un-wrapped and app.py's health handler mistook it for a health.json failure.
    class HtmlResponse(FakeResponse):
        def json(self):
            raise ValueError("Expecting value: line 1 column 1 (char 0)")

    monkeypatch.setattr(geocode.requests, "get", lambda *a, **k: HtmlResponse(None))

    with pytest.raises(geocode.GeocodeError):
        geocode.reverse_geocode(3.4, 101.3)


def test_non_dict_address_raises_geocode_error(monkeypatch):
    monkeypatch.setattr(geocode.requests, "get", lambda *a, **k: FakeResponse(["not", "a", "dict"]))

    with pytest.raises(geocode.GeocodeError):
        geocode.reverse_geocode(3.4, 101.3)


def test_should_regeocode_backs_off_after_a_failure_even_with_no_cache():
    # Regression: a failed lookup leaves location_cache unwritten, so the `cached is None`
    # branch retried on every single refresh — one Nominatim request per 30s auto-poll
    # tick, indefinitely, which is what gets an IP blocked under their usage policy.
    assert geocode.should_regeocode(None, 3.4, 101.3, now_ts=1000.0, failed_at=900.0) is False


def test_should_regeocode_retries_once_the_backoff_window_has_passed():
    now = 900.0 + geocode.FAILURE_BACKOFF_SEC
    assert geocode.should_regeocode(None, 3.4, 101.3, now_ts=now, failed_at=900.0) is True


def test_a_stale_failure_does_not_block_a_move_triggered_regeocode():
    cached = {"lat": 3.4, "lon": 101.3, "ts": 1000.0, "town": "Bestari Jaya, Selangor"}
    now = 1000.0 + geocode.FAILURE_BACKOFF_SEC
    assert geocode.should_regeocode(cached, 3.4054, 101.3, now_ts=now, failed_at=0.0) is True
