"""`init` as the only writer of config.json.

Nobody authors the config by hand: `init` builds it and saves it, creating the
file and its parent directory if they are absent. The flag that names the file
goes in two directions -- a destination on `init`, a source everywhere else -- so
these tests always pass --config, which also keeps them off the real
~/.config/inventory-mcp/config.json.
"""

import json

from inventory_mcp import cli
from inventory_mcp.config import Config


def _init(config_path, root, *extra):
    return cli.main(["--config", str(config_path), "init", "--root", str(root), *extra])


def test_init_creates_the_config_it_was_asked_to_write(tmp_path, capsys):
    cfg_path = tmp_path / "db.config.json"
    assert _init(cfg_path, tmp_path / "db") == 0
    assert cfg_path.is_file(), "init is what creates the config; nothing pre-exists"
    cfg = Config.load(cfg_path)
    assert cfg.backend == "local"
    assert cfg.local_root == str(tmp_path / "db")
    # A fresh install has nothing being reset, so it must not be scolded.
    assert "rewrites the config" not in capsys.readouterr().err


def test_rerunning_init_says_which_edited_values_it_resets(tmp_path, capsys):
    """The whole file is rewritten from the command line, so anything not named
    there falls back to its default. Loudly, because `drive_file_ids` is written by
    nobody but a person."""
    cfg_path = tmp_path / "db.config.json"
    assert _init(cfg_path, tmp_path / "db", "--actor", "you") == 0
    capsys.readouterr()

    raw = json.loads(cfg_path.read_text())
    raw.update(cache_ttl_seconds=99.0, keep_backups=3, drive_file_ids={"items": "FILEID"})
    cfg_path.write_text(json.dumps(raw))

    assert _init(cfg_path, tmp_path / "db", "--actor", "you") == 0
    err = capsys.readouterr().err
    assert "rewrites the config" in err
    for field in ("cache_ttl_seconds", "keep_backups", "drive_file_ids"):
        assert field in err, f"{field} was dropped without being named"
    assert "actor" not in err, "--actor was given, so it is not a reset"

    after = Config.load(cfg_path)
    assert (after.cache_ttl_seconds, after.keep_backups, after.drive_file_ids) == (5.0, 20, {})


def test_an_unedited_config_reinitialises_quietly(tmp_path, capsys):
    cfg_path = tmp_path / "db.config.json"
    assert _init(cfg_path, tmp_path / "db", "--actor", "you") == 0
    capsys.readouterr()
    assert _init(cfg_path, tmp_path / "db", "--actor", "you") == 0
    captured = capsys.readouterr()
    assert "rewrites the config" not in captured.err, "nothing changed; say nothing"
    assert "tables created   (all present)" in captured.out
