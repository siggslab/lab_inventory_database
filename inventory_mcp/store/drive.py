"""The Google Drive backend: one spreadsheet, one tab per table.

The database is a single workbook in one Drive folder, addressed by name, with a
tab per table named after the table. One file rather than twelve because a person
opening the database should see the whole of it, because a shared grant then
covers everything at once, and because batching: reading all twelve tables is one
HTTP request instead of twelve, which is the difference between a command that
answers and one that appears to hang.

Four hazards specific to Drive, and what this module does about each:

*Mime type.* Anything uploaded without declaring what it is gets converted into a
Google-native file, which then answers to different API calls than the one that
wrote it. Every create here states its mimeType. A backend that stored tables as
plain CSV files in the folder existed once, for the sake of strictness, and spent
its read path detecting that Drive had converted the files anyway; it is gone.

*Shared drives.* A file inside one is reported as **not found** unless the request
passes ``supportsAllDrives``, which is indistinguishable from a deleted table. The
flag is on every Drive call for that reason; production lives in a shared drive.

*Coercion.* Sheets rewrites what it thinks it understands -- ``47.2`` becomes a
number, ``2024-05-16T11:00`` can become a date cell. Values are written RAW and
stringified back on read; only the trivial widening Sheets does to our own ISO
timestamps is undone. Anything genuinely ambiguous -- a locale-formatted date --
is left alone for the validator to flag, because guessing at a measurement date is
how a lab ends up with a plausible wrong record.

*Concurrency.* There is no precondition for "only write if this range still holds
what I read". Compare-and-swap therefore means read-immediately-before-write and
abort on mismatch, which catches the spreadsheet-saved-underneath-us case but
cannot close the race between the check and the update. The window is small and
the failure mode is a lost edit rather than corruption, and both sessions leave a
Drive revision behind -- but it is a real weakening versus the local backend. It
surfaces where it matters, as the text of the conflict: the abort names the browser
edit as the likely cause instead of reporting a mystery failure.
"""

from __future__ import annotations

import re
import sys
import time
from datetime import datetime, timezone
from typing import Any, Iterable, Optional, Sequence

from .. import schema
from ..auth import AuthError, drive_service, sheets_service
from .base import (
    Row,
    RevisionConflict,
    StoreError,
    TableData,
    TableStore,
    compute_revision,
)

try:  # optional extra
    from googleapiclient.errors import HttpError  # type: ignore
except ImportError:  # pragma: no cover
    HttpError = None  # type: ignore[assignment]

_RETRY_STATUS = {429, 500, 502, 503, 504}

#: What the workbook is called inside the folder. Deliberately not a pretty name:
#: the folder carries the human label, and a stable ASCII name is what discovery
#: resolves against and what a hand-authored override has to repeat.
WORKBOOK_NAME = "inventory"

#: Tabs that belong to the interface rather than to the data. They are created with
#: the database so the layout is fixed at init, and they are invisible to every
#: table-shaped question the rest of the app asks.
#:
#: Named for what a non-engineer would read them as, not for what the code calls
#: them: ``database_errors`` because whoever opens it is looking for what is broken,
#: and ``Start`` because it is where they begin.
#:
#: There is deliberately no request queue. An edit the sheet cannot prove legal is
#: refused there and then, with the reason and what to do about it, rather than filed
#: somewhere for a command to drain later -- a queue would make the workbook depend on
#: something outside itself and would make the manual route second-class. Whatever a
#: button refuses stays available by hand in the data tabs, and on the MCP surface.
RESERVED_TABS = ("Start", "database_errors", "options")

#: Hidden because it is machine-generated reference data for the sheet's dropdowns
#: and macros, not something to scroll past looking for samples.
HIDDEN_TABS = ("options",)

#: Subfolder holding pre-write copies of the workbook. A folder, so it drops out of
#: the stray report on its own; kept beside the database because a backup three
#: clicks from the thing it restores is a backup people can actually use.
BACKUP_FOLDER_NAME = "inventory-backups"

#: Sheets widens our minute-precision timestamps to whole seconds and sometimes drops or
#: re-adds the zone marker. Undoing that is safe because the shape is unambiguously ours;
#: a locale-formatted date is not, and is left for the validator to flag rather than be
#: guessed at. The rendering always comes back as the stored spelling: UTC to the minute
#: with ``Z``, which is what every write emits.
_SHEET_TIMESTAMP_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})(?::\d{2})?(?:\.0+)?"
    r"(?:\s*(?:Z|UTC|\+00:?00))?$",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# shared plumbing
# ---------------------------------------------------------------------------


def note(message: str) -> None:
    """Report something the operator should see while we are working.

    stderr, unconditionally: stdout is the MCP protocol channel for ``serve``, and
    this has to be callable from the store without knowing which command loaded it.
    """
    print(message, file=sys.stderr, flush=True)


#: A quota window is one minute, so waiting past a minute buys nothing except an
#: unresponsive-looking terminal. The old uncapped ladder reached 480 s on a single
#: request, which is how a working command came to look broken.
_MAX_WAIT = 60.0


