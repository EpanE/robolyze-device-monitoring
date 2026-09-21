import pytest

import driver


@pytest.fixture
def path(tmp_path):
    return tmp_path / "driver.json"


def test_load_on_a_missing_file_is_empty_not_an_error(path):
    assert driver.load_driver(path=path) == ""


def test_save_then_load_round_trips(path):
    driver.save_driver("Alice", path=path)

    assert driver.load_driver(path=path) == "Alice"


def test_save_overwrites_the_previous_name(path):
    driver.save_driver("Alice", path=path)
    driver.save_driver("Bob", path=path)

    assert driver.load_driver(path=path) == "Bob"


def test_load_on_a_corrupt_file_is_empty_not_an_error(path):
    path.write_text("not json", encoding="utf-8")

    assert driver.load_driver(path=path) == ""


def test_load_on_unexpected_json_shape_is_empty_not_an_error(path):
    path.write_text("[1, 2, 3]", encoding="utf-8")

    assert driver.load_driver(path=path) == ""


def test_save_survives_a_write_failure(monkeypatch, path):
    # Losing the driver name is a nice-to-have failure, not one that should ever surface
    # to the caller (the request that triggered it must still succeed).
    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(driver.Path, "write_text", boom)

    driver.save_driver("Alice", path=path)  # must not raise
