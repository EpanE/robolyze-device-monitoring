"""The driver's name, persisted across restarts until someone types a new one.

Deliberately a flat file under logs/, same reasoning as history.py: it has to survive a
laptop restart, and a dashboard for one truck does not need a database for one string.
"""

import json
import logging

from pathlib import Path

DRIVER_PATH = Path(__file__).resolve().parent / "logs" / "driver.json"

log = logging.getLogger("dashboard")


def load_driver(path=None):
    """The stored name, or "" if none has ever been set (or the file can't be read)."""
    path = Path(path) if path is not None else DRIVER_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return data.get("name", "") if isinstance(data, dict) else ""


def save_driver(name, path=None):
    """Persist the name. A write failure is logged, not raised — losing this must never
    break the request that triggered it."""
    path = Path(path) if path is not None else DRIVER_PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"name": name}), encoding="utf-8")
    except OSError:
        log.warning("could not write %s; driver name was not saved", path, exc_info=True)
