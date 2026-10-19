"""Which credential file answers, what ships inside the package, and how a
hand-relayed consent flow is completed.

Nothing here reaches the network. The guarantees worth pinning are that a fresh
install can authorise at all, that the shipped client is a client rather than
somebody's token, that a caller with no terminal is refused instead of left waiting
for a browser it will never get, and that the paste coming back from a browser on
another machine is read for what it says -- including when what it says is "denied".
"""

import json
from pathlib import Path

import pytest

from inventory_mcp.auth import (
    PACKAGED_CLIENT_SECRET,
    SCOPES,
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
    """`serve` owns stdin; blocking on browser consent there looks like a hang.

    Needs the OAuth stack to exist at all -- without the `drive` extra the refusal is
    the install hint instead, which is a different promise and a passing one.
    """
    pytest.importorskip("google.oauth2.credentials", reason="needs the 'drive' extra")
    cfg = Config(backend="drive", drive_folder_id="FOLDER",
                 token_path=str(tmp_path / "no-token.json"))
    with pytest.raises(AuthError, match="inventory-mcp auth"):
        credentials(cfg, interactive=False)


# ---------------------------------------------------------------------------
# the hand-relayed consent flow (`auth --manual`, and init's inline equivalent)
# ---------------------------------------------------------------------------

STATE = "STATE-of-this-attempt"
CODE = "4/0AXlqoi78_okUBLORmv-X2aCY0bz423H7yVuSURaqjnO65CVwuRvlyWyJU-OiGGbWN4ca7w"
#: What the browser's address bar holds after approving: a loopback address that
#: will not load, carrying the code. Note the trailing `scope` -- real redirects
#: contain other URLs inside the query, which rules out naive string surgery.
PASTED = (f"http://localhost:8080/?state={STATE}"
          "&iss=https://accounts.google.com"
          f"&code={CODE}&scope=https://www.googleapis.com/auth/drive")


class _FakeFlow:
    """Stands in for InstalledAppFlow, recording how the exchange was requested."""

    last = None
    requested_scopes = None

    def __init__(self):
        self.redirect_uri = ""
        self.asks = []
        _FakeFlow.last = self

    @classmethod
    def from_client_secrets_file(cls, path, scopes):
        # What the consent screen would offer. The whole point of --admin is that this
        # differs between an ordinary run and a setup run, so it has to be observable.
        _FakeFlow.requested_scopes = list(scopes)
        return cls()

    def authorization_url(self, **kwargs):
        url = ("https://accounts.google.com/o/oauth2/auth?response_type=code"
               f"&redirect_uri=http%3A%2F%2Flocalhost%3A8080%2F&state={STATE}")
        return url, STATE

    def fetch_token(self, **kwargs):
        self.asks.append(kwargs)

    @property
    def credentials(self):
        return type("Creds", (), {"valid": True})()


def _manual(monkeypatch, pasted):
    from inventory_mcp import auth

    monkeypatch.setattr("builtins.input", lambda prompt="": pasted)
    return auth._run_flow(_FakeFlow, Path("unused-client-secret.json"),
                          manual=True, port=8080)


def test_the_pasted_redirect_is_exchanged_by_code(monkeypatch):
    """The whole URL gets parsed, but only the code is handed to the exchange.

    oauthlib refuses an ``http://`` authorization response outright -- it checks the
    scheme of whatever URI it parses, and a loopback redirect is http by definition
    -- so passing `authorization_response=` failed here before any request left the
    machine, blaming the operator's paste for a library policy. The state still has
    to be honoured, because it is what ties the paste to this attempt.
    """
    creds = _manual(monkeypatch, PASTED)
    assert _FakeFlow.last.asks == [{"code": CODE}], (
        "the exchange has to be asked for by code; the redirect URL is not passable")
    assert creds.valid


def test_oauthlib_still_refuses_an_http_redirect_url():
    """Pinned so the workaround above can be deleted the day it is unnecessary."""
    pytest.importorskip("google_auth_oauthlib.flow", reason="needs the 'drive' extra")
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(PACKAGED_CLIENT_SECRET), SCOPES)
    flow.redirect_uri = "http://localhost:8080/"
    _, state = flow.authorization_url(prompt="consent")
    with pytest.raises(Exception) as excinfo:
        flow.fetch_token(authorization_response=f"http://localhost:8080/?code=x&state={state}")
    assert "insecure_transport" in str(excinfo.value)


