"""Google credentials for the Drive backends.

Two facts shape this module.

The MCP server speaks stdio, so stdin belongs to the protocol and nothing here
may block waiting for a human. Authorisation is therefore a separate command run
beforehand; the server only ever loads or refreshes a token that already exists,
and says so plainly when there isn't one.

And the browser is frequently not on the same machine as the process -- an RA on
a lab workstation, a job running on a compute node over ssh. The library's own
console flow was removed when Google retired the out-of-band redirect, so the
headless path here relays the redirect URL by hand: the user opens the URL
anywhere, gets redirected to a loopback address that fails to load on their end,
and pastes the address bar back. The code in that URL is exchanged locally, which
means it never passes through a third-party relay of ours -- there isn't one.

Tokens live on the machine that earned them, at 0600, and are never read into the
database files or the exports.
"""

from __future__ import annotations

import os
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib import parse as urlparse

#: Full Drive scope. The narrower ``drive.file`` only covers files this app
#: created itself, which is useless for pointing at a folder that already exists;
#: restricting writes to that folder is our job, not the token's.
SCOPES = ["https://www.googleapis.com/auth/drive"]

#: Setup-only permissions, requested only by a command run with ``--admin``.
#:
#: ``script.projects`` lets ``ui --install`` write the Apps Script project bound to a
#: workbook, which is the only way a button gets anything to bind to. No everyday
#: action needs it, and it is not the permission the *sheet* asks for either: that is
#: a separate consent each editor gives when they first click the menu, and it stays
#: at ``spreadsheets.currentonly``. Keeping this out of the default list is what stops
#: a bench person's consent screen from offering Google an Apps Script write permission
#: they have no possible use for.
ADMIN_SCOPES = ["https://www.googleapis.com/auth/script.projects"]


def scopes_for(admin: bool = False) -> list[str]:
    """The scopes to ask for. Admin grants are additive, so one token can carry both."""
    return list(SCOPES) + (list(ADMIN_SCOPES) if admin else [])


def missing_scopes(granted, wanted) -> list[str]:
    """Which of ``wanted`` this grant does not cover.

    A stored token records what a human actually consented to, and Google answers a
    call outside that set with ``ACCESS_TOKEN_SCOPE_INSUFFICIENT`` -- an error naming
    neither the token file nor the fix, because as far as it is concerned the token is
    perfectly valid. Comparing the two here is what turns that into "re-run auth".
    """
    have = {str(scope).strip() for scope in (granted or [])}
    return [scope for scope in wanted if scope not in have]


def recorded_scopes(path: Path) -> Optional[list[str]]:
    """The scopes a saved token file says were consented to; None if it does not say.

    This has to come from the file rather than from a ``Credentials`` object:
    ``from_authorized_user_file`` puts the scopes *you passed in* onto the object, so
    asking it for the Drive scope hands back a credential claiming the Drive scope no
    matter what was ever consented to. Checking that against what we asked for would
    compare a value with itself and always pass.

    None means an older or hand-written file with no record. Nothing is asserted about
    it -- Google gets to be the authority, and its answer arrives at the call site.
    """
    import json

    try:
        info = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(info, dict) or "scopes" not in info:
        return None
    scopes = info.get("scopes") or []
    return [str(scope).strip() for scope in scopes] if isinstance(scopes, list) else []


def _scope_error(gaps: list[str]) -> AuthError:
    """Built when raised, so it reflects the flags as they are now."""
    admin = " --admin" if any(gap in ADMIN_SCOPES for gap in gaps) else ""
    return AuthError(
        f"the stored token does not cover: {', '.join(gaps)}.\n"
        "A consent cannot be widened in place, it has to be re-given. Run once:\n"
        f"  inventory-mcp auth{admin} --force"
    )


class AuthError(RuntimeError):
    pass


_INSTALL_HINT = (
    "Drive support needs the 'drive' extra:  pip install -e '.[drive]'"
)

#: The lab's own OAuth client, shipped inside the package. An installed-app client
#: secret is not something to keep confidential (RFC 8252 treats it as distributed
#: with the binary): it authorises nothing on its own — a human still has to approve
#: the consent screen — and without it nobody can start the flow that produces a
#: token. What stays private is the token, which lives on the machine that earned it.
PACKAGED_CLIENT_SECRET = Path(__file__).resolve().with_name("client_secret.json")


def resolve_client_secret(config) -> Path:
    """Where the installed-app client JSON actually comes from.

    A copy at the configured path wins, so anyone who registered their own client
    keeps using theirs; otherwise the packaged one answers, which leaves a fresh
    install able to run ``auth`` with no setup step at all.
    """
    raw = str(getattr(config, "client_secret_path", "") or "").strip()
    if raw:
        configured = Path(raw).expanduser()
        # is_file, not exists: an unset path arrives as "" and Path("") is the
        # working directory, which exists and is not a client secret.
        if configured.is_file():
            return configured
    return PACKAGED_CLIENT_SECRET


