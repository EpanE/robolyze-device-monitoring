import datetime
import hashlib
import logging
import secrets
import threading
import time

from urllib.parse import urlparse

from flask import Flask, redirect, render_template, request, Response, url_for

import config
import contacts
import driver
import geocode
import clarity_log
import history
import logging_setup
import outage_log
import report
import ssh_client
import status

app = Flask(__name__)

# Every except block below stores a short string in `state` for the banner and logs the
# same failure, with its traceback, here. The banner is what the operator sees now; the
# log is what survives the next refresh, which overwrites the banner. Handlers are
# attached by create_app (setup_logging), so importing this module never creates logs/ —
# the test suite doesn't litter the repo.
log = logging.getLogger("dashboard")


def new_state():
    return {
        "health": None,
        # How old the snapshot was when we read it, by the Pi's clock. None means the age
        # could not be established, which is not the same as fresh — see status.is_stale.
        "health_age_sec": None,
        "tailscale": None,
        "connection_error": None,
        "health_error": None,
        "tailscale_error": None,
        "last_refreshed": None,
        "last_known_gps": None,
        "location_cache": None,
        "location_error": None,
        "location_error_ts": None,
        # One record — {"bytes", "version", "ts"} — or None. Kept together rather than as
        # three sibling keys because the version names the bytes: a reader that caught the
        # gap between two of those assignments would cache one capture's image under the
        # other's URL, for the full hour the Cache-Control header promises.
        "screenshot": None,
        # About the last *attempt*, not the stored image — a failed grab deliberately keeps
        # the previous capture, so these two do not move together.
        "screenshot_error": None,
        # Set once a reboot command has been sent; cleared by the next refresh that
        # manages to open an SSH session again -- that is what "back up" means here, not
        # any particular health.json content.
        "rebooting": False,
    }


state = new_state()

# One SSH session to the truck at a time. Each refresh holds a worker thread for
# the length of three round trips (the screenshot alone takes seconds), so
# without this an impatient double-click stacks whole SSH sessions and the
# waitress queue backs up. A refresh that arrives while one is running is a
# no-op: the in-flight one is about to publish fresher state anyway.
_ssh_lock = threading.Lock()

# The live SSH session, reused across refreshes. Opening one costs a TCP handshake plus
# paramiko password auth — far more than the three tiny commands a refresh actually runs —
# and the 30s auto-poll was paying it every tick. Only ever touched while _ssh_lock is
# held, which is what makes a plain module global safe here.
_ssh_session = None

# A full-res PNG of the Pi's screen is the bulk of what this app pulls over the link, and
# the auto-poll was pulling one every 30s for a screen nobody is necessarily watching. The
# screenshot's own timestamp is rendered beside it, so a throttled one is labelled, not
# passed off as current. The manual Grab Screenshot button ignores this entirely.
SCREENSHOT_MIN_INTERVAL_SEC = 300


def drop_ssh_session():
    """Forget the cached session, closing it if we can. Safe to call at any time."""
    global _ssh_session
    session, _ssh_session = _ssh_session, None
    if session is not None:
        try:
            session.close()
        except Exception:
            log.debug("SSH teardown failed", exc_info=True)


def _ssh_session_for_use():
    """The cached session if it is still usable, otherwise a fresh one.

    Connection failures propagate: the caller reports them in the connection banner, and
    nothing is cached, so the next refresh retries from scratch rather than reusing a
    client that never connected.
    """
    global _ssh_session
    if _ssh_session is not None:
        if ssh_client.is_alive(_ssh_session):
            return _ssh_session
        # The truck dropped off Tailscale, or the Pi rebooted. Reconnect rather than
        # issuing commands into a transport that is already gone.
        log.info("cached SSH session is no longer alive; reconnecting")
        drop_ssh_session()

    _ssh_session = ssh_client.connect(config.SSH_HOST, config.SSH_USER, config.SSH_PASSWORD)
    return _ssh_session


