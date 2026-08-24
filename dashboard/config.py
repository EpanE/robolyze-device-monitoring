import os
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
DEFAULT_PORT = 5000

# Every key the dashboard cannot run without, paired with the attribute it populates.
REQUIRED_KEYS = ("TAILSCALE_IP_ADDRESS", "USERNAME", "SSH_PASSWORD")


class ConfigError(Exception):
    pass


def load_env_file(path=ENV_PATH):
    values = {}
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


_env = load_env_file()

# Read straight from this project's .env, not os.environ: keys like USERNAME
# collide with real OS environment variables (Windows sets USERNAME to the
# logged-in user), which would silently shadow the intended value.
SSH_HOST = _env.get("TAILSCALE_IP_ADDRESS", "")
SSH_USER = _env.get("USERNAME", "")
SSH_PASSWORD = _env.get("SSH_PASSWORD", "")
DEFAULT_CONFIDENCE_THRESHOLD = _env.get("CONFIRMED_MIN_CONFIDENCE", "0.75")
LOG_LEVEL = _env.get("LOG_LEVEL", "INFO")

# Optional. Set it and every route asks for HTTP Basic credentials (any username); leave
# it empty and the dashboard is open, which is the right default for a laptop bound to
# loopback and the wrong one the moment HOST is opened up — app.py warns about exactly
# that combination at startup.
DASHBOARD_PASSWORD = _env.get("DASHBOARD_PASSWORD", "")


def load_host(env=None):
    """Loopback unless asked otherwise.

    Reaching HOST:PORT is enough to trigger an SSH session to the truck, so binding to
    every interface is a decision the operator makes deliberately in .env — not one they
    inherit from a default and discover via a Windows Firewall prompt.
    """
    return (env if env is not None else _env).get("HOST", "127.0.0.1")


HOST = load_host()

# Parsed in validate(), not here: a bare parse at import time raises before __main__'s
# ConfigError handler is reachable, so a typo'd PORT surfaced as a traceback instead of
# the readable message parse_port was written to produce — and broke pytest collection
# on any machine whose .env had one.
_PORT_RAW = _env.get("PORT")
PORT = DEFAULT_PORT


def parse_port(raw):
    if not raw:
        return DEFAULT_PORT
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"PORT in {ENV_PATH} must be a number, got {raw!r}") from exc


def missing_keys(host=None, user=None, password=None):
    values = (
        host if host is not None else SSH_HOST,
        user if user is not None else SSH_USER,
        password if password is not None else SSH_PASSWORD,
    )
    return [name for name, value in zip(REQUIRED_KEYS, values) if not value]


def validate():
    """Check every .env value at startup, loudly, rather than as a confusing paramiko error
    mid-refresh (or a parse traceback before the caller can catch it)."""
    global PORT
    PORT = parse_port(_PORT_RAW)

    missing = missing_keys()
    if missing:
        raise ConfigError(
            f"Missing from {ENV_PATH}: {', '.join(missing)}. "
            "See dashboard/README.md for the expected keys."
        )

