import pathlib
import socket

import paramiko

import ssh_client as ssh_client_module
import pytest

from ssh_client import (
    SSHCommandError,
    fetch_health_snapshot,
    fetch_tailscale_status,
    grab_screenshot,
    reboot_device,
)


class FakeChannel:
    def __init__(self, exit_status):
        self._exit_status = exit_status

    def recv_exit_status(self):
        return self._exit_status


class FakeStream:
    def __init__(self, data, exit_status=0):
        self._data = data
        self.channel = FakeChannel(exit_status)

    def read(self):
        return self._data


class FakeSSHClient:
    """Test double matching the slice of paramiko.SSHClient's interface we use."""

    def __init__(self, stdout_data=b"", stderr_data=b"", exit_status=0, raises=None):
        self._stdout_data = stdout_data
        self._stderr_data = stderr_data
        self._exit_status = exit_status
        self._raises = raises
        self.last_command = None
        self.last_timeout = None

    def exec_command(self, command, timeout=None):
        self.last_command = command
        self.last_timeout = timeout
        if self._raises is not None:
            raise self._raises
        stdout = FakeStream(self._stdout_data, self._exit_status)
        stderr = FakeStream(self._stderr_data)
        return None, stdout, stderr


def test_fetch_tailscale_status_parses_stdout_on_success():
    client = FakeSSHClient(stdout_data=b'{"BackendState": "Running"}')
    result = fetch_tailscale_status(client)
    assert result == {"BackendState": "Running"}
    assert "tailscale status --json" in client.last_command


def test_fetch_tailscale_status_raises_on_nonzero_exit():
    client = FakeSSHClient(stderr_data=b"command not found", exit_status=127)
    with pytest.raises(SSHCommandError, match="command not found"):
        fetch_tailscale_status(client)


def test_grab_screenshot_returns_raw_bytes_on_success():
    png_bytes = b"\x89PNG\r\n\x1a\nfake-image-bytes"
    client = FakeSSHClient(stdout_data=png_bytes, exit_status=0)
    result = grab_screenshot(client)
    assert result == png_bytes


def test_grab_screenshot_raises_on_nonzero_exit():
    client = FakeSSHClient(stderr_data=b"raspi2png: command not found", exit_status=127)
    with pytest.raises(SSHCommandError, match="raspi2png"):
        grab_screenshot(client)


def test_grab_screenshot_raises_on_empty_output_even_if_exit_zero():
    client = FakeSSHClient(stdout_data=b"", exit_status=0)
    with pytest.raises(SSHCommandError, match="empty"):
        grab_screenshot(client)


def test_grab_screenshot_uses_grim_with_wayland_env():
    # raspi2png depends on the dispmanx API, which the Pi 5 dropped — grim is the
    # Wayland-native replacement. It needs XDG_RUNTIME_DIR/WAYLAND_DISPLAY exported
    # since a plain SSH shell doesn't inherit the desktop session's environment.
    client = FakeSSHClient(stdout_data=b"\x89PNG fake-bytes", exit_status=0)
    grab_screenshot(client)
    assert "grim" in client.last_command
    assert "WAYLAND_DISPLAY=" in client.last_command
    assert "XDG_RUNTIME_DIR=" in client.last_command


def test_grab_screenshot_command_does_not_swallow_grim_stderr():
    # Regression: grim's own stderr must reach the SSH stderr channel so a real
    # failure (e.g. "failed to create display" when no compositor is running) is
    # readable instead of showing up as an empty error message.
    client = FakeSSHClient(stdout_data=b"\x89PNG fake-bytes", exit_status=0)
    grab_screenshot(client)
    assert "2>&1" not in client.last_command