@app.before_request
def _guard_request():
    """Two checks, both cheap, both about who is allowed to make this app talk to the Pi.

    Auth is opt-in: an unset DASHBOARD_PASSWORD leaves the dashboard open, because the
    field workflow is double-clicking run.bat on a trusted LAN and a password nobody
    configured would only lock the operator out.

    The origin check is not opt-in. Any page the operator happens to have open can submit
    a cross-origin form POST here; it can't read the response, but it can make this app
    open an SSH session to the truck. GETs are left alone — they change nothing, and
    blocking them would break an <img> embed for no gain.
    """
    if config.DASHBOARD_PASSWORD:
        auth = request.authorization
        # compare_digest, not ==: string comparison returns early on the first wrong byte,
        # which is a (small, but free to avoid) oracle for guessing the password.
        if not auth or not secrets.compare_digest(auth.password or "", config.DASHBOARD_PASSWORD):
            return Response(
                "authentication required", status=401,
                headers={"WWW-Authenticate": 'Basic realm="Duromac Monitor"'},
                mimetype="text/plain",
            )

    if request.method in ("POST", "PUT", "DELETE", "PATCH"):
        origin = request.headers.get("Origin")
        # Absent is allowed: curl and the launcher's own checks send no Origin, while
        # browsers have sent it on every cross-origin POST for years. Its absence is
        # therefore not the request this is defending against.
        if origin and urlparse(origin).netloc != request.host:
            log.warning("rejected a cross-origin %s to %s from %s",
                        request.method, request.path, origin)
            return Response("cross-origin request rejected", status=403, mimetype="text/plain")


def _back_to_referrer(extra_query=None):
    # request.referrer is the raw, caller-supplied Referer header: a link from anywhere
    # could otherwise trigger the SSH connection to the Pi and then bounce the operator
    # off-site. Only the path and query are reused, rebuilt onto this host — comparing
    # netloc alone let two shapes through, `javascript:...` (no netloc at all) and
    # `/\evil.example` (no netloc until the browser normalises it to protocol-relative).
    target = None
    referrer = request.referrer
    if referrer:
        parts = urlparse(referrer)
        if not parts.netloc or parts.netloc == request.host:
            path = parts.path or "/"
            # A path may not begin a second slash-ish segment, or it stops being a path.
            if path.startswith("/") and not path.startswith(("//", "/\\")):
                target = path + (f"?{parts.query}" if parts.query else "")
    if target is None:
        target = url_for("index")
    if extra_query:
        target += ("&" if "?" in target else "?") + extra_query
    return redirect(target)


def _is_xhr():
    # The auto-poll fetch() sets this header; a browser form POST never does. Lets the one
    # route answer both callers in the shape each actually wants.
    return request.headers.get("X-Requested-With") == "fetch"


def _current_town():
    return state["location_cache"]["town"] if state["location_cache"] else None


def _store_screenshot(png_bytes):
    # Content hash rather than a counter: new_state() reset the counter to 0 on every
    # restart, so the first capture after one landed back on /api/screenshot?v=1 — a URL
    # the browser still had hard-cached (max-age=3600) from the previous run, showing an
    # hour-old screen with nothing to hint at it. Identical bytes now mean an identical
    # URL, so a cache hit is always the correct answer.
    #
    # Built whole, then published in one assignment: readers run on other waitress threads
    # and must see either the old capture or the new one, never a half-updated pair.
    state["screenshot"] = {
        "bytes": png_bytes,
        "version": hashlib.sha256(png_bytes).hexdigest()[:12],
        "ts": datetime.datetime.now(),
    }


def _current_driver():
    # Read from disk per request rather than held in memory: it survives a restart that
    # way, same reasoning as history.load() below, and it only ever changes on a manual
    # edit via /api/driver.
    return driver.load_driver()


def _contacts_with_links():
    # wa_link computed here, not in JS: contacts.json's phone formats are the one thing
    # both the dropdown-population and the WhatsApp button need, so parse them once.
    return [{**c, "wa_link": contacts.wa_link(c["phone"])} for c in contacts.load_contacts()]


def _driver_wa_link(contacts_list, driver_name):
    match = next((c for c in contacts_list if c.get("name") == driver_name), None)
    return match["wa_link"] if match else None


