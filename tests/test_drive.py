"""The Drive backend exercised against a fake Drive/Sheets service.

No network, no credentials. The point is to get the awkward paths -- coercion,
revision conflicts, shrink-trim, missing tabs -- right in front of a
deterministic stand-in, so that the first time they run against a real folder they
are being *verified* rather than discovered.

The fakes mirror the shape of the discovery client: ``files()`` / ``spreadsheets()``
/ ``values()`` return resource objects, each owning its own methods. Keeping them
separate matters -- ``spreadsheets().get`` and ``values().get`` take different
arguments, and collapsing them into one object hides exactly the kind of bug this
suite exists to catch.
"""

from __future__ import annotations

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
    DriveSheetsStore,
    _column_letter,
    _from_sheet,
    verify_folder,
)

#: What a table is under this backend: one Google spreadsheet each, not a file
#: anyone opens as text.
SHEET = "application/vnd.google-apps.spreadsheet"


class HttpErr(Exception):
    def __init__(self, status, reason="boom"):
        self.resp = type("R", (), {"status": status})()
        self.reason = reason


class FakeRequest:
    def __init__(self, fn):
        self._fn = fn

    def execute(self, num_retries=0):
        return self._fn()


def media_bytes(media):
    """Read what a MediaIoBaseUpload will send.

    Constructing the upload object seeks the stream to measure its length, so the
    cursor is not where we left it -- rewind before reading or the payload looks
    empty, which is a fake bug rather than a production one.
    """
    if media is None:
        return b""
    fd = getattr(media, "_fd", None)
    if fd is None:  # pragma: no cover - the SDK always keeps the handle
        raise AssertionError("unexpected media type in fake")
    pos = fd.tell()
    fd.seek(0)
    data = fd.read()
    fd.seek(pos)
    return data


class FakeSheet:
    """One spreadsheet's worth of named tabs held as grids."""

    def __init__(self):
        self.tabs: dict[str, list[list]] = {"Sheet1": []}


class FakeDrive:
    def __init__(self):
        self.by_id: dict[str, dict] = {}
        self.next_id = 1
        self.fail_next: list[Exception] = []
        #: errors raised on every call, for the case that never clears
        self.always_fail: list[Exception] = []
        #: every files() call and its kwargs, to assert what was asked for
        self.calls: list[tuple[str, dict]] = []

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

    def only(self) -> dict:
        assert len(self.by_id) == 1, self.by_id
        return next(iter(self.by_id.values()))

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
        self.d.calls.append(("files.list", kw))
        parent = q.split("'")[1]
        return self._guard(lambda: {"files": [
            # `properties` matters: it carries the title marker this app stamps on
            # files it created, which is how init tells its own empty spreadsheet
            # from somebody else's converted upload.
            {k: v for k, v in f.items()
             if k in ("id", "name", "mimeType", "properties") and v}
            for f in self.d.by_id.values() if f["parent"] == parent
        ]})

    def create(self, body=None, media_body=None, fields="", **kw):
        self.d.calls.append(("files.create", kw))
        data = media_bytes(media_body)
        mime = (body or {}).get("mimeType", "")

        def run():
            fid = self.d.new_id()
            entry = {
                "id": fid,
                "name": body["name"],
                "mimeType": mime,
                "content": data,
                "parent": (body.get("parents") or ["root"])[0],
                # Whatever custom file properties were asked for, kept: the sheets
                # backend stamps a title marker here and init later reads it back to
                # tell its own files from a stranger's.
                "properties": dict(body.get("properties") or {}),
                "sheet": None,
            }
            if mime == SHEET:
                entry["sheet"] = FakeSheet()
                entry["content"] = b""
            self.d.by_id[fid] = entry
            return {"id": fid}

        return self._guard(run)

    def get(self, fileId, fields="", **kw):
        """Read one object's metadata, which is how a folder gets verified."""
        self.d.calls.append(("files.get", {"fileId": fileId, "fields": fields}))
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

    def update(self, fileId, body=None, media_body=None, **kw):
        data = media_bytes(media_body)

        def run():
            entry = self.d.by_id[fileId]
            if media_body is not None:
                entry["content"] = data
            return {"id": fileId}

        return self._guard(run)