def _missing_client_error(config=None) -> AuthError:
    """Built when raised, so it names the paths as they are now -- including the
    configured one, which is the likelier place to have a typo."""
    configured = str(getattr(config, "client_secret_path", "") or "").strip()
    looked = ([f"the configured path {configured}"] if configured else []) + \
             [f"the packaged copy {PACKAGED_CLIENT_SECRET}"]
    return AuthError(
        f"no OAuth client secret found; looked in {' and '.join(looked)}.\n"
        "Create a 'Desktop app' OAuth client in Google Cloud Console with the Drive and "
        "Sheets APIs enabled, download the JSON, and save it at either path. "
        "Then run:  inventory-mcp auth"
    )


def _require_oauth():
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise AuthError(_INSTALL_HINT) from exc
    return Request, Credentials, InstalledAppFlow


# ---------------------------------------------------------------------------
# obtaining credentials
# ---------------------------------------------------------------------------


def credentials(config, *, interactive: bool = False, manual: bool = False,
                port: int = 8080, force: bool = False, admin: bool = False):
    """Return usable Google credentials for ``config``.

    Order of preference: a service-account key if one is configured, otherwise a
    cached OAuth token, otherwise -- only when ``interactive`` -- run the flow.
    Non-interactive callers get an actionable error instead of a hang, because
    they are usually the stdio server with no terminal to ask in.

    ``admin`` asks for the setup-only scopes as well. It changes nothing about what
    the returned credentials may do beyond what was consented to; it is a request, and
    a token minted without the flag simply fails the check below with the command that
    would fix it.
    """
    sa = getattr(config, "service_account_path", "") or ""
    if sa and Path(sa).expanduser().exists():
        return _service_account_credentials(sa)

    secret = resolve_client_secret(config)
    if not secret.is_file():
        raise _missing_client_error(config)

    Request, Credentials, InstalledAppFlow = _require_oauth()
    token = Path(config.token_path).expanduser()
    wanted = scopes_for(admin)

    if not force and token.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token), wanted)
        except (ValueError, OSError) as exc:
            raise AuthError(
                f"token file {token} is unreadable ({exc}); re-run:  inventory-mcp auth --force"
            ) from exc
        if creds.valid or (creds.expired and creds.refresh_token):
            if creds.expired and creds.refresh_token:
                # Read before the refresh writes: this is the last moment the file still
                # says what the human actually consented to.
                consented = recorded_scopes(token)
                try:
                    creds.refresh(Request())
                except Exception as exc:  # google.auth.exceptions.RefreshError et al
                    raise AuthError(
                        f"stored token could not be refreshed ({exc}).\n"
                        "Usually the consent was revoked, or the app is still in "
                        "Testing and its refresh tokens expired. Re-run:  "
                        "inventory-mcp auth --force"
                    ) from exc
                _save_token(token, _refreshed_payload(creds, consented))
            recorded = recorded_scopes(token)
            # None means the file records nothing about its own grant -- an older write,
            # or one a person made by hand. Inventing a gap there would refuse a token
            # that may work perfectly well, so the check abstains and Google decides.
            gaps = missing_scopes(recorded, wanted) if recorded is not None else []
            if gaps and getattr(creds, "refresh_token", None):
                # The record can be narrower than the grant behind it -- see
                # :func:`_refreshed_payload` for how that happens -- so before demanding a
                # consent, ask the token server what it will actually honour. One round trip
                # beats sending someone through a browser to re-approve what they approved.
                live = _live_grant(creds, Request, token)
                if live:
                    gaps = missing_scopes(live, wanted)
            if gaps:
                # A token good for Drive is not good for the Script API, and the two
                # failures look identical from Google's side. Asking again is the only
                # remedy, so say so rather than falling through to a silent browser
                # launch in a process that may not have one.
                if not interactive:
                    raise _scope_error(gaps)
            elif creds.valid:
                return creds
        # else fall through to a fresh consent

    if not interactive:
        raise AuthError(
            f"not authorised for Drive; no usable token at {token}.\n"
            f"Run once in a terminal:  inventory-mcp{' --config ...' if getattr(config, 'local_root', '') else ''} auth"
            "   (add --manual if the browser is not on this machine)"
        )

    creds = _run_flow(InstalledAppFlow, secret, manual=manual, port=port, scopes=wanted)
    _save_token(token, creds.to_json())
    return creds