def _wait_for(exc: Exception, status: int, delay: float) -> float:
    """How long to sit out a rejection: Google's number if it gave one, else ours.

    A 429 here is the per-minute quota rather than a momentary hiccup, so its floor
    is a window rather than the half-second the ladder starts at -- retrying
    immediately would only spend the attempts on the same closed window.
    """
    resp = getattr(exc, "resp", None)
    if hasattr(resp, "get"):
        try:
            stated = resp.get("retry-after")
        except Exception:  # noqa: BLE001 - a header lookup must not choose the error path
            stated = None
        if stated is not None:
            try:
                return max(1.0, min(float(stated), _MAX_WAIT))
            except (TypeError, ValueError):
                pass  # an HTTP-date or nonsense value; fall back to our own ladder
    if status == 429:
        delay = max(delay, 30.0)
    return min(delay, _MAX_WAIT)


def tab_of(range_: str) -> str:
    """The tab a quoted A1 range names: ``'item links'!A1:C9`` -> ``item links``."""
    head = range_.split("!")[0]
    return head.strip("'")


def column_letter(width: int) -> str:
    """1-based column number as Sheets spells it: 1 -> ``A``, 28 -> ``AB``."""
    letters = ""
    while width > 0:
        width, rem = divmod(width - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return letters or "A"


def _short(rev: Optional[str]) -> str:
    return (rev or "<absent>")[-8:]


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _to_sheet(value: Any) -> Any:
    """Everything goes over as text; RAW stops formula interpretation."""
    return _text(value)


def _from_sheet(value: Any) -> str:
    """Stringify a cell the way our CSV would hold it.

    Numbers come back as int/float and would otherwise print as ``47.2`` or the
    unwanted ``500.0``; timestamps come back widened to seconds and sometimes stripped
    of their zone marker, which is our own format stretched by Sheets and safe to
    re-render. Locale-formatted dates are deliberately left as-is so validation reports
    them instead of guessing.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else repr(value)
    text = str(value).strip()
    widened = _SHEET_TIMESTAMP_RE.match(text)
    if widened:
        return f"{widened.group(1)}T{widened.group(2)}Z"
    return text


def _absent(name: str) -> TableData:
    """What a table that is not there looks like: no header, no rows, empty revision."""
    return TableData(name=name, header=(), rows=(), revision=compute_revision((), ()))


def _grid_to_table(name: str, raw_rows: Sequence[Sequence[Any]]) -> TableData:
    """Turn a fetched grid into a table, trimming the blank tail Sheets reports back."""
    grid = [[_from_sheet(c) for c in row] for row in raw_rows]
    while grid and not any(str(c).strip() for c in grid[-1]):
        grid.pop()
    if not grid:
        return TableData(name=name, header=(), rows=(), revision=compute_revision((), ()))
    header = tuple(grid[0])
    rows = [dict(zip(header, r)) for r in grid[1:]]
    for r in rows:  # short rows read as absent cells, which are empty strings
        for col in header:
            r.setdefault(col, "")
    return TableData(name=name, header=header, rows=tuple(rows),
                     revision=compute_revision(header, rows))


def _table_to_grid(header: tuple[str, ...], rows: Iterable[Row], fill_to: int) -> list[list[str]]:
    """Header plus rows as a rectangular block, padded to cover ``fill_to`` lines.

    The padding is what replaces a separate clear: a shrink writes blanks over its own
    leftovers in the same request, so the stale rows are gone before anyone can read
    between two calls. Growing is not a problem -- an update to a taller range extends
    the grid.
    """
    grid = [list(header)] + [[_to_sheet(r.get(c, "")) for c in header] for r in rows]
    width = max((len(header), *(len(g) for g in grid)), default=1)
    grid = [g + [""] * (width - len(g)) for g in grid]
    while len(grid) < fill_to:
        grid.append([""] * width)
    return grid


# ---------------------------------------------------------------------------
# the store
# ---------------------------------------------------------------------------


class DriveWorkbookStore(TableStore):
    """One spreadsheet in one folder; a tab per table, named after the table."""

    kind = "drive"

    def __init__(self, config):
        self.config = config
        self.folder_id = (config.drive_folder_id or "").strip()
        if not self.folder_id:
            raise StoreError("drive backends need drive_folder_id (a folder id or URL)")
        self.ttl = max(float(getattr(config, "cache_ttl_seconds", 5.0) or 0.0), 0.0)
        self._override_id = str(getattr(config, "drive_spreadsheet_id", "") or "").strip()
        self._listing: Optional[dict[str, dict]] = None
        self._spreadsheet_id: Optional[str] = None
        self._resolved = False           # looked up once; absence is remembered too
        self._tab_ids: Optional[dict[str, int]] = None
        self._tab_stamp = 0.0
        self._cache: dict[str, tuple[float, TableData]] = {}
        try:
            self._drive = drive_service(config)
            self._sheets = sheets_service(config)
        except AuthError as exc:
            raise StoreError(str(exc)) from exc

    def describe(self) -> str:
        return f"{self.kind}:drive-folder:{self.folder_id}"

    # -- requests ------------------------------------------------------------

    def _retry(self, request, *, attempts: int = 8):
        """Run an ApiRequest, backing off on the transient statuses -- out loud.

        Being rate limited is routine here and worth sleeping through; what is not
        acceptable is sleeping silently. A minute of quiet looks exactly like a
        wedged command, and the operator has nothing to check but whether the
        process is still alive.
        """
        delay = 0.5
        for i in range(attempts):
            try:
                return request.execute(num_retries=0)
            except Exception as exc:  # noqa: BLE001 - classified below
                status = getattr(getattr(exc, "resp", None), "status", None)
                if status is None and HttpError is not None and isinstance(exc, HttpError):
                    status = exc.resp.status if getattr(exc, "resp", None) else None
                if status in _RETRY_STATUS and i < attempts - 1:
                    wait = _wait_for(exc, status, delay)
                    note(f"drive: {status} from Google, waiting {wait:.0f}s "
                         f"(attempt {i + 2} of {attempts})")
                    time.sleep(wait)
                    delay *= 2
                    continue
                raise self._as_store_error(exc) from exc

    def _as_store_error(self, exc: Exception) -> StoreError:
        status = getattr(getattr(exc, "resp", None), "status", None)
        detail = str(exc)
        if HttpError is not None and isinstance(exc, HttpError):
            detail = getattr(exc, "reason", "") or detail
        if status == 404:
            return StoreError(f"not found in Drive: {detail}")
        if status == 403:
            hint = ""
            if "insufficient" in detail.lower():
                hint = " -- the folder may be view-only for this account"
            elif "permission" in detail.lower():
                hint = " -- this account cannot write to that folder"
            return StoreError(f"Drive refused the operation ({detail}){hint}")
        if status == 401:
            return StoreError(
                "Drive rejected the credentials (expired or revoked consent). "
                "Run: inventory-mcp auth --force"
            )
        return StoreError(f"Drive error ({status}): {detail}")

    # -- discovery -----------------------------------------------------------

    def _list_folder(self, *, refresh: bool = False) -> dict[str, dict]:
        if self._listing is not None and not refresh:
            return self._listing
        resp = self._retry(
            self._drive.files().list(
                q=f"'{self.folder_id}' in parents and trashed = false",
                fields="files(id,name,mimeType,size,modifiedTime,properties(title))",
                pageSize=1000,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
        )
        listing: dict[str, dict] = {}
        for f in resp.get("files", []):
            listing.setdefault(f["name"], f)  # first wins; duplicates reported on use
        self._listing = listing
        return listing

    def workbook_id(self) -> Optional[str]:
        """This database's spreadsheet, resolved by name once and then remembered.

        Name-based because ids change when a file is replaced and names are what the
        schema declares -- the same reasoning that made the twelve-file backend look
        things up by name. ``drive_spreadsheet_id`` overrides the lookup for the case
        where somebody renames the workbook, which used to be twelve chances to hit
        that and is now one.
        """
        if self._override_id:
            return self._override_id
        if not self._resolved:
            entry = self._list_folder().get(WORKBOOK_NAME)
            self._spreadsheet_id = entry["id"] if entry else None
            self._resolved = True
        return self._spreadsheet_id

    def _require_workbook_id(self) -> str:
        fid = self.workbook_id()
        if fid is None:
            raise StoreError(
                f"no {WORKBOOK_NAME!r} spreadsheet in the Drive folder "
                f"{self.folder_id!r}; run init to create one"
            )
        return fid

    def _tabs(self, *, refresh: bool = False) -> dict[str, int]:
        """Tab title -> sheet id, cached on the same clock as table content.

        Which tabs exist changes far less often than what is in them, but it does
        change: renaming a tab in the browser is exactly the surprise this app
        promises to notice rather than write through, and an MCP server process stays
        alive for hours. Caching it for the lifetime of the store would mean the
        rename is invisible until restart, so it expires like any other read.
        """
        if (self._tab_ids is not None and not refresh and self.ttl > 0
                and (time.monotonic() - self._tab_stamp) <= self.ttl):
            return self._tab_ids
        meta = self._retry(
            self._sheets.spreadsheets().get(
                spreadsheetId=self._require_workbook_id(),
                fields="sheets.properties.sheetId,sheets.properties.title",
            )
        )
        out: dict[str, int] = {}
        for s in meta.get("sheets", []):
            props = s.get("properties", {})
            if props.get("title"):
                out[props["title"]] = props.get("sheetId", 0)
        self._tab_ids = out
        self._tab_stamp = time.monotonic()
        return out

    def _create_workbook(self) -> tuple[str, list[str]]:
        """Create the workbook with every tab it will ever have, in three requests.

        The default ``Sheet1`` is renamed into service as the first interface tab
        rather than left behind: an orphan tab in a file a human opens is clutter that
        looks like it means something. Its id is read back instead of assumed -- this
        module has already been bitten once by a grid detail that turned out not to be
        constant -- and the rest are appended in order, ``Start`` first so that is what
        a colleague sees, with the reference tab hidden as it is created.

        Returns the id and the titles now in the file. The titles come from what was
        asked for rather than another read: addressing cells needs only a title, so
        the ids stay unknown until something actually needs them.
        """
        body = {
            # Drive's own filename comes from `name`; without it the file lands as
            # "Untitled spreadsheet" and every later lookup by name misses it.
            "name": WORKBOOK_NAME,
            "mimeType": "application/vnd.google-apps.spreadsheet",
            "parents": [self.folder_id],
            # The adoption marker. An empty spreadsheet wearing this name is ours;
            # an empty spreadsheet without it is somebody's blank sheet, and the two
            # are otherwise indistinguishable from the inside.
            "properties": {"title": WORKBOOK_NAME},
        }
        created = self._retry(
            self._drive.files().create(body=body, fields="id", supportsAllDrives=True)
        )
        fid = created["id"]
        self._spreadsheet_id = fid
        self._resolved = True
        self._tab_ids = None

        # The id, not the title -- `_tabs` is keyed by title, and Sheets addresses a
        # tab by number. A fresh spreadsheet has exactly one sheet, whatever it is
        # called, so "the first one" is the only meaningful answer here.
        default = next(iter(self._tabs().values()), None)
        if default is None:
            raise StoreError(
                f"the new {WORKBOOK_NAME} spreadsheet reported no tabs; refusing to "
                "guess at its structure"
            )
        wanted = [*RESERVED_TABS, *[t.name for t in schema.TABLES]]
        requests: list[dict] = [{
            "updateSheetProperties": {
                "properties": {"sheetId": default, "title": wanted[0]},
                "fields": "title",
            }
        }]
        requests += [{"addSheet": {"properties": {
            "title": title, "hidden": title in HIDDEN_TABS}}} for title in wanted[1:]]
        self._batch_update(fid, requests)
        self._tab_ids = None
        self._listing = None
        return fid, wanted

    def _ensure_tabs(self, titles: Sequence[str]) -> None:
        """Add whichever tabs are absent. One batchUpdate for all of them."""
        missing = [t for t in titles if t not in self._tabs()]
        if not missing:
            return
        self._batch_update(self._require_workbook_id(),
                           [{"addSheet": {"properties": {"title": title}}}
                            for title in missing])
        self._tab_ids = None

    def _batch_update(self, spreadsheet_id: str, requests: list[dict]) -> dict:
        return self._retry(
            self._sheets.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id, body={"requests": requests})
        )

    # -- layout --------------------------------------------------------------

    def tab_ids(self) -> dict[str, int]:
        """Tab title -> sheet id, for code that builds formatting requests.

        Sheets addresses a tab by number while everything else here speaks titles, so
        layout code has to cross that gap somewhere. It happens at the point of
        building the request, where a missing tab is still an understandable error
        ("there is no tab called X") rather than a batchUpdate refusing a body nobody
        can read.
        """
        return self._tabs()

    def apply_layout(self, requests: Sequence[dict]) -> None:
        """Apply formatting requests -- validation rules, formats, widths, freezing.

        One call, because a layout is one decision: twelve requests sent one at a
        time is twelve chances to be rate limited halfway through, leaving a form
        with some dropdowns and not others.
        """
        if not requests:
            return
        self._batch_update(self._require_workbook_id(), list(requests))

    # -- interface tabs ------------------------------------------------------

    def reset_tabs(self, titles: Sequence[str]) -> dict[str, int]:
        """Recreate interface tabs from nothing, in the order a new workbook gives them.

        Deleting rather than writing over is the whole point. A tab collects things no
        generator can remove by writing -- merged cells, conditional formats, protected
        ranges, cell notes, a dropdown made by hand, an image pasted in -- and the reason
        to reset is that somebody changed exactly one of those. Overwriting replaces the
        values and the handful of formats this tool sets again; everything else survives
        underneath, which is how a form ended up with a strict dropdown across nine
        inputs and its own Result line.

        The cost is stated rather than buried: the press buttons are images sitting on
        ``Start``, so they leave with the tab and have to be added again. Drive's version
        history is the only undo.

        Returns the number of non-empty rows each tab held when it was deleted, so the
        caller can say what was discarded instead of merely that it happened.
        """
        live = self._tabs(refresh=True)
        order = list(live)  # the API returns sheets in tab order
        fid = self._require_workbook_id()
        present = [t for t in titles if t in live]

        held: dict[str, int] = {}
        if present:
            blocks = self._retry(
                self._sheets.spreadsheets().values().batchGet(
                    spreadsheetId=fid, ranges=[self._range(t) for t in present],
                    valueRenderOption="FORMATTED_VALUE",
                )
            )
            for block in blocks.get("valueRanges", []):
                title = tab_of(block["range"]) if block.get("range") else ""
                if title:
                    held[title] = sum(1 for row in (block.get("values") or [])
                                      if any(str(cell).strip() for cell in row))

        requests = [{"deleteSheet": {"sheetId": live[t]}} for t in present]
        requests += [{"addSheet": {"properties": {
            "title": t, "hidden": t in HIDDEN_TABS}}} for t in titles]
        replies = (self._batch_update(fid, requests) or {}).get("replies") or []
        added = replies[len(requests) - len(titles):]

        # Sheets addresses a tab by number and the old numbers are gone, so the new ones
        # come from the response rather than another read. Each recreated tab then goes back
        # where a freshly created workbook would put it -- not where the file happened to
        # have drifted to, since repairing that drift is part of why anyone resets a tab.
        # Destinations are applied ascending, because an index update moves a tab within the
        # layout as it stands at that moment: applying them in the caller's order left
        # ``options`` behind two data tabs on the first live run.
        canonical = [*RESERVED_TABS, *[t.name for t in schema.TABLES]]
        added_id = {title: ((added[offset].get("addSheet") or {}).get("properties") or {})
                    .get("sheetId") for offset, title in enumerate(titles)}
        last = len(order) - 1

        def destination(title: str) -> int:
            wanted = canonical.index(title) if title in canonical else last
            return min(wanted, last)

        restore = [{"updateSheetProperties": {
            "properties": {"sheetId": added_id[title], "index": destination(title)},
            "fields": "index"}}
            for title in sorted((t for t in titles if t in added_id), key=destination)
            if added_id[title] is not None]
        if restore:
            self._batch_update(fid, restore)

        self._tab_ids = None
        self._listing = None
        return held

    def read_grid(self, title: str) -> list[list[str]]:
        """Raw cells of a tab, with no header or row interpretation.

        The interface tabs hold generated reference data and prose. There is no
        primary key to look up and nothing to compare-and-swap against, so they get
        a read that does not pretend otherwise -- and, importantly, one that never
        hands a caller a revision it could mistake for a precondition.
        """
        fid = self.workbook_id()
        if fid is None or title not in self._tabs():
            return []
        resp = self._retry(
            self._sheets.spreadsheets().values().get(
                spreadsheetId=fid, range=self._range(title),
                valueRenderOption="FORMATTED_VALUE",
            )
        )
        return [[_from_sheet(c) for c in row] for row in resp.get("values", [])]

    def write_grids(self, grids: dict[str, Sequence[Sequence[str]]]
                    ) -> dict[str, list[list[str]]]:
        """Replace the contents of several interface tabs in two requests.

        Not ``write_batch``: these tabs are derived, so there is no revision to
        honour and no rollback to promise. If the request fails the tab is either
        still old or partly new, and the fix is to run the command again rather than
        to reconstruct a previous state nobody was relying on.

        Returns what each tab held beforehand. The caller had to read it anyway to
        know how far to pad, so handing it back is what lets a generator compare its
        own last output without spending a third request to remember it.

        Each grid is padded out to whatever the tab previously held, because Sheets
        does not clear cells outside the block it is handed -- a shortened error
        report would otherwise leave last week's rows sitting under the new ones,
        which is worse than no report, since it reads as current.
        """
        if not grids:
            return {}
        titles = list(grids)
        present = set(self._tabs())
        missing = [t for t in titles if t not in present]
        if missing:
            self._ensure_tabs(missing)
            present |= set(missing)

        fid = self._require_workbook_id()
        resp = self._retry(
            self._sheets.spreadsheets().values().batchGet(
                spreadsheetId=fid,
                ranges=[self._range(t) for t in titles if t in present],
                valueRenderOption="FORMATTED_VALUE",
            )
        )
        blocks = resp.get("valueRanges", [])
        by_tab = {tab_of(b["range"]): b for b in blocks if b.get("range")}

        payloads = []
        before: dict[str, list[list[str]]] = {}
        for title in titles:
            block = by_tab.get(title, {})
            old = [[_from_sheet(c) for c in row] for row in (block.get("values") or [])]
            before[title] = old
            width = max([len(r) for r in old] + [len(g) for g in grids[title]] + [1])
            height = max(len(old), len(grids[title]))
            grid = [[_to_sheet(c) for c in list(row) + [""] * (width - len(row))]
                    for row in grids[title]]
            grid += [[""] * width] * (height - len(grid))
            payloads.append({"range": self._range(title), "values": grid})

        self._retry(
            self._sheets.spreadsheets().values().batchUpdate(
                spreadsheetId=fid,
                body={"valueInputOption": "RAW", "includeValuesInResponse": False,
                      "data": payloads},
            )
        )
        return before

    def invalidate(self) -> None:
        """Forget everything memoised, including which workbook this is.

        Used when storage is believed to have changed shape underneath us. Dropping
        the resolved id costs a folder listing on the next lookup, which is the point:
        after a surprise the cached answer is exactly what we should not trust.
        """
        self._listing = None
        self._tab_ids = None
        self._resolved = False
        self._spreadsheet_id = None
        self._cache.clear()

    # -- which tables exist --------------------------------------------------

    def list_tables(self) -> list[str]:
        if self.workbook_id() is None:
            return []
        present = set(self._tabs())
        return [t.name for t in schema.TABLES if t.name in present]

    def exists(self, name: str) -> bool:
        if self.workbook_id() is None:
            return False
        return name in self._tabs()

    def stray_objects(self) -> list[str]:
        """Tabs and files in the folder that are not part of the database.

        Folders are excluded because the backup subfolder is ours and a stray report
        that lists it every run trains people to ignore the report.
        """
        out: list[str] = []
        wanted = {t.name for t in schema.TABLES} | set(RESERVED_TABS)
        if self.workbook_id() is not None:
            out += sorted(t for t in self._tabs() if t not in wanted)
        for name, entry in self._list_folder().items():
            if name == WORKBOOK_NAME or name == BACKUP_FOLDER_NAME:
                continue
            if entry.get("mimeType") == "application/vnd.google-apps.folder":
                continue
            out.append(name)
        return sorted(set(out))

    def preflight(self) -> list[str]:
        """Reasons not to build a database in this folder, checked before any write.

        Two ways to make a mess here, and neither is caught by looking at tables:

        The abandoned layout -- a folder holding a spreadsheet per table is a working
        database that this version cannot see, and building a second database beside
        it produces two sources of truth with only one of them live.

        Somebody else's workbook already wearing our filename. A blank Google sheet
        named ``inventory`` has no tables to inspect, so the per-table adoption check
        never fires on it; without the marker test below the first init would simply
        start adding tabs to whatever that file was being used for.
        """
        reasons: list[str] = []
        listing = self._list_folder()

        legacy = sorted(
            name for name, entry in listing.items()
            if name in {t.name for t in schema.TABLES}
            and entry.get("mimeType") == "application/vnd.google-apps.spreadsheet"
        )
        if legacy:
            reasons.append(
                f"{len(legacy)} spreadsheet(s) named after tables "
                f"({', '.join(legacy[:6])}{'...' if len(legacy) > 6 else ''}) -- that is "
                "the older one-file-per-table layout, which this version neither reads "
                "nor migrates. Point --drive-folder at an empty folder, or move those "
                "files out. Nothing was created."
            )

        fid = self.workbook_id()
        entry = next((f for f in listing.values() if f.get("id") == fid), None) if fid else None
        if entry is not None and (entry.get("properties") or {}).get("title") != WORKBOOK_NAME:
            reasons.append(
                f"the spreadsheet {entry.get('name', fid)!r} ({fid}) is where this "
                f"config points, but it does not carry the title marker this app stamps "
                f"({WORKBOOK_NAME!r}), so it was not created by us -- it is somebody's "
                "own workbook. Move it aside or point the config at a different one; "
                "nothing was written into it."
            )
        return reasons

    # -- caching -------------------------------------------------------------

    def _cached(self, name: str) -> Optional[TableData]:
        # A TTL of zero means every read goes to the network. Treating it as
        # falsy-and-therefore-unbounded would make the knob's obvious "off" value
        # mean "never expire", which is the opposite of what it says.
        if self.ttl <= 0:
            return None
        hit = self._cache.get(name)
        if not hit:
            return None
        stamp, data = hit
        if (time.monotonic() - stamp) > self.ttl:
            return None
        return data

    def _store_cache(self, name: str, data: TableData) -> None:
        self._cache[name] = (time.monotonic(), data)

    # -- reads ---------------------------------------------------------------

    @staticmethod
    def _range(name: str) -> str:
        return f"'{name}'!A1:ZZZ"

    def _read_many(self, names: Sequence[str], *,
                   present: Optional[Iterable[str]] = None) -> dict[str, TableData]:
        """Fetch several tabs in one request, returning only the ones that answered.

        Only tabs that exist are requested: Sheets cannot parse a range naming a tab
        that is not there, and one bad name fails the whole batch -- which would turn
        a missing table into an unreadable database. Absent tabs are left out rather
        than filled in as empty, so a caller that cares whether a table exists can
        tell, and ``db.load`` still reports it as missing instead of silently zero.

        ``present`` lets a caller that already looked reuse the answer; without it a
        write would pay for its structure read twice.
        """
        out: dict[str, TableData] = {}
        if not names:
            return out
        fid = self.workbook_id()
        if fid is None:
            return out
        known = set(present) if present is not None else set(self._tabs())
        live = [n for n in names if n in known]
        if not live:
            return out

        resp = self._retry(
            self._sheets.spreadsheets().values().batchGet(
                spreadsheetId=fid,
                ranges=[self._range(n) for n in live],
                valueRenderOption="FORMATTED_VALUE",
            )
        )
        blocks = resp.get("valueRanges", [])
        # Trust the echoed range over the position: Sheets trims a returned range to
        # the data it found, and a response that skipped or reordered one tab would
        # otherwise land somebody's rows under the wrong table name.
        by_tab = {tab_of(b["range"]): b for b in blocks if b.get("range")}
        for index, name in enumerate(live):
            block = by_tab.get(name) or (blocks[index] if index < len(blocks) else {})
            data = _grid_to_table(name, block.get("values", []))
            out[name] = data
            self._store_cache(name, data)
        return out

    def _read_one(self, name: str) -> TableData:
        """One tab, freshly read. Used where a small payload beats a whole-database read."""
        fid = self.workbook_id()
        if fid is None:
            return TableData(name=name, header=(), rows=(), revision=compute_revision((), ()))
        if name not in self._tabs():
            return TableData(name=name, header=(), rows=(), revision=compute_revision((), ()))
        resp = self._retry(
            self._sheets.spreadsheets().values().get(
                spreadsheetId=fid, range=self._range(name),
                valueRenderOption="FORMATTED_VALUE",
            )
        )
        data = _grid_to_table(name, resp.get("values", []))
        self._store_cache(name, data)
        return data

    def read_table(self, name: str) -> TableData:
        cached = self._cached(name)
        if cached is not None:
            return cached
        if not self.exists(name):
            raise StoreError(f"table {name!r} has no tab in the {WORKBOOK_NAME} spreadsheet")
        return self._read_one(name)

    def read_all(self) -> dict[str, TableData]:
        """Every table in one request, which is why loading a database is quick."""
        return self._read_many([t.name for t in schema.TABLES])

    def current_revision(self, name: str) -> Optional[str]:
        """Always refetches: this is the compare half of compare-and-swap."""
        try:
            if not self.exists(name):
                return None
            return self._read_one(name).revision
        except StoreError:
            return None

    # -- writes --------------------------------------------------------------

    def write_table(self, name, header, rows, expected_revision) -> str:  # noqa: ANN001
        return self.write_batch([(name, header, list(rows), expected_revision)])[name]

    def write_batch(self, changes, written: Optional[dict] = None) -> dict[str, str]:
        """Write several tables in three requests: read, apply, confirm.

        The read covers every table in play, so a commit touching items and samples
        pays one round trip for its compare-and-swap rather than two, and the read
        also tells us how far each tab currently extends -- which is what lets the
        apply step blank its own leftovers instead of clearing afterwards.

        Not atomic: ``values.batchUpdate`` applies its ranges in order, so a request
        that fails part-way can leave some landed and others not, and we cannot ask
        afterwards which were which. That is why the confirmation pass reads the same
        tables back and, when the content disagrees with what we sent, puts the
        pre-write state back here rather than handing an unknown half-state upward --
        this store holds the only copy of what was there before.
        """
        if not changes:
            return {}
        names = [c[0] for c in changes]
        existed = self.workbook_id() is not None
        # One structure read serves the whole write: it decides which tables to ask
        # about here, and what to expect to exist by the time the write lands.
        present = set(self._tabs()) if existed else set()
        fresh = self._read_many(names, present=present)

        for name, _header, _rows, expected in changes:
            if expected is None:
                continue
            if self.workbook_id() is None:
                raise RevisionConflict(
                    f"table {name!r} has no spreadsheet to update; it was deleted "
                    "while this session held it open"
                )
            actual = fresh[name].revision if name in fresh else None
            if actual != expected:
                raise RevisionConflict(
                    f"table {name!r} changed underneath us "
                    f"(expected {_short(expected)}, found {_short(actual)}). "
                    "Someone edited this in the browser or another session is "
                    "writing. Reload and retry."
                )

        existed = self.workbook_id() is not None
        if not existed:
            # ``fresh`` was read against a folder with no workbook in it, so every
            # table came back at height zero -- which is exactly the padding a brand
            # new tab needs. No second read required, and none for the tab names
            # either: we just wrote them.
            _new_id, made = self._create_workbook()
            present = set(made)
        else:
            added = [n for n in names if n not in present]
            if added:
                self._ensure_tabs(added)
                present |= set(added)

        fid = self._require_workbook_id()
        payloads = []
        new_revisions: dict[str, str] = {}
        for name, header, rows, _expected in changes:
            # Padding to the height we read covers whatever this write shortens away,
            # so the stale rows are blanked by the same request that writes the new ones.
            grid = _table_to_grid(tuple(header), rows,
                                  1 + len(fresh.get(name, _absent(name)).rows))
            payloads.append({"range": self._range(name), "values": grid})
            new_revisions[name] = compute_revision(tuple(header), rows)

        self._retry(
            self._sheets.spreadsheets().values().batchUpdate(
                spreadsheetId=fid,
                body={"valueInputOption": "RAW", "includeValuesInResponse": False,
                      "data": payloads},
            )
        )
        if written is not None:
            # Either all of it landed or the request raised and none of it is claimed
            # to have; there is no intermediate state to report from a single call.
            written.update(new_revisions)

        # Verify what landed, on create as much as on update. Sheets can coerce
        # values on the way in and Drive can convert a file type, and either one
        # means the bytes we believe are stored are not the bytes we validated --
        # which would make the next revision comparison silently wrong.
        after = self._read_many(names, present=present)
        landed_state = {n: after.get(n) or _absent(n) for n in names}
        wrong = [n for n in names if landed_state[n].revision != new_revisions[n]]
        if wrong:
            reverted, stuck = self._restore(fresh, landed_state)
            detail = ""
            if reverted:
                detail += f"\nReverted: {', '.join(reverted)}."
            if stuck:
                detail += (f"\nCOULD NOT REVERT: {', '.join(stuck)} -- they changed "
                           "again while this write was aborting. Run doctor before "
                           "writing more; do not overwrite by hand.")
            raise StoreError(
                f"Drive stored a different rendering of table {wrong[0]!r} than we sent "
                f"({_short(landed_state[wrong[0]].revision)} "
                f"vs {_short(new_revisions[wrong[0]])})"
                + (f" (also {', '.join(wrong[1:])})" if len(wrong) > 1 else "")
                + ". A spreadsheet conversion or coercion changed the content -- inspect "
                f"the {WORKBOOK_NAME} workbook before writing again." + detail
            )
        if not existed:
            # The folder listing is a page behind now that this workbook is in it;
            # the resolved id stays, because re-resolving would cost a listing to
            # rediscover something we just created ourselves.
            self._listing = None
        return new_revisions

    def _restore(self, before: dict[str, TableData], after: dict[str, TableData]
                 ) -> tuple[list[str], list[str]]:
        """Push back the content a refused write replaced. Best effort, said out loud.

        Tables that had no header before this write get emptied rather than rewritten:
        the tab itself may be something this write created, and deleting a tab through
        the values API is not a thing -- an empty tab is the shape a fresh table has.
        Tables that never answered the pre-write read are in that category too, which
        is the case that matters most: on a create, nothing existed to go back to, so
        "put it back" means "leave nothing behind".
        """
        names = list(before) + [n for n in after if n not in before]
        updates, cleared = [], []
        for name in names:
            data = before.get(name)
            if data is None or not data.header:
                cleared.append(name)
                continue
            grid = _table_to_grid(data.header, list(data.rows),
                                  1 + len(after.get(name, data).rows))
            updates.append({"range": self._range(name), "values": grid})

        #: What came out of this tells the caller which tables are safe to write to
        #: again and which are in an unknown state, so the two halves are tracked
        #: separately -- a failed revert of one table must not mark the others stuck.
        reverted: list[str] = []
        stuck: list[str] = []
        restored = [n for n in names if before.get(n) and before[n].header]
        try:
            if updates:
                self._retry(
                    self._sheets.spreadsheets().values().batchUpdate(
                        spreadsheetId=self._require_workbook_id(),
                        body={"valueInputOption": "RAW", "includeValuesInResponse": False,
                              "data": updates},
                    )
                )
            reverted += restored
        except StoreError:
            stuck += restored
        for name in cleared:
            try:
                self._retry(
                    self._sheets.spreadsheets().values().clear(
                        spreadsheetId=self._require_workbook_id(),
                        range=self._range(name),
                    )
                )
                reverted.append(name)
            except StoreError:
                stuck.append(name)
        return reverted, stuck

    # -- undo ----------------------------------------------------------------

    def snapshot(self, tables: Iterable[str], operation: str) -> Optional[str]:
        """Copy the workbook before it changes, which is the undo Drive never had.

        The local backend copies files into ``backups/`` and the remote one reported
        ``snapshot: none`` and pointed at Drive's version history -- reasonable while
        only the server wrote. Once colleagues are editing cells by hand, "copy the
        file" is the one recovery they can perform themselves, so the store does it
        here for one extra request per commit. ``keep_backups: 0`` opts out.
        """
        keep = max(int(getattr(self.config, "keep_backups", 20) or 0), 0)
        if keep <= 0:
            return None
        fid = self.workbook_id()
        if fid is None:
            return None  # nothing exists yet, so there is nothing to preserve
        folder = self._backup_folder_id()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        name = f"{stamp}-{operation}"
        created = self._retry(
            self._drive.files().copy(
                fileId=fid, body={"name": name, "parents": [folder]},
                fields="id", supportsAllDrives=True,
            )
        )
        # The folder listing stays cached: the copy is a child of the backup
        # subfolder, so nothing the database-level listing claims to know changed.
        # Dropping it here would cost a listing per commit to learn nothing.
        return f"drive:{BACKUP_FOLDER_NAME}/{name}#{created['id']}"

    def prune_snapshots(self) -> int:
        """Drop the oldest copies past ``keep_backups``. Run by init, not by commits.

        Checking on every write would add a listing to every commit to discover a
        number that almost never changed; setup is where the surplus actually piles
        up, and it is an operator command either way.
        """
        keep = max(int(getattr(self.config, "keep_backups", 20) or 0), 0)
        if self.workbook_id() is None:
            return 0
        folder = self._backup_folder_id(create=False)
        if folder is None:
            return 0
        resp = self._retry(
            self._drive.files().list(
                q=f"'{folder}' in parents and trashed = false",
                fields="files(id,name,createdTime)",
                orderBy="createdTime desc",
                pageSize=1000,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
        )
        surplus = resp.get("files", [])[keep:]
        for entry in surplus:
            self._retry(self._drive.files().delete(fileId=entry["id"],
                                                   supportsAllDrives=True))
        return len(surplus)

    def _backup_folder_id(self, *, create: bool = True) -> Optional[str]:
        entry = self._list_folder().get(BACKUP_FOLDER_NAME)
        if entry:
            return entry["id"]
        if not create:
            return None
        made = self._retry(
            self._drive.files().create(
                body={"name": BACKUP_FOLDER_NAME,
                      "mimeType": "application/vnd.google-apps.folder",
                      "parents": [self.folder_id]},
                fields="id", supportsAllDrives=True,
            )
        )
        self._listing = None
        return made["id"]


def verify_folder(config) -> dict:
    """Ask Drive for the folder itself, before anything is created in it.

    Listing is not enough. A folder id that is wrong lists empty, which is exactly
    what a correct-but-new folder looks like, so ``init`` proceeds and fails on its
    first ``files().create`` with a parent Google has never heard of -- an error
    naming neither the flag nor the folder. Reading the folder by id separates
    "nothing in it yet" from "no such folder", and gives back its name, which is
    how you tell the right folder from a similarly-named one.
    """
    folder = (getattr(config, "drive_folder_id", "") or "").strip()
    if not folder:
        raise StoreError("drive backends need drive_folder_id (a folder id or URL)")
    try:
        meta = drive_service(config).files().get(
            fileId=folder, fields="id,name,mimeType,capabilities(canEdit)",
            supportsAllDrives=True
        ).execute()
    except AuthError:
        raise
    except Exception as exc:  # noqa: BLE001 - mapped below, by status
        status = getattr(getattr(exc, "resp", None), "status", None)
        detail = getattr(exc, "reason", None) or str(exc)
        if status == 404:
            raise StoreError(
                f"no such Drive folder: {folder}. Either the id is wrong or the "
                "folder is not shared with the account that authorised. Open it in "
                "the browser and copy the id from its URL."
            ) from exc
        if status == 403:
            raise StoreError(
                f"Drive would not show the folder {folder} to this account ({detail}). "
                "It needs to be shared with that account, with edit access."
            ) from exc
        raise StoreError(f"could not read the Drive folder {folder} ({status}): {detail}") from exc
    mime = meta.get("mimeType") or ""
    if mime and mime != "application/vnd.google-apps.folder":
        raise StoreError(
            f"{folder} is a {mime.replace('application/vnd.google-apps.', '')}, not a "
            "folder. Point --drive-folder at a folder; the database goes inside it."
        )
    # Only an explicit False counts: Drive omits capabilities it will not promise,
    # and a missing key must not be read as "read-only".
    if (meta.get("capabilities") or {}).get("canEdit") is False:
        named = meta.get("name") or folder
        raise StoreError(
            f"the folder {named!r} is read-only for this account, and init has to "
            f"create the {WORKBOOK_NAME} spreadsheet inside it. Get edit access, or "
            "point --drive-folder at a folder this account can write to."
        )
    return meta