def _build_report(stale):
    """The report text for the current state, or None if there's nothing trustworthy to
    show yet. Shared by index() and api_status() so both render the exact same text."""
    # A stale snapshot is withheld rather than rendered: this report gets copied into
    # WhatsApp and signed off, so a "✅ System running" line read off a frozen file is
    # worse than no report at all.
    if state["health"] is None or stale:
        return None
    return report.build_report(
        state["health"], config.DEFAULT_CONFIDENCE_THRESHOLD, datetime.date.today(),
        town=_current_town(), driver_name=_current_driver(),
    )


@app.route("/")
def index():
    age_sec = state["health_age_sec"]
    cards = status.build_status_cards(state["health"], state["tailscale"], age_sec)
    marker = status.build_location_marker(state["health"], state["last_known_gps"], age_sec)
    # Read from disk per request rather than held in memory: it survives a restart that
    # way, and one file read of a few hundred KB is nothing next to the SSH round trips.
    trends = history.build_trends(history.load())
    stale = status.is_stale(age_sec)
    driver_name = _current_driver()
    contacts_list = _contacts_with_links()
    return render_template(
        "live_status.html", cards=cards, state=state, marker=marker, town=_current_town(),
        stale=stale, age_sec=age_sec, trends=trends,
        pipeline_alert=status.pipeline_down_while_connected(cards, stale),
        pipeline_evidence=outage_log.detection_evidence(state["health"] or {}),
        driver_name=driver_name, report_text=_build_report(stale),
        contacts=contacts_list, driver_wa_link=_driver_wa_link(contacts_list, driver_name),
        ssh_target=f"{config.SSH_USER or '<user>'}@{config.SSH_HOST or '<pi-ip>'}",
    )


@app.route("/api/status")
def api_status():
    """Everything the Live Status page shows, for the auto-poll to patch in place.

    The poll used to finish with location.reload(): a whole page fetch every 30s that tore
    down and rebuilt the Leaflet map, losing the operator's pan, zoom and scroll position
    each time — while they were looking at it.

    Banners and cards come back as HTML rendered from the same partials the full page
    includes, not as data for JavaScript to re-render. Rebuilding a card client-side means
    two implementations of what a card looks like, and they drift.

    Touches no SSH: this reads published state only. POST /api/refresh is what talks to
    the Pi.
    """
    age_sec = state["health_age_sec"]
    stale = status.is_stale(age_sec)
    cards = status.build_status_cards(state["health"], state["tailscale"], age_sec)
    marker = status.build_location_marker(state["health"], state["last_known_gps"], age_sec)
    record = state["screenshot"]

    return {
        "banners_html": render_template(
            "_banners.html", state=state, stale=stale, age_sec=age_sec,
            pipeline_alert=status.pipeline_down_while_connected(cards, stale),
            pipeline_evidence=outage_log.detection_evidence(state["health"] or {}),
        ),
        "cards_html": render_template("_cards.html", cards=cards),
        "trends_html": render_template("_trends.html", trends=history.build_trends(history.load())),
        "report_html": render_template("_report.html", report_text=_build_report(stale), stale=stale),
        "marker": marker,
        "town": _current_town(),
        "stale": stale,
        "driver_wa_link": _driver_wa_link(_contacts_with_links(), _current_driver()),
        "last_refreshed": _format_ts(state["last_refreshed"]),
        "screenshot": (
            {"version": record["version"], "ts": _format_ts(record["ts"])} if record else None
        ),
        "screenshot_error": state["screenshot_error"],
    }


def _clarity_days(problems_only):
    now = datetime.datetime.now()
    return clarity_log.build_events(
        clarity_log.load(), now=now, last_seen=clarity_log.last_seen(), problems_only=problems_only,
    )


@app.route("/history")
def clarity_history():
    """When the camera was blurry and when it was not, from the dashboard's own record."""
    problems_only = request.args.get("filter") == "problems"
    return render_template(
        "history.html", days=_clarity_days(problems_only), problems_only=problems_only,
        retention_days=clarity_log.RETENTION.days,
    )