# Regression: the truck drops off Tailscale mid-refresh. paramiko surfaces that as
# ConnectionResetError/SSHException/socket.timeout, none of which are
# SSHCommandError — so before these were funnelled, the exception escaped the
# per-command handlers in app.refresh() and became an HTTP 500.
@pytest.mark.parametrize("exc", [
    ConnectionResetError(10054, "An existing connection was forcibly closed by the remote host"),
    paramiko.SSHException("SSH session not active"),
    EOFError(),
])
def test_transport_failures_become_ssh_command_error(exc):
    client = FakeSSHClient(raises=exc)
    with pytest.raises(SSHCommandError, match="SSH connection lost"):
        fetch_health_snapshot(client)
    with pytest.raises(SSHCommandError, match="SSH connection lost"):
        grab_screenshot(client)


def test_command_timeout_becomes_ssh_command_error():
    client = FakeSSHClient(raises=socket.timeout())
    with pytest.raises(SSHCommandError, match="timed out"):
        fetch_health_snapshot(client)


def test_every_command_is_issued_with_a_timeout():
    # No timeout means a half-open link pins a waitress worker thread forever,
    # which is what backed the request queue up.
    client = FakeSSHClient(stdout_data=b"1755000000\n1755000000\n{}")
    fetch_health_snapshot(client)
    assert client.last_timeout is not None and client.last_timeout > 0

    client = FakeSSHClient(stdout_data=b"fake-png-bytes")
    grab_screenshot(client)
    assert client.last_timeout is not None and client.last_timeout > 0


def test_malformed_json_becomes_ssh_command_error():
    # A truncated read on a flaky link exits 0 with partial output; json.loads
    # would otherwise raise JSONDecodeError straight past the caller's handler.
    client = FakeSSHClient(stdout_data=b'1755000000\n1755000000\n{"pipeline": {"ali', exit_status=0)
    with pytest.raises(SSHCommandError, match="invalid output"):
        fetch_health_snapshot(client)


# --- snapshot age (staleness detection) ---------------------------------------------


def test_fetch_health_snapshot_returns_payload_and_age_from_pi_clock():
    # mtime, then the Pi's own `date +%s`, then the file. Age is computed from the Pi's
    # clock on purpose: the laptop's may be minutes off, which would fake staleness.
    client = FakeSSHClient(stdout_data=b'1755000000\n1755000042\n{"pipeline": {"ok": true}}')
    health, age_sec = fetch_health_snapshot(client)
    assert health == {"pipeline": {"ok": True}}
    assert age_sec == 42.0


def test_fetch_health_snapshot_clamps_negative_age_to_zero():
    # A write landing in the same second the Pi's clock steps backwards (NTP correction)
    # must not render as "-3s old".
    client = FakeSSHClient(stdout_data=b'1755000045\n1755000042\n{"pipeline": {}}')
    _, age_sec = fetch_health_snapshot(client)
    assert age_sec == 0.0


def test_fetch_health_snapshot_raises_when_header_lines_are_missing():
    # Old Pi image without the stat/date preamble, or a truncated read.
    client = FakeSSHClient(stdout_data=b'{"pipeline": {}}')
    with pytest.raises(SSHCommandError, match="invalid output"):
        fetch_health_snapshot(client)


def test_fetch_health_snapshot_raises_on_nonzero_exit():
    client = FakeSSHClient(stderr_data=b"No such file or directory", exit_status=1)
    with pytest.raises(SSHCommandError, match="No such file or directory"):
        fetch_health_snapshot(client)


def test_fetch_health_snapshot_reads_mtime_and_file_in_one_command():
    # One exec, not three: each round trip costs a channel over Tailscale from a truck.
    client = FakeSSHClient(stdout_data=b'1755000000\n1755000000\n{}')
    fetch_health_snapshot(client)
    assert client.last_command.count("health.json") >= 2
    assert "date +%s" in client.last_command


# --- host key trust -----------------------------------------------------------------
# AutoAddPolicy with no known_hosts file accepted whatever answered on the Pi's Tailscale
# IP, every single time, and then handed it the SSH password. Nothing in the UI would have
# shown a difference.