class FakeSheets:
    def __init__(self, drive: FakeDrive, mangle: bool = False):
        self.d = drive
        self.mangle = mangle

    def _sheet(self, spreadsheetId) -> FakeSheet:
        return self.d.by_id[spreadsheetId]["sheet"]

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
        sheet = self.d.by_id[spreadsheetId]["sheet"]
        return FakeRequest(lambda: {
            "sheets": [
                {"properties": {
                    "sheetId": 100 + i,
                    "title": title,
                    "gridProperties": {"rowCount": max(len(grid), 1000)},
                }}
                for i, (title, grid) in enumerate(sheet.tabs.items())
            ]
        })

    def batchUpdate(self, spreadsheetId, body=None, **kw):
        sheet = self.d.by_id[spreadsheetId]["sheet"]
        for req in body.get("requests", []):
            if "addSheet" in req:
                sheet.tabs[req["addSheet"]["properties"]["title"]] = []
            elif "updateSheetProperties" in req:
                up = req["updateSheetProperties"]["properties"]
                old = next(t for i, t in enumerate(sheet.tabs) if 100 + i == up["sheetId"])
                sheet.tabs[up["title"]] = sheet.tabs.pop(old)
            elif "deleteDimension" in req:
                r = req["deleteDimension"]["range"]
                title = next(t for i, t in enumerate(sheet.tabs) if 100 + i == r["sheetId"])
                del sheet.tabs[title][r["startIndex"]:r["endIndex"]]
        return FakeRequest(lambda: {})


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

    def get(self, spreadsheetId, range="", **kw):
        grid = self.d.by_id[spreadsheetId]["sheet"].tabs.get(self._tab(range), [])
        return FakeRequest(lambda: {"values": grid})

    def update(self, spreadsheetId, range="", body=None, **kw):
        rows = [list(r) for r in body["values"]]
        if self.mangle:
            # Stand-in for Sheets deciding a value "means" something else.
            rows = [[str(v).upper() for v in row] for row in rows]
        tab = self.d.by_id[spreadsheetId]["sheet"].tabs[self._tab(range)]
        # Real Sheets overwrites only the written range; rows below survive --
        # which is how a stale-row bug survived an early live Drive session.
        start = self._start_row(range) - 1
        tab[start:start + len(rows)] = rows
        return FakeRequest(lambda: {"updatedRows": len(rows)})

    def clear(self, spreadsheetId, range="", **kw):
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
    store._overrides = dict(config.drive_file_ids or {})
    store._listing = None
    store._cache = {}
    store._drive = drive
    if sheets is not None:
        store._sheets = sheets
    return store


def sheets_store(config, drive, mangle=False):
    return make_store(DriveSheetsStore, config, drive, FakeSheets(drive, mangle=mangle))


class TestHelpers:
    def test_column_letters(self):
        assert _column_letter(1) == "A"
        assert _column_letter(26) == "Z"
        assert _column_letter(27) == "AA"
        assert _column_letter(702) == "ZZ"

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
        drive.only()["sheet"].tabs["items"].append(["smp_bbbbbbbb"])
        assert len(store.read_table("items").rows) == 2

    def test_ttl_positive_serves_the_cache(self, drive):
        cfg = Config(backend="drive", drive_folder_id="FOLDERID", cache_ttl_seconds=60)
        store = sheets_store(cfg, drive)
        store.write_table("items", ("item_id",), [{"item_id": "smp_aaaaaaaa"}], None)
        assert len(store.read_table("items").rows) == 1
        drive.only()["sheet"].tabs["items"].append(["smp_bbbbbbbb"])
        assert len(store.read_table("items").rows) == 1, "within TTL the cache answers"


