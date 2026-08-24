import pytest

import config


def test_port_parses_a_normal_value():
    assert config.parse_port("8080") == 8080


def test_blank_port_falls_back_to_the_default():
    assert config.parse_port("") == config.DEFAULT_PORT
    assert config.parse_port(None) == config.DEFAULT_PORT


def test_junk_port_raises_a_readable_error_naming_the_env_file():
    # Previously a bare int() ValueError traceback at import time, which reads as a crash
    # rather than "you typo'd .env".
    with pytest.raises(config.ConfigError) as excinfo:
        config.parse_port("five thousand")

    message = str(excinfo.value)
    assert "PORT" in message
    assert "five thousand" in message
    assert ".env" in message


def test_missing_keys_lists_the_env_names_the_operator_must_set():
    assert config.missing_keys(host="", user="", password="") == [
        "TAILSCALE_IP_ADDRESS", "USERNAME", "SSH_PASSWORD",
    ]


def test_missing_keys_empty_when_all_present():
    assert config.missing_keys(host="100.1.1.1", user="rasp", password="hunter2") == []


def test_validate_raises_naming_every_missing_key(monkeypatch):
    monkeypatch.setattr(config, "SSH_HOST", "")
    monkeypatch.setattr(config, "SSH_USER", "rasp")
    monkeypatch.setattr(config, "SSH_PASSWORD", "")

    with pytest.raises(config.ConfigError) as excinfo:
        config.validate()

    message = str(excinfo.value)
    assert "TAILSCALE_IP_ADDRESS" in message
    assert "SSH_PASSWORD" in message
    assert "USERNAME" not in message


def test_a_junk_port_does_not_explode_at_import_time(monkeypatch):
    # Regression: PORT was parsed at module scope, so a typo in .env raised during
    # `import config` — before __main__'s ConfigError handler could turn it into a
    # readable message, and while pytest was still collecting.
    monkeypatch.setattr(config, "PORT", config.PORT)
    monkeypatch.setattr(config, "_PORT_RAW", "five thousand")

    with pytest.raises(config.ConfigError) as excinfo:
        config.validate()

    assert "PORT" in str(excinfo.value)
    assert ".env" in str(excinfo.value)


def test_validate_publishes_the_parsed_port(monkeypatch):
    monkeypatch.setattr(config, "PORT", config.PORT)
    monkeypatch.setattr(config, "_PORT_RAW", "8081")
    monkeypatch.setattr(config, "SSH_HOST", "100.1.1.1")
    monkeypatch.setattr(config, "SSH_USER", "rasp")
    monkeypatch.setattr(config, "SSH_PASSWORD", "hunter2")

    config.validate()

    assert config.PORT == 8081


def test_host_defaults_to_loopback():
    # Reaching HOST:PORT is enough to open an SSH session to the truck, so exposure to the
    # LAN is a decision the operator makes in .env, not the default they inherit.
    assert config.load_host(env={}) == "127.0.0.1"


def test_host_can_be_opened_up_explicitly():
    assert config.load_host(env={"HOST": "0.0.0.0"}) == "0.0.0.0"