def test_exchanging_by_code_still_names_the_redirect_uri():
    """Google matches the token request's ``redirect_uri`` against the one in the
    authorization request, so dropping the URL must not drop the URI. Checked against
    the real library because the session and the client hold it separately, and a
    version that stopped threading it through would fail at Google with a message
    about a mismatch nobody could diagnose from here."""
    pytest.importorskip("google_auth_oauthlib.flow", reason="needs the 'drive' extra")
    from urllib.parse import parse_qs

    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(PACKAGED_CLIENT_SECRET), SCOPES)
    flow.redirect_uri = "http://localhost:8080/"
    body = flow.oauth2session._client.prepare_request_body(
        code="whatever", body="", redirect_uri=flow.oauth2session.redirect_uri,
        include_client_id=False, client_secret="x", code_verifier="v")
    assert parse_qs(body).get("redirect_uri") == ["http://localhost:8080/"]


def test_a_declined_consent_is_reported_as_such(monkeypatch):
    """Clicking Deny produces a redirect too, and it is not a malformed paste."""
    denied = ("http://localhost:8080/?error=access_denied"
              "&error_description=The+user+canceled+the+access+request"
              f"&state={STATE}")
    with pytest.raises(AuthError) as excinfo:
        _manual(monkeypatch, denied)
    assert "access_denied" in str(excinfo.value)
    assert "canceled" in str(excinfo.value)
    assert _FakeFlow.last.asks == [], "nothing to exchange, so nothing was exchanged"


def test_a_state_from_another_attempt_is_refused(monkeypatch):
    """Two `auth --manual` runs open at once is easy to do; their codes are not
    interchangeable, because each is bound to its own PKCE verifier."""
    stale = PASTED.replace(STATE, "STATE-of-a-different-run")
    with pytest.raises(AuthError, match="state") as excinfo:
        _manual(monkeypatch, stale)
    assert "PKCE" in str(excinfo.value)
    assert _FakeFlow.last.asks == []


def test_pasting_the_link_we_printed_is_not_mistaken_for_an_answer(monkeypatch):
    """The printed link and the answer are both long URLs copied from a browser,
    so mixing them up is the likeliest mistake. Say which one came back."""
    printed = ("https://accounts.google.com/o/oauth2/auth?response_type=code"
               f"&client_id=whoever&redirect_uri=http%3A%2F%2Flocalhost%3A8080%2F&state={STATE}")
    with pytest.raises(AuthError) as excinfo:
        _manual(monkeypatch, printed)
    assert "no authorization code" in str(excinfo.value)
    assert "8080" in str(excinfo.value), "say what the answer is supposed to look like"


def test_a_bare_code_is_enough(monkeypatch):
    """Some browsers refuse to show an unloadable page's address bar at all, leaving
    the code as the only thing a person can copy."""
    _manual(monkeypatch, CODE)
    assert _FakeFlow.last.asks == [{"code": CODE}]


def test_a_refused_exchange_blames_google_not_the_paste(monkeypatch):
    """Once the paste parses, a failure is the server's answer -- most often an
    expired code. Pointing at the paste again would send them in circles."""
    from inventory_mcp import auth

    class Expired(_FakeFlow):
        def fetch_token(self, **kwargs):
            self.asks.append(kwargs)
            raise RuntimeError("invalid_grant: code_expired")

    monkeypatch.setattr("builtins.input", lambda prompt="": PASTED)
    with pytest.raises(AuthError) as excinfo:
        auth._run_flow(Expired, Path("unused.json"), manual=True, port=8080)
    assert "Google refused" in str(excinfo.value)
    assert "code_expired" in str(excinfo.value), "the server's reason has to survive"
    assert "pasted the whole" not in str(excinfo.value), (
        "the paste was complete and correct; the old wording sent people back to it")


# ---------------------------------------------------------------------------
# the setup-only scopes
# ---------------------------------------------------------------------------


def _token_file(tmp_path, scopes):
    """A stored grant, so the code under test reads what a consent actually left.

    The expiry has to be in the future: google-auth treats a token with no expiry as
    expired, which sends ``credentials()`` off to refresh against a fake client id and
    fails for reasons that have nothing to do with the scope being tested.
    """
    path = tmp_path / "oauth_token.json"
    path.write_text(json.dumps({
        "token": "ya29.fake-access-token",
        "refresh_token": "fake-refresh",
        "client_id": "client.apps.googleusercontent.com",
        "client_secret": "secret",
        "expiry": "2999-01-01T00:00:00Z",
        "scopes": list(scopes),
        "type": "authorized_user",
    }), encoding="utf-8")
    return path


def _drive_config(token_path):
    return Config(backend="drive", drive_folder_id="FOLDER",
                  token_path=str(token_path), service_account_path="")