def authorize(config, *, manual: bool = False, port: int = 8080,
              force: bool = False, admin: bool = False) -> Path:
    """Interactive entry point for ``inventory-mcp auth``; returns the token path."""
    sa = getattr(config, "service_account_path", "") or ""
    if sa and Path(sa).expanduser().exists():
        # Nothing to consent to; verify the key can reach the API instead.
        drive_service(config)
        return Path(sa)
    Request, Credentials, InstalledAppFlow = _require_oauth()
    secret = resolve_client_secret(config)
    if not secret.is_file():
        raise _missing_client_error(config)
    creds = _run_flow(InstalledAppFlow, secret, manual=manual, port=port,
                      scopes=scopes_for(admin))
    token = Path(config.token_path).expanduser()
    _save_token(token, creds.to_json())
    return token


def granted_scopes(config) -> list[str]:
    """What the stored token actually covers, for reporting after a consent."""
    return sorted(recorded_scopes(Path(config.token_path).expanduser()) or [])


def _service_account_credentials(path: str):
    try:
        from google.oauth2 import service_account
    except ImportError as exc:  # pragma: no cover
        raise AuthError(_INSTALL_HINT) from exc
    try:
        return service_account.Credentials.from_service_account_file(path, scopes=SCOPES)
    except Exception as exc:
        raise AuthError(f"service account key {path!r} could not be loaded: {exc}") from exc


def _run_flow(InstalledAppFlow, secret: Path, *, manual: bool, port: int,
              scopes: Optional[list[str]] = None):
    try:
        flow = InstalledAppFlow.from_client_secrets_file(str(secret), scopes or SCOPES)
    except Exception as exc:
        raise AuthError(
            f"{secret} is not a usable installed-app client secret: {exc}"
        ) from exc

    # prompt=consent forces a refresh token; without it Google may hand back a
    # short-lived access token and every session after the first needs a browser.
    if manual:
        flow.redirect_uri = f"http://localhost:{port}/"
        url, state = flow.authorization_url(prompt="consent")
        print(
            "Open this URL in any browser, approve, then copy the address bar of\n"
            "the page it lands on -- even if that page fails to load -- and paste\n"
            "it below. Nothing leaves this machine except the token exchange.\n"
            "\nIf the consent screen says 'Google hasn't verified this app', choose\n"
            "Advanced -> Continue (it is your own app, unverified because it is yours).\n"
        )
        print(url)
        print()
        try:
            response = input("Redirect URL: ").strip()
        except EOFError as exc:
            raise AuthError(
                "no input available. If a browser can reach this machine, drop "
                "--manual and let it open one; otherwise forward the port with "
                f"'ssh -L {port}:localhost:{port} <host>' and retry."
            ) from exc
        if not response:
            raise AuthError("nothing pasted; authorisation cancelled")
        code = _code_from_redirect(response, expected_state=state, port=port)
        try:
            # Exchanged by code, not by the redirect URL -- see
            # :func:`_code_from_redirect` for why the URL cannot be handed over.
            flow.fetch_token(code=code)
        except Exception as exc:
            raise AuthError(
                f"Google refused that code: {exc}. Codes last about a minute, so "
                "re-run and paste it promptly."
            ) from exc
        creds = flow.credentials
    else:
        try:
            creds = flow.run_local_server(
                host="localhost", port=port, open_browser=True,
                prompt="consent", timeout_seconds=300,
            )
        except Exception as exc:
            raise AuthError(
                f"the browser flow did not complete: {exc}\n"
                "If this machine has no reachable browser, use:  inventory-mcp auth --manual"
            ) from exc

    if not creds or not creds.valid:
        raise AuthError("the flow completed but produced no usable credentials")
    return creds


def _code_from_redirect(pasted: str, *, expected_state: str, port: int) -> str:
    """The authorization code inside whatever came back from the browser.

    The exchange has to be handed a code rather than the redirect URL, because
    oauthlib refuses to *parse* an ``http://`` authorization response: it checks the
    scheme of the URI it is given, and a loopback redirect is http by definition
    (RFC 8252 §7.3). That check happens before any request leaves the machine, so
    the failure looks like a bad paste when nothing was pasted wrong. Google's own
    library sidesteps it in the browser flow by rewriting the scheme to https
    before handing the URL over; parsing it here instead keeps the URI honest, and
    buys the ability to name each way this goes wrong -- which matters because a
    person is typing this under a one-minute clock.

    The state is checked here for the same reason: it is the one thing that proves
    the paste belongs to this attempt, and it is worthless if nobody looks at it.
    """
    if "://" not in pasted and "?" not in pasted:
        # Just the code. Nothing carries a state to check, and there is no
        # attacker who can slip a code into a terminal prompt.
        return pasted

    query = urlparse.parse_qs(urlparse.urlparse(pasted).query)
    if "error" in query:
        why = (query.get("error_description") or [""])[0]
        name = query["error"][0]
        raise AuthError(f"Google reported {name}" + (f": {why}" if why else ""))

    got = (query.get("state") or [""])[0]
    if got != expected_state:
        raise AuthError(
            "that redirect carries no state, or the state of a different "
            "authorisation attempt -- its code would be exchanged against the "
            "wrong PKCE verifier anyway. Re-run and paste the whole address bar "
            f"of the page the browser landed on (it starts http://localhost:{port}/)."
        )

    code = (query.get("code") or [""])[0]
    if not code:
        raise AuthError(
            "no authorization code in what you pasted. It should be the address "
            f"bar of the page the browser reached after approving -- usually a page "
            f"that fails to load, starting http://localhost:{port}/ -- not the link "
            "this command printed."
        )
    return code


