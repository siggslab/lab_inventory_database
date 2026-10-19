"""The Drive backend exercised against a fake Drive/Sheets service.

No network, no credentials. The point is to get the awkward paths -- coercion,
revision conflicts, shrink-trim, absent tabs -- right in front of a deterministic
stand-in, so that the first time they run against a real folder they are being
*verified* rather than discovered.

The fakes mirror the shape of the discovery client: ``files()`` / ``spreadsheets()``
/ ``values()`` return resource objects, each owning its own methods. Keeping them
separate matters -- ``spreadsheets().get`` and ``values().get`` take different
arguments, and collapsing them into one object hides exactly the kind of bug this
suite exists to catch.

Every recorded call lands in ``drive.calls``, because request volume is a property
worth testing: this backend exists partly because reading twelve tables used to
cost twelve round trips, and nothing but a counted assertion stops that creeping
back.
"""

from __future__ import annotations

from collections import Counter

import pytest

from inventory_mcp import schema
from inventory_mcp.config import Config
from inventory_mcp.initdb import init_database
from inventory_mcp.store.base import (
    ForeignTableError,
    RevisionConflict,
    StoreError,
    compute_revision,
)
from inventory_mcp.store.drive import (
    BACKUP_FOLDER_NAME,
    RESERVED_TABS,
    WORKBOOK_NAME,
    DriveWorkbookStore,
    _from_sheet,
    column_letter,
    verify_folder,
)

#: The two Google-native object types this backend meets.
SHEET = "application/vnd.google-apps.spreadsheet"
FOLDER = "application/vnd.google-apps.folder"


class HttpErr(Exception):
    def __init__(self, status, reason="boom"):
        self.resp = type("R", (), {"status": status})()
        self.reason = reason


class FakeRequest:
    def __init__(self, fn):
        self._fn = fn

    def execute(self, num_retries=0):
        return self._fn()


class FakeSheet:
    """One workbook: named tabs holding grids, with a stable id per tab."""

    def __init__(self):
        self.tabs: dict[str, list[list]] = {"Sheet1": []}
        self.ids: dict[str, int] = {"Sheet1": 100}
        self.hidden: set[str] = set()
        #: Layout, kept as it was asked for. Formatting is a thing we *send*, not a
        #: thing the fake renders, so what a test can meaningfully assert is that the
        #: right request went out for the right range -- which is exactly the failure
        #: mode worth catching (a dropdown on the wrong column looks fine until
        #: somebody types into it).
        self.validations: dict[str, list[tuple[dict, dict]]] = {}
        self.formats: dict[str, list[tuple[dict, dict]]] = {}
        self.frozen_rows: dict[str, int] = {}
        #: Whether a tab asked for its gridlines to be hidden -- the difference
        #: between a form and a spreadsheet, as far as a first-time reader is concerned.
        self.gridlines: dict[str, bool] = {}
        self.dimensions: list[tuple[str, dict, dict]] = []
        self._next = 101

    def add(self, title, hidden=False, index=None):
        """Append a tab, or place it at ``index`` -- dict order *is* the tab order here."""
        if index is None or index >= len(self.tabs):
            self.tabs[title] = []
            self.ids[title] = self._next
        else:
            keys = [k for k in self.tabs if k != title]
            keys.insert(index, title)
            self.tabs = {k: ([] if k == title else self.tabs[k]) for k in keys}
            self.ids = {k: (self._next if k == title else self.ids[k]) for k in keys}
        self._next += 1
        if hidden:
            self.hidden.add(title)

    def delete(self, title):
        """Remove a tab and everything filed under its name, as Sheets does."""
        del self.tabs[title]
        del self.ids[title]
        self.hidden.discard(title)
        for bucket in (self.validations, self.formats, self.frozen_rows, self.gridlines):
            bucket.pop(title, None)
        self.dimensions = [entry for entry in self.dimensions if entry[0] != title]

    def move(self, title, index):
        keys = [k for k in self.tabs if k != title]
        keys.insert(max(0, min(index, len(keys))), title)
        self.tabs = {k: self.tabs[k] for k in keys}
        self.ids = {k: self.ids[k] for k in keys}

    def title_of(self, sheet_id) -> str:
        """The tab an id names -- or a readable failure, not a StopIteration."""
        for title, got in self.ids.items():
            if got == sheet_id:
                return title
        raise AssertionError(f"request named sheetId {sheet_id!r}; tabs are {sorted(self.ids)}")

    def rename(self, old, new):
        # Rebuilding rather than pop-and-insert: a rename keeps a tab where it was,
        # and the order is what puts Start in front of the data tabs.
        self.tabs = {new if k == old else k: v for k, v in self.tabs.items()}
        self.ids = {new if k == old else k: v for k, v in self.ids.items()}
        for bucket in (self.validations, self.formats, self.frozen_rows, self.gridlines):
            if old in bucket:
                bucket[new] = bucket.pop(old)
        if old in self.hidden:
            self.hidden.discard(old)
            self.hidden.add(new)
        self.dimensions = [(new if t == old else t, r, p) for t, r, p in self.dimensions]

    def copy(self) -> "FakeSheet":
        out = FakeSheet()
        out.tabs = {k: [list(r) for r in v] for k, v in self.tabs.items()}
        out.ids = dict(self.ids)
        out.hidden = set(self.hidden)
        out.validations = {k: list(v) for k, v in self.validations.items()}
        out.formats = {k: list(v) for k, v in self.formats.items()}
        out.frozen_rows = dict(self.frozen_rows)
        out.gridlines = dict(self.gridlines)
        out.dimensions = list(self.dimensions)
        out._next = self._next
        return out


class FakeDrive:
    def __init__(self):
        self.by_id: dict[str, dict] = {}
        self.next_id = 1
        self.fail_next: list[Exception] = []
        #: errors raised on every call, for the case that never clears
        self.always_fail: list[Exception] = []
        #: every API call made, Drive or Sheets, in order
        self.calls: list[tuple[str, dict]] = []

    def record(self, name: str, kw: dict):
        self.calls.append((name, kw))

    def counts(self) -> Counter:
        return Counter(name for name, _ in self.calls)

    def forget_calls(self):
        self.calls.clear()

    def new_id(self) -> str:
        self.next_id += 1
        return f"fid{self.next_id}"

    def add(self, name, content=b"", mime="text/csv", sheet=None, parent="FOLDERID",
            properties=None):
        fid = self.new_id()
        self.by_id[fid] = {"id": fid, "name": name, "mimeType": mime,
                           "content": content, "sheet": sheet, "parent": parent,
                           "properties": properties or {}}
        return fid

    def workbook(self, tabs=None, name=WORKBOOK_NAME, parent="FOLDERID", marker=True):
        """A spreadsheet in the folder, optionally pre-filled with named tabs.

        ``marker`` is the title property this app stamps on files it created -- the
        only thing that tells an empty workbook of ours apart from somebody else's
        blank sheet wearing the same name.
        """
        sheet = FakeSheet()
        sheet.tabs = {"Sheet1": []}
        sheet.ids = {"Sheet1": 100}
        for title, grid in (tabs or {}).items():
            sheet.tabs[title] = [list(r) for r in grid]
            sheet.ids[title] = sheet._next
            sheet._next += 1
        if tabs:
            # A spreadsheet always has at least one sheet, so the default tab is only
            # dropped once something else is there to hold the file open.
            sheet.tabs.pop("Sheet1", None)
            sheet.ids.pop("Sheet1", None)
        return self.add(name, mime=SHEET, sheet=sheet, parent=parent,
                        properties={"title": WORKBOOK_NAME} if marker else {})

    def here(self, name=WORKBOOK_NAME) -> dict:
        return next(e for e in self.by_id.values() if e["name"] == name)

    def tabs(self, name=WORKBOOK_NAME) -> dict[str, list[list]]:
        return self.here(name)["sheet"].tabs

    def in_backup_folder(self) -> list[dict]:
        folder = next((f for f in self.by_id.values()
                       if f["name"] == BACKUP_FOLDER_NAME), None)
        if folder is None:
            return []
        return [f for f in self.by_id.values() if f["parent"] == folder["id"]]

    def files(self):
        return _Files(self)