def test_using_a_database_never_asks_for_apps_script():
    """The default list is what every bench person sees on their consent screen.

    Apps Script write access there would be a second permission offered to people who
    have no way to use it -- and on an unverified app each extra box is another reason
    for Google's interstitial to matter.
    """
    from inventory_mcp.auth import ADMIN_SCOPES, scopes_for

    assert scopes_for() == SCOPES
    assert not set(SCOPES) & set(ADMIN_SCOPES)


def test_admin_adds_the_setup_scopes_and_keeps_the_everyday_ones():
    from inventory_mcp.auth import ADMIN_SCOPES, scopes_for

    assert scopes_for(admin=True) == SCOPES + ADMIN_SCOPES, (
        "an admin grant replaces the Drive scope rather than adding to it, which would "
        "break every ordinary command afterwards")


def test_the_flow_asks_google_for_exactly_what_the_flag_asked_for(monkeypatch):
    """The flag has to reach the consent URL, not just the local scope check."""
    from inventory_mcp import auth
    from inventory_mcp.auth import ADMIN_SCOPES, scopes_for

    monkeypatch.setattr("builtins.input", lambda prompt="": PASTED)

    auth._run_flow(_FakeFlow, Path("unused.json"), manual=True, port=8080,
                   scopes=scopes_for())
    plain = list(_FakeFlow.requested_scopes)
    assert ADMIN_SCOPES[0] not in plain

    auth._run_flow(_FakeFlow, Path("unused.json"), manual=True, port=8080,
                   scopes=scopes_for(admin=True))
    assert _FakeFlow.requested_scopes == plain + ADMIN_SCOPES, (
        "an admin run asks for more, and asks for it on top of the everyday scope")


def test_a_drive_only_token_is_refused_for_setup_and_names_the_command(tmp_path):
    """Google's own answer here is "insufficient authentication scopes" about a token
    that is perfectly valid -- useless to anyone reading it. Catch it locally."""
    from inventory_mcp.auth import ADMIN_SCOPES, SCOPES as base

    cfg = _drive_config(_token_file(tmp_path, base))
    with pytest.raises(AuthError) as excinfo:
        credentials(cfg, interactive=False, admin=True)
    message = str(excinfo.value)
    assert ADMIN_SCOPES[0] in message, "name the permission that is missing"
    assert "--admin" in message and "--force" in message, (
        "and the exact re-run, because consent cannot be widened in place")


def test_an_ordinary_call_still_works_on_a_wider_token(tmp_path):
    """The point of additive grants: getting the setup scope must not cost somebody
    their everyday access."""
    from inventory_mcp.auth import ADMIN_SCOPES, SCOPES as base

    cfg = _drive_config(_token_file(tmp_path, base + ADMIN_SCOPES))
    creds = credentials(cfg, interactive=False)
    assert creds.valid


def test_granted_scopes_reads_the_file_because_the_object_cannot_be_trusted(tmp_path):
    """The trap this avoids, pinned so nobody "simplifies" it back.

    ``Credentials.from_authorized_user_file(path, scopes)`` assigns the scopes *you
    passed in* onto the credential. Asking it for the Drive scope therefore hands back
    an object claiming the Drive scope whatever was consented to -- comparing that with
    what we asked for compares a value with itself and always passes.
    """
    from google.oauth2.credentials import Credentials

    from inventory_mcp.auth import ADMIN_SCOPES, SCOPES as base, granted_scopes, recorded_scopes

    path = _token_file(tmp_path, base)
    lying = Credentials.from_authorized_user_file(str(path), [ADMIN_SCOPES[0]])
    assert lying.scopes == [ADMIN_SCOPES[0]], (
        "if this ever stops being true, the file-reading below can be deleted")

    cfg = _drive_config(path)
    assert granted_scopes(cfg) == sorted(base)
    assert recorded_scopes(path) == list(base)


def test_a_token_that_records_nothing_is_not_guessed_at(tmp_path):
    """An older or hand-written file may carry no scopes. Asserting a gap there would
    refuse a working token; Google gets to be the authority instead."""
    from inventory_mcp.auth import recorded_scopes

    path = tmp_path / "oauth_token.json"
    path.write_text(json.dumps({"token": "t", "refresh_token": "r",
                                "client_id": "c", "client_secret": "s",
                                "expiry": "2999-01-01T00:00:00Z",
                                "type": "authorized_user"}), encoding="utf-8")
    assert recorded_scopes(path) is None
    # ... and an admin call against it proceeds rather than inventing a refusal.
    creds = credentials(_drive_config(path), interactive=False, admin=True)
    assert creds.valid