def _refreshed_payload(creds, consented) -> str:
    """The refreshed token, recording the grant rather than the request.

    The write direction of the same trap as :func:`recorded_scopes`. google-auth holds two
    lists: ``scopes`` is what the caller asked for -- ``from_authorized_user_file`` stamps it
    in, and a refresh does not change it -- while ``granted_scopes`` is the ``scope`` field
    from the server's own reply. ``to_json()`` serialises the first and drops the second, so
    saving it verbatim lets an ordinary ``doctor`` run shave the setup scope off the record on
    any day the access token happens to be stale. That is not hypothetical: it is how a token
    holding ``script.projects`` came to be filed as Drive-only here.

    So: believe the server when it speaks, and otherwise keep what the human consented to.
    Never write our own request back as if it were a grant.
    """
    import json

    payload = json.loads(creds.to_json())
    asked = [str(scope).strip() for scope in (payload.get("scopes") or [])]
    # Empty granted_scopes means "same as requested", per the library's own docstring, not
    # "nothing was granted" -- hence the fallback rather than trusting an empty list.
    granted = [str(scope).strip() for scope in (getattr(creds, "granted_scopes", None) or [])]
    payload["scopes"] = sorted(set(granted)) if granted else sorted(set(asked) | set(consented or []))
    return json.dumps(payload)


def _live_grant(creds, Request, token: Path):
    """Ask the token server what this refresh token can still get, when it is cheap to ask.

    Worth one round trip because the alternative is demanding a consent nobody revoked: the
    file's record can be narrower than the grant it stands for, and ``--admin`` reads the
    record. Returns the scope list Google named, or None if it wouldn't say.
    """
    try:
        creds.expiry = datetime(2000, 1, 1, tzinfo=timezone.utc)
        # ``refresh`` posts regardless; resetting the expiry keeps the intent legible and
        # stops a later version from treating a still-valid token as a reason to skip it.
        creds.refresh(Request())
    except Exception:  # noqa: BLE001 - a failed probe leaves the local answer standing
        return None
    # Only the server's own reply counts here. ``creds.scopes`` is what *we* asked for, so
    # reading it would compare our request with our request and wave through every gap --
    # the same self-referential check that :func:`recorded_scopes` exists to avoid.
    scopes = [str(scope).strip() for scope in (getattr(creds, "granted_scopes", None) or [])]
    if scopes:
        _save_token(token, _refreshed_payload(creds, scopes))
    return scopes or None


def _save_token(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - exotic filesystems
        pass


# ---------------------------------------------------------------------------
# services
# ---------------------------------------------------------------------------


def drive_service(config, **kwargs):
    return _build(config, "drive", "v3", **kwargs)


def sheets_service(config, **kwargs):
    return _build(config, "sheets", "v4", **kwargs)


def script_service(config, **kwargs):
    """The Apps Script API, which needs the setup-only scope to be worth calling.

    Requesting the credentials with ``admin=True`` here rather than at the call site
    is what makes ``ui --install`` fail with "run auth --admin --force" instead of a
    Google error about an otherwise perfectly good token.
    """
    return _build(config, "script", "v1", admin=True, **kwargs)


def _build(config, api: str, version: str, admin: bool = False, **kwargs):
    try:
        from googleapiclient.discovery import build
    except ImportError as exc:  # pragma: no cover
        raise AuthError(_INSTALL_HINT) from exc
    creds = credentials(config, interactive=False, admin=admin)
    try:
        return build(api, version, credentials=creds, cache_discovery=False, **kwargs)
    except ValueError as exc:
        # googleapiclient raises ValueError when the API is disabled for the project
        raise AuthError(
            f"the {api} API is not enabled for the project that issued these "
            f"credentials: {exc}"
        ) from exc


def whoami(config) -> dict:
    """Identity check used by ``inventory-mcp auth`` to confirm it worked."""
    info: dict[str, Optional[str]] = {"account": None, "kind": None}
    creds = credentials(config, interactive=False)
    info["kind"] = type(creds).__name__
    try:
        about = drive_service(config).about().get(fields="user(emailAddress)").execute()
        info["account"] = (about.get("user") or {}).get("emailAddress")
    except Exception as exc:  # noqa: BLE001 - diagnostic only
        info["error"] = str(exc)
    return info