class FakeParamikoClient:
    def __init__(self):
        self.policy = None
        self.loaded = None
        self.saved = None
        self.connect_kwargs = None
        self.connect_raises = None

    def set_missing_host_key_policy(self, policy):
        self.policy = policy

    def load_host_keys(self, filename):
        self.loaded = filename
        # paramiko reads the file here, so a missing one raises — the real behaviour that
        # a no-op double hid: the very first connect on a new laptop crashed.
        open(filename, "r").close()

    def save_host_keys(self, filename):
        # paramiko's save_host_keys re-loads the file before writing it, so this raises
        # for a missing file too, even though it is the call meant to create it.
        self.load_host_keys(filename)
        self.saved = filename

    def connect(self, host, **kwargs):
        self.connect_kwargs = dict(kwargs, host=host)
        if self.connect_raises is not None:
            raise self.connect_raises

    def get_transport(self):
        return None


@pytest.fixture
def fake_paramiko(monkeypatch, tmp_path):
    fake = FakeParamikoClient()
    monkeypatch.setattr(ssh_client_module.paramiko, "SSHClient", lambda: fake)
    monkeypatch.setattr(ssh_client_module, "KNOWN_HOSTS_PATH", tmp_path / "known_hosts")
    return fake


def test_connect_loads_the_pinned_host_keys(fake_paramiko, tmp_path):
    ssh_client_module.connect("100.110.189.87", "rasp", "pw")
    assert fake_paramiko.loaded == str(tmp_path / "known_hosts")


def test_connect_persists_a_newly_learned_host_key(fake_paramiko, tmp_path):
    # First connect is trust-on-first-use; every one after it is pinned, which is only
    # true if the key we learned actually reached disk.
    ssh_client_module.connect("100.110.189.87", "rasp", "pw")
    assert fake_paramiko.saved == str(tmp_path / "known_hosts")


def test_connect_creates_a_missing_known_hosts_file(fake_paramiko, tmp_path):
    # First run on a new laptop. paramiko reads this file on its way to writing it, so
    # "not there yet" used to crash the connect before authentication rather than being
    # the ordinary first-use case it is.
    assert not (tmp_path / "known_hosts").exists()

    ssh_client_module.connect("100.110.189.87", "rasp", "pw")

    assert (tmp_path / "known_hosts").exists()
    assert fake_paramiko.connect_kwargs["host"] == "100.110.189.87"


def test_connect_still_works_when_host_keys_cannot_be_stored(fake_paramiko, monkeypatch):
    # A read-only or otherwise unusable location costs the pinning, not the dashboard.
    monkeypatch.setattr(ssh_client_module, "KNOWN_HOSTS_PATH",
                        pathlib.Path("/nonexistent-root-dir/known_hosts"))
    fake_paramiko.save_host_keys = lambda filename: None

    ssh_client_module.connect("100.110.189.87", "rasp", "pw")

    assert fake_paramiko.connect_kwargs["host"] == "100.110.189.87"


def test_connect_does_not_swallow_a_changed_host_key(fake_paramiko):
    # The one failure this pinning exists to produce. It must reach the operator's banner,
    # not be retried or downgraded into a generic timeout.
    fake_paramiko.connect_raises = paramiko.BadHostKeyException("host", None, None)
    with pytest.raises(paramiko.BadHostKeyException):
        ssh_client_module.connect("100.110.189.87", "rasp", "pw")


def test_tailscale_status_does_not_pull_the_peer_list():
    # Only BackendState is read. The full peer list is the bulk of that JSON and crosses
    # the link on every 30s auto-poll tick, from a truck on cellular.
    client = FakeSSHClient(stdout_data=b'{"BackendState": "Running"}')
    fetch_tailscale_status(client)
    assert "--peers=false" in client.last_command


def test_is_alive_is_false_without_a_transport():
    class NoTransport:
        def get_transport(self):
            return None

    assert ssh_client_module.is_alive(NoTransport()) is False


def test_is_alive_follows_the_transport():
    class Transport:
        def __init__(self, active):
            self._active = active

        def is_active(self):
            return self._active

    class Client:
        def __init__(self, active):
            self._t = Transport(active)

        def get_transport(self):
            return self._t

    assert ssh_client_module.is_alive(Client(True)) is True
    assert ssh_client_module.is_alive(Client(False)) is False


