import math

import requests

NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
USER_AGENT = "robolyze-duromac-dashboard/1.0 (internal fleet monitoring tool)"
MOVE_THRESHOLD_KM = 0.5
RECHECK_INTERVAL_SEC = 300
# A failed lookup leaves location_cache unwritten, which on its own makes every
# subsequent refresh retry — one Nominatim request per 30s auto-poll tick, forever.
# Repeated failing requests are exactly what their usage policy blocks IPs for, so a
# failure buys the same quiet window a success does.
FAILURE_BACKOFF_SEC = RECHECK_INTERVAL_SEC


class GeocodeError(Exception):
    pass


def reverse_geocode(lat: float, lon: float, timeout: int = 5) -> str:
    try:
        resp = requests.get(
            NOMINATIM_URL,
            params={"format": "jsonv2", "lat": lat, "lon": lon, "zoom": 14, "addressdetails": 1},
            headers={"User-Agent": USER_AGENT},
            timeout=timeout,
        )
        resp.raise_for_status()
        payload = resp.json()
    except requests.RequestException as exc:
        raise GeocodeError(str(exc)) from exc
    except ValueError as exc:
        # Nominatim serves an HTML page rather than JSON when rate-limiting, and a captive
        # portal does the same. Wrapping it here keeps every failure of this function a
        # GeocodeError, which is the only thing callers handle.
        raise GeocodeError(f"non-JSON response: {exc}") from exc

    address = payload.get("address") if isinstance(payload, dict) else None
    if not isinstance(address, dict):
        raise GeocodeError("geocode response had no address object")
    # Most specific first: a suburb/neighbourhood (e.g. Bestari Jaya) is often nested inside
    # a larger enclosing town/city (e.g. Kuala Selangor) in OSM's admin hierarchy — prefer
    # the specific locality when tagged, fall back to the broader settlement otherwise.
    town = (
        address.get("neighbourhood") or address.get("suburb")
        or address.get("village") or address.get("town") or address.get("city")
        or address.get("municipality")
    )
    if not town:
        raise GeocodeError("no town/city/village in geocode response")
    state = address.get("state")
    return f"{town}, {state}" if state else town


def _haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def should_regeocode(
    cached: dict | None, lat: float, lon: float, now_ts: float, failed_at: float | None = None
) -> bool:
    if failed_at is not None and now_ts - failed_at < FAILURE_BACKOFF_SEC:
        return False
    if cached is None:
        return True
    if now_ts - cached["ts"] >= RECHECK_INTERVAL_SEC:
        return True
    return _haversine_km(cached["lat"], cached["lon"], lat, lon) >= MOVE_THRESHOLD_KM