class _Files:
    def __init__(self, drive: FakeDrive):
        self.d = drive

    def _guard(self, ok):
        """Decide failure at execute() time, so a retry can actually succeed."""
        d = self.d

        def run():
            if d.always_fail:
                raise d.always_fail[0]
            if d.fail_next:
                raise d.fail_next.pop(0)
            return ok()

        return FakeRequest(run)

    def list(self, q="", fields="", pageSize=1000, **kw):
        self.d.record("files.list", kw)
        parent = q.split("'")[1]
        entries = [f for f in self.d.by_id.values() if f["parent"] == parent]
        if str(kw.get("orderBy", "")).startswith("createdTime desc"):
            entries = list(reversed(entries))  # insertion order is creation order here
        return self._guard(lambda: {"files": [
            # `properties` matters: it carries the title marker this app stamps on
            # files it created, which is how init tells its own workbook from a
            # stranger's wearing the same name.
            {k: v for k, v in f.items()
             if k in ("id", "name", "mimeType", "properties") and v}
            for f in entries
        ]})

    def create(self, body=None, fields="", **kw):
        self.d.record("files.create", kw)
        mime = (body or {}).get("mimeType", "")

        def run():
            fid = self.d.new_id()
            self.d.by_id[fid] = {
                "id": fid,
                "name": body["name"],
                "mimeType": mime,
                "content": b"",
                "parent": (body.get("parents") or ["root"])[0],
                # Whatever custom file properties were asked for, kept: the store
                # stamps a title marker here and reads it back before adopting.
                "properties": dict(body.get("properties") or {}),
                "sheet": FakeSheet() if mime == SHEET else None,
            }
            return {"id": fid}

        return self._guard(run)

    def copy(self, fileId, body=None, fields="", **kw):
        self.d.record("files.copy", kw)

        def run():
            src = self.d.by_id[fileId]
            fid = self.d.new_id()
            self.d.by_id[fid] = {
                "id": fid,
                "name": (body or {}).get("name", src["name"]),
                "mimeType": src["mimeType"],
                "content": src["content"],
                "parent": ((body or {}).get("parents") or [src["parent"]])[0],
                "properties": dict(src.get("properties") or {}),
                # Deep copy: a backup that shares grids with the live workbook is
                # not a backup, it is a second name for the same rows.
                "sheet": src["sheet"].copy() if src.get("sheet") else None,
            }
            return {"id": fid}

        return self._guard(run)

    def delete(self, fileId, **kw):
        self.d.record("files.delete", kw)

        def run():
            self.d.by_id.pop(fileId, None)
            return {}

        return self._guard(run)

    def get(self, fileId, fields="", **kw):
        """Read one object's metadata, which is how a folder gets verified."""
        self.d.record("files.get", {"fileId": fileId, "fields": fields})
        entry = self.d.by_id.get(fileId)

        def run():
            if entry is None:
                raise HttpErr(404, "itemNotFound")
            meta = {k: entry[k] for k in ("id", "name", "mimeType") if k in entry}
            # Absent unless a test sets it: Drive omits capabilities it will not
            # promise, and the code under test must treat that as unknown, not False.
            if "canEdit" in entry:
                meta["capabilities"] = {"canEdit": entry["canEdit"]}
            return meta

        return self._guard(run)


class FakeSheets:
    def __init__(self, drive: FakeDrive, mangle: bool = False):
        self.d = drive
        self.mangle = mangle

    def spreadsheets(self):
        return _Spreadsheets(self.d, self.mangle)


class _Spreadsheets:
    """``spreadsheets`` owns ``values`` as a sub-resource, as in the real client."""

    def __init__(self, drive: FakeDrive, mangle: bool = False):
        self.d = drive
        self.mangle = mangle

    def values(self):
        return _Values(self.d, mangle=self.mangle)

    def get(self, spreadsheetId, fields="", **kw):
        self.d.record("spreadsheets.get", {"spreadsheetId": spreadsheetId})
        sheet = self.d.by_id[spreadsheetId]["sheet"]
        return FakeRequest(lambda: {
            "sheets": [
                {"properties": {
                    "sheetId": sheet.ids[title],
                    "title": title,
                    "hidden": title in sheet.hidden,
                    "gridProperties": {"rowCount": max(len(grid), 1000)},
                }}
                for title, grid in sheet.tabs.items()
            ]
        })

    def batchUpdate(self, spreadsheetId, body=None, **kw):
        self.d.record("spreadsheets.batchUpdate", {"n": len((body or {}).get("requests", []))})
        sheet = self.d.by_id[spreadsheetId]["sheet"]
        replies: list[dict] = []
        for req in body.get("requests", []):
            problem = _shape_error(req)
            if problem:
                raise AssertionError(f"batchUpdate would be refused: {problem}\n{req!r}")
            reply: dict = {}
            if "addSheet" in req:
                props = req["addSheet"]["properties"]
                if props["title"] in sheet.tabs:
                    # The reason `ui --reset` deletes before it adds: a second tab with
                    # the same name is refused outright, not merged.
                    raise AssertionError(
                        f"addSheet would be refused: {props['title']!r} is already a tab")
                sheet.add(props["title"], hidden=bool(props.get("hidden")),
                          index=props.get("index"))
                # A recreated tab gets a new number, which is why anything addressing a
                # tab by id has to read it back from here rather than remember the old one.
                reply = {"addSheet": {"properties": {
                    "sheetId": sheet.ids[props["title"]], "title": props["title"]}}}
            elif "deleteSheet" in req:
                sheet_id = req["deleteSheet"]["sheetId"]
                if len(sheet.tabs) <= 1:
                    raise AssertionError(
                        "deleteSheet would be refused: a spreadsheet needs one tab")
                sheet.delete(sheet.title_of(sheet_id))
            elif "updateSheetProperties" in req:
                up = req["updateSheetProperties"]["properties"]
                title = sheet.title_of(up["sheetId"])
                if "title" in up:
                    sheet.rename(title, up["title"])
                    title = up["title"]
                if "index" in up:
                    sheet.move(title, up["index"])
                if "hidden" in up:
                    (sheet.hidden.add if up["hidden"] else sheet.hidden.discard)(title)
                grid = up.get("gridProperties") or {}
                if "frozenRowCount" in grid:
                    sheet.frozen_rows[title] = grid["frozenRowCount"]
                if "hideGridlines" in grid:
                    sheet.gridlines[title] = bool(grid["hideGridlines"])
            elif "setDataValidation" in req:
                # An empty rule removes validation, as it does in the real API --
                # re-running the layout must not stack a second dropdown on a cell.
                # Removal is by overlap, not equality: a wide clear has to take the
                # narrow rules inside it with it. Google splits an enclosing rule at the
                # edge instead of discarding the whole thing, which nothing published
                # here relies on -- every rule this tool sends is one cell, except the
                # sheet-wide clear that runs before them.
                put = req["setDataValidation"]
                title = sheet.title_of(put["range"]["sheetId"])
                rules = [pair for pair in sheet.validations.setdefault(title, [])
                         if not _overlaps(pair[0], put["range"])]
                rule = put.get("rule") or {}
                if rule:
                    rules.append((put["range"], rule))
                sheet.validations[title] = rules
            elif "repeatCell" in req:
                put = req["repeatCell"]
                title = sheet.title_of(put["range"]["sheetId"])
                pairs = [pair for pair in sheet.formats.setdefault(title, [])
                         if not _same_cells(pair[0], put["range"])]
                pairs.append((put["range"], put.get("cell") or {}))
                sheet.formats[title] = pairs
            elif "updateDimensionProperties" in req:
                put = req["updateDimensionProperties"]
                title = sheet.title_of(put["range"]["sheetId"])
                sheet.dimensions.append((title, put["range"], put.get("properties") or {}))
            # One reply per request, in order -- the shape the real API answers with, and
            # what a caller that needs a new sheet id has to read from.
            replies.append(reply)
        return FakeRequest(lambda: {"replies": replies})


def _same_cells(left: dict, right: dict) -> bool:
    """Do these two GridRanges name the same cells? (Half-open indexes.)"""
    keys = ("sheetId", "startRowIndex", "endRowIndex", "startColumnIndex", "endColumnIndex")
    return all(left.get(k) == right.get(k) for k in keys)


def _overlaps(left: dict, right: dict) -> bool:
    """Do these two GridRanges share at least one cell? (Half-open indexes.)"""
    if left.get("sheetId") != right.get("sheetId"):
        return False
    return (left["startRowIndex"] < right["endRowIndex"]
            and right["startRowIndex"] < left["endRowIndex"]
            and left["startColumnIndex"] < right["endColumnIndex"]
            and right["startColumnIndex"] < left["endColumnIndex"])


