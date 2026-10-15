"""Loading a config that is wrong, rather than merely unfamiliar.

The interesting cases are all mistakes a person makes by copying somebody else's
config.json: a backend name from before a rename, a folder reference pasted from
a browser bar, a key this version dropped. Each has to come back as a ConfigError
that names the fix, because the CLI prints that message and nothing else -- an
uncaught ValueError arrives as a traceback over a file the caller never opened.

Nothing here reaches the network or reads a machine's own config: paths are explicit
`tmp_path` arguments, and the folder id below is made up.
"""

import json

import pytest

from inventory_mcp.config import BACKENDS, Config, ConfigError, parse_drive_folder

# Synthetic, same shape as a real id (33 chars of [A-Za-z0-9_-]). Nothing here talks to
# Google -- these strings are only ever parsed -- but a live folder id in a test file
# reads like an invitation to go looking.
FOLDER_ID = "1FAKEFOLDERidforTests000000000000"


def test_a_retired_backend_says_what_it_became():
    """"drive_sheets" is not a typo, it is an old config. Say so."""
    with pytest.raises(ConfigError) as e:
        Config.from_dict({"backend": "drive_sheets", "drive_folder_id": FOLDER_ID})
    assert "renamed to 'drive'" in str(e.value)


def test_the_removed_csv_backend_points_at_the_replacement_and_the_reason():
    with pytest.raises(ConfigError) as e:
        Config.from_dict({"backend": "drive_csv", "drive_folder_id": FOLDER_ID})
    msg = str(e.value)
    assert "use 'drive'" in msg
    # The reason matters: someone who hit the conversion problem will otherwise
    # assume the new backend has it too.
    assert "converted" in msg


def test_an_unknown_backend_lists_the_options_without_inventing_history():
    with pytest.raises(ConfigError) as e:
        Config.from_dict({"backend": "dropbox", "drive_folder_id": FOLDER_ID})
    msg = str(e.value)
    for name in BACKENDS:
        assert name in msg
    assert "rename" not in msg


def test_a_folder_reference_that_is_not_one_fails_as_a_config_error(tmp_path):
    """Not a ValueError: this happens while reading a file, where nobody is waiting
    for a traceback."""
    path = tmp_path / "config.json"
    path.write_text(json.dumps(
        {"backend": "drive", "drive_folder_id": "https://drive.google.com/nonsense"}
    ), encoding="utf-8")
    with pytest.raises(ConfigError, match="drive_folder_id"):
        Config.load(path)


@pytest.mark.parametrize(
    "pasted",
    [
        f"https://drive.google.com/drive/folders/{FOLDER_ID}?usp=sharing",
        f"https://drive.google.com/drive/u/1/folders/{FOLDER_ID}",
        f"https://drive.google.com/drive/#folder/{FOLDER_ID}",
        f"https://drive.google.com/open?id={FOLDER_ID}&resourcekey=abc",
        FOLDER_ID,
    ],
)
def test_every_pasted_form_resolves_to_the_same_folder_id(pasted):
    cfg = Config.from_dict({"backend": "drive", "drive_folder_id": pasted})
    assert cfg.drive_folder_id == FOLDER_ID
    assert parse_drive_folder(pasted) == FOLDER_ID


def test_a_backend_needs_its_location():
    with pytest.raises(ConfigError, match="local_root"):
        Config.from_dict({"backend": "local"})
    with pytest.raises(ConfigError, match="drive_folder_id"):
        Config.from_dict({"backend": "drive"})


def test_unknown_keys_are_reported_but_not_written_back(tmp_path):
    """A key from a newer version should be announced, not silently kept alive in
    the file every time we re-save it."""
    cfg = Config.from_dict({"backend": "local", "local_root": "/tmp/x", "quantum_mode": True})
    assert cfg.unknown_keys == ["quantum_mode"]
    path = tmp_path / "config.json"
    cfg.save(path)
    assert "quantum_mode" not in json.loads(path.read_text())
    assert Config.load(path).backend == "local"


def test_save_and_load_round_trip(tmp_path):
    path = tmp_path / "nested" / "config.json"
    original = Config(backend="drive", drive_folder_id=FOLDER_ID, cache_ttl_seconds=2.5,
                      actor="ra@lab")
    original.save(path)
    loaded = Config.load(path)
    assert loaded.to_dict() == original.to_dict()
    assert path.stat().st_mode & 0o777 == 0o600, "credential paths live in this file"


def test_no_config_anywhere_tells_you_how_to_make_one(tmp_path):
    with pytest.raises(ConfigError, match="inventory-mcp init"):
        Config.load(tmp_path / "absent.json")
