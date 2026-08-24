import logging
import logging.handlers
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_FILE_NAME = "dashboard.log"

# Bounded on purpose. The Pi-side utils/logger.py opens a fresh, never-deleted file per
# process start and reached 3,078 files / 305M in the field (reports/pi-system-report.md
# §9, which recommends exactly this handler). One rotating file with a hard cap applies
# that fix here before the laptop can repeat it: 6 files, ~6 MB, worst case.
MAX_BYTES = 1_000_000
BACKUP_COUNT = 5

# Chatty at INFO: paramiko logs a line per connection and werkzeug one per request, which
# would bury the errors this file exists to preserve. Left alone at DEBUG — asking for
# DEBUG is asking for the firehose.
NOISY_LIBRARIES = ("paramiko", "urllib3", "werkzeug")

FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"

_handlers = []


def resolve_level(raw, default=logging.INFO):
    """LOG_LEVEL is a convenience knob, not a required key. A typo must not stop the
    dashboard from starting — and least of all must it stop the logging."""
    if not raw:
        return default
    level = logging.getLevelName(str(raw).strip().upper())
    # getLevelName returns the int for a known name and the string "Level X" otherwise.
    return level if isinstance(level, int) else default


def setup_logging(log_dir=None, level=logging.INFO):
    """Send everything — this app's except blocks, Flask's unhandled-500 traceback,
    waitress, paramiko — to one rotating file plus the console. Returns the file path.

    Idempotent: calling it again replaces its own handlers rather than stacking a second
    copy of each, which would write every line twice.
    """
    global _handlers

    root = logging.getLogger()
    for handler in _handlers:
        root.removeHandler(handler)
        handler.close()
    _handlers = []

    directory = Path(log_dir) if log_dir is not None else LOG_DIR
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / LOG_FILE_NAME

    formatter = logging.Formatter(FORMAT)
    file_handler = logging.handlers.RotatingFileHandler(
        path,
        maxBytes=MAX_BYTES,
        backupCount=BACKUP_COUNT,
        # Explicit: Windows would otherwise open this cp1252, and a Malaysian town name or
        # a non-ASCII error string would raise UnicodeEncodeError inside the handler —
        # losing the very message being logged.
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    # Console as well. Attaching any root handler retires logging's lastResort handler, so
    # a file-only setup would silently stop printing tracebacks to the terminal.
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    for handler in (file_handler, console_handler):
        root.addHandler(handler)
        _handlers.append(handler)
    root.setLevel(level)

    if level > logging.DEBUG:
        for name in NOISY_LIBRARIES:
            logging.getLogger(name).setLevel(logging.WARNING)

    return path