# What the Sheets v4 discovery document accepts, restated so the fake can refuse a
# malformed request the way Google does -- at send time, naming the field. A fake that
# files anything it is handed ships a wrong shape green and fails on live Drive, which
# is exactly what happened the first time layout ran against the real API: an
# inputMessage sent as {type, text} instead of a string was rejected, and it took the
# whole layout batch down with it.
_GRID_RANGE_KEYS = {"sheetId", "startRowIndex", "endRowIndex", "startColumnIndex",
                    "endColumnIndex"}
_NUMBER_FORMATS = {"TEXT", "NUMBER", "PERCENT", "CURRENCY", "DATE", "TIME", "DATE_TIME",
                   "SCIENTIFIC", "NUMBER_FORMAT_TYPE_UNSPECIFIED"}
_CELL_FORMAT_KEYS = {"numberFormat", "textFormat", "backgroundColor", "backgroundColorStyle",
                     "wrapStrategy", "horizontalAlignment", "verticalAlignment", "borders",
                     "padding", "textRotation", "hyperlinkDisplayType", "textDirection"}
_TEXT_FORMAT_KEYS = {"bold", "fontSize", "fontFamily", "foregroundColor",
                     "foregroundColorStyle", "italic", "strikethrough", "underline", "link"}
_GRID_PROPERTIES_KEYS = {"rowCount", "columnCount", "frozenRowCount", "frozenColumnCount",
                         "hideGridlines", "rowGroupControlAfter", "columnGroupControlAfter"}
_DIMENSION_KEYS = {"hiddenByUser", "pixelSize", "developerMetadata"}


def _unknown(got, allowed, where):
    extra = set(got) - allowed
    return f"{where}: unknown field(s) {sorted(extra)}" if extra else ""


def _shape_error(request: dict) -> str:
    """Why Google would refuse this formatting request, or "" if its shape is legal."""
    if "setDataValidation" in request:
        put = request["setDataValidation"]
        rule = put.get("rule") or {}
        for problem in (_unknown(put, {"range", "rule"}, "set_data_validation"),
                        _unknown(put.get("range") or {}, _GRID_RANGE_KEYS, "range"),
                        _unknown(rule, {"condition", "inputMessage", "showCustomUi",
                                        "strict"}, "rule") if rule else ""):
            if problem:
                return problem
        if not isinstance(rule.get("inputMessage", ""), str):
            return ("rule.inputMessage is a plain string; sending an object answers "
                    "'Starting an object on a scalar field'")
        condition = rule.get("condition") or {}
        problem = _unknown(condition, {"type", "values"}, "rule.condition")
        if problem:
            return problem
        if rule.get("showCustomUi") and condition.get("type") not in ("ONE_OF_LIST",
                                                                     "ONE_OF_RANGE"):
            return "showCustomUi only customises a list condition"
        for value in condition.get("values", []):
            problem = _unknown(value, {"userEnteredValue", "userEnteredFormula"},
                               "rule.condition.values[]")
            if problem:
                return problem
        return ""

    if "repeatCell" in request:
        put = request["repeatCell"]
        for problem in (_unknown(put, {"range", "cell", "fields", "operator"}, "repeat_cell"),
                        _unknown(put.get("range") or {}, _GRID_RANGE_KEYS, "range"),
                        _unknown(put.get("cell") or {}, {"userEnteredFormat", "effectiveFormat",
                                                         "note", "textFormat"}, "cell")):
            if problem:
                return problem
        if not put.get("fields"):
            return "repeat_cell needs fields: without it nothing is applied"
        cell_format = (put.get("cell") or {}).get("userEnteredFormat") or {}
        problem = _unknown(cell_format, _CELL_FORMAT_KEYS, "cell.userEnteredFormat")
        if problem:
            return problem
        number = cell_format.get("numberFormat") or {}
        if number and number.get("type") not in _NUMBER_FORMATS:
            return f"numberFormat.type {number.get('type')!r} is not a NumberFormatType"
        text = cell_format.get("textFormat") or {}
        return _unknown(text, _TEXT_FORMAT_KEYS, "cell.userEnteredFormat.textFormat")

    if "updateSheetProperties" in request:
        put = request["updateSheetProperties"]
        properties = put.get("properties") or {}
        for problem in (_unknown(properties, {"sheetId", "title", "index", "sheetType",
                                              "hidden", "tabColor", "gridProperties"},
                                 "update_sheet_properties.properties"),
                        _unknown(properties.get("gridProperties") or {}, _GRID_PROPERTIES_KEYS,
                                 "gridProperties")):
            if problem:
                return problem
        fields = [f.strip() for f in str(put.get("fields") or "").split(",")]
        if not any(fields):
            return "update_sheet_properties needs fields"
        grid = properties.get("gridProperties")
        # The trap that cost a live round trip: a broad field mask applies the whole
        # GridProperties object, and the members left out come back as zero -- which
        # reads to Sheets as "shrink this tab to no rows".
        if grid is not None and "gridProperties" in fields \
                and not {"rowCount", "columnCount"} <= set(grid):
            return ("fields=gridProperties sends rowCount/columnCount as zero; name "
                    "gridProperties.frozenRowCount and friends individually")
        return ""

    if "updateDimensionProperties" in request:
        put = request["updateDimensionProperties"]
        where = put.get("range") or {}
        for problem in (_unknown(where, {"sheetId", "dimension", "startIndex", "endIndex",
                                         "majorDimension"}, "dimension_range"),
                        _unknown(put.get("properties") or {}, _DIMENSION_KEYS,
                                 "dimension properties")):
            if problem:
                return problem
        if where.get("dimension") not in ("ROWS", "COLUMNS"):
            return f"dimension {where.get('dimension')!r} must be ROWS or COLUMNS"
        # hiddenByFilter exists on read and is read-only on write; there is no `hidden`.
        return "" if put.get("fields") else "update_dimension_properties needs fields"

    if "deleteSheet" in request:
        return _unknown(request["deleteSheet"], {"sheetId"}, "delete_sheet")

    if "addSheet" in request:
        props = (request["addSheet"] or {}).get("properties") or {}
        return _unknown(props, {"sheetId", "title", "index", "sheetType", "hidden",
                                "tabColor", "gridProperties"}, "add_sheet.properties")

    return ""


class _Values:
    def __init__(self, drive: FakeDrive, mangle: bool = False):
        self.d = drive
        self.mangle = mangle

    @staticmethod
    def _tab(range_):
        return range_.split("!")[0].strip("'")

    @staticmethod
    def _start_row(range_):
        cell = range_.split("!")[1].split(":")[0]
        digits = "".join(ch for ch in cell if ch.isdigit())
        return int(digits) if digits else 1

    @staticmethod
    def _echo(sheet, title):
        """How Sheets names a range back: trimmed to the data it actually found."""
        grid = sheet.tabs[title]
        width = max((len(r) for r in grid), default=1)
        return f"'{title}'!A1:{column_letter(max(width, 1))}{max(len(grid), 1)}"

    def _coerce(self, rows):
        """Stand-in for Sheets deciding a value 'means' something else."""
        return [[str(v).upper() for v in row] for row in rows] if self.mangle else rows

    def _write(self, spreadsheetId, range_, rows):
        tab = self.d.by_id[spreadsheetId]["sheet"].tabs[self._tab(range_)]
        start = self._start_row(range_) - 1
        tab[start:start + len(rows)] = [list(r) for r in rows]

    def get(self, spreadsheetId, range="", **kw):
        self.d.record("values.get", {"range": range})
        sheet = self.d.by_id[spreadsheetId]["sheet"]
        grid = sheet.tabs.get(self._tab(range), [])
        return FakeRequest(lambda: {"values": [list(r) for r in grid]})

    def update(self, spreadsheetId, range="", body=None, **kw):
        self.d.record("values.update", {"range": range})
        # Real Sheets overwrites only the written range; rows below survive --
        # which is how a stale-row bug survived an early live Drive session.
        self._write(spreadsheetId, range, self._coerce(body["values"]))
        return FakeRequest(lambda: {"updatedRows": len(body["values"])})

    def batchGet(self, spreadsheetId, ranges=None, **kw):
        self.d.record("values.batchGet", {"ranges": list(ranges or [])})
        sheet = self.d.by_id[spreadsheetId]["sheet"]
        wanted = [self._tab(r) for r in (ranges or [])]
        # A tab that is not there is not answered for; asking for one is what would
        # fail the whole request against the real API.
        blocks = [{"range": self._echo(sheet, t), "values": [list(r) for r in sheet.tabs[t]]}
                  for t in wanted if t in sheet.tabs]
        return FakeRequest(lambda: {"valueRanges": blocks})

    def batchUpdate(self, spreadsheetId, body=None, **kw):
        data = (body or {}).get("data", [])
        self.d.record("values.batchUpdate", {"ranges": [d["range"] for d in data]})
        for block in data:
            self._write(spreadsheetId, block["range"], self._coerce(block["values"]))
        return FakeRequest(lambda: {"totalUpdatedRows": sum(len(d["values"]) for d in data)})

    def clear(self, spreadsheetId, range="", **kw):
        self.d.record("values.clear", {"range": range})
        tab = self.d.by_id[spreadsheetId]["sheet"].tabs.get(self._tab(range))
        cleared = 0
        if tab is not None:
            start = self._start_row(range) - 1
            cleared = len(tab) - start
            del tab[start:]
        return FakeRequest(lambda: {"clearedRows": cleared})


