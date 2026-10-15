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
from pathlib import Path
from typing import Optional

#: Full Drive scope. The narrower ``drive.file`` only covers files this app
#: created itself, which is useless for pointing at a folder that already exists;
#: restricting writes to that folder is our job, not the token's.
SCOPES = ["https://www.googleapis.com/auth/drive"]


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
                port: int = 8080, force: bool = False):
    """Return usable Google credentials for ``config``.

    Order of preference: a service-account key if one is configured, otherwise a
    cached OAuth token, otherwise -- only when ``interactive`` -- run the flow.
    Non-interactive callers get an actionable error instead of a hang, because
    they are usually the stdio server with no terminal to ask in.
    """
    sa = getattr(config, "service_account_path", "") or ""
    if sa and Path(sa).expanduser().exists():
        return _service_account_credentials(sa)

    secret = resolve_client_secret(config)
    if not secret.is_file():
        raise _missing_client_error(config)

    Request, Credentials, InstalledAppFlow = _require_oauth()
    token = Path(config.token_path).expanduser()

    if not force and token.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token), SCOPES)
        except (ValueError, OSError) as exc:
            raise AuthError(
                f"token file {token} is unreadable ({exc}); re-run:  inventory-mcp auth --force"
            ) from exc
        if creds.valid or (creds.expired and creds.refresh_token):
            if creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                except Exception as exc:  # google.auth.exceptions.RefreshError et al
                    raise AuthError(
                        f"stored token could not be refreshed ({exc}).\n"
                        "Usually the consent was revoked, or the app is still in "
                        "Testing and its refresh tokens expired. Re-run:  "
                        "inventory-mcp auth --force"
                    ) from exc
                _save_token(token, creds.to_json())
            if creds.valid:
                return creds
        # else fall through to a fresh consent

    if not interactive:
        raise AuthError(
            f"not authorised for Drive; no usable token at {token}.\n"
            f"Run once in a terminal:  inventory-mcp{' --config ...' if getattr(config, 'local_root', '') else ''} auth"
            "   (add --manual if the browser is not on this machine)"
        )

    creds = _run_flow(InstalledAppFlow, secret, manual=manual, port=port)
    _save_token(token, creds.to_json())
    return creds


def authorize(config, *, manual: bool = False, port: int = 8080,
              force: bool = False) -> Path:
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
    creds = _run_flow(InstalledAppFlow, secret, manual=manual, port=port)
    token = Path(config.token_path).expanduser()
    _save_token(token, creds.to_json())
    return token


def _service_account_credentials(path: str):
    try:
        from google.oauth2 import service_account
    except ImportError as exc:  # pragma: no cover
        raise AuthError(_INSTALL_HINT) from exc
    try:
        return service_account.Credentials.from_service_account_file(path, scopes=SCOPES)
    except Exception as exc:
        raise AuthError(f"service account key {path!r} could not be loaded: {exc}") from exc


def _run_flow(InstalledAppFlow, secret: Path, *, manual: bool, port: int):
    try:
        flow = InstalledAppFlow.from_client_secrets_file(str(secret), SCOPES)
    except Exception as exc:
        raise AuthError(
            f"{secret} is not a usable installed-app client secret: {exc}"
        ) from exc

    # prompt=consent forces a refresh token; without it Google may hand back a
    # short-lived access token and every session after the first needs a browser.
    if manual:
        flow.redirect_uri = f"http://localhost:{port}/"
        url, _state = flow.authorization_url(prompt="consent")
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
        try:
            flow.fetch_token(authorization_response=response)
        except Exception as exc:
            raise AuthError(
                f"could not exchange that redirect for a token: {exc}. Make sure you "
                "pasted the whole URL from the failed redirect, promptly -- codes "
                "expire in about a minute."
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


def _build(config, api: str, version: str, **kwargs):
    try:
        from googleapiclient.discovery import build
    except ImportError as exc:  # pragma: no cover
        raise AuthError(_INSTALL_HINT) from exc
    creds = credentials(config, interactive=False)
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