# --- reboot ---------------------------------------------------------------------------
# The Pi tears the SSH session down almost the instant `reboot` actually runs — often
# before the exit status, or even the rest of stderr, can be read back. That is the
# expected, successful case here, not a failure: only a rejection that arrives *before*
# the connection dies (e.g. a wrong sudo password) is a real error.


class FakeRebootChannel:
    def __init__(self, exit_status=0):
        self._exit_status = exit_status

    def recv_exit_status(self):
        return self._exit_status


class FakeRebootStream:
    def __init__(self, data=b"", exit_status=0, raises_on_read=None):
        self._data = data
        self._raises_on_read = raises_on_read
        self.channel = FakeRebootChannel(exit_status)

    def read(self):
        if self._raises_on_read is not None:
            raise self._raises_on_read
        return self._data


class FakeRebootStdin:
    def __init__(self, raises=None):
        self.written = ""
        self._raises = raises

    def write(self, data):
        if self._raises is not None:
            raise self._raises
        self.written += data

    def flush(self):
        pass


class FakeRebootClient:
    """Test double for the slice of paramiko's interface reboot_device uses: a three-stream
    exec_command result, with the stdin/stdout/stderr streams independently swappable so a
    failure can be injected at exactly the point the real connection would drop."""

    def __init__(self, stdin=None, stdout=None, stderr=None, raises=None):
        self.stdin = stdin if stdin is not None else FakeRebootStdin()
        self.stdout = stdout if stdout is not None else FakeRebootStream()
        self.stderr = stderr if stderr is not None else FakeRebootStream()
        self._raises = raises
        self.last_command = None
        self.last_timeout = None

    def exec_command(self, command, timeout=None):
        self.last_command = command
        self.last_timeout = timeout
        if self._raises is not None:
            raise self._raises
        return self.stdin, self.stdout, self.stderr


def test_reboot_device_runs_sudo_reboot():
    client = FakeRebootClient()
    reboot_device(client, "pw")
    assert client.last_command == "sudo -S reboot"


def test_reboot_device_writes_the_sudo_password_to_stdin():
    client = FakeRebootClient()
    reboot_device(client, "s3cret")
    assert client.stdin.written == "s3cret\n"


def test_reboot_device_treats_a_dropped_connection_as_success():
    stderr = FakeRebootStream(raises_on_read=EOFError())
    client = FakeRebootClient(stderr=stderr)
    reboot_device(client, "pw")  # must not raise


def test_reboot_device_treats_a_reset_connection_as_success():
    stderr = FakeRebootStream(
        raises_on_read=ConnectionResetError(10054, "forcibly closed")
    )
    client = FakeRebootClient(stderr=stderr)
    reboot_device(client, "pw")  # must not raise


def test_reboot_device_raises_when_exec_command_itself_fails():
    # The command never even reached the Pi — a genuine failure, distinct from the
    # connection dropping after `reboot` was already sent.
    client = FakeRebootClient(raises=paramiko.SSHException("no session"))
    with pytest.raises(SSHCommandError, match="SSH connection lost"):
        reboot_device(client, "pw")


def test_reboot_device_raises_on_a_rejected_sudo_password():
    # A wrong password is rejected *before* the connection drops — exit 1, sudo's own
    # message on stderr — and that must surface as a real failure, not be swallowed as
    # the expected post-reboot disconnect.
    stdout = FakeRebootStream(exit_status=1)
    stderr = FakeRebootStream(data=b"Sorry, try again.\nsudo: 1 incorrect password attempt")
    client = FakeRebootClient(stdout=stdout, stderr=stderr)
    with pytest.raises(SSHCommandError, match="incorrect password"):
        reboot_device(client, "wrong")


# --- fetch_logs ---------------------------------------------------------------------


class FakeStdin:
    def __init__(self):
        self.written = []

    def write(self, data):
        self.written.append(data)

    def flush(self):
        pass