@pytest.fixture
def drive():
    return FakeDrive()


@pytest.fixture
def config():
    # ttl 0 => no caching, so every assertion observes the shared fake state
    return Config(backend="drive", drive_folder_id="FOLDERID", actor="tester",
                  cache_ttl_seconds=0)


def make_store(cls, config, drive, sheets=None):
    store = cls.__new__(cls)
    store.config = config
    store.folder_id = config.drive_folder_id
    store.ttl = float(config.cache_ttl_seconds or 0)
    store._override_id = str(getattr(config, "drive_spreadsheet_id", "") or "").strip()
    store._listing = None
    store._spreadsheet_id = None
    store._resolved = False
    store._tab_ids = None
    store._tab_stamp = 0.0
    store._cache = {}
    store._drive = drive
    if sheets is not None:
        store._sheets = sheets
    return store


def sheets_store(config, drive, mangle=False):
    return make_store(DriveWorkbookStore, config, drive, FakeSheets(drive, mangle=mangle))


def _cfg(**kw):
    base = dict(backend="drive", drive_folder_id="FOLDERID", actor="tester",
                cache_ttl_seconds=0)
    base.update(kw)
    return Config(**base)


class TestHelpers:
    def test_column_letters(self):
        assert column_letter(1) == "A"
        assert column_letter(26) == "Z"
        assert column_letter(27) == "AA"
        assert column_letter(702) == "ZZ"

    def test_sheet_cells_stringify_like_csv(self):
        assert _from_sheet(47.2) == "47.2"
        assert _from_sheet(500.0) == "500"          # not "500.0"
        assert _from_sheet(10) == "10"
        assert _from_sheet(True) == "true"
        assert _from_sheet(None) == ""
        assert _from_sheet("2024-05-16T11:00:00") == "2024-05-16T11:00Z"
        # a locale-formatted date is left alone for the validator to flag, not guessed
        assert _from_sheet("5/16/2024, 11:00:00 AM") == "5/16/2024, 11:00:00 AM"

    def test_ttl_zero_means_no_caching(self, config, drive):
        store = sheets_store(config, drive)
        store.write_table("items", ("item_id",), [{"item_id": "smp_aaaaaaaa"}], None)
        assert len(store.read_table("items").rows) == 1
        drive.tabs()["items"].append(["smp_bbbbbbbb"])
        assert len(store.read_table("items").rows) == 2

    def test_ttl_positive_serves_the_cache(self, drive):
        cfg = _cfg(cache_ttl_seconds=60)
        store = sheets_store(cfg, drive)
        store.write_table("items", ("item_id",), [{"item_id": "smp_aaaaaaaa"}], None)
        assert len(store.read_table("items").rows) == 1
        drive.tabs()["items"].append(["smp_bbbbbbbb"])
        assert len(store.read_table("items").rows) == 1, "within TTL the cache answers"


