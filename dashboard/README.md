# Duromac Dashboard (Plane B)

Laptop-side, team-run web dashboard. Pulls the Pi's health snapshot over SSH via
Tailscale — **nothing persistent runs on the Pi**. See
`../monitoring_implementation_plan.md` Phase 4 for the full design and
`SCHEMA.md` for the `health.json` contract this depends on.

## Setup

1. Tailscale must be connected on this laptop and able to reach the Pi's
   Tailscale IP (`100.110.189.87`).
2. `../.env` (project root, one level up) needs the three required keys — copy
   `../.env.example` and fill it in:
   ```
   USERNAME="rasp"
   TAILSCALE_IP_ADDRESS="100.110.189.87"
   SSH_PASSWORD="..."
   ```
   Any missing key aborts startup with a message naming it, rather than surfacing
   later as a confusing paramiko error mid-refresh.
   Password auth via `paramiko`, stored in plaintext in `.env` — matches this
   project's existing "secrets live in `.env`" convention. Rotate to SSH-key
   auth later if that becomes a concern (swap `ssh_client.connect`'s
   `password=` for `key_filename=`).

   The root `.gitignore` excludes `.env`. This folder is not a git repo today;
   that entry is there so the password does not go along the moment it becomes one.
3. Optional `.env` overrides (defaults shown work for most setups):
   ```
   HOST="127.0.0.1"
   PORT="5000"
   DASHBOARD_PASSWORD=""
   ```

## Host key pinning

The Pi's SSH host key is learned on the first successful connect and written to
`../known_hosts`, then pinned: every later connect verifies against it, and a
substituted host fails with `BadHostKeyException` in the connection banner
rather than quietly receiving the SSH password.

Only the first connection is taken on faith. If the Pi is legitimately
reimaged its key changes and connects will fail until you delete
`../known_hosts` — that failure is the feature working.

## Run

**Easiest — launcher script** (creates the venv, installs dependencies the
first time, and starts the server):
```
run.bat          # Windows — double-click, or run from a terminal
./run.sh         # macOS/Linux — chmod +x run.sh once, then ./run.sh
```

This starts the production server (Waitress) bound to `127.0.0.1:5000` by
default — this laptop only — and auto-opens your browser there. See
**Reachability** below for sharing it with others on the LAN.

**Manual equivalent**, if you'd rather not use the script:
```
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt   # Windows
.venv/bin/pip install -r requirements.txt        # macOS/Linux

# To run the tests too, install the dev extras instead:
.venv/Scripts/pip install -r requirements-dev.txt

.venv/Scripts/python app.py   # Windows
.venv/bin/python app.py       # macOS/Linux
```

Under an external WSGI host, use the factory — it is what configures logging,
parses `PORT` and validates `.env`:

```
waitress-serve --call app:create_app
```

Importing `app:app` directly skips all three: no log file, and an empty `.env`
surfacing later as a confusing paramiko error mid-refresh.

**Local-only dev mode** (Flask dev server, debug/auto-reload, bound to
`127.0.0.1` only — use this while editing templates/routes, never for
sharing with others):
```
.venv/Scripts/python app.py --dev   # Windows
.venv/bin/python app.py --dev       # macOS/Linux
```

Click **Refresh Now** on the Live Status tab to pull a fresh snapshot —
nothing auto-fetches on page load.

**Offline starts work.** The launcher only runs `pip install` when the pinned
dependencies do not import, so a normal start needs no network at all; if an
install is needed and fails, it warns and starts anyway rather than refusing to
launch a working dashboard because PyPI was unreachable from a truck. Both
launchers read `PORT` from `../.env` instead of assuming 5000.

Dependencies are pinned exactly (`==`) in `requirements.txt`. The launcher used
to run `pip install` on every start against `>=` floors, so a field launch could
silently pull a new Flask or paramiko with no way to roll back. Upgrade
deliberately, then re-run the tests.

## Reachability

By default (`run.bat`/`run.sh`/`python app.py`) the dashboard binds
`127.0.0.1:5000` — this laptop only. Reaching `HOST:PORT` is enough to open an
SSH session to the truck, so exposure is opt-in rather than inherited.

