"""`init` as the only writer of config.json.

Nobody authors the config by hand: `init` builds it and saves it, creating the
file and its parent directory if they are absent. The flag that names the file
goes in two directions -- a destination on `init`, a source everywhere else -- so
these tests always pass --config, which also keeps them off the real
~/.config/inventory-mcp/config.json.

The Drive half is about ordering rather than networking. A fresh machine has no
token, and `init` used to save its config only after the Drive work that needed
one, so it died leaving nothing behind while telling you to run `auth` -- which
refuses to run without a config. Nothing here contacts Google: the credential and
folder-check seams are stubbed, because the promise under test is what `init` does
and says around them.
"""

import json

import pytest

from inventory_mcp import cli
from inventory_mcp.auth import AuthError
from inventory_mcp.config import Config
from inventory_mcp.store.base import StoreError

FOLDER_ID = "1FAKEFOLDERidforTests000000000000"


class _Terminal:
    """Stands in for sys.stdin so the consent flow can be allowed or refused."""

    def __init__(self, tty):
        self._tty = tty

    def isatty(self):
        return self._tty


class _FakeService:
    def doctor(self):
        return {"ok": True, "error_count": 0, "warning_count": 0}


@pytest.fixture
def drive_seams(monkeypatch, tmp_path):
    """Stub the three things `init` asks of the outside world, recording each ask."""
    from inventory_mcp import auth, initdb
    from inventory_mcp.store import drive

    calls = {"credentials": [], "authorize": [], "verify": [], "created": []}

    def credentials(config, *, interactive=False, **kw):
        calls["credentials"].append(interactive)
        raise AuthError("no usable token")

    def authorize(config, **kw):
        calls["authorize"].append(config.drive_folder_id)
        token = tmp_path / "oauth_token.json"
        token.write_text("{}", encoding="utf-8")
        return token

    def verify_folder(config):
        calls["verify"].append(config.drive_folder_id)
        return {"id": config.drive_folder_id, "name": "Lab inventory"}

    def init_database(config, store=None, progress=None):
        calls["created"].append(config.drive_folder_id)
        calls["progress"] = progress
        return _FakeService(), ["items"]

    monkeypatch.setattr(auth, "credentials", credentials)
    monkeypatch.setattr(auth, "authorize", authorize)
    monkeypatch.setattr(drive, "verify_folder", verify_folder)
    monkeypatch.setattr(initdb, "init_database", init_database)
    return calls


def _init(config_path, root, *extra):
    return cli.main(["--config", str(config_path), "init", "--root", str(root), *extra])


def _init_drive(config_path, *extra):
    return cli.main(["--config", str(config_path), "init", "--backend", "drive", *extra])


def test_init_creates_the_config_it_was_asked_to_write(tmp_path, capsys):
    cfg_path = tmp_path / "db.config.json"
    assert _init(cfg_path, tmp_path / "db") == 0
    assert cfg_path.is_file(), "init is what creates the config; nothing pre-exists"
    cfg = Config.load(cfg_path)
    assert cfg.backend == "local"
    assert cfg.local_root == str(tmp_path / "db")
    # A fresh install has nothing being reset, so it must not be scolded; and building
    # from nothing should say so, rather than reading like an attach.
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "database         created (empty)" in captured.out


def test_rerunning_init_keeps_what_the_command_line_never_named(tmp_path, capsys):
    """`init` merges into the config instead of rebuilding it.

    Not a nicety. `drive_spreadsheet_id` is authored by hand when the workbook is not
    findable by name -- somebody else's folder, a renamed file -- and dropping it makes
    the next init resolve by name, miss, and build a second empty workbook beside the
    real one. A warning was never much protection, because the person who set the
    override is rarely the person re-running setup.
    """
    cfg_path = tmp_path / "db.config.json"
    assert _init(cfg_path, tmp_path / "db", "--actor", "you") == 0
    capsys.readouterr()

    raw = json.loads(cfg_path.read_text())
    raw.update(cache_ttl_seconds=99.0, keep_backups=3, drive_spreadsheet_id="FILEID")
    cfg_path.write_text(json.dumps(raw))

    assert _init(cfg_path, tmp_path / "db", "--actor", "you") == 0
    captured = capsys.readouterr()
    after = Config.load(cfg_path)
    assert (after.cache_ttl_seconds, after.keep_backups, after.drive_spreadsheet_id) == (
        99.0, 3, "FILEID"), "hand-authored values have to survive setup"
    assert captured.err == "", "nothing moved, so nothing to say"