class TestOneWorkbook:
    """The layout itself: one spreadsheet, a tab per table, interface tabs alongside."""

    def test_creating_a_table_makes_one_workbook_holding_every_tab(self, config, drive):
        store = sheets_store(config, drive)
        store.write_table("items", ("item_id", "name"),
                          [{"item_id": "smp_aaaaaaaa", "name": "one"}], None)

        workbooks = [e for e in drive.by_id.values() if e["mimeType"] == SHEET]
        assert len(workbooks) == 1, "the database is one file, whatever tables are written"
        assert workbooks[0]["name"] == WORKBOOK_NAME
        tabs = list(workbooks[0]["sheet"].tabs)
        assert "items" in tabs
        assert "Sheet1" not in tabs, "the default tab is renamed into service, not orphaned"
        assert tabs[0] == "Start", "the interface is what a person opening it sees first"
        assert tabs[-len(schema.TABLES):] == [t.name for t in schema.TABLES]
        assert "options" in workbooks[0]["sheet"].hidden

    def test_the_interface_tabs_are_scaffolding_not_plumbing(self, config, drive):
        """Delete every interface tab and the database still reads and writes.

        The interface is a convenience layered on manual editing, never a gate in
        front of storage: someone working the data tabs by hand has to be able to do
        everything without ever seeing these, and nobody's tidy-up should be able to
        break storage by removing one. So this asserts the absence rather than the
        presence -- the property fails the moment some core path starts needing them,
        which is how a dependency would creep in.
        """
        store = sheets_store(config, drive)
        store.write_table("items", ("item_id",), [{"item_id": "smp_aaaaaaaa"}], None)

        sheet = drive.here()["sheet"]
        for title in RESERVED_TABS:
            sheet.tabs.pop(title, None)
            sheet.ids.pop(title, None)

        assert store.list_tables() == [t.name for t in schema.TABLES], (
            "the interface is not part of the database")
        assert not set(store.list_tables()) & set(RESERVED_TABS)
        assert store.read_table("items").rows[0]["item_id"] == "smp_aaaaaaaa"
        store.write_table(
            "items", ("item_id",), [{"item_id": "smp_bbbbbbbb"}],
            compute_revision(("item_id",), [{"item_id": "smp_aaaaaaaa"}]),
        )
        assert drive.tabs()["items"][1] == ["smp_bbbbbbbb"]
        assert not store.stray_objects(), "nothing left to explain about the missing tabs"

    def test_a_layout_lands_in_one_request_and_replaces_what_it_set(self, config, drive):
        """Worth asserting on because a layout applied to the wrong range looks fine.

        A dropdown on the wrong column is invisible until somebody types into it, so
        the fake keeps what was asked for instead of rendering it -- and applying a
        layout twice has to update the rule, not stack a second one on the same cells.
        """
        store = sheets_store(config, drive)
        store.write_table("items", ("item_id", "class"),
                          [{"item_id": "smp_aaaaaaaa", "class": "sample"}], None)
        ids = store.tab_ids()
        where = {"sheetId": ids["items"], "startRowIndex": 1, "endRowIndex": 50,
                 "startColumnIndex": 1, "endColumnIndex": 2}

        def dropdown(*values):
            return {"setDataValidation": {
                "range": dict(where),
                "rule": {"condition": {"type": "ONE_OF_LIST",
                                       "values": [{"userEnteredValue": v} for v in values]},
                         "strict": True, "showCustomUi": True}}}

        drive.forget_calls()
        store.apply_layout([dropdown("sample", "supply")])
        assert drive.counts()["spreadsheets.batchUpdate"] == 1, "a layout is one decision"

        sheet = drive.here()["sheet"]
        rules = sheet.validations["items"]
        assert len(rules) == 1
        got_range, got_rule = rules[0]
        assert got_range["startColumnIndex"] == 1, "the class column, not somewhere else"
        assert [v["userEnteredValue"] for v in got_rule["condition"]["values"]] == [
            "sample", "supply"]
        assert got_rule["condition"]["type"] == "ONE_OF_LIST"
        assert got_rule["strict"] is True, "an invalid entry should be rejected, not warned"

        store.apply_layout([dropdown("sample", "supply", "container")])
        assert len(sheet.validations["items"]) == 1, "re-applying replaces, never stacks"
        assert [v["userEnteredValue"]
                for v in sheet.validations["items"][0][1]["condition"]["values"]] == [
            "sample", "supply", "container"]

    def test_an_interface_tab_holds_cells_not_a_table(self, config, drive):
        """The one place the store writes cells it will never validate.

        No header meaning, no revision, no validation pass -- which is exactly why it
        gets its own door rather than a special case inside ``write_batch``, and why
        the door has to leave the tables alone.
        """
        store = sheets_store(config, drive)
        store.write_table("items", ("item_id",), [{"item_id": "smp_aaaaaaaa"}], None)
        revision = store.current_revision("items")

        drive.forget_calls()
        store.write_grids({"options": [["stamp"], ["digest"], ["abc"]]})
        counts = drive.counts()
        assert counts["values.batchGet"] == 1 and counts["values.batchUpdate"] == 1, \
            "however many tabs are written, it is one read and one write"
        assert drive.tabs()["options"] == [["stamp"], ["digest"], ["abc"]]
        assert store.read_grid("options")[1] == ["digest"]
        assert store.current_revision("items") == revision, \
            "writing the interface must not disturb what a commit compares against"
        assert store.list_tables() == [t.name for t in schema.TABLES], \
            "a reference tab is not a table"

    def test_writing_a_shorter_grid_blanks_what_was_there(self, config, drive):
        """A shortened error report that leaves rows behind reads as the old report.

        Sheets only overwrites the block it is handed, so padding is on purpose: this
        is the same hazard the table shrink guards, arriving a second time because the
        interface tabs shrink constantly -- every run reports whatever broke today.
        """
        store = sheets_store(config, drive)
        store.write_table("items", ("item_id",), [{"item_id": "smp_aaaaaaaa"}], None)
        store.write_grids({"database_errors": [[f"row {n}", "x", "y", "z"] for n in range(6)]})
        store.write_grids({"database_errors": [["severity", "table"]]})

        grid = drive.tabs()["database_errors"]
        assert grid[0][:2] == ["severity", "table"]
        stale = [row for row in grid if any(str(c).strip() for c in row[2:])]
        assert not stale, f"the previous report's extra columns survived: {stale}"
        strays = [line for line in grid[1:] if any(str(c).strip() for c in line)]
        assert not strays, f"the previous report's extra rows survived: {strays}"

    def test_writing_a_grid_hands_back_what_it_replaced(self, config, drive):
        """The caller already paid for that read, so it should not need a second one."""
        store = sheets_store(config, drive)
        store.write_table("items", ("item_id",), [{"item_id": "smp_aaaaaaaa"}], None)
        assert store.write_grids({"options": [["key", "value"], ["digest", "old"]] \
                                 })["options"] == []
        back = store.write_grids({"options": [["key", "value"], ["digest", "new"]]})
        assert back["options"] == [["key", "value"], ["digest", "old"]]

    def test_a_tab_that_is_not_there_reads_empty_rather_than_raising(self, config, drive):
        """``ui --check`` on a database that predates the interface is a normal question."""
        drive.workbook(tabs={"items": [["item_id"], ["smp_aaaaaaaa"]]})
        store = sheets_store(config, drive)
        assert store.read_grid("options") == []

    def test_numbers_come_back_as_the_text_we_sent(self, config, drive):
        store = sheets_store(config, drive)
        store.write_table("measurements", ("measurement_id", "value_num"),
                          [{"measurement_id": "msm_aaaaaaaa", "value_num": "47.2"}], None)
        drive.tabs()["measurements"][1][1] = 47.2  # Sheets coerced it
        assert store.read_table("measurements").rows[0]["value_num"] == "47.2"

    def test_integer_cells_do_not_grow_a_spurious_decimal(self, config, drive):
        drive.workbook(tabs={"containers": [["container_id", "rows"], ["ctr_aaaaaaaa", 10]]})
        store = sheets_store(config, drive)
        assert store.read_table("containers").rows[0]["rows"] == "10"

    def test_a_shrink_blanks_the_rows_it_leaves_behind(self, config, drive):
        store = sheets_store(config, drive)
        header = ("item_id",)
        many = [{"item_id": f"smp_{n:08d}"} for n in range(5)]
        store.write_table("items", header, many, None)
        few = [{"item_id": "smp_00000001"}]
        rev = store.write_table("items", header, few, store.read_table("items").revision)

        grid = drive.tabs()["items"]
        leftover = grid[2:]
        assert not any(str(c).strip() for row in leftover for c in row), (
            "stale rows must not survive a shrink, however they are stored")
        assert store.read_table("items").revision == rev
        assert len(store.read_table("items").rows) == 1

    def test_an_absent_tab_is_reported_absent_not_empty(self, config, drive):
        """So a table nobody created yet reads as missing, not as a zero-row table."""
        drive.workbook(tabs={"items": [["item_id"], ["smp_aaaaaaaa"]]})
        store = sheets_store(config, drive)
        assert store.exists("items")
        assert not store.exists("samples")
        with pytest.raises(StoreError, match="no tab"):
            store.read_table("samples")
        assert "samples" not in store.read_all()

    def test_a_renamed_tab_shows_as_a_stray_and_its_table_as_absent(self, config, drive):
        """Someone renaming a tab loses the table, and must not overwrite the data.

        The renamed tab is reported as a stray and the table as missing, so the fix
        is visible: rename it back. What must not happen is init quietly creating an
        empty table while the real rows sit there under a wrong name.
        """
        store = sheets_store(config, drive)
        store.write_table("items", ("item_id",), [{"item_id": "smp_00000001"}], None)
        drive.here()["sheet"].rename("items", "items DO NOT TOUCH")

        assert not store.exists("items")
        assert "items DO NOT TOUCH" in store.stray_objects()
        with pytest.raises(RevisionConflict):
            store.write_table(
                "items", ("item_id",), [{"item_id": "smp_00000002"}],
                compute_revision(("item_id",), [{"item_id": "smp_00000001"}]),
            )
        assert drive.tabs()["items DO NOT TOUCH"][1] == ["smp_00000001"], "untouched"

    def test_coerced_content_is_detected_and_put_back(self, config, drive):
        """If Sheets rewrites what we sent, say so -- and do not leave it lying there."""
        store = sheets_store(config, drive, mangle=True)
        with pytest.raises(StoreError, match="different rendering"):
            store.write_table("items", ("item_id", "name"),
                              [{"item_id": "smp_aaaaaaaa", "name": "lower case"}], None)
        assert drive.tabs()["items"] == [], "a table this write created is emptied again"

    def test_extra_columns_survive_a_read(self, config, drive):
        """Someone added a column by hand; it must be visible, not dropped."""
        drive.workbook(tabs={"items": [["item_id", "notes_backup"],
                                       ["smp_aaaaaaaa", "typed in the browser"]]})
        store = sheets_store(config, drive)
        data = store.read_table("items")
        assert data.rows[0]["notes_backup"] == "typed in the browser"

    def test_every_drive_call_asks_about_shared_drives(self, config, drive):
        """Production lives in a shared drive, where a request that doesn't pass
        supportsAllDrives is answered with 404 -- precisely what a deleted table
        looks like. So it has to be every Drive call, not the ones that look
        relevant. (The Sheets API has no such parameter; it is Drive-only.)"""
        store = sheets_store(config, drive)
        store.write_table("items", ("item_id",), [{"item_id": "smp_aaaaaaaa"}], None)
        store.invalidate()
        store.read_table("items")
        asked = [(name, kw) for name, kw in drive.calls if name.startswith("files.")]
        assert asked, "expected the folder to have been listed"
        blind = [name for name, kw in asked if not kw.get("supportsAllDrives")]
        assert not blind, f"these would 404 in a shared drive: {blind}"

    def test_transient_errors_are_retried(self, config, drive, monkeypatch):
        """Retried, and retried *after* a quota window -- asserted without sitting
        through it, which used to make this the slowest test in the suite."""
        from inventory_mcp.store import drive as drive_module

        slept: list[float] = []
        monkeypatch.setattr(drive_module.time, "sleep", slept.append)
        store = sheets_store(config, drive)
        drive.fail_next = [HttpErr(429)]
        assert store.write_table("items", ("item_id",), [{"item_id": "smp_aaaaaaaa"}], None)
        assert slept, "a 429 should be waited out, not burned through attempts"

    def test_permanent_error_becomes_a_readable_store_error(self, config, drive):
        store = sheets_store(config, drive)
        drive.always_fail = [HttpErr(403, "Insufficient Permission")]
        with pytest.raises(StoreError, match="view-only|cannot write"):
            store.write_table("items", ("item_id",), [{"item_id": "smp_aaaaaaaa"}], None)

    def test_a_deleted_workbook_is_not_resurrected_by_an_update(self, config, drive):
        store = sheets_store(config, drive)
        store.write_table("items", ("item_id",), [{"item_id": "smp_aaaaaaaa"}], None)
        rev = store.current_revision("items")
        drive.by_id.clear()
        store.invalidate()
        with pytest.raises(RevisionConflict, match="no spreadsheet"):
            store.write_table("items", ("item_id",), [{"item_id": "smp_bbbbbbbb"}], rev)

    def test_the_configured_id_wins_over_the_name_lookup(self, drive):
        """The override exists for a workbook renamed out from under the lookup."""
        fid = drive.workbook(name="renamed-away", tabs={"items": [["item_id"]]})
        store = sheets_store(_cfg(drive_spreadsheet_id=fid), drive)
        assert store.workbook_id() == fid
        assert store.list_tables() == ["items"]


