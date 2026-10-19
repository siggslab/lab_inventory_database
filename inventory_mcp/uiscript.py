"""Installing the interface script into a workbook's bound Apps Script project.

The buttons on Start are pictures with a function name written on them, and that name
resolves only against a script project *bound to that spreadsheet*. One call creates such
a project -- ``projects.create`` with the sheet as ``parentId`` -- and one replaces its
contents (``projects.updateContent``). Nothing else can: there is no image builder, and
the Sheets API cannot attach a script to an object it created, which is why this module
exists instead of the install being another batchUpdate on the way to publishing tabs.

Pushing the whole file every time is deliberate. ``updateContent`` replaces the project
outright, so installing is idempotent and a half-edited script left behind by someone
poking at the editor cannot survive it: what runs afterwards is what is in this package,
or the command failed loudly. There is no attempt to merge, diff, or preserve local
edits -- if hand-editing the script ever becomes a real workflow, this is the file that
has to grow second thoughts.

Which project belongs to which workbook is recorded in the workbook's own Drive
``appProperties``, not in this machine's config. The id is a property of the database, so
that is where it should live: a second person installing the same workbook finds the
existing project rather than creating a second one nobody knew about.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Optional

#: Where the script lives inside this package. It ships with the wheel for the same
#: reason the OAuth client does: a build without it can only tell you to go find a checkout.
SCRIPT_SOURCE = Path(__file__).resolve().with_name("inventory_ui.gs")

#: What the project is called in the Apps Script dashboard.
PROJECT_TITLE = "Lab inventory interface"

#: The script's own filename, minus extension -- Apps Script adds it.
SCRIPT_FILE = "InventoryUi"

#: Drive appProperties key holding the bound project's id.
PROPERTY_KEY = "inventoryUiScript"

EDITOR_URL = "https://script.google.com/d/{script_id}/edit"


class InstallError(RuntimeError):
    pass


def manifest() -> dict:
    """``appsscript.json``, with the consent scopes pinned.

    Apps Script normally infers scopes from whatever the code touches, so one stray
    ``DriveApp`` or ``UrlFetchApp`` call in a future edit widens the consent prompt for
    every editor at once -- and past 100 distinct authorizers an unverified app stops
    working outright. A pinned manifest turns that silent widening into an
    authorization error on the line that caused it.

    ``userinfo.email`` is deliberately absent right now. With it declared, a function run from
    the editor could read the spreadsheet while the same function clicked from the Extensions
    menu failed with "You do not have permission to call
    SpreadsheetApp.getActiveSpreadsheet -- Required permissions:
    (spreadsheets.currentonly || spreadsheets)" -- naming a scope that was already in the
    manifest, which reads as the whole grant coming back empty rather than one scope missing.
    Without the email scope the fallback chain loses its middle rung and an unlabelled press is
    stamped 'unknown'; that is the cost on record if it goes back in.
    """
    return {
        "timeZone": "Etc/UTC",
        "dependencies": {},
        "exceptionLogging": "STACKDRIVER",
        "runtimeVersion": "V8",
        "oauthScopes": ["https://www.googleapis.com/auth/spreadsheets.currentonly"],
    }


def source() -> str:
    """The Apps Script source this build ships."""
    try:
        text = SCRIPT_SOURCE.read_text(encoding="utf-8")
    except OSError as exc:
        raise InstallError(f"the packaged interface script is unreadable ({SCRIPT_SOURCE}): {exc}") from exc
    if not text.strip():
        raise InstallError(f"{SCRIPT_SOURCE} is empty; refusing to install an empty script")
    return text


def files() -> list[dict]:
    """The project contents, in the shape ``projects.updateContent`` wants.

    ``SERVER_JS`` is the Apps Script file type -- the API's enum is exactly
    ``SERVER_JS | HTML | JSON``, and inventing a fourth makes the whole request fail.
    """
    return [
        {"name": SCRIPT_FILE, "type": "SERVER_JS", "source": source()},
        {"name": "appsscript", "type": "JSON", "source": json.dumps(manifest(), indent=2)},
    ]


def recorded_project_id(drive, spreadsheet_id: str) -> str:
    """The project this workbook already owns, if we are the ones who gave it one."""
    found = drive.files().get(fileId=spreadsheet_id, fields="appProperties",
                              supportsAllDrives=True).execute()
    return str((found.get("appProperties") or {}).get(PROPERTY_KEY) or "")


def _record_project_id(drive, spreadsheet_id: str, script_id: str) -> None:
    """Write the project id onto the workbook as private Drive metadata.

    ``appProperties`` need no visibility flag in Drive v3 -- they are private to the
    app by definition, and there is no parameter to make them so. A leftover
    ``updatePropertyVisibility`` from the v2 shape raises TypeError inside googleapiclient
    before the request is sent, which lands here after the project was already created.
    """
    drive.files().update(fileId=spreadsheet_id,
                         fields="id", body={"appProperties": {PROPERTY_KEY: script_id}},
                         supportsAllDrives=True).execute()


#: Why Google says "permission denied" when the real answer is "you never turned this
#: API on". Both spellings carry the console link that fixes it.
_DISABLED_REASONS = ("SERVICE_DISABLED", "ACCESS_NOT_CONFIGURED")


def _disabled_api_remedy(error) -> str:
    """Turn a disabled-API 403 into the sentence and the link a person can act on.

    The status is PERMISSION_DENIED and the prose is "has not been used in project …
    before or it is disabled", which reads like a scope problem to anyone who just
    consented to the right scope. The actionable part -- the exact console page -- is
    only in the structured details, so read them instead of the message. Returns "" for
    every other error, which leaves Google's own text and the traceback intact.
    """
    try:
        details = json.loads(error.content.decode("utf-8"))["error"]["details"]
    except (AttributeError, ValueError, KeyError, TypeError):
        return ""
    for detail in details if isinstance(details, list) else []:
        if not isinstance(detail, dict) or detail.get("reason") not in _DISABLED_REASONS:
            continue
        meta = detail.get("metadata") or {}
        service = meta.get("serviceTitle") or "That API"
        where = f" ({meta['consumer']})" if meta.get("consumer") else ""
        link = meta.get("activationUrl") or "https://console.cloud.google.com/apis/library"
        return (f"{service} is not enabled for the Google Cloud project behind these "
                f"credentials{where}. Enable it, give Google a minute to notice, then "
                f"re-run this command:\n  {link}")
    return ""


def install(config, spreadsheet_id: str, *,
            progress: Optional[Callable[[str], None]] = None) -> dict:
    """Create or replace the workbook's bound Apps Script project.

    Returns what happened, including whether the project had to be created -- worth
    saying out loud, because creating one is the only irreversible part of setup.
    """
    from .auth import drive_service, script_service

    say = progress or (lambda message: None)
    if not isinstance(spreadsheet_id, str):
        # ``store.workbook_id`` left unparenthesised is truthy, so it survives every
        # other check and comes back from Drive as a 404 quoting a Python repr.
        raise InstallError(
            f"the workbook arrived as a {type(spreadsheet_id).__name__}, not an id "
            "string; the caller has to pass the id itself")
    if not spreadsheet_id:
        raise InstallError("no workbook to bind to; publish the interface first (inventory-mcp ui)")

    try:
        script = script_service(config)   # fails here, with the remedy, if the scope is missing
        drive = drive_service(config)

        script_id = recorded_project_id(drive, spreadsheet_id)
        created = False
        if script_id:
            say(f"updating the existing script project {script_id}")
        else:
            say("creating the Apps Script project bound to this workbook")
            made = script.projects().create(
                body={"title": PROJECT_TITLE, "parentId": spreadsheet_id}).execute()
            script_id = str(made.get("scriptId") or "")
            if not script_id:
                raise InstallError(f"Google created a project but did not name it: {made!r}")
            created = True
            # Recorded before the content goes up: an installed-but-unrecorded project is
            # invisible to the next run, which would then make a second one.
            try:
                _record_project_id(drive, spreadsheet_id, script_id)
            except Exception as exc:  # noqa: BLE001 - the project now exists either way
                # Creating was the irreversible half, and it succeeded. Without this the
                # only trace of the new project is a traceback that never mentions its id,
                # and the next run silently makes a second one beside it.
                raise InstallError(
                    f"created the script project {script_id} but could not record it on "
                    f"the workbook ({exc}). It exists and is bound to this spreadsheet; "
                    "delete it at https://script.google.com/home before re-running, or "
                    "the next install will create another one."
                ) from exc

        payload = files()
        size = sum(len(item["source"]) for item in payload)
        say(f"writing {len(payload)} files, {size} bytes")
        script.projects().updateContent(scriptId=script_id, body={"files": payload}).execute()
    except InstallError:
        raise
    except Exception as exc:  # noqa: BLE001 - translated below, otherwise untouched
        remedy = _disabled_api_remedy(exc)
        if remedy:
            raise InstallError(remedy) from exc
        raise

    return {
        "script_id": script_id,
        "created": created,
        "bytes": size,
        "files": [item["name"] for item in payload],
        "editor": EDITOR_URL.format(script_id=script_id),
    }