def test_init_names_a_value_that_genuinely_moved(tmp_path, capsys):
    """Silent when it changes nothing, explicit when it does -- repointing a config at
    a different database is the last thing to discover afterwards."""
    cfg_path = tmp_path / "db.config.json"
    assert _init(cfg_path, tmp_path / "db", "--actor", "you") == 0
    capsys.readouterr()

    assert _init(cfg_path, tmp_path / "elsewhere", "--actor", "you") == 0
    err = capsys.readouterr().err
    assert "local_root:" in err and "->" in err, "the value it moved has to be shown"
    assert Config.load(cfg_path).local_root == str(tmp_path / "elsewhere")


def test_reset_config_discards_hand_edited_values_and_says_which(tmp_path, capsys):
    """Merging is the default; this is the opt-out, and it still itemises the damage.

    The listing matters even when the flag was typed on purpose: `drive_spreadsheet_id`
    is the field whose loss sends init looking for a workbook by name, so resetting it
    quietly can point somebody at an empty file they did not create. The person resetting
    should see it named before they go hunting for vanished rows.
    """
    cfg_path = tmp_path / "db.config.json"
    assert _init(cfg_path, tmp_path / "db", "--actor", "you") == 0
    raw = json.loads(cfg_path.read_text())
    raw.update(cache_ttl_seconds=99.0, keep_backups=3, drive_spreadsheet_id="FILEID")
    cfg_path.write_text(json.dumps(raw))
    capsys.readouterr()

    assert _init(cfg_path, tmp_path / "db", "--reset-config") == 0
    err = capsys.readouterr().err
    assert "reset the config" in err
    for field in ("cache_ttl_seconds", "keep_backups", "drive_spreadsheet_id"):
        assert field in err, f"{field} was discarded without being named"
    after = Config.load(cfg_path)
    assert (after.cache_ttl_seconds, after.keep_backups, after.drive_spreadsheet_id) == (
        5.0, 20, "")
    assert after.actor == "", "--reset-config discards the stored actor too"


def test_the_row_wiping_flag_is_named_for_what_it_wipes(tmp_path):
    """`--reset` meant the rows while `--reset-config` means the file; two flags one
    letter apart, both starting with "reset", is how somebody wipes the wrong thing."""
    cfg_path = tmp_path / "db.config.json"
    with pytest.raises(SystemExit) as e:
        cli.main(["--config", str(cfg_path), "init", "--root", str(tmp_path / "db"),
                  "--reset"])
    assert e.value.code == 2, "the old name has to fail loudly, not be accepted"
    assert not cfg_path.exists()


def test_the_destructive_flags_are_gated_behind_admin(tmp_path, capsys):
    """``--admin`` replaced a brake that lived in a commented-out assert.

    The assert protected against nothing except being in a hurry, and left edited
    checkouts behind. A flag says who decided, in the command line people paste when
    something goes wrong -- and it still has to refuse before the command touches
    anything, or the gate is a receipt rather than a barrier.
    """
    cfg_path = tmp_path / "c.json"
    root = tmp_path / "db"

    for flag in ("--seed", "--reset-db"):
        rc = cli.main(["--config", str(cfg_path), "init", "--root", str(root), flag])
        assert rc == 2, f"{flag} without --admin must not run"
        err = capsys.readouterr().err
        assert "--admin" in err, f"the refusal has to name the way through: {err}"
        assert flag in err, "and name the flag it is refusing"
    assert not cfg_path.exists(), "refused means nothing was written, config included"
    assert not root.exists(), "refused means no tables directory either"


def test_admin_lets_the_seed_run_and_says_what_it_wrote(tmp_path, capsys):
    cfg_path = tmp_path / "c.json"
    root = tmp_path / "db"
    assert cli.main(["--config", str(cfg_path), "init", "--root", str(root),
                     "--admin", "--seed"]) == 0
    out = capsys.readouterr().out
    assert "seeded" in out, "a seed that ran should report its row counts"
    assert (root / "tables" / "items.csv").exists()