class TestResettingInterfaceTabs:
    """`ui --reset`: delete the tab and rebuild it, because overwriting leaves things behind.

    The generator replaces values and the handful of formats it sets itself. It cannot
    remove a merged range, a conditional format, a protection, a note, or a dropdown
    somebody made in the UI -- and those are exactly what a reset is asked for. So the
    operation is a delete plus an add, and these tests hold the two things that could
    quietly break: where the tab sits, and whether anything survived it.
    """

    def _seed(self, config, drive):
        store = sheets_store(config, drive)
        store.write_table("items", ("item_id",), [{"item_id": "smp_aaaaaaaa"}], None)
        store.write_grids({"Start": [["Lab inventory"],
                                     ["a note somebody typed under the form"]]})
        sheet = drive.here()["sheet"]
        start = sheet.ids["Start"]
        cells = {"sheetId": start, "startRowIndex": 7, "endRowIndex": 18,
                 "startColumnIndex": 1, "endColumnIndex": 2}
        sheet.validations["Start"] = [(cells, {"strict": True})]
        sheet.formats["Start"] = [(cells, {"userEnteredFormat": {"textFormat": {"bold": True}}})]
        return store, sheet

    def test_a_reset_takes_the_hand_made_rules_with_the_tab(self, config, drive):
        store, sheet = self._seed(config, drive)
        before = sheet.ids["Start"]

        held = store.reset_tabs(["Start"])

        assert held == {"Start": 2}, "the caller has to be able to say what was discarded"
        assert store.read_grid("Start") == [], "nothing of the old tab remains"
        assert not sheet.validations.get("Start"), "the dropdown made by hand is gone"
        assert not sheet.formats.get("Start"), "so is the formatting nobody generated"
        assert sheet.ids["Start"] != before, "a recreated tab is a new sheet, not a reused one"
        assert list(sheet.tabs)[0] == "Start", "and it came back where it belongs"
        assert drive.tabs()["items"] == [["item_id"], ["smp_aaaaaaaa"]], \
            "a reset touches the tabs it was asked about and no others"

    def test_a_reset_puts_the_tabs_back_where_a_new_workbook_puts_them(self, config, drive):
        store, sheet = self._seed(config, drive)
        # Tabs drift: a tab added later lands at the end, and dragging one in the browser
        # moves it. Repairing that is part of what a reset is for, so the destination is
        # the generated order rather than whatever the file happens to hold.
        sheet.move("options", 4)
        assert list(sheet.tabs).index("options") == 4

        store.reset_tabs(list(RESERVED_TABS))

        assert list(sheet.tabs)[:3] == list(RESERVED_TABS), \
            "the interface tabs come back in the order creation gives them"
        assert "options" in sheet.hidden, "the reference tab comes back hidden as it was made"
        assert "Start" not in sheet.hidden

    def test_the_callers_order_does_not_decide_where_the_tabs_land(self, config, drive):
        """The first live run left the reference tab behind two data tabs.

        ``publish`` hands over its tabs in build order rather than sheet order, and an
        index update moves a tab within the layout as it stands at that moment -- so only
        ascending destinations land clean. The store sorts them itself instead of trusting
        whoever called, which is what this pins.
        """
        store, sheet = self._seed(config, drive)
        order = list(sheet.tabs)

        store.reset_tabs(["options", "Start", "database_errors"])

        assert list(sheet.tabs) == order, "the caller's list order shuffled the workbook"

    def test_resetting_a_tab_that_is_absent_just_makes_it(self, config, drive):
        store, sheet = self._seed(config, drive)
        sheet.delete("database_errors")

        held = store.reset_tabs(["database_errors"])

        assert held == {}, "there was nothing there to discard, and it says so plainly"
        assert "database_errors" in sheet.tabs


class TestCallVolume:
    """Request counts, because silence that is really just slowness is a bug report.

    These are not micro-optimisations for their own sake: the complaint that started
    this layout was an ``init`` that looked wedged for minutes. Twelve tables used to
    mean twelve reads, twelve metadata reads and twelve writes; a ceiling on the
    count is what keeps that from coming back one convenient change at a time.
    """

    def test_creating_a_database_fits_in_a_handful_of_requests(self, config, drive):
        """Twelve tables used to cost 132 requests; this is the whole setup now.

        The shape matters more than the total: one file created, one request adding
        every tab, one request writing every header. Anything that turns those back
        into per-table loops shows up here as a jump of a dozen, not a drift of one.

        The interface tabs add three requests to the run -- read what they held, write
        all three, format them -- and none of them scales with the number of tables.
        """
        init_database(config, store=sheets_store(config, drive))
        counts = drive.counts()
        assert counts["files.create"] == 1, "one workbook, created once"
        assert counts["values.batchUpdate"] == 2, (
            "one for all twelve headers, one for all three interface tabs")
        assert counts["values.get"] == 0, "no per-table reads on the create path"
        assert counts["spreadsheets.batchUpdate"] == 2, (
            "every tab added in one request, the whole layout in another")
        assert sum(counts.values()) <= 16, f"regression in setup cost: {dict(counts)}"

    def test_loading_a_database_is_one_request(self, config, drive):
        """One content request for twelve tables, plus one cheap structural read.

        The structural read answers "which tabs exist" -- Sheets refuses to parse a
        batch naming a tab that is not there, so it cannot be folded into the content
        request. It rides the same cache TTL as table content, so a process that
        loads repeatedly pays it once per window; this assertion is the cold case.
        """
        store = sheets_store(config, drive)
        init_database(config, store=store)
        drive.forget_calls()
        tables = store.read_all()
        assert len(tables) == len(schema.TABLES)
        assert drive.counts()["values.batchGet"] == 1, "every table in one fetch"
        assert drive.counts()["values.get"] == 0, "never one read per table"
        assert sum(drive.counts().values()) <= 2, dict(drive.calls)

    def test_a_commit_touching_two_tables_is_one_batch(self, config, drive):
        """Read, apply, confirm -- twice more than an unverified write, once per
        table fewer than before, and the confirmation is what catches a spreadsheet
        quietly rewriting what we sent.

        The extra ``files.copy`` is the pre-write copy that makes the commit undoable
        and ``files.create`` is the backup folder being made the first time only.
        """
        from inventory_mcp.initdb import init_database

        svc, _ = init_database(config, store=sheets_store(config, drive))
        drive.forget_calls()
        svc.create_sample("Qubit later", "dna")
        counts = drive.counts()
        assert counts["values.batchUpdate"] == 1, "items and samples land together"
        assert counts["values.get"] == 0
        assert counts["files.copy"] == 1, "one undo copy for one decision"
        assert sum(counts.values()) <= 6, f"commit cost crept: {dict(counts)}"

    def test_attaching_to_an_existing_database_reads_it_once(self, config, drive):
        """Re-running init is how a second machine joins, so it has to be cheap.

        It used to ask ``exists`` per table and then read each one back to check its
        header -- two round trips per table, twenty-four of them to conclude "nothing
        to do". Measured live, that was twelve single-range fetches on every attach.
        """
        init_database(config, store=sheets_store(config, drive))
        drive.forget_calls()
        _, created = init_database(config, store=sheets_store(config, drive))
        assert created == [], "nothing should have been built the second time"
        counts = drive.counts()
        assert counts["values.get"] == 0, "headers come from the bulk read, not per table"
        # The interface is regenerated on an attach too -- this is how a second machine
        # joins, and its workbook may predate the current schema. Reading what the tabs
        # held, writing them, and formatting them is three requests, none per table.
        assert sum(counts.values()) <= 14, f"attach cost crept: {dict(counts)}"

    def test_seeding_copies_the_workbook_once_not_once_per_record(self, config, drive):
        from inventory_mcp.initdb import init_database
        from inventory_mcp.seed import seed

        svc, _ = init_database(config, store=sheets_store(config, drive))
        drive.forget_calls()
        seed(svc)
        assert drive.counts()["files.copy"] == 1, (
            "a seed is one decision to undo, so it gets one pre-write copy")

    def test_resetting_is_one_batch(self, config, drive):
        from inventory_mcp.initdb import init_database
        from inventory_mcp.seed import reset, seed

        svc, _ = init_database(config, store=sheets_store(config, drive))
        seed(svc)
        drive.forget_calls()
        reset(svc)
        assert drive.counts()["values.batchUpdate"] == 1
        assert drive.counts()["values.get"] == 0