class TestDriveSheets:
    def test_create_spreadsheet_and_rename_default_tab(self, config, drive):
        store = sheets_store(config, drive)
        store.write_table("items", ("item_id", "name"),
                          [{"item_id": "smp_aaaaaaaa", "name": "one"}], None)
        entry = drive.only()
        assert entry["name"] == "items"         # no .csv suffix on a spreadsheet
        assert "items" in entry["sheet"].tabs   # Sheet1 renamed, not orphaned
        assert "Sheet1" not in entry["sheet"].tabs

    def test_numbers_come_back_as_the_text_we_sent(self, config, drive):
        store = sheets_store(config, drive)
        store.write_table("measurements", ("measurement_id", "value_num"),
                          [{"measurement_id": "msm_aaaaaaaa", "value_num": "47.2"}], None)
        drive.only()["sheet"].tabs["measurements"][1][1] = 47.2  # Sheets coerced it
        assert store.read_table("measurements").rows[0]["value_num"] == "47.2"

    def test_integer_cells_do_not_grow_a_spurious_decimal(self, config, drive):
        drive.add("containers", mime=SHEET,
                  sheet=FakeSheet())
        store = sheets_store(config, drive)
        drive.only()["sheet"].tabs["containers"] = [["container_id", "rows"],
                                                    ["ctr_aaaaaaaa", 10]]
        assert store.read_table("containers").rows[0]["rows"] == "10"

    def test_shrink_trims_the_rows_below(self, config, drive):
        store = sheets_store(config, drive)
        header = ("item_id",)
        many = [{"item_id": f"smp_{n:08d}"} for n in range(5)]
        store.write_table("items", header, many, None)
        few = [{"item_id": "smp_00000001"}]
        rev = store.write_table("items", header, few, store.read_table("items").revision)
        grid = drive.only()["sheet"].tabs["items"]
        assert len(grid) == 2, "header plus one row; stale rows must not survive a shrink"
        assert store.read_table("items").revision == rev

    def test_missing_tab_reads_as_empty_not_error(self, config, drive):
        drive.add("items", mime=SHEET,
                  sheet=FakeSheet())
        store = sheets_store(config, drive)
        data = store.read_table("items")
        assert data.header == () and data.rows == ()

    def test_renamed_tab_refuses_to_read_as_empty(self, config, drive):
        store = sheets_store(config, drive)
        store.write_table("items", ("item_id",), [{"item_id": "smp_00000001"}], None)
        # A human renames the tab; the grid survives under its new name.
        sheet = drive.only()["sheet"]
        sheet.tabs["items DO NOT TOUCH"] = sheet.tabs.pop("items")
        with pytest.raises(StoreError, match="none named"):
            store.read_table("items")
        # A session holding the old revision cannot write over it either.
        with pytest.raises(RevisionConflict):
            store.write_table(
                "items", ("item_id",), [{"item_id": "smp_00000002"}],
                compute_revision(("item_id",), [{"item_id": "smp_00000001"}]),
            )

    def test_coerced_content_is_detected_after_write(self, config, drive):
        """If Sheets rewrites what we sent, say so rather than believing our own write."""
        store = sheets_store(config, drive, mangle=True)
        with pytest.raises(StoreError, match="different rendering"):
            store.write_table("items", ("item_id", "name"),
                              [{"item_id": "smp_aaaaaaaa", "name": "lower case"}], None)

    def test_extra_columns_survive_a_read(self, config, drive):
        """Someone added a column by hand; it must be visible, not dropped."""
        drive.add("items", mime=SHEET,
                  sheet=FakeSheet())
        store = sheets_store(config, drive)
        drive.only()["sheet"].tabs["items"] = [
            ["item_id", "notes_backup"], ["smp_aaaaaaaa", "typed in the browser"]
        ]
        data = store.read_table("items")
        assert data.rows[0]["notes_backup"] == "typed in the browser"

    def test_every_drive_call_asks_about_shared_drives(self, config, drive):
        """Production lives in a shared drive, where a request that doesn't pass
        supportsAllDrives is answered with 404 -- precisely what a deleted table
        looks like. So it has to be every call, not the ones that look relevant."""
        store = sheets_store(config, drive)
        store.write_table("items", ("item_id",), [{"item_id": "smp_aaaaaaaa"}], None)
        store.invalidate()
        store.read_table("items")
        assert drive.calls, "expected the folder to have been listed"
        blind = [name for name, kw in drive.calls if not kw.get("supportsAllDrives")]
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

    def test_deleted_file_is_not_resurrected_by_an_update(self, config, drive):
        store = sheets_store(config, drive)
        store.write_table("items", ("item_id",), [{"item_id": "smp_aaaaaaaa"}], None)
        rev = store.current_revision("items")
        drive.by_id.clear()
        store.invalidate()
        with pytest.raises(RevisionConflict, match="no Drive file"):
            store.write_table("items", ("item_id",), [{"item_id": "smp_bbbbbbbb"}], rev)