def test_granted_scopes_of_a_missing_file_is_nothing_to_report(tmp_path):
    from inventory_mcp.auth import granted_scopes

    assert granted_scopes(_drive_config(tmp_path / "absent.json")) == []


def test_a_refresh_during_an_ordinary_command_keeps_the_wide_grant(tmp_path, monkeypatch):
    """The write direction of the lying-scopes trap.

    ``from_authorized_user_file(path, wanted)`` stamps *wanted* onto the credential, so a
    command that has to refresh would save back a scope list describing only what it asked
    for. An admin grant would then evaporate from the record on any day the access token
    happened to be stale, and the next setup run would demand a consent nobody revoked --
    which is exactly how a working wide token came to look narrow here.
    """
    from inventory_mcp import auth
    from inventory_mcp.auth import ADMIN_SCOPES, SCOPES as base, recorded_scopes

    path = tmp_path / "oauth_token.json"
    path.write_text(json.dumps({
        "token": "ya29.stale", "refresh_token": "fake-refresh",
        "client_id": "c", "client_secret": "s",
        "expiry": "2000-01-01T00:00:00Z",          # expired, so a refresh is forced
        "scopes": sorted(base + ADMIN_SCOPES), "type": "authorized_user",
    }), encoding="utf-8")

    class Refreshed:
        """A credential that reports back whatever scopes it was handed at load."""

        def __init__(self, scopes):
            self.scopes = list(scopes)
            self.refresh_token = "fake-refresh"
            self.expired = True
            self.valid = False

        @classmethod
        def from_authorized_user_file(cls, path, scopes):
            return cls(scopes)

        def refresh(self, request):
            self.expired = False
            self.valid = True

        def to_json(self):
            return json.dumps({"token": "ya29.fresh", "refresh_token": self.refresh_token,
                               "client_id": "c", "client_secret": "s",
                               "expiry": "2999-01-01T00:00:00Z",
                               "scopes": self.scopes, "type": "authorized_user"})

    monkeypatch.setattr(auth, "_require_oauth", lambda: (object, Refreshed, object))

    creds = credentials(_drive_config(path), interactive=False)  # an ordinary, non-admin call
    assert creds.valid
    assert recorded_scopes(path) == sorted(base + ADMIN_SCOPES), (
        "the record has to outlive a refresh that only ever knew about Drive")


def test_a_narrow_record_is_checked_against_the_server_before_refusing(tmp_path, monkeypatch):
    """The record can be narrower than the grant behind it, and refusing on that alone
    sends somebody to a browser to re-approve something they already approved.

    This is the repair path for exactly the state this machine got into: the token could
    still mint ``script.projects`` -- proven against Google directly -- while the file said
    Drive only, because the library serialises what was asked for rather than what came back.
    """
    from inventory_mcp import auth
    from inventory_mcp.auth import ADMIN_SCOPES, SCOPES as base, recorded_scopes

    path = tmp_path / "oauth_token.json"
    path.write_text(json.dumps({
        "token": "ya29.fresh-enough", "refresh_token": "fake-refresh",
        "client_id": "c", "client_secret": "s",
        "expiry": "2999-01-01T00:00:00Z",          # valid, so nothing refreshes on its own
        "scopes": list(base),                      # the damaged, too-narrow record
        "type": "authorized_user",
    }), encoding="utf-8")

    class Probed:
        """Loaded with whatever was asked; the server's reply arrives on refresh."""

        def __init__(self, scopes):
            self.scopes = list(scopes)
            self.granted_scopes = None
            self.refresh_token = "fake-refresh"
            self.expiry = None
            self.valid = True
            self.expired = False

        @classmethod
        def from_authorized_user_file(cls, path, scopes):
            return cls(scopes)

        def refresh(self, request):
            self.granted_scopes = sorted(base + ADMIN_SCOPES)   # what Google actually honours
            self.valid = True
            self.expired = False

        def to_json(self):
            return json.dumps({"token": "ya29.minted", "refresh_token": self.refresh_token,
                               "client_id": "c", "client_secret": "s",
                               "expiry": "2999-01-01T00:00:00Z",
                               "scopes": self.scopes, "type": "authorized_user"})

    monkeypatch.setattr(auth, "_require_oauth", lambda: (object, Probed, object))

    creds = credentials(_drive_config(path), interactive=False, admin=True)
    assert creds.valid, "no fresh consent was needed; the grant was there all along"
    assert recorded_scopes(path) == sorted(base + ADMIN_SCOPES), (
        "and the record gets repaired on the way past, not just forgiven")