class TestSnapshots:
    """Undo, which the Drive backend never had.

    Locally a commit copied the files it was about to change; remotely it reported
    ``snapshot: none`` and pointed at Drive's version history. That was reasonable
    while only the server wrote. Once colleagues are typing into cells, "copy the
    file" is the recovery they can perform themselves, so the store does it.
    """

    def _commit(self, config, drive):
        from inventory_mcp.initdb import init_database

        svc, _ = init_database(config, store=sheets_store(config, drive))
        return svc.create_sample("Before the edit", "dna")

    def test_a_write_leaves_a_copy_inside_the_backup_folder(self, config, drive):
        result = self._commit(_cfg(), drive)
        assert result.snapshot.path.startswith(f"drive:{BACKUP_FOLDER_NAME}/")
        copies = drive.in_backup_folder()
        assert len(copies) == 1
        assert "-create_sample" in copies[0]["name"]

    def test_the_copy_does_not_move_when_the_live_data_moves(self, config, drive):
        result = self._commit(_cfg(), drive)
        copy = drive.in_backup_folder()[0]
        before = [list(r) for r in copy["sheet"].tabs["items"]]
        drive.tabs()["items"].append(["smp_later000", "", "", "", "", "", "", "", "", "",
                                      "", "", ""])
        assert [list(r) for r in copy["sheet"].tabs["items"]] == before

    def test_keep_backups_zero_declines_the_copy(self, drive):
        result = self._commit(_cfg(keep_backups=0), drive)
        assert result.snapshot.path is None
        assert drive.in_backup_folder() == []

    def test_pruning_keeps_the_newest_and_drops_the_rest(self, drive):
        cfg = _cfg(keep_backups=2)
        store = sheets_store(cfg, drive)
        store.write_table("items", ("item_id",), [{"item_id": "smp_aaaaaaaa"}], None)
        for _ in range(4):
            store.snapshot(["items"], "test")
        made = [c["id"] for c in drive.in_backup_folder()]
        assert len(made) == 4

        assert store.prune_snapshots() == 2
        assert [c["id"] for c in drive.in_backup_folder()] == made[2:], (
            "the generations to keep are the newest ones, not the first ones named")

    def test_the_backup_folder_is_not_reported_as_a_stray(self, config, drive):
        self._commit(config, drive)
        store = sheets_store(config, drive)
        store.invalidate()
        assert BACKUP_FOLDER_NAME not in store.stray_objects()


class TestThroughTheService:
    """The whole stack -- init, seed, validate, commit -- over a fake Drive.

    The point is not to re-test the service; it is to find the places where the
    service assumes something about storage that only the local backend provides.
    """

    def test_init_and_seed_over_one_workbook(self, config, drive):
        from inventory_mcp.seed import seed

        svc, created = init_database(config, store=sheets_store(config, drive))
        assert len(created) == 12
        counts = seed(svc)
        assert counts["samples"] == 10
        report = svc.doctor()
        assert report["error_count"] == 0, report["errors"]
        assert report["table_counts"]["measurements"] == 18

    def test_reads_and_aggregates_work_after_seeding(self, config, drive):
        from inventory_mcp.seed import seed

        svc, _ = init_database(config, store=sheets_store(config, drive))
        seed(svc)
        present = svc.search_items(item_class="sample", status="present")["rows"]
        assert present
        boxes = [c for c in svc._occupancy_rows() if c["has_capacity"]]
        assert all(b["occupied"] <= b["capacity"] for b in boxes)
        out = svc.run_query("select count(*) n from items where class='sample'")
        assert out["rows"][0]["n"] == 10

    def test_browser_edit_aborts_the_next_commit(self, config, drive):
        from inventory_mcp.service import Conflict

        store = sheets_store(config, drive)
        svc, _ = init_database(config, store=store)
        svc.create_sample("Registered before the edit", "dna")

        # Someone types into the open spreadsheet. It has to be a table the next
        # commit will touch -- CAS compares only what it is about to write, which
        # is exactly why an edit elsewhere in the workbook goes unnoticed.
        grid = drive.tabs()["items"]
        name_col = grid[0].index("name")
        grid[1][name_col] = "renamed by a human"

        with pytest.raises(Conflict, match="browser"):
            svc.create_sample("Racing a human", "dna")
        rows = store.read_table("items").rows
        assert rows[0]["name"] == "renamed by a human", "the hand edit survived the abort"

    def test_a_conflict_stops_the_commit_before_any_table_lands(self, config, drive):
        """Batching turned detect-and-abort into refuse-before-writing.

        The old per-table loop wrote ``items`` and only then failed on ``samples``,
        which is how a half-written ghost row got into a live folder (kits, in the
        original finding). Reading every table in the batch first means the mismatch
        surfaces before a single range is pushed, so there is nothing to revert.
        """
        store = sheets_store(config, drive)
        svc, _ = init_database(config, store=store)

        rev = store.current_revision("samples")
        store.write_table("samples", ("item_id",), [{"item_id": "smp_poison00"}], rev)
        drive.forget_calls()
        with pytest.raises(Exception, match="underneath us"):
            svc.create_sample("Half-written ghost", "dna")

        assert drive.counts()["values.batchUpdate"] == 0, "nothing was pushed"
        assert all(r.get("name") != "Half-written ghost" for r in store.read_table("items").rows)


class TestFolderResolution:
    def test_only_files_in_the_named_folder_count(self, config, drive):
        drive.workbook(tabs={"items": [["item_id"]]})
        drive.workbook(name="elsewhere-holding-nothing", parent="somewhere_else")
        store = sheets_store(config, drive)
        assert store.list_tables() == ["items"]
        assert store.stray_objects() == []

    def test_unrelated_files_are_reported_as_strays(self, config, drive):
        drive.workbook(tabs={"items": [["item_id"]]})
        drive.add("old_airtable_export")
        store = sheets_store(config, drive)
        assert store.stray_objects() == ["old_airtable_export"]

    def test_a_renamed_tab_is_a_stray_and_a_missing_table(self, config, drive):
        drive.workbook(tabs={"items": [["item_id"]], "whatever": [["x"]]})
        store = sheets_store(config, drive)
        assert store.stray_objects() == ["whatever"]

    def test_the_old_one_file_per_table_layout_is_refused(self, config, drive):
        """Not migrated, not ignored: a folder full of table spreadsheets is a
        database this version cannot see, and building a second one beside it would
        leave two sources of truth with only one of them live."""
        drive.add("items", mime=SHEET, sheet=FakeSheet())
        drive.add("samples", mime=SHEET, sheet=FakeSheet())
        with pytest.raises(ForeignTableError, match="older one-file-per-table layout"):
            init_database(config, store=sheets_store(config, drive))
        assert not any(e["name"] == WORKBOOK_NAME for e in drive.by_id.values())

    def test_somebody_elses_workbook_wearing_our_name_is_refused(self, config, drive):
        """A blank sheet named ``inventory`` has no tables to inspect, so the marker
        is the only thing standing between init and adding tabs to their file."""
        fid = drive.workbook(name=WORKBOOK_NAME, marker=False)
        with pytest.raises(ForeignTableError, match="not created by us"):
            init_database(config, store=sheets_store(config, drive))
        assert drive.here(WORKBOOK_NAME)["sheet"].tabs == {"Sheet1": []}, "left as found"
        assert fid

    def test_folder_id_from_a_pasted_url(self):
        cfg = Config(backend="drive",
                     drive_folder_id="https://drive.google.com/drive/folders/"
                                      "1AbCdEfGhIjKlMnOpQrStUvWxYz")
        assert cfg.drive_folder_id == "1AbCdEfGhIjKlMnOpQrStUvWxYz"


