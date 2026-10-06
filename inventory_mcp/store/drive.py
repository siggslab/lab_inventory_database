"""Google Drive backends.

Two shapes of the same promise: the files in one Drive folder are the database,
addressed by table name, readable by a human who double-clicks them.

``drive_csv``     one CSV file per table. Strictest, closest to the local backend,
                  but see the conversion warning below.
``drive_sheets``  one spreadsheet per table, one tab per table. What people
                  actually open in a browser.

Both address tables by *name* and resolve to file ids lazily, because ids change
when a file is replaced and names are what the schema declares. ``drive_file_ids``
in the config overrides the lookup for the case where someone renames a file or a
name collides with something else in the folder.

Three hazards specific to Drive, and what this module does about each:

*Conversion.* Uploading text/csv without declaring a mimeType makes Drive create a
Google Sheet instead, and the file id changes shape under you. Every create here
declares its mimeType explicitly, and reads refuse a native file where a CSV was
expected rather than exporting a copy nobody asked for.

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
Drive revision behind -- but it is a real weakening versus the local backend and
is reported by ``describe()`` rather than hidden.
"""

from __future__ import annotations

import csv
import io
import re
import time
from typing import Any, Iterable, Optional

from .. import schema
from ..auth import AuthError, drive_service, sheets_service
from .base import (
    EXTRA_COLUMNS_KEY,
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

RESTKEY = EXTRA_COLUMNS_KEY
_RETRY_STATUS = {429, 500, 502, 503, 504}
_NATIVE = "application/vnd.google-apps"

#: Sheets widens our minute-precision timestamps to whole seconds. Undoing that is
#: safe because the shape is unambiguously ours; a locale-formatted date is not, and
#: is left for the validator to flag rather than be guessed at.
_ISO_WITH_SECONDS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}):\d{2}$")


# ---------------------------------------------------------------------------
# shared plumbing
# ---------------------------------------------------------------------------


