"""Which credential file answers, and what ships inside the package.

Nothing here reaches the network. The guarantees worth pinning are that a fresh
install can authorise at all, that the shipped client is a client rather than
somebody's token, and that a caller with no terminal is refused instead of left
waiting for a browser it will never get.
"""

import json

import pytest

from inventory_mcp.auth import (
    PACKAGED_CLIENT_SECRET,
    AuthError,
    credentials,
    resolve_client_secret,
)
from inventory_mcp.config import Config


def test_packaged_client_ships_with_the_package():
    assert PACKAGED_CLIENT_SECRET.is_file(), (
        "the installed-app client is gone from the package; without it nobody can "
        "start the consent flow, which is the only way a token comes to exist"
    )


def test_packaged_client_is_a_client_and_not_someone_s_token():
    inner = json.loads(PACKAGED_CLIENT_SECRET.read_text())["installed"]
    for field in ("client_id", "client_secret", "auth_uri", "token_uri", "redirect_uris"):
        assert inner.get(field), f"{field} missing from the packaged client"
    # Swapping a token in here would hand every install that user's Drive access,
    # and nothing in a JSON diff would look wrong. Refuse the shape outright.
    for private in ("refresh_token", "token", "expiry", "account"):
        assert private not in inner, f"packaged client carries {private!r}: that is a token"


def test_a_configured_copy_wins_over_the_packaged_one(tmp_path):
    mine = tmp_path / "client_secret.json"
    mine.write_text(json.dumps({"installed": {"client_id": "whoever"}}), encoding="utf-8")
    cfg = Config(client_secret_path=str(mine))
    assert resolve_client_secret(cfg) == mine


def test_unset_or_missing_path_falls_back_to_the_package(tmp_path):
    assert resolve_client_secret(
        Config(client_secret_path=str(tmp_path / "absent.json"))
    ) == PACKAGED_CLIENT_SECRET
    # Path("") is the working directory, which exists on every machine. An unset
    # path has to reach the package, not be read as a file that is there.
    assert resolve_client_secret(Config(client_secret_path="")) == PACKAGED_CLIENT_SECRET
    # A directory is not a client secret either.
    assert resolve_client_secret(Config(client_secret_path=str(tmp_path))) == PACKAGED_CLIENT_SECRET


def test_no_client_anywhere_names_both_places_it_looked(tmp_path, monkeypatch):
    """Only reachable when the packaged copy is gone, so that is what is faked."""
    from inventory_mcp import auth

    monkeypatch.setattr(auth, "PACKAGED_CLIENT_SECRET", tmp_path / "packaged-absent.json")
    cfg = Config(backend="drive", drive_folder_id="FOLDER",
                 client_secret_path=str(tmp_path / "absent.json"),
                 token_path=str(tmp_path / "no-token.json"))
    with pytest.raises(AuthError) as e:
        credentials(cfg, interactive=False)
    assert str(tmp_path / "absent.json") in str(e.value)
    assert "inventory-mcp auth" in str(e.value)


def test_a_non_interactive_caller_is_refused_not_hung(tmp_path):
    """`serve` owns stdin; blocking on browser consent there looks like a hang."""
    cfg = Config(backend="drive", drive_folder_id="FOLDER",
                 token_path=str(tmp_path / "no-token.json"))
    with pytest.raises(AuthError, match="inventory-mcp auth"):
        credentials(cfg, interactive=False)