class TestVerifyFolder:
    """Asking Drive what the named folder actually is, before anything is written.

    Listing cannot answer this: an id that names nothing lists empty, which is the
    same response a brand-new folder gives. So ``init`` would go ahead and fail on
    its first create, with an error naming neither the flag nor the folder.
    """

    @pytest.fixture
    def api(self, drive, monkeypatch):
        from inventory_mcp.store import drive as drive_module

        monkeypatch.setattr(drive_module, "drive_service", lambda config, **kw: drive)
        return drive

    def _check(self, fid):
        return verify_folder(Config(backend="drive", drive_folder_id=fid))

    def test_the_folder_answers_with_its_name(self, api):
        fid = api.add("Lab inventory", mime=FOLDER, parent="root")
        meta = self._check(fid)
        assert meta["name"] == "Lab inventory", "the name is how you catch the wrong folder"
        asked = api.calls[-1][1]["fields"]
        assert "canEdit" in asked, "without it the read-only check below silently never fires"

    def test_an_id_that_names_nothing_says_so(self, api):
        api.add("Lab inventory", mime=FOLDER, parent="root")
        with pytest.raises(StoreError, match="no such Drive folder"):
            self._check("fid999")

    def test_a_spreadsheet_is_not_a_folder(self, api):
        fid = api.add(WORKBOOK_NAME, mime=SHEET, sheet=FakeSheet(), parent="root")
        with pytest.raises(StoreError, match="is a spreadsheet, not a folder"):
            self._check(fid)

    def test_a_folder_this_account_cannot_write_is_refused_early(self, api):
        fid = api.add("Shared read-only", mime=FOLDER, parent="root")
        api.by_id[fid]["canEdit"] = False
        with pytest.raises(StoreError, match="read-only"):
            self._check(fid)

    def test_capabilities_drive_will_not_promise_are_not_read_as_read_only(self, api):
        """A missing key means unknown. Treating it as False would refuse every
        shared drive, which is exactly the deployment this backend exists for."""
        fid = api.add("Team drive root", mime=FOLDER, parent="root")
        assert "capabilities" not in api.by_id[fid]
        assert self._check(fid)["id"] == fid

    def test_a_folder_that_won_t_show_itself_names_the_fix(self, api):
        api.add("Elsewhere", mime=FOLDER, parent="root")
        api.always_fail = [HttpErr(403, "insufficient")]
        with pytest.raises(StoreError, match="shared with that account"):
            self._check("fid2")


class TestAdoption:
    """``init`` refusing to take over a workbook that merely wears our name.

    The dangerous case is the quiet one: an empty spreadsheet is empty whether it is
    ours or somebody's, and content cannot tell them apart. What can is the title
    property ``_create_workbook`` stamps and nothing else carries -- checked once for
    the workbook by ``preflight``, before any tab is looked at.
    """

    def _init(self, cfg, drive):
        return init_database(cfg, store=sheets_store(cfg, drive))

    def test_our_own_empty_workbook_is_adopted(self, config, drive):
        """The state immediately after creation, which re-running init must survive."""
        drive.workbook(tabs={t: [[]] for t in RESERVED_TABS})
        _, created = self._init(config, drive)
        assert len(created) == len(schema.TABLES)
        assert len([e for e in drive.by_id.values() if e["mimeType"] == SHEET]) == 1, (
            "adopting must not create a second workbook")

    def test_somebody_else_s_header_in_our_tab_is_refused_with_evidence(self, config, drive):
        drive.workbook(tabs={"items": [["order", "patient"], ["47", "P-9"]]})
        with pytest.raises(ForeignTableError, match=r"\['order', 'patient'\]"):
            self._init(config, drive)
        assert drive.tabs()["items"] == [["order", "patient"], ["47", "P-9"]]

    def test_an_existing_database_reinitialises_without_losing_rows(self, config, drive):
        """Attaching from a second machine is the normal case, not an edge one."""
        self._init(config, drive)
        store = sheets_store(config, drive)
        spec = schema.get_table("items")
        store.write_table("items", spec.column_names,
                          [{"item_id": "ITM_1", "name": "freezer rack"}], None)

        _, created = self._init(config, drive)
        assert created == [], "nothing should be created over a complete database"
        again = sheets_store(config, drive)
        rows = again.read_table("items").rows
        assert [r["item_id"] for r in rows] == ["ITM_1"]

    def test_a_pre_v3_header_attaches_rather_than_being_refused(self, config, drive):
        """Overlap, not equality: live databases predate column renames, and refusing
        them would lock out the second machine attaching to the first one's work."""
        spec = schema.get_table("containers")
        old = [c if c != "capacity" else "capacity_override" for c in spec.column_names]
        drive.workbook(tabs={"containers": [old]})
        _, created = self._init(config, drive)
        assert "containers" not in created, "adopted as ours despite the older spelling"


class TestWaitingOutQuota:
    """What happens -- and what is said -- when Google says not yet.

    Being rate limited is ordinary here, and sleeping through it is right. Sleeping
    *silently* is not: an `init` that says nothing for four minutes is
    indistinguishable from one that is wedged, which is exactly how it was reported.
    Both halves are pinned -- the wait is announced, and it is bounded, because the
    ladder this replaced reached 480 s on a single request.
    """

    @staticmethod
    def _flaky(*errors, result="ok"):
        class Flaky:
            def __init__(self):
                self.left = list(errors)

            def execute(self, num_retries=0):
                if self.left:
                    raise self.left.pop(0)
                return result

        return Flaky()

    @staticmethod
    def _rejected(status, **headers):
        """An API error shaped like the real one, headers included.

        ``resp`` subclasses dict because that is how httplib2 presents a response --
        ``Retry-After`` is read with ``.get``, so a bare attribute-only stand-in
        would leave that path untested.
        """
        exc = Exception(f"status {status}")
        exc.resp = type("R", (dict,), {"status": status})(headers)
        exc.reason = ""
        return exc

    @pytest.fixture
    def waits(self, monkeypatch):
        """Collect the sleeps instead of sitting through them."""
        from inventory_mcp.store import drive as drive_module

        slept: list[float] = []
        monkeypatch.setattr(drive_module.time, "sleep", slept.append)
        return slept

    def test_a_quota_wait_is_announced(self, config, drive, waits, capsys):
        store = sheets_store(config, drive)
        assert store._retry(self._flaky(HttpErr(429))) == "ok"
        assert waits and waits[0] >= 30, (
            "the quota window is a minute; retrying sooner only spends attempts")
        err = capsys.readouterr().err
        assert "429" in err and "waiting" in err, "an unannounced wait looks like a hang"

    def test_the_server_says_how_long_so_we_wait_that(self, config, drive, waits):
        store = sheets_store(config, drive)
        store._retry(self._flaky(self._rejected(503, **{"retry-after": "7"})))
        assert waits == [7.0], "Google's own number beats our guess"

    def test_a_wait_never_runs_past_a_quota_window(self, config, drive, waits):
        """Whatever is asked for, sitting out more than a minute buys nothing and
        costs the operator an unresponsive-looking command."""
        store = sheets_store(config, drive)
        store._retry(self._flaky(self._rejected(429, **{"retry-after": "3600"})))
        assert waits == [60.0]

    def test_an_unparseable_header_falls_back_to_our_ladder(self, config, drive, waits):
        store = sheets_store(config, drive)
        store._retry(self._flaky(self._rejected(429, **{"retry-after": "Thu, 01 Jan 2026 00:00:00 GMT"})))
        assert waits == [30.0]

    def test_giving_up_still_costs_no_more_than_a_few_minutes(self, config, drive, waits):
        store = sheets_store(config, drive)
        with pytest.raises(StoreError):
            store._retry(self._flaky(*[self._rejected(429) for _ in range(8)]))
        assert len(waits) == 7, "eight attempts, seven waits"
        assert sum(waits) <= 7 * 60, f"bounded ladder, got {sum(waits)}s"