@app.route("/api/clarity-log")
def api_clarity_log():
    days = _clarity_days(request.args.get("filter") == "problems")
    return {"days": [
        {"day": d["day"], "events": [
            {**e, "start": e["start"].isoformat(timespec="seconds"),
             "end": e["end"].isoformat(timespec="seconds")} for e in d["events"]]}
        for d in days
    ]}


def _outage_days(problems_only):
    now = datetime.datetime.now()
    return outage_log.build_events(
        outage_log.load(path=outage_log.OUTAGE_LOG_PATH), now=now,
        last_seen=outage_log.last_seen(outage_log.OUTAGE_LOG_PATH), problems_only=problems_only,
    )


@app.route("/outages")
def outage_history():
    """When the detection pipeline was down, and what the service said about it."""
    problems_only = request.args.get("filter") == "problems"
    return render_template(
        "outages.html", days=_outage_days(problems_only), problems_only=problems_only,
        retention_days=outage_log.RETENTION.days,
    )


@app.route("/api/outage-log")
def api_outage_log():
    days = _outage_days(request.args.get("filter") == "problems")
    return {"days": [
        {"day": d["day"], "events": [
            {**e, "start": e["start"].isoformat(timespec="seconds"),
             "end": e["end"].isoformat(timespec="seconds")} for e in d["events"]]}
        for d in days
    ]}


@app.route("/logs")
def logs_page():
    """The log viewer shell. Touches no SSH: POST /api/logs is what fetches."""
    source = request.args.get("source")
    if source not in ssh_client.LOG_SOURCES:
        source = "detection"
    return render_template(
        "logs.html", sources=[(key, label) for key, (label, _) in ssh_client.LOG_SOURCES.items()],
        selected=source, default_lines=ssh_client.DEFAULT_LOG_LINES,
        max_lines=ssh_client.MAX_LOG_LINES,
    )


@app.route("/api/logs", methods=["POST"])
def fetch_logs():
    source = request.form.get("source", "")
    if source not in ssh_client.LOG_SOURCES:
        return Response("unknown log source", status=400, mimetype="text/plain")
    try:
        lines = int(request.form.get("lines", ssh_client.DEFAULT_LOG_LINES))
    except ValueError:
        return Response("lines must be a number", status=400, mimetype="text/plain")

    if not _ssh_lock.acquire(blocking=False):
        return Response("another SSH request is in flight, try again", status=503, mimetype="text/plain")
    try:
        return _do_fetch_logs(source, lines)
    finally:
        _ssh_lock.release()


def _do_fetch_logs(source, lines):
    try:
        client = _ssh_session_for_use()
    except Exception as exc:
        state["connection_error"] = str(exc)
        drop_ssh_session()
        log.exception("SSH connect to %s failed for a log fetch", config.SSH_HOST)
        return Response(str(exc), status=502, mimetype="text/plain")

    try:
        text = ssh_client.fetch_logs(client, source, lines, password=config.SSH_PASSWORD)
    except Exception as exc:
        drop_ssh_session()
        log.exception("log fetch (%s) failed", source)
        return Response(str(exc), status=502, mimetype="text/plain")

    return {"source": source, "text": text, "fetched_at": _format_ts(datetime.datetime.now())}


def _format_ts(value):
    return value.strftime("%Y-%m-%d %H:%M:%S") if value else None


@app.route("/api/driver", methods=["POST"])
def set_driver():
    """Persist the driver's name. No SSH involved — covered by the auth/origin guard
    like every other POST here, but it never touches the truck."""
    name = request.form.get("name", "").strip()
    driver.save_driver(name)
    return {"status": "ok", "name": name}


def _fix_timestamp(gps, now):
    """When the fix was *taken*, not when we read a file still quoting it.

    The Pi keeps republishing the last coordinates long after the fix went stale, so
    stamping these with the read time made an hours-old position render as "as of a
    moment ago" — the one thing a last-known-location marker exists to be honest about.

    Returns None when the fix time genuinely can't be established; the caller keeps any
    stamp it already had rather than inventing a fresher one.
    """
    age_sec = status.as_float(gps.get("last_valid_age_sec"))
    if age_sec is not None:
        return now - datetime.timedelta(seconds=age_sec)
    # No age field, but the Pi vouches for the fix: it is current as of this read.
    return now if gps.get("ok") else None