class TestThroughTheService:
    """The whole stack -- init, seed, validate, commit -- over a fake Drive.

    The point is not to re-test the service; it is to find the places where the
    service assumes something about storage that only the local backend provides.
    """

    def test_init_and_seed_over_sheets(self, drive):
        from inventory_mcp.initdb import init_database
        from inventory_mcp.seed import seed

        cfg = Config(backend="drive", drive_folder_id="FOLDERID", actor="tester",
                     cache_ttl_seconds=0)
        store = sheets_store(cfg, drive)
        svc, created = init_database(cfg, store=store)
        assert len(created) == 12
        counts = seed(svc)
        assert counts["samples"] == 10
        report = svc.doctor()
        assert report["error_count"] == 0, report["errors"]
        assert report["table_counts"]["measurements"] == 18

    def test_reads_and_aggregates_work_after_seeding(self, drive):
        from inventory_mcp.initdb import init_database
        from inventory_mcp.seed import seed
        from inventory_mcp.service import Service

        cfg = Config(backend="drive", drive_folder_id="FOLDERID", actor="tester",
                     cache_ttl_seconds=0)
        svc, _ = init_database(cfg, store=sheets_store(cfg, drive))
        seed(svc)
        present = svc.search_items(item_class="sample", status="present")["rows"]
        assert present
        boxes = [c for c in svc._occupancy_rows() if c["has_capacity"]]
        assert all(b["occupied"] <= b["capacity"] for b in boxes)
        out = svc.run_query("select count(*) n from items where class='sample'")
        assert out["rows"][0]["n"] == 10

    def test_writes_have_no_local_undo_and_say_so(self, drive):
        """Snapshots need a directory; Drive's own version history is the fallback."""
        from inventory_mcp.initdb import init_database

        cfg = Config(backend="drive", drive_folder_id="FOLDERID", actor="tester",
                     cache_ttl_seconds=0)
        svc, _ = init_database(cfg, store=sheets_store(cfg, drive))
        result = svc.create_sample("Remote tube", "dna")
        assert result.snapshot.path is None

    def test_browser_edit_aborts_the_next_commit(self, drive):
        from inventory_mcp.initdb import init_database
        from inventory_mcp.service import Conflict

        cfg = Config(backend="drive", drive_folder_id="FOLDERID", actor="tester",
                     cache_ttl_seconds=0)
        store = sheets_store(cfg, drive)
        svc, _ = init_database(cfg, store=store)
        svc.create_sample("Registered before the edit", "dna")

        # Someone types into the open spreadsheet. It has to be a table the next
        # commit will touch -- CAS compares only what it is about to write, which
        # is exactly why an edit elsewhere in the workbook goes unnoticed.
        entry = next(e for e in drive.by_id.values() if e["name"] == "items")
        grid = entry["sheet"].tabs["items"]
        name_col = grid[0].index("name")
        grid[1][name_col] = "renamed by a human"

        with pytest.raises(Conflict, match="browser"):
            svc.create_sample("Racing a human", "dna")
        # the hand edit survived the abort
        rows = store.read_table("items").rows
        assert rows[0]["name"] == "renamed by a human"

    def test_partial_commit_rolls_back_tables_already_written(self, drive):
        """A CAS failure mid-loop must not leave earlier tables half-committed.

        Live finding: with the kits spreadsheet gone, create_supply wrote items
        first and aborted on kits -- an orphaned items row that then poisoned
        every later commit. The poison here goes in SAMPLES, the second table
        create_sample writes, so items lands before the abort.
        """
        from inventory_mcp.initdb import init_database
        from inventory_mcp.service import Conflict

        cfg = Config(backend="drive", drive_folder_id="FOLDERID", actor="tester",
                     cache_ttl_seconds=0)
        store = sheets_store(cfg, drive)
        svc, _ = init_database(cfg, store=store)

        rev = store.current_revision("samples")
        store.write_table("samples", ("sample_id",), [{"sample_id": "smp_poison00"}], rev)
        with pytest.raises(Conflict, match="Reverted: items"):
            svc.create_sample("Half-written ghost", "dna")
        rows = store.read_table("items").rows
        assert all(r.get("name") != "Half-written ghost" for r in rows), \
            "aborted commit left an items row behind"