def test_both_destructive_flags_are_named_together_when_both_are_missing(tmp_path, capsys):
    rc = cli.main(["--config", str(tmp_path / "c.json"), "init",
                   "--root", str(tmp_path / "db"), "--reset-db", "--seed"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "--seed and --reset-db" in captured.err, "both offenders, one refusal"
    assert "are gated" in captured.err, "plural grammar for two flags"


def test_the_gate_refuses_before_the_command_line_is_even_parsed_further(tmp_path, capsys):
    """A bad ``--root`` would be a second error; the gate has to answer first."""
    rc = cli.main(["--config", str(tmp_path / "c.json"), "init", "--seed"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "--root is required" not in err, "the gate speaks before the argument check"
    assert "--admin" in err


def test_an_unedited_config_reinitialises_quietly(tmp_path, capsys):
    cfg_path = tmp_path / "db.config.json"
    assert _init(cfg_path, tmp_path / "db", "--actor", "you") == 0
    capsys.readouterr()
    assert _init(cfg_path, tmp_path / "db", "--actor", "you") == 0
    captured = capsys.readouterr()
    assert captured.err == "", "nothing changed; say nothing"
    assert "attached (12 tables already present)" in captured.out, (
        "attaching to an existing database has to look different from creating one")
    assert "tables created   (all present)" in captured.out


def test_a_folder_reference_that_names_nothing_is_settled_before_authorisation(
        tmp_path, capsys, monkeypatch):
    """Consent cannot be asked for first -- it buys access to a folder we have not
    yet established exists -- so the reference itself has to be checked offline."""
    from inventory_mcp import auth

    def no_ask(*a, **k):
        raise AssertionError("asked for credentials over an unusable folder reference")

    monkeypatch.setattr(auth, "credentials", no_ask)
    cfg_path = tmp_path / "db.config.json"
    assert _init_drive(cfg_path, "--drive-folder", "https://drive.google.com/nonsense") == 2
    assert "--drive-folder" in capsys.readouterr().err
    assert not cfg_path.exists(), "we learned nothing about this folder; write nothing"


def test_without_a_terminal_init_leaves_a_config_that_auth_can_read(
        tmp_path, capsys, drive_seams, monkeypatch):
    """The deadlock this replaced: init blamed the missing token, died before saving
    the config, and `auth` refused to run without one. Each remedy needed the other.
    So the config has to survive the handoff, or the named command still fails."""
    monkeypatch.setattr(cli.sys, "stdin", _Terminal(tty=False))
    cfg_path = tmp_path / "db.config.json"
    rc = _init_drive(cfg_path, "--drive-folder",
                     f"https://drive.google.com/drive/u/1/folders/{FOLDER_ID}")
    captured = capsys.readouterr()
    assert rc == 2
    assert not drive_seams["created"], "unauthorised, so nothing gets created"
    assert not drive_seams["verify"], "a folder cannot be checked without credentials"
    cfg = Config.load(cfg_path)  # the file whose absence used to sink `auth`
    assert cfg.drive_folder_id == FOLDER_ID, "a pasted URL has to be stored as an id"
    assert "inventory-mcp" in captured.err and "auth" in captured.err
    assert "re-run" in captured.err, "the handoff has to say what comes after auth"


def test_with_a_terminal_init_authorises_itself_and_names_the_folder(
        tmp_path, capsys, drive_seams, monkeypatch):
    """One command on a fresh machine, which is what the quickstart promises."""
    monkeypatch.setattr(cli.sys, "stdin", _Terminal(tty=True))
    cfg_path = tmp_path / "db.config.json"
    assert _init_drive(cfg_path, "--drive-folder", FOLDER_ID) == 0
    assert drive_seams["authorize"] == [FOLDER_ID], "the consent flow ran here, not later"
    assert drive_seams["created"] == [FOLDER_ID]
    out = capsys.readouterr().out
    assert "Lab inventory" in out, "the folder's name is how you spot the wrong one"
    assert Config.load(cfg_path).backend == "drive"


def test_an_authorised_machine_is_not_asked_to_consent_again(
        tmp_path, capsys, drive_seams, monkeypatch):
    from inventory_mcp import auth

    monkeypatch.setattr(cli.sys, "stdin", _Terminal(tty=True))
    monkeypatch.setattr(auth, "credentials", lambda config, **kw: object())
    assert _init_drive(tmp_path / "db.config.json", "--drive-folder", FOLDER_ID) == 0
    assert drive_seams["authorize"] == [], "a working token is not a reason to open a browser"
    assert drive_seams["created"] == [FOLDER_ID]


def test_a_folder_that_is_not_there_stops_before_anything_is_written(
        tmp_path, capsys, drive_seams, monkeypatch):
    """A wrong id lists empty, which looks like a new database; the folder read is
    what tells those apart. Failing it must leave no config behind pointing at it."""
    from inventory_mcp.store import drive

    def missing(config):
        raise StoreError(f"no such Drive folder: {config.drive_folder_id}")

    monkeypatch.setattr(cli.sys, "stdin", _Terminal(tty=True))
    monkeypatch.setattr(drive, "verify_folder", missing)
    cfg_path = tmp_path / "db.config.json"
    assert _init_drive(cfg_path, "--drive-folder", FOLDER_ID) == 2
    assert "no such Drive folder" in capsys.readouterr().err
    assert not drive_seams["created"]
    assert not cfg_path.exists()


def test_drive_setup_names_the_table_it_is_working_on(tmp_path, capsys, drive_seams, monkeypatch):
    """Minutes of silence are what made a working command look broken.

    The lines themselves come from `init_database`; under test here is that `init`
    hands it a sink that reaches the terminal, on stderr so the report below stays
    clean to pipe, and only when the backend is slow enough for silence to mislead.
    """
    from inventory_mcp import initdb

    def chatty(config, store=None, progress=None):
        assert progress is not None, "Drive setup has to be given a progress sink"
        progress("[ 1/12] checking items")
        progress("reading the database back")
        return _FakeService(), ["items"]

    monkeypatch.setattr(cli.sys, "stdin", _Terminal(tty=True))
    monkeypatch.setattr(initdb, "init_database", chatty)
    assert _init_drive(tmp_path / "db.config.json", "--drive-folder", FOLDER_ID) == 0
    err = capsys.readouterr().err
    assert "checking items" in err and "reading the database back" in err


def test_local_setup_stays_quiet_because_it_is_fast(tmp_path, capsys, monkeypatch):
    """The absence of progress is information too: nothing is happening slowly."""
    from inventory_mcp import initdb

    seen = []

    def quiet(config, store=None, progress=None):
        seen.append(progress)
        return _FakeService(), []

    monkeypatch.setattr(initdb, "init_database", quiet)
    assert _init(tmp_path / "db.config.json", tmp_path / "db") == 0
    assert capsys.readouterr().err == ""
    assert callable(seen[0]), "the callback is still passed; it just has nothing to say"


def test_init_refuses_to_adopt_a_file_that_is_not_ours(tmp_path, capsys):
    """A file wearing a table's name is not that table.

    This was the one way setup could destroy data without a destructive flag: the name
    matched so `init` treated it as existing, its contents read as an empty table, and
    the first ordinary commit would have replaced whatever was really in there. The
    refusal happens before anything is written, so saying no costs nothing.
    """
    tables = tmp_path / "db" / "tables"
    tables.mkdir(parents=True)
    (tables / "items.csv").write_text('order,patient,notes\n47,P-9,"keep this file"\n',
                                      encoding="utf-8")
    cfg_path = tmp_path / "db.config.json"

    assert _init(cfg_path, tmp_path / "db") == 2
    err = capsys.readouterr().err
    assert "refusing to adopt" in err
    assert "items" in err and "order" in err, "name the file and show what was found instead"
    assert [p.name for p in tables.iterdir()] == ["items.csv"], "nothing else was written"
    assert not cfg_path.exists(), "a refused init leaves no config pointing here"


def test_an_export_carrying_our_id_column_is_still_refused(tmp_path, capsys):
    """The primary key alone is not enough evidence.

    A lab export with `item_id` plus its own columns shares our identity column but is
    not our table; adopting it would put the first write on top of somebody's report.
    Requiring a second agreeing column costs nothing on a real database, where tables
    share eight to fourteen columns.
    """
    tables = tmp_path / "db" / "tables"
    tables.mkdir(parents=True)
    (tables / "items.csv").write_text("item_id,their_order,their_result\nITM_1,4,+3\n",
                                      encoding="utf-8")

    assert _init(tmp_path / "db.config.json", tmp_path / "db") == 2
    assert "refusing to adopt" in capsys.readouterr().err


def test_every_offending_file_is_named_in_one_pass(tmp_path, capsys):
    """Otherwise the operator fixes one and re-runs, forever."""
    tables = tmp_path / "db" / "tables"
    tables.mkdir(parents=True)
    for name in ("items", "samples"):
        (tables / f"{name}.csv").write_text("whatever,columns\n1,2\n", encoding="utf-8")

    assert _init(tmp_path / "db.config.json", tmp_path / "db") == 2
    err = capsys.readouterr().err
    assert "2 file(s)" in err and "items:" in err and "samples:" in err


def test_an_older_schema_attaches_rather_than_being_refused(tmp_path, capsys):
    """Overlap, not equality.

    Live spreadsheets predate column renames -- `capacity_override` became `capacity` --
    and a version-tolerant check is what lets a second machine attach to a database the
    first one wrote. Refusing drift would break the main reason to run init twice.
    """
    from inventory_mcp import schema

    tables = tmp_path / "db" / "tables"
    tables.mkdir(parents=True)
    spec = schema.get_table("containers")
    old_header = [c if c != "capacity" else "capacity_override" for c in spec.column_names]
    (tables / "containers.csv").write_text(",".join(old_header) + "\n", encoding="utf-8")

    assert _init(tmp_path / "db.config.json", tmp_path / "db") == 0
    out = capsys.readouterr().out
    assert "containers" not in out.split("tables created")[1], (
        "adopted as ours, not rewritten over")
    assert "attached (1 found, 11 added)" in out