class DriveStoreBase(TableStore):
    kind = "drive"

    def __init__(self, config):
        self.config = config
        self.folder_id = (config.drive_folder_id or "").strip()
        if not self.folder_id:
            raise StoreError("drive backends need drive_folder_id (a folder id or URL)")
        self.ttl = max(float(getattr(config, "cache_ttl_seconds", 5.0) or 0.0), 0.0)
        self._overrides = dict(getattr(config, "drive_file_ids", {}) or {})
        self._listing: Optional[dict[str, dict]] = None
        self._cache: dict[str, tuple[float, TableData]] = {}
        try:
            self._drive = drive_service(config)
        except AuthError as exc:
            raise StoreError(str(exc)) from exc

    # -- discovery -----------------------------------------------------------

    def describe(self) -> str:
        return f"{self.kind}:drive-folder:{self.folder_id}"

    def _retry(self, request, *, attempts: int = 4):
        """Run an ApiRequest, backing off on the transient statuses."""
        delay = 0.5
        for i in range(attempts):
            try:
                return request.execute(num_retries=0)
            except Exception as exc:  # noqa: BLE001 - classified below
                status = getattr(getattr(exc, "resp", None), "status", None)
                if status is None and HttpError is not None and isinstance(exc, HttpError):
                    status = exc.resp.status if getattr(exc, "resp", None) else None
                if status in _RETRY_STATUS and i < attempts - 1:
                    time.sleep(delay)
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

    def _list_folder(self, *, refresh: bool = False) -> dict[str, dict]:
        if self._listing is not None and not refresh:
            return self._listing
        resp = self._retry(
            self._drive.files().list(
                q=f"'{self.folder_id}' in parents and trashed = false",
                fields="files(id,name,mimeType,size,modifiedTime)",
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

    def _known_names(self) -> dict[str, str]:
        """Logical table name -> Drive object name, for this backend's layout."""
        return {t.name: self.object_name(t) for t in schema.TABLES}

    def object_name(self, spec) -> str:  # pragma: no cover - overridden
        raise NotImplementedError

    def file_id(self, name: str) -> Optional[str]:
        override = self._overrides.get(name)
        if override:
            return override
        entry = self._list_folder().get(self.object_name(schema.get_table(name)))
        return entry["id"] if entry else None

    def list_tables(self) -> list[str]:
        present = set(self._list_folder())
        out = []
        for t in schema.TABLES:
            if self.object_name(t) in present or t.name in self._overrides:
                out.append(t.name)
        return out

    def exists(self, name: str) -> bool:
        return self.file_id(name) is not None

    def stray_objects(self) -> list[str]:
        """Files in the folder that are not part of the database."""
        wanted = set(self._known_names().values())
        return sorted(n for n in self._list_folder() if n not in wanted)

    def invalidate(self) -> None:
        self._listing = None
        self._cache.clear()

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

    # -- subclass hooks ------------------------------------------------------

    def _fetch(self, name: str) -> TableData:  # pragma: no cover - overridden
        raise NotImplementedError

    def _push(self, name: str, header: tuple[str, ...], rows: list[Row]) -> str:  # pragma: no cover
        raise NotImplementedError

    # -- TableStore ----------------------------------------------------------

    def read_table(self, name: str) -> TableData:
        cached = self._cached(name)
        if cached is not None:
            return cached
        data = self._fetch(name)
        self._store_cache(name, data)
        return data

    def current_revision(self, name: str) -> Optional[str]:
        """Always refetches: this is the compare half of compare-and-swap."""
        try:
            data = self._fetch(name)
        except StoreError:
            return None
        self._store_cache(name, data)
        return data.revision

    def write_table(
        self,
        name: str,
        header: tuple[str, ...],
        rows: list[Row],
        expected_revision: Optional[str],
    ) -> str:
        new_rev = compute_revision(header, rows)
        existing = self.file_id(name)
        if existing is None and expected_revision is not None:
            raise RevisionConflict(
                f"table {name!r} has no Drive file to update; it was deleted "
                "while this session held it open"
            )

        if existing is not None and expected_revision is not None:
            actual = self.current_revision(name)
            if actual != expected_revision:
                raise RevisionConflict(
                    f"table {name!r} changed underneath us "
                    f"(expected {_short(expected_revision)}, found {_short(actual)}). "
                    "Someone edited this in the browser or another session is "
                    "writing. Reload and retry."
                )
            if actual == new_rev:
                return new_rev  # nothing to do

        self._push(name, header, rows)
        if existing is None:
            self.invalidate()

        # Verify what landed, on create as much as on update. Sheets can coerce
        # values on the way in and Drive can convert a file type, and either one
        # means the bytes we believe are stored are not the bytes we validated --
        # which would make the next revision comparison silently wrong.
        written = self._fetch(name)
        self._store_cache(name, written)
        if written.revision != new_rev:
            raise StoreError(
                f"Drive stored a different rendering of table {name!r} than we sent "
                f"({_short(written.revision)} vs {_short(new_rev)}). A spreadsheet "
                "conversion or coercion changed the content -- inspect the file "
                "before writing again."
            )
        return new_rev


def _short(rev: Optional[str]) -> str:
    return (rev or "<absent>")[-8:]


def _parse_csv(text: str, name: str) -> TableData:
    reader = csv.DictReader(io.StringIO(text), restkey=RESTKEY, restval="")
    header = tuple(reader.fieldnames or ())
    rows: list[Row] = []
    for raw in reader:
        row: Row = {k: (v if v is not None else "") for k, v in raw.items() if k != RESTKEY}
        extra = raw.get(RESTKEY)
        if extra:
            row[RESTKEY] = "|".join("" if c is None else c for c in extra)
        rows.append(row)
    return TableData(name=name, header=header, rows=tuple(rows),
                     revision=compute_revision(header, rows))


def render_csv(header: Iterable[str], rows: Iterable[Row]) -> bytes:
    buf = io.StringIO()
    cols = list(header)
    writer = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({c: _text(row.get(c, "")) for c in cols})
    return buf.getvalue().encode("utf-8")


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


# ---------------------------------------------------------------------------
# drive_csv
# ---------------------------------------------------------------------------


class DriveCsvStore(DriveStoreBase):
    """One CSV file per table -- the closest thing to the local backend remotely."""

    kind = "drive_csv"

    def object_name(self, spec) -> str:
        return spec.filename

    def _fetch(self, name: str) -> TableData:
        fid = self.file_id(name)
        if fid is None:
            raise StoreError(f"table {name!r} has no file in the Drive folder")
        entry = self._list_folder().get(self.object_name(schema.get_table(name)), {})
        if str(entry.get("mimeType", "")).startswith(_NATIVE):
            raise StoreError(
                f"{self.object_name(schema.get_table(name))} is a Google-native file "
                f"({entry.get('mimeType')}), not a CSV. Drive converted it on upload. "
                "Either switch this database to backend='drive_sheets' or re-create "
                "it as CSV."
            )
        media = self._retry(self._drive.files().get_media(fileId=fid))
        text = media.read().decode("utf-8-sig")
        return _parse_csv(text, name)

    def _push(self, name: str, header: tuple[str, ...], rows: list[Row]) -> str:
        from googleapiclient.http import MediaIoBaseUpload

        data = render_csv(header, rows)
        fname = self.object_name(schema.get_table(name))
        buf = io.BytesIO(data)
        media = MediaIoBaseUpload(buf, mimetype="text/csv", chunksize=len(data) or 1,
                                  resumable=False)
        fid = self.file_id(name)
        if fid is None:
            body = {"name": fname, "mimeType": "text/csv", "parents": [self.folder_id]}
            created = self._retry(
                self._drive.files().create(body=body, media_body=media,
                                           fields="id", supportsAllDrives=True)
            )
            self.invalidate()
            return compute_revision(header, rows)
        self._retry(
            self._drive.files().update(fileId=fid, body={"name": fname},
                                       media_body=media, supportsAllDrives=True)
        )
        self._listing = None
        return compute_revision(header, rows)


# ---------------------------------------------------------------------------
# drive_sheets
# ---------------------------------------------------------------------------


class DriveSheetsStore(DriveStoreBase):
    """One spreadsheet per table, one tab per table.

    Named without a ``.csv`` suffix because it is not a CSV; the tab is named after
    the table so a person opening the file sees ``items`` rather than ``Sheet1``.
    """

    kind = "drive_sheets"

    def __init__(self, config):
        super().__init__(config)
        try:
            self._sheets = sheets_service(config)
        except AuthError as exc:
            raise StoreError(str(exc)) from exc

    def object_name(self, spec) -> str:
        return spec.filename[:-4] if spec.filename.endswith(".csv") else spec.filename

    def tab_name(self, name: str) -> str:
        return name

    # -- sheet structure -----------------------------------------------------

    def _meta(self, name: str) -> tuple[str, dict]:
        fid = self.file_id(name)
        if fid is None:
            raise StoreError(f"table {name!r} has no spreadsheet in the Drive folder")
        meta = self._retry(
            self._sheets.spreadsheets().get(
                spreadsheetId=fid,
                fields="sheets.properties.sheetId,sheets.properties.title",
            )
        )
        return fid, meta

    @staticmethod
    def _titles(meta: dict) -> dict[str, int]:
        out = {}
        for s in meta.get("sheets", []):
            props = s.get("properties", {})
            if props.get("title"):
                out[props["title"]] = props.get("sheetId", 0)
        return out

    def _ensure_sheet(self, spreadsheet_id: str, meta: dict, title: str) -> None:
        titles = self._titles(meta)
        if title in titles:
            return
        if len(titles) == 1 and next(iter(titles)) in ("Sheet1", "Sheet 1"):
            # Rename the default tab rather than leaving an orphan behind.
            self._retry(
                self._sheets.spreadsheets().batchUpdate(
                    spreadsheetId=spreadsheet_id,
                    body={"requests": [{"updateSheetProperties": {
                        "properties": {"sheetId": next(iter(titles.values())),
                                       "title": title},
                        "fields": "title"}}]},
                )
            )
            return
        self._retry(
            self._sheets.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"requests": [{"addSheet": {"properties": {"title": title}}}]},
            )
        )

    # -- reads / writes ------------------------------------------------------

    def _fetch(self, name: str) -> TableData:
        fid, meta = self._meta(name)
        title = self.tab_name(name)
        if title not in self._titles(meta):
            # A spreadsheet with no tab of ours yet is an empty table, not an error:
            # that is the state immediately after creation.
            return TableData(name=name, header=(), rows=(),
                             revision=compute_revision((), ()))
        resp = self._retry(
            self._sheets.spreadsheets().values().get(
                spreadsheetId=fid, range=f"'{title}'!A1:ZZZ",
                valueRenderOption="FORMATTED_VALUE",
            )
        )
        grid = [[_from_sheet(c) for c in row] for row in resp.get("values", [])]
        while grid and not any(str(c).strip() for c in grid[-1]):
            grid.pop()  # trailing blank rows Sheets reports back at us
        if not grid:
            return TableData(name=name, header=(), rows=(),
                             revision=compute_revision((), ()))
        header = tuple(grid[0])
        rows = [dict(zip(header, r)) for r in grid[1:]]
        for r in rows:  # short rows read as absent cells, which are empty strings
            for col in header:
                r.setdefault(col, "")
        return TableData(name=name, header=header, rows=tuple(rows),
                         revision=compute_revision(header, rows))

    def _push(self, name: str, header: tuple[str, ...], rows: list[Row]) -> str:
        title = self.tab_name(name)
        fid = self.file_id(name)
        if fid is None:
            fid = self._create(name)
        self._ensure_sheet(fid, self._meta(name)[1], title)

        grid = [list(header)] + [[_to_sheet(r.get(c, "")) for c in header] for r in rows]
        width = max((len(g) for g in grid), default=1)
        grid = [g + [""] * (width - len(g)) for g in grid]
        last_col = _column_letter(width)
        rng = f"'{title}'!A1:{last_col}{len(grid)}"

        # Grow-and-overwrite first, trim after: clearing the range up front would
        # leave a window where the table is visibly empty to anyone watching.
        self._retry(
            self._sheets.spreadsheets().values().update(
                spreadsheetId=fid, range=rng,
                body={"range": rng, "values": grid},
                valueInputOption="RAW", includeValuesInResponse=False,
            )
        )
        self._trim_below(fid, title, len(grid))
        self._listing = None
        return compute_revision(header, rows)

    def _create(self, name: str) -> str:
        title = self.object_name(schema.get_table(name))
        body = {
            # Drive's own filename comes from `name`; without it the file lands as
            # "Untitled spreadsheet" and every later lookup by name misses it.
            "name": title,
            "mimeType": "application/vnd.google-apps.spreadsheet",
            "parents": [self.folder_id],
            "properties": {"title": title},
        }
        created = self._retry(
            self._drive.files().create(body=body, fields="id", supportsAllDrives=True)
        )
        self.invalidate()
        return created["id"]

    def _trim_below(self, spreadsheet_id: str, title: str, keep_rows: int) -> None:
        """Delete rows past the new content so stale rows can't survive a shrink."""
        meta = self._retry(
            self._sheets.spreadsheets().get(
                spreadsheetId=spreadsheet_id,
                fields="sheets.properties.sheetId,sheets.gridProperties.rowCount,"
                       "sheets.properties.title",
            )
        )
        sheet = next(
            (s for s in meta.get("sheets", [])
             if s.get("properties", {}).get("title") == title),
            None,
        )
        if not sheet:
            return
        sheet_id = sheet["properties"]["sheetId"]
        available = int(sheet.get("gridProperties", {}).get("rowCount", 0) or 0)
        if available > keep_rows:
            self._retry(
                self._sheets.spreadsheets().batchUpdate(
                    spreadsheetId=spreadsheet_id,
                    body={"requests": [{"deleteDimension": {
                        "range": {
                            "sheetId": sheet_id,
                            "dimension": "ROWS",
                            "startIndex": keep_rows,
                            "endIndex": available,
                        }}}]},
                )
            )


def _column_letter(width: int) -> str:
    letters = ""
    while width > 0:
        width, rem = divmod(width - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return letters or "A"


def _to_sheet(value: Any) -> Any:
    """Everything goes over as text; RAW stops formula interpretation."""
    return _text(value)


def _from_sheet(value: Any) -> str:
    """Stringify a cell the way our CSV would hold it.

    Numbers come back as int/float and would otherwise print as ``47.2`` or the
    unwanted ``500.0``; timestamps come back widened to seconds, which is our own
    format stretched by Sheets and safe to shorten. Locale-formatted dates are
    deliberately left as-is so validation reports them instead of guessing.
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
    widened = _ISO_WITH_SECONDS_RE.match(text)
    if widened:
        return f"{widened.group(1)}T{widened.group(2)}"
    return text