class SequencedSSHClient:
    """Answers each exec_command from a queue, so a first attempt and a sudo retry can differ."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.commands = []
        self.stdins = []

    def exec_command(self, command, timeout=None):
        self.commands.append(command)
        out, err, status = self._responses.pop(0)
        stdin = FakeStdin()
        self.stdins.append(stdin)
        return stdin, FakeStream(out, status), FakeStream(err)


@pytest.mark.parametrize("source, fragment", [
    ("detection", "journalctl -u hailo-detection.service"),
    ("health-agent", "journalctl -u health-agent.service"),
    ("network", "journalctl -u tailscaled"),
    ("boot-prev", "journalctl -b -1"),
    ("edge", "edge_*.log"),
])
def test_fetch_logs_runs_the_fixed_command_for_each_source(source, fragment):
    client = FakeSSHClient(stdout_data=b"hello\n")

    text = ssh_client_module.fetch_logs(client, source, 50)

    assert text == "hello\n"
    assert fragment in client.last_command
    assert "-n 50" in client.last_command


def test_fetch_logs_rejects_an_unknown_source_without_touching_the_connection():
    client = FakeSSHClient()

    with pytest.raises(ValueError):
        ssh_client_module.fetch_logs(client, "rm -rf /", 50)

    assert client.last_command is None


@pytest.mark.parametrize("asked, sent", [(5000, 1000), (0, 1), (-3, 1), ("25", 25)])
def test_fetch_logs_clamps_the_line_count(asked, sent):
    client = FakeSSHClient(stdout_data=b"x")

    ssh_client_module.fetch_logs(client, "detection", asked)

    assert f"-n {sent} " in client.last_command


def test_fetch_logs_rejects_a_non_numeric_line_count():
    with pytest.raises(ValueError):
        ssh_client_module.fetch_logs(FakeSSHClient(), "detection", "lots")


def test_fetch_logs_replaces_undecodable_bytes():
    client = FakeSSHClient(stdout_data=b"ok \xff\xfe bad")

    assert "ok" in ssh_client_module.fetch_logs(client, "detection", 10)


def test_fetch_logs_keeps_the_tail_when_output_is_too_big():
    big = b"A" * (ssh_client_module.MAX_LOG_BYTES + 5000) + b"THE-END"
    client = FakeSSHClient(stdout_data=big)

    text = ssh_client_module.fetch_logs(client, "detection", 1000)

    assert text.endswith("THE-END")
    assert "truncated" in text.splitlines()[0]
    assert len(text) < len(big)


def test_fetch_logs_raises_with_stderr_on_failure():
    client = FakeSSHClient(stderr_data=b"no edge_*.log found", exit_status=1)

    with pytest.raises(SSHCommandError, match="no edge_"):
        ssh_client_module.fetch_logs(client, "edge", 10)


def test_fetch_logs_turns_a_timeout_into_ssh_command_error():
    client = FakeSSHClient(raises=socket.timeout())

    with pytest.raises(SSHCommandError, match="timed out"):
        ssh_client_module.fetch_logs(client, "detection", 10)


def test_fetch_logs_retries_with_sudo_when_the_journal_is_not_readable():
    hint = b"Hint: You are currently not seeing messages from other users and the system.\n"
    client = SequencedSSHClient((hint + b"-- No entries --\n", b"", 0), (b"real log line\n", b"", 0))

    text = ssh_client_module.fetch_logs(client, "detection", 10, password="pw")

    assert text == "real log line\n"
    assert client.commands[1].startswith("sudo -S")
    assert client.stdins[1].written == ["pw\n"]
    # The password travels over stdin, never on the command line.
    assert "pw" not in client.commands[1]


def test_fetch_logs_does_not_retry_without_a_password():
    hint = b"Hint: You are currently not seeing messages from other users and the system.\n"
    client = SequencedSSHClient((hint, b"", 0))

    text = ssh_client_module.fetch_logs(client, "detection", 10)

    assert "not seeing messages" in text
    assert len(client.commands) == 1


def test_fetch_logs_does_not_sudo_the_edge_log():
    client = SequencedSSHClient((b"", b"permission denied", 1))

    with pytest.raises(SSHCommandError):
        ssh_client_module.fetch_logs(client, "edge", 10, password="pw")

    assert len(client.commands) == 1