To share it, set both of these in `../.env`:

```
HOST="0.0.0.0"
DASHBOARD_PASSWORD="something-only-the-team-knows"
```

Then it's reachable from:

- **This laptop**: `http://127.0.0.1:5000` (auto-opened by the launcher).
- **Same LAN**: other devices on the same Wi-Fi/network, via this laptop's
  LAN IP — find it with `ipconfig` (Windows) / `ifconfig` or `ip addr`
  (macOS/Linux), e.g. `http://192.168.1.23:5000`.

Binding wide with no password still works, and logs a warning at startup
saying exactly what it exposes.

(Tailscale peers can technically still reach it via this laptop's Tailscale
IP, since the bind is `0.0.0.0` — but that path isn't the documented/primary
one; it was slow in practice and isn't necessary for LAN-based access.)

**Windows Firewall**: the first time you run this bound to `0.0.0.0`,
Windows will likely prompt "Windows Defender Firewall has blocked some
features of this app." Allow access (at least on Private networks) or other
devices on the LAN won't be able to connect.

**Security note**: `DASHBOARD_PASSWORD` is optional. Unset, every route is
open to anyone who can reach `HOST:PORT` — fine on the loopback default, not
fine once `HOST` is opened up, which is why that combination warns at startup.
Set, every route requires HTTP Basic credentials (any username; the password
is what is checked, in constant time).

Basic auth over plain HTTP sends that password with each request, base64'd,
readable by anything on the path. That is an accepted trade on a trusted LAN;
do not put this on the open internet.

Cross-origin POSTs are rejected (403) by an `Origin` check, so a page the
operator happens to have open cannot make the dashboard open an SSH session to
the Pi. Requests with no `Origin` header at all are allowed, so `curl` keeps
working — browsers have sent it on every cross-origin POST for years, so its
absence isn't the case being defended against.

## Degraded-Pi behaviour

Nothing about a sick Pi should break this page — every failure mode below
renders a banner and leaves **Refresh Now** clickable:

- **Pi unreachable / SSH dies mid-command**: connection banner; every SSH
  command runs under a timeout (15s, 30s for the screenshot) so a half-open
  link can't pin a server thread.
- **`health-agent.service` dead, snapshot frozen**: the file it last wrote keeps
  reading back as a healthy truck forever, so every refresh also reads its mtime
  (by the Pi's own clock, in the same command). Past 180s — three missed 60s
  writes — the five health cards go grey, an error banner names the age and the
  service, the map pin drops to stale, and the Daily Report refuses to generate
  rather than putting "✅ System running" into a WhatsApp message.
- **`/run/roadmon/health.json` missing or unreadable**: System/Pipeline/Camera/
  GPS/Upload show "No data" with a warning banner.
- **`health.json` partial or missing a section**: that component renders as a
  grey ⚪ "not determined" card. The rest of the snapshot still renders.
- **Tailscale card** works independently of `health.json` (separate
  `tailscale status --json` call).
- **Screenshot (`grim`) unavailable**: error text in the screenshot panel; the
  rest of the refresh still succeeds. The previous capture stays on the page,
  labelled with the time it was taken — a stale screen beats a blank panel when
  the compositor is the thing that died.
- **Reverse geocoding (Nominatim) unavailable**: warning banner; the town label
  goes stale but the health snapshot is untouched. Failures back off for 5
  minutes rather than retrying on every refresh, so an outage can't turn the 30s
  auto-poll into a request-per-tick loop against Nominatim's usage policy.
- **No internet**: Leaflet is vendored in `static/vendor/leaflet`, so the map
  still initialises — but OSM tiles come from the network, so the marker will
  sit on a blank grid.
- **No health data at all**: the Daily Report tab prompts to refresh instead of
  rendering a broken report.

## What a refresh costs

The connection is **reused between refreshes**. Opening one costs a TCP handshake
plus paramiko password auth — far more than the small commands a refresh actually
runs — and the 30s auto-poll used to pay it every tick. The session is dropped and
reopened when its transport goes inactive (truck off Tailscale, Pi rebooted) or
when a command fails on it.

The **screenshot is throttled** to one capture per 5 minutes
(`app.SCREENSHOT_MIN_INTERVAL_SEC`) during a refresh. A full-res PNG on every 30s
tick was the bulk of what this pulls over the link, for a screen nobody is
necessarily watching. The **Grab Screenshot** button is never throttled, and the
capture timestamp beside the image says how old the displayed frame is.

`tailscale status` is fetched with `--peers=false`; only `BackendState` is read
from it and the peer list is most of the payload.

The auto-poll **patches the page in place** rather than reloading it. It POSTs
`/api/refresh`, then GETs `/api/status`, which returns banners, cards and trends
as HTML rendered from the same partials the full page includes (`_banners.html`,
`_cards.html`, `_trends.html` — one source of truth, so the two can't drift) plus
the marker as data. The Leaflet map is built once and the marker moved; the
operator's pan and zoom survive a tick, which a full reload destroyed.

## Trends

Every successful refresh appends one JSON line to `logs/history.jsonl`: fps,
heartbeat age, CPU temp, disk used, upload queue depth, dropped total, speed, and
the snapshot age. The Live Status tab draws each as a sparkline.

This exists because every other part of this page answers "is it broken" and none
of them answer "since when" — which is the question actually asked afterwards. Did
the disk fill overnight, has fps been sagging all week, did the queue back up
before or after the reboot.

- **Stale snapshots are not recorded.** Frozen values repeated every 30s would
  draw a flat line that reads as a healthy, steady truck.
- **Gaps are recorded as null and break the line** rather than being plotted as
  zero — a stopped pipeline is not a pipeline running at 0 fps.
- Capped at 2880 rows (~24h at the 30s cadence), trimmed on a threshold rather
  than rewritten every refresh. `logs/` is gitignored.
- A history write failure is logged and swallowed: the trend is a nice-to-have,
  the live status is not.

Only one SSH session runs at a time; a Refresh that arrives while one is in
flight is dropped rather than queued — and says so, via a banner (button) or an
HTTP 409 (auto-poll), instead of quietly re-rendering stale state.

## Logs

Every failure the dashboard swallows into a banner is also written, with its full
traceback, to:

```
dashboard/logs/dashboard.log
```

This exists because the banners are lossy: `state[...]_error` holds only
`str(exc)` for display, and the next refresh overwrites it. By the time
someone asks "what happened at 3pm?", the page has moved on. The log hasn't.

What lands there:

- SSH connect failures (truck off Tailscale, auth, timeout).
- `health.json` read failures, `tailscale status` failures, `grim` screenshot
  failures — each independently, as they already are on the page.
- Reverse-geocode failures (at most one per 5-minute backoff window).
- Any unhandled exception Flask turns into a 500, plus waitress's own errors.
- A startup line naming the log path.

Errors also still print to the terminal, so nothing you used to see there is
lost.

**It is size-capped**: one file, rotated at 1 MB, 5 old copies kept — about
6 MB worst case, forever. This is deliberate. The Pi-side `utils/logger.py`
opens a fresh, never-deleted file per process start and was measured at 3,078
files / 305M (`../reports/pi-system-report.md` §9); that report's own
recommendation was a `RotatingFileHandler`, which is what this uses.

`logs/` is gitignored and created on first start — nothing to set up. Set
`LOG_LEVEL` in `../.env` to change verbosity (`DEBUG` also unmutes paramiko
and werkzeug); a typo there falls back to `INFO` rather than blocking startup.

**The SSH password is never logged.** Connect failures log the host and the
traceback; the traceback quotes the *source line*
(`ssh_client.connect(..., config.SSH_PASSWORD)`), which names the attribute,
never its value. There is a test pinning this.

## Tests

```
.venv/Scripts/python -m pytest tests/ -v
```

Requires the dev extras (`requirements-dev.txt`).

`report.py` and `status.py` are pure-function TDD'd logic (health-dict in,
text/cards out). `ssh_client.py` is tested against a fake paramiko-shaped test
double, not a real connection. `app.py`'s routes are tested with Flask's test
client plus monkeypatched `ssh_client` calls — no test hits the network.
`logging_setup.py` writes to a `tmp_path`; running the suite never creates
`logs/`, because handlers are attached in `__main__` rather than at import.