def _update_location(health, now=None):
    now = now or datetime.datetime.now()
    gps = (health or {}).get("gps") or {}
    # Same guard status.py applies to these fields: a GPS layer emitting raw NMEA text
    # would otherwise reach math.radians() inside should_regeocode and TypeError there,
    # surfacing as a bogus "geocode failed" banner.
    lat, lon = status.as_float(gps.get("lat")), status.as_float(gps.get("lon"))
    if lat is None or lon is None:
        return

    fix_ts = _fix_timestamp(gps, now)
    previous = state["last_known_gps"]
    if fix_ts is not None:
        state["last_known_gps"] = {
            "lat": lat, "lon": lon, "ts": fix_ts.strftime("%Y-%m-%d %H:%M:%S"),
        }
    elif previous is None:
        # Nothing known about when this was taken — but a position with an honest
        # "time unknown" still beats no marker at all.
        state["last_known_gps"] = {"lat": lat, "lon": lon, "ts": None}

    now_ts = now.timestamp()
    if not geocode.should_regeocode(
        state["location_cache"], lat, lon, now_ts, state["location_error_ts"]
    ):
        return

    try:
        town = geocode.reverse_geocode(lat, lon)
    except geocode.GeocodeError as exc:
        state["location_error"] = str(exc)
        # Stamped so should_regeocode backs off. A failure leaves location_cache unwritten,
        # which on its own makes every later refresh retry — one Nominatim request per
        # auto-poll tick until they block the IP.
        state["location_error_ts"] = now_ts
        log.exception("reverse geocode failed; backing off for %ss", geocode.FAILURE_BACKOFF_SEC)
        return

    state["location_cache"] = {"lat": lat, "lon": lon, "ts": now_ts, "town": town}
    state["location_error"] = None
    state["location_error_ts"] = None


@app.route("/api/refresh", methods=["POST"])
def refresh():
    if not _ssh_lock.acquire(blocking=False):
        # Was a bare 302, indistinguishable from success: a dropped double-click or poll
        # tick looked like a refresh that had simply found nothing new.
        if _is_xhr():
            return Response(
                "another refresh is already in flight, skipped", status=409, mimetype="text/plain"
            )
        return _back_to_referrer(extra_query="refresh=skipped")
    try:
        _do_refresh()
    finally:
        _ssh_lock.release()

    # 204 rather than the redirect: fetch() follows a 302 by default and downloads the whole
    # page body, which the caller discards before location.reload() fetches it a second time.
    if _is_xhr():
        return Response(status=204)
    return _back_to_referrer()


