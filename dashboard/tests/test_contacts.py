import json

import contacts


def _write(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")


def test_load_on_a_missing_file_is_empty_not_an_error(tmp_path):
    assert contacts.load_contacts(path=tmp_path / "missing.json") == []


def test_load_round_trips_a_real_file(tmp_path):
    path = tmp_path / "contacts.json"
    _write(path, [{"id": 1, "name": "Solah", "phone": "+60 111-8987234"}])

    assert contacts.load_contacts(path=path) == [
        {"id": 1, "name": "Solah", "phone": "+60 111-8987234"}
    ]


def test_load_on_a_corrupt_file_is_empty_not_an_error(tmp_path):
    path = tmp_path / "contacts.json"
    path.write_text("not json", encoding="utf-8")

    assert contacts.load_contacts(path=path) == []


def test_load_on_unexpected_json_shape_is_empty_not_an_error(tmp_path):
    path = tmp_path / "contacts.json"
    _write(path, {"not": "a list"})

    assert contacts.load_contacts(path=path) == []


def test_wa_link_strips_spaces_and_dashes():
    assert contacts.wa_link("+60 111-8987234") == "https://wa.me/601118987234"


def test_wa_link_strips_spaces_in_a_different_grouping():
    assert contacts.wa_link("+60 12-367 7957") == "https://wa.me/60123677957"
