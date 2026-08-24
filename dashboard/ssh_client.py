import json
import logging
import socket

from pathlib import Path

import paramiko

HEALTH_JSON_PATH = "/run/roadmon/health.json"

# Trust-on-first-use, then pinned. Sits beside .env, next to the credential it protects,
# and is gitignored: it is this laptop's record of which machine it met, not shared config.
KNOWN_HOSTS_PATH = Path(__file__).resolve().parent.parent / "known_hosts"

log = logging.getLogger("dashboard")

# Per-command wall-clock budget. Without one, a link that dies mid-command (the
# truck dropping off Tailscale) leaves exec_command blocked indefinitely, pinning
# a waitress worker thread — which is what grows the "Task queue depth" warnings.
EXEC_TIMEOUT = 15
SCREENSHOT_TIMEOUT = 30


class SSHCommandError(Exception):
    pass


def connect(host, user, password, timeout=10):
    """Connect to the Pi, pinning its host key after the first successful connection.

    AutoAddPolicy on its own — no known_hosts file, nothing persisted — re-accepted
    whatever answered on the Pi's Tailscale IP on every single connect, and then sent it
    the SSH password. Loading and saving the key file turns that into trust-on-first-use:
    the first connection is still taken on faith, but every one after it is pinned, and a
    substituted host raises BadHostKeyException instead of silently collecting the
    credential. That exception is left to propagate — app.py renders it in the connection
    banner, which is the whole point of noticing.
    """
    client = paramiko.SSHClient()
    # Created up front because paramiko *reads* this file on the way to writing it —
    # save_host_keys re-loads it first, and AutoAddPolicy calls save_host_keys the moment
    # it learns a key. Without the file already there, the first connect on a new laptop
    # died with FileNotFoundError inside the policy, before authentication.
    path = Path(KNOWN_HOSTS_PATH)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)
        client.load_host_keys(str(path))
    except OSError:
        # An unwritable location must not stop the dashboard connecting; it only costs
        # the pinning, which is what the warning is for.
        log.warning("could not use %s for host key pinning; this connection is unpinned",
                    path, exc_info=True)
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        host, username=user, password=password, timeout=timeout,
        banner_timeout=timeout, auth_timeout=timeout,
    )
    # Explicit rather than relying on paramiko's own write-back: without a key on disk
    # there is nothing to pin against, so a silent failure to persist would leave every
    # connect as blind as before.
    client.save_host_keys(str(KNOWN_HOSTS_PATH))
    # Keepalive probes let paramiko notice a half-open link instead of blocking on
    # a read that will never return.
    transport = client.get_transport()
    if transport is not None:
        transport.set_keepalive(15)
    return client


def _exec(client, command, timeout=EXEC_TIMEOUT):
    # Every low-level failure is funnelled into SSHCommandError so callers have a
    # single exception type to handle. ConnectionResetError, socket.timeout and
    # paramiko's own SSHException all mean the same thing here: this command did
    # not produce an answer.
    try:
        _, stdout, stderr = client.exec_command(command, timeout=timeout)
        # Read before recv_exit_status(): reads honour the channel timeout, the
        # exit-status wait does not, so a dead link surfaces here instead of
        # hanging forever.
        out = stdout.read()
        err = stderr.read()
        exit_status = stdout.channel.recv_exit_status()
    except socket.timeout as exc:
        raise SSHCommandError(f"timed out after {timeout}s") from exc
    except (paramiko.SSHException, OSError, EOFError) as exc:
        raise SSHCommandError(f"SSH connection lost: {exc}") from exc
    return out, err, exit_status


def _exec_json(client, command, label):
    out, err, exit_status = _exec(client, command)
    if exit_status != 0:
        raise SSHCommandError(f"{label} failed: {err.decode(errors='replace').strip()}")
    try:
        return json.loads(out.decode())
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SSHCommandError(f"{label} returned invalid output: {exc}") from exc


def fetch_health_snapshot(client):
    """Return (health, age_sec) — the snapshot plus how stale it is, in one round trip.

    The agent rewrites health.json every 60s. If it dies, the file it left behind keeps
    reading back as a perfectly healthy snapshot forever, so the contents alone cannot
    tell a live truck from a dead agent. The file's mtime can.

    Age comes from the Pi's own clock (`date +%s`), not the laptop's: the two are
    routinely minutes apart, which would manufacture staleness (or hide it).
    """
    command = f"stat -c %Y {HEALTH_JSON_PATH} && date +%s && cat {HEALTH_JSON_PATH}"
    out, err, exit_status = _exec(client, command)
    if exit_status != 0:
        raise SSHCommandError(f"health.json read failed: {err.decode(errors='replace').strip()}")
    try:
        mtime_line, now_line, payload = out.decode().split("\n", 2)
        # Clamped: an NTP step backwards between the write and the read would otherwise
        # render as a negative age.
        age_sec = max(0.0, float(now_line) - float(mtime_line))
        return json.loads(payload), age_sec
    except (ValueError, UnicodeDecodeError) as exc:
        # ValueError covers the unpack, the float parse and json.JSONDecodeError alike.
        raise SSHCommandError(f"health.json read returned invalid output: {exc}") from exc


def fetch_tailscale_status(client):
    # --peers=false: only BackendState is ever read from this, and the peer list is the
    # bulk of the payload — pulled across the link on every 30s auto-poll tick, from a
    # truck on cellular.
    return _exec_json(client, "tailscale status --json --peers=false", "tailscale status")


def is_alive(client):
    """Whether this client can still be used, without sending anything.

    A refresh reuses the previous connection rather than paying a fresh TCP handshake plus
    password auth every 30 seconds. That is only safe if a link the truck dropped is
    detectable — which the transport's own state answers, having had keepalives running
    on it since connect().
    """
    transport = client.get_transport()
    return transport is not None and transport.is_active()


def grab_screenshot(client):
    # raspi2png depends on the dispmanx API, which the Pi 5 dropped — grim is the
    # Wayland-native replacement. `grim -` writes the PNG straight to stdout, so no
    # temp file/cat round trip is needed. A plain SSH shell doesn't inherit the
    # desktop session's environment, so XDG_RUNTIME_DIR/WAYLAND_DISPLAY must be set
    # explicitly; grim's own stderr is left unredirected so a real failure (e.g. "failed
    # to create display" when no compositor session is active) is readable.
    command = 'XDG_RUNTIME_DIR=/run/user/$(id -u) WAYLAND_DISPLAY=wayland-0 grim -'
    data, err, exit_status = _exec(client, command, timeout=SCREENSHOT_TIMEOUT)
    if exit_status != 0:
        raise SSHCommandError(f"screenshot capture failed: {err.decode(errors='replace').strip()}")
    if not data:
        raise SSHCommandError("screenshot capture failed: empty output")
    return data