def _do_refresh():
    state["connection_error"] = None
    try:
        client = _ssh_session_for_use()
    except Exception as exc:  # connection-level failure (auth/timeout/network)
        state["connection_error"] = str(exc)
        # Nothing usable was produced, so nothing is cached — the next refresh starts over.
        drop_ssh_session()
        # The traceback quotes the source line `ssh_client.connect(config.SSH_HOST, ...,
        # config.SSH_PASSWORD)` — the attribute name, never its value. No credential
        # reaches this file.
        log.exception("SSH connect to %s failed", config.SSH_HOST)
        return

    # A live SSH session is what "back up" means here -- independent of whether the
    # commands below succeed.
    state["rebooting"] = False

    # Set by any command that fails below. A command failing on a link the transport still
    # calls alive is the ambiguous case: reconnecting costs one handshake, whereas reusing
    # a wedged session costs every refresh after it.
    session_suspect = False
    try:
        try:
            state["health"], state["health_age_sec"] = ssh_client.fetch_health_snapshot(client)
            state["health_error"] = None
        except Exception as exc:  # ssh_client funnels transport loss into SSHCommandError; this is the backstop
            state["health"] = None
            # Cleared with the snapshot it described: a leftover age would keep grading a
            # page that no longer has a snapshot to grade.
            state["health_age_sec"] = None
            state["health_error"] = str(exc)
            session_suspect = True
            log.exception("health.json read failed")

        # Deliberately its own block: this derives from the snapshot rather than fetching
        # it, and it reaches the public internet (Nominatim). Sharing a try with the SSH
        # read above meant a rate-limited geocode nulled out a snapshot that had already
        # arrived intact, and reported it in the banner as a health.json failure.
        try:
            _update_location(state["health"])
        except Exception as exc:
            state["location_error"] = str(exc)
            state["location_error_ts"] = time.time()
            log.exception("location update failed")

        try:
            state["tailscale"] = ssh_client.fetch_tailscale_status(client)
            state["tailscale_error"] = None
        except Exception as exc:
            state["tailscale"] = None
            state["tailscale_error"] = str(exc)
            session_suspect = True
            log.exception("tailscale status failed")

        if _screenshot_is_due():
            try:
                _store_screenshot(ssh_client.grab_screenshot(client))
                state["screenshot_error"] = None
            except Exception as exc:
                # The previous capture is deliberately kept. Nulling it rendered an <img>
                # with no src — a broken-image icon — exactly when the Pi's compositor was
                # down, which is when the last known screen is most worth seeing. The
                # timestamp rendered beside it says how old it is.
                state["screenshot_error"] = str(exc)
                session_suspect = True
                log.exception("screenshot capture failed; keeping the previous capture")

        try:
            # After the cards, and swallowing its own failures: the trend is a
            # nice-to-have, the live status is not.
            history.record(state["health"], state["health_age_sec"])
        except Exception:
            log.exception("could not record trend history")

        try:
            clarity_log.record(state["health"], state["health_age_sec"])
        except Exception:
            log.exception("could not record clarity history")

        try:
            outage_log.record(state["health"], state["health_age_sec"])
        except Exception:
            log.exception("could not record outage history")

        state["last_refreshed"] = datetime.datetime.now()
    finally:
        # The session is deliberately left open for the next refresh — that is the whole
        # saving. It is only dropped when something suggested it is no longer trustworthy.
        if session_suspect:
            drop_ssh_session()


def _screenshot_is_due(now=None):
    """Whether this refresh should pay for a screenshot.

    Capped rather than skipped outright: the panel would otherwise freeze at whatever the
    last manual grab produced. SCREENSHOT_MIN_INTERVAL_SEC turns "every 30s tick" into
    "every 5 minutes", and the capture timestamp beside the image says which it is.
    """
    record = state["screenshot"]
    if record is None:
        return True
    now = now or datetime.datetime.now()
    return (now - record["ts"]).total_seconds() >= SCREENSHOT_MIN_INTERVAL_SEC


@app.route("/api/screenshot")
def screenshot():
    """Serve the last captured image. Never opens SSH — POST to this route to capture."""
    # Read once. Re-reading state["screenshot"] per field would reintroduce exactly the
    # split this record exists to prevent.
    record = state["screenshot"]
    if not record:
        return Response("no screenshot captured yet", status=404, mimetype="text/plain")

    resp = Response(record["bytes"], mimetype="image/png")
    if request.args.get("v") == record["version"]:
        # The URL names these exact bytes, so they are immutable under it and safe to
        # cache hard.
        resp.headers["Cache-Control"] = "private, max-age=3600"
    else:
        # The page was rendered against an older capture, or asked without a version at
        # all. Serving what we have is right; letting the browser file it away for an hour
        # under a URL that names *different* bytes is not — that binding would outlive the
        # capture and resurface as an hour-old screen presented as current.
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/screenshot", methods=["POST"])
def grab_screenshot():
    if not _ssh_lock.acquire(blocking=False):
        return Response("another SSH request is in flight, try again", status=503, mimetype="text/plain")
    try:
        return _do_screenshot()
    finally:
        _ssh_lock.release()


