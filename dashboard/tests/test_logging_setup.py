import logging

import pytest

import logging_setup


@pytest.fixture(autouse=True)
def restore_root_logger():
    """setup_logging mutates the root logger, which every other test shares."""
    root = logging.getLogger()
    original_handlers, original_level = list(root.handlers), root.level
    noisy_levels = {name: logging.getLogger(name).level for name in logging_setup.NOISY_LIBRARIES}
    yield
    for handler in list(root.handlers):
        root.removeHandler(handler)
    for handler in original_handlers:
        root.addHandler(handler)
    root.setLevel(original_level)
    for name, level in noisy_levels.items():
        logging.getLogger(name).setLevel(level)
    logging_setup._handlers = []


def read_log(directory):
    return (directory / logging_setup.LOG_FILE_NAME).read_text(encoding="utf-8")


def test_setup_creates_the_directory_and_writes_errors_to_it(tmp_path):
    log_dir = tmp_path / "logs"
    assert not log_dir.exists()

    path = logging_setup.setup_logging(log_dir)
    logging.getLogger("dashboard").error("health.json read failed")

    assert path == log_dir / logging_setup.LOG_FILE_NAME
    assert "health.json read failed" in read_log(log_dir)


def test_tracebacks_are_preserved_not_just_the_message(tmp_path):
    # The whole point of the file: state[...]_error keeps only str(exc) for the banner, so
    # without this the traceback of every swallowed failure was gone for good.
    logging_setup.setup_logging(tmp_path)

    try:
        raise ValueError("boom")
    except ValueError:
        logging.getLogger("dashboard").exception("screenshot capture failed")

    contents = read_log(tmp_path)
    assert "screenshot capture failed" in contents
    assert "Traceback (most recent call last)" in contents
    assert "ValueError: boom" in contents


def test_non_ascii_messages_do_not_blow_up_the_handler(tmp_path):
    # Windows would open this cp1252 by default and a Malaysian place name or a °C reading
    # would raise UnicodeEncodeError inside the handler, losing the message being logged.
    logging_setup.setup_logging(tmp_path)

    logging.getLogger("dashboard").error("reverse geocode failed for Bestari Jaya — 44.6°C")

    assert "Bestari Jaya — 44.6°C" in read_log(tmp_path)


def test_calling_setup_twice_does_not_write_every_line_twice(tmp_path):
    logging_setup.setup_logging(tmp_path)
    logging_setup.setup_logging(tmp_path)

    logging.getLogger("dashboard").error("only-once")

    assert read_log(tmp_path).count("only-once") == 1


def test_the_log_is_size_capped_rather_than_growing_without_bound(tmp_path, monkeypatch):
    # The Pi's utils/logger.py has no rotation and reached 3,078 files / 305M in the field
    # (reports/pi-system-report.md §9). This laptop-side log must not repeat that.
    monkeypatch.setattr(logging_setup, "MAX_BYTES", 2048)
    monkeypatch.setattr(logging_setup, "BACKUP_COUNT", 2)
    logging_setup.setup_logging(tmp_path)

    log = logging.getLogger("dashboard")
    for i in range(500):
        log.error("a failure that repeats every refresh while the truck is offline %d", i)

    files = list(tmp_path.glob(logging_setup.LOG_FILE_NAME + "*"))
    assert len(files) == 3  # the live file plus BACKUP_COUNT rotations, never more
    assert all(f.stat().st_size < 8192 for f in files)


def test_console_output_is_kept_alongside_the_file(tmp_path):
    # Attaching any root handler retires logging's lastResort handler, so a file-only
    # setup would silently stop printing tracebacks to the terminal.
    logging_setup.setup_logging(tmp_path)

    handlers = logging.getLogger().handlers
    assert any(isinstance(h, logging.StreamHandler)
               and not isinstance(h, logging.FileHandler) for h in handlers)


def test_chatty_libraries_are_clamped_so_they_do_not_bury_the_errors(tmp_path):
    logging_setup.setup_logging(tmp_path, level=logging.INFO)

    logging.getLogger("paramiko").info("Connected (version 2.0, client OpenSSH_9.2p1)")

    assert "Connected (version" not in read_log(tmp_path)
    assert logging.getLogger("werkzeug").level == logging.WARNING


def test_debug_level_deliberately_lets_the_firehose_through(tmp_path):
    logging_setup.setup_logging(tmp_path, level=logging.DEBUG)

    assert logging.getLogger("paramiko").level != logging.WARNING


@pytest.mark.parametrize("raw, expected", [
    ("ERROR", logging.ERROR),
    ("warning", logging.WARNING),
    ("  debug  ", logging.DEBUG),
    ("", logging.INFO),
    (None, logging.INFO),
    ("chatty", logging.INFO),
    ("42", logging.INFO),
])
def test_resolve_level_never_lets_a_bad_log_level_stop_the_dashboard(raw, expected):
    assert logging_setup.resolve_level(raw) == expected