class TestFolderResolution:
    def test_only_files_in_the_named_folder_count(self, config, drive):
        drive.add("items", mime=SHEET, sheet=FakeSheet())
        drive.add("samples", mime=SHEET, sheet=FakeSheet(), parent="somewhere_else")
        store = sheets_store(config, drive)
        assert store.list_tables() == ["items"]
        assert store.stray_objects() == []

    def test_unrelated_files_are_reported_as_strays(self, config, drive):
        drive.add("items", mime=SHEET, sheet=FakeSheet())
        drive.add("old_airtable_export")
        store = sheets_store(config, drive)
        assert store.stray_objects() == ["old_airtable_export"]

    def test_explicit_file_id_override_wins(self, drive):
        cfg = Config(backend="drive", drive_folder_id="FOLDERID",
                     drive_file_ids={"items": "fid2"}, cache_ttl_seconds=0)
        drive.add("whatever-we-call-it", mime=SHEET, sheet=FakeSheet())
        store = sheets_store(cfg, drive)
        assert store.file_id("items") == "fid2"
        assert "items" in store.list_tables()

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

    FOLDER = "application/vnd.google-apps.folder"

    @pytest.fixture
    def api(self, drive, monkeypatch):
        from inventory_mcp.store import drive as drive_module

        monkeypatch.setattr(drive_module, "drive_service", lambda config, **kw: drive)
        return drive

    def _check(self, fid):
        return verify_folder(Config(backend="drive", drive_folder_id=fid))

    def test_the_folder_answers_with_its_name(self, api):
        fid = api.add("Lab inventory", mime=self.FOLDER, parent="root")
        meta = self._check(fid)
        assert meta["name"] == "Lab inventory", "the name is how you catch the wrong folder"
        asked = api.calls[-1][1]["fields"]
        assert "canEdit" in asked, "without it the read-only check below silently never fires"

    def test_an_id_that_names_nothing_says_so(self, api):
        api.add("Lab inventory", mime=self.FOLDER, parent="root")
        with pytest.raises(StoreError, match="no such Drive folder"):
            self._check("fid999")

    def test_a_spreadsheet_is_not_a_folder(self, api):
        fid = api.add("items", mime=SHEET, sheet=FakeSheet(), parent="root")
        with pytest.raises(StoreError, match="is a spreadsheet, not a folder"):
            self._check(fid)

    def test_a_folder_this_account_cannot_write_is_refused_early(self, api):
        fid = api.add("Shared read-only", mime=self.FOLDER, parent="root")
        api.by_id[fid]["canEdit"] = False
        with pytest.raises(StoreError, match="read-only"):
            self._check(fid)

    def test_capabilities_drive_will_not_promise_are_not_read_as_read_only(self, api):
        """A missing key means unknown. Treating it as False would refuse every
        shared drive, which is exactly the deployment this backend exists for."""
        fid = api.add("Team drive root", mime=self.FOLDER, parent="root")
        assert "capabilities" not in api.by_id[fid]
        assert self._check(fid)["id"] == fid

    def test_a_folder_that_won_t_show_itself_names_the_fix(self, api):
        fid = api.add("Elsewhere", mime=self.FOLDER, parent="root")
        api.always_fail = [HttpErr(403, "insufficient")]
        with pytest.raises(StoreError, match="shared with that account"):
            self._check(fid)


class TestAdoption:
    """``init`` refusing to take over a spreadsheet that merely wears a table's name.

    The dangerous case is the quiet one. A CSV converted on upload arrives as one
    Google spreadsheet with a single default tab -- so reading it yields no header at
    all, which looks exactly like an empty table of ours. ``init`` found nothing to
    object to, and the first ordinary commit renamed that default tab and wrote over
    the contents. Nothing here contacts Google; the fake models the shapes involved.
    """

    def _init(self, cfg, drive):
        return init_database(cfg, store=sheets_store(cfg, drive))

    def test_a_converted_upload_wearing_our_name_is_refused(self, config, drive):
        drive.add("items", mime=SHEET, sheet=FakeSheet(), parent="FOLDERID")
        with pytest.raises(ForeignTableError, match="does not carry the title marker"):
            self._init(config, drive)
        assert len(drive.by_id) == 1, "refusing must not leave a half-built database"

    def test_somebody_else_s_header_in_our_tab_is_refused_with_evidence(self, config, drive):
        stranger = FakeSheet()
        stranger.tabs = {"items": [["order", "patient"], ["47", "P-9"]]}
        drive.add("items", mime=SHEET, sheet=stranger, parent="FOLDERID")
        with pytest.raises(ForeignTableError, match=r"\['order', 'patient'\]"):
            self._init(config, drive)

    def test_our_own_empty_spreadsheet_is_adopted(self, config, drive):
        """The state immediately after creation, which re-running init must survive."""
        drive.add("items", mime=SHEET, sheet=FakeSheet(), parent="FOLDERID",
                  properties={"title": "items"})
        _, created = self._init(config, drive)
        assert "items" not in created
        assert len(created) == len(schema.TABLES) - 1

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
        """Overlap, not equality: live folders predate column renames, and refusing
        them would lock out the second machine attaching to the first one's work."""
        spec = schema.get_table("containers")
        old = [c if c != "capacity" else "capacity_override" for c in spec.column_names]
        drifted = FakeSheet()
        drifted.tabs = {"containers": [old]}
        drive.add("containers", mime=SHEET, sheet=drifted, parent="FOLDERID")
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