def _do_screenshot():
    try:
        client = ssh_client.connect(config.SSH_HOST, config.SSH_USER, config.SSH_PASSWORD)
    except Exception as exc:
        log.exception("SSH connect to %s failed for a manual screenshot", config.SSH_HOST)
        return Response(str(exc), status=502, mimetype="text/plain")

    try:
        png_bytes = ssh_client.grab_screenshot(client)
        # Stored, not just returned: a manual grab used to vanish on the next page load,
        # silently reverting to the older screenshot from the last refresh.
        _store_screenshot(png_bytes)
        state["screenshot_error"] = None
    except Exception as exc:
        state["screenshot_error"] = str(exc)
        log.exception("manual screenshot capture failed")
        return Response(str(exc), status=502, mimetype="text/plain")
    finally:
        try:
            client.close()
        except Exception:
            log.debug("SSH teardown after manual screenshot failed", exc_info=True)

    resp = Response(png_bytes, mimetype="image/png")
    # So the page can point its <img> at the URL that actually names these bytes. Guessing
    # (?v=<timestamp>) produced a URL the GET route can never match, making every manual
    # capture uncacheable.
    resp.headers["X-Screenshot-Version"] = state["screenshot"]["version"]
    return resp


@app.route("/api/reboot", methods=["POST"])
def reboot():
    if not _ssh_lock.acquire(blocking=False):
        return Response("another SSH request is in flight, try again", status=503, mimetype="text/plain")
    try:
        return _do_reboot()
    finally:
        _ssh_lock.release()


def _do_reboot():
    try:
        client = _ssh_session_for_use()
    except Exception as exc:
        state["connection_error"] = str(exc)
        drop_ssh_session()
        log.exception("SSH connect to %s failed for a reboot", config.SSH_HOST)
        return Response(str(exc), status=502, mimetype="text/plain")

    try:
        ssh_client.reboot_device(client, config.SSH_PASSWORD)
    except Exception as exc:
        log.exception("reboot command failed")
        return Response(str(exc), status=502, mimetype="text/plain")
    finally:
        # Whether the Pi is now rebooting or the connection merely dropped mid-command,
        # the cached session no longer points at a machine worth reusing.
        drop_ssh_session()

    state["rebooting"] = True
    return {"status": "ok", "message": "Reboot sent"}


def create_app():
    """Wire up logging and validate the config, then hand back the app.

    This lives in a factory rather than under `__main__` because that is the only place a
    WSGI host looks. Served as `waitress-serve app:create_app`, the old arrangement ran
    neither: no log file, no PORT parsed, and an empty .env sailing through to surface as
    a paramiko error on the operator's first refresh.

    Logging is configured first, deliberately: a config error has to land in the log file
    rather than only flashing past in a terminal nobody opened (run.bat is double-clicked).
    """
    log_path = logging_setup.setup_logging(level=logging_setup.resolve_level(config.LOG_LEVEL))
    config.validate()

    # Opening the bind is a deliberate act; leaving it open with no password is usually
    # not. Anyone who can reach the port can trigger an SSH session to the truck.
    if config.HOST not in ("127.0.0.1", "localhost") and not config.DASHBOARD_PASSWORD:
        log.warning(
            "Bound to %s with no DASHBOARD_PASSWORD set — anyone who can reach this port "
            "can view the dashboard and open an SSH session to the Pi. Set "
            "DASHBOARD_PASSWORD in .env, or bind HOST to 127.0.0.1.", config.HOST,
        )

    log.info("Dashboard starting — errors are logged to %s", log_path)
    return app


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dev", action="store_true",
        help="Flask dev server on 127.0.0.1 with debug/reload — local only, never exposed.",
    )
    args = parser.parse_args()

    # Validation happens here rather than at import so the test suite (which never
    # connects) doesn't need a populated .env.
    try:
        create_app()
    except config.ConfigError as exc:
        log.error("Configuration error: %s", exc)
        raise SystemExit(1)

    if args.dev:
        # Hardcoded to 127.0.0.1 regardless of config.HOST — the Werkzeug
        # debugger (debug=True) must never be reachable off this machine.
        app.run(host="127.0.0.1", port=config.PORT, debug=True)
    else:
        from waitress import serve
        print(f"Serving on http://{config.HOST}:{config.PORT}")
        serve(app, host=config.HOST, port=config.PORT)
