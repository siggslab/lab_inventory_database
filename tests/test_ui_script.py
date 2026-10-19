"""The bound Apps Script, run offline against the interface this build publishes.

``inventory_mcp/inventory_ui.gs`` runs inside the workbook, pushed there by
``ui --install`` or pasted by hand, so nothing normally checks that it still agrees with
this codebase -- and a mistake in it surfaces as a wrong row in someone's lab record. Two
things are tested here.

**Agreement.** Where the script has to reach the same verdict as ``validation.py`` or
``service.py`` (is this well real, which reading does this replace, is this id usable,
what is this container's capacity), its answer is compared with Python's on the same
input. Its refusal *wording* is deliberately not compared: that is written for someone at
a bench, not for a tool caller.

**Behaviour.** Below that, the script runs against a stub of the handful of Sheets calls
it makes, driven by the real ``Start`` layout and the real ``options`` map produced by
``sheetui``. Boxes are filled by their published cell references and the button is
pressed, so what comes back is the actual state of the tabs afterwards -- including the
Result line, which is the only feedback a person pressing a button ever gets.

node is required. Without it these tests skip rather than pretend: there is no second way
to run a Google Apps Script.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from inventory_mcp import ids, schema, sheetui, uiscript, validation
from inventory_mcp.config import Config
from inventory_mcp.initdb import init_database

# One definition of where the script lives: the same path `ui --install` reads, so a
# move cannot leave the tests proving that an old file still agrees with this build.
SCRIPT = uiscript.SCRIPT_SOURCE
NODE = shutil.which("node")

requires_node = pytest.mark.skipif(NODE is None, reason="node is not installed")

# ---------------------------------------------------------------------------
# the stub: enough of Apps Script for the script's own logic to run
# ---------------------------------------------------------------------------

STUBS = r"""
// One sheet. Only the calls inventory_ui.gs actually makes are implemented; anything
// else throws, so a new call in the script shows up here as a failure rather than as a
// test that quietly proved nothing.
function FakeSheet(grid) {
  this.grid = grid.map(function (row) { return row.slice(); });
  this.formats = {};
  this.wraps = {};
}
FakeSheet.prototype.getDataRange = function () {
  var self = this;
  return {
    getValues: function () {
      return self.grid.map(function (row) { return row.slice(); });
    }
  };
};
FakeSheet.prototype.getMaxRows = function () { return Math.max(this.grid.length, 40); };
FakeSheet.prototype.getLastRow = function () { return this.grid.length; };
FakeSheet.prototype.insertRowsAfter = function (after, howMany) {
  for (var i = 0; i < howMany; i++) this.grid.push([]);
};
FakeSheet.prototype.getRange = function (row, col, numRows, numCols) {
  return new FakeRange(this, row, col, numRows || 1, numCols || 1);
};
FakeSheet.prototype.setName = function (name) { this.name = name; };

// Over-grid images: what an inserted button is. The real API gives them no id either,
// which is why the script recognises its own buttons by alt text.
FakeSheet.prototype.getImages = function () {
  return (this.images || []).slice();
};
FakeSheet.prototype.insertImage = function (blob, column, row) {
  // Column before row, unlike getRange -- kept in that order so a swap shows up here.
  var image = new FakeImage(this, blob, column, row);
  this.images = this.images || [];
  this.images.push(image);
  return image;
};

function FakeImage(sheet, blob, column, row) {
  this.sheet = sheet;
  this.blob = blob;
  this.column = column;
  this.row = row;
  this.altTitle = '';
  this.altDescription = '';
  this.assigned = '';
  this.width = 0;
  this.height = 0;
}
FakeImage.prototype.setAltTextTitle = function (title) { this.altTitle = title; return this; };
FakeImage.prototype.getAltTextTitle = function () { return this.altTitle; };
FakeImage.prototype.setAltTextDescription = function (text) {
  this.altDescription = text;
  return this;
};
FakeImage.prototype.getAltTextDescription = function () { return this.altDescription; };
FakeImage.prototype.assignScript = function (name) { this.assigned = name; return this; };
FakeImage.prototype.getScript = function () { return this.assigned; };
FakeImage.prototype.setWidth = function (width) { this.width = width; return this; };
FakeImage.prototype.setHeight = function (height) { this.height = height; return this; };
FakeImage.prototype.setAnchorCell = function (range) {
  this.column = range.col;
  this.row = range.row;
  return this;
};
FakeImage.prototype.getAnchorCell = function () {
  return new FakeRange(this.sheet, this.row, this.column);
};
FakeImage.prototype.remove = function () {
  var self = this;
  this.sheet.images = (this.sheet.images || []).filter(function (one) {
    return one !== self;
  });
};

function FakeRange(sheet, row, col, numRows, numCols) {
  this.sheet = sheet;
  this.row = row;
  this.col = col;
  this.numRows = numRows;
  this.numCols = numCols;
}
FakeRange.prototype.setNumberFormat = function (format) {
  this.sheet.formats[this.row + ':' + this.col] = format;
  return this;
};
FakeRange.prototype.setWrap = function (wrap) {
  this.sheet.wraps[this.row + ':' + this.col] = wrap;
  return this;
};
FakeRange.prototype.setValues = function (values) {
  for (var r = 0; r < this.numRows; r++) {
    for (var c = 0; c < this.numCols; c++) {
      putCell(this.sheet, this.row + r, this.col + c, values[r][c]);
    }
  }
  return this;
};
FakeRange.prototype.setValue = function (value) {
  putCell(this.sheet, this.row, this.col, value);
  return this;
};
FakeRange.prototype.getValue = function () {
  var line = this.sheet.grid[this.row - 1] || [];
  var value = line[this.col - 1];
  return value === undefined ? '' : String(value);
};

function putCell(sheet, row, col, value) {
  while (sheet.grid.length < row) sheet.grid.push([]);
  var line = sheet.grid[row - 1];
  while (line.length < col) line.push('');
  line[col - 1] = value;
}

function FakeWorkbook(grids) {
  this.sheets = {};
  var self = this;
  Object.keys(grids).forEach(function (title) {
    var sheet = new FakeSheet(grids[title]);
    sheet.name = title;
    self.sheets[title] = sheet;
  });
}
FakeWorkbook.prototype.getSheetByName = function (title) {
  return this.sheets.hasOwnProperty(title) ? this.sheets[title] : null;
};
FakeWorkbook.prototype.getSpreadsheetTimeZone = function () { return 'Etc/UTC'; };

var WORKBOOK = null;

// The Ui records what was built rather than showing anything, because "one menu item per
// published action" is exactly the kind of promise worth testing.
function FakeUi() {
  this.menus = [];
  this.alerts = [];
  this.ButtonSet = {OK: 'ok'};
}
FakeUi.prototype.createMenu = function (caption) {
  // Only the methods Apps Script's MenuBuilder actually has. An earlier version of this
  // stub carried `addAction` because the script called it -- the double agreeing with the
  // bug, so the suite passed while Sheets threw "menu.addAction is not a function" on open.
  // Anything the script invents now fails here exactly where production fails.
  var menu = {caption: caption, items: []};
  this.menus.push(menu);
  return {
    addItem: function (label, fn) {
      menu.items.push({label: label, call: fn});
      return this;
    },
    addSeparator: function () {
      menu.items.push({separator: true});
      return this;
    },
    addToUi: function () {
      menu.added = true;
      return this;
    }
  };
};
FakeUi.prototype.alert = function (title, message) {
  this.alerts.push({title: title, message: message});
  return {Button: 'ok'};
};

var UI = new FakeUi();

var SpreadsheetApp = {
  getActiveSpreadsheet: function () { return WORKBOOK; },
  getUi: function () { return UI; },
  flush: function () {}
};
var PropertiesService = {
  // The current Properties API: getProperty / setProperty / deleteProperty. The legacy
  // getString / setString are gone from the V8 runtime, but this stub carried them --
  // the double agreeing with the script again, exactly as `addAction` did, so the suite
  // passed while a refused cell value surfaced as "setString is not a function".
  //
  // One store for the process, because script properties persist across calls in
  // production; a fresh object per call would make every read look like a miss.
  getScriptProperties: function () {
    return SCRIPT_PROPERTIES;
  }
};
var SCRIPT_PROPERTIES = (function () {
  var store = {};
  return {
    getProperty: function (key) { return store.hasOwnProperty(key) ? store[key] : null; },
    setProperty: function (key, value) { store[key] = String(value); },
    deleteProperty: function (key) { delete store[key]; },
    _all: function () { return store; }
  };
})();
var LockService = {
  getScriptLock: function () {
    return { tryLock: function () { return true; }, releaseLock: function () {} };
  }
};
// Blank by default, because that is what a script without the userinfo.email scope gets.
// A test names an account with `account=` rather than by editing this, so the fallback
// chain is exercised instead of assumed.
var SESSION_EMAIL = '';
var Session = {
  getActiveUser: function () {
    return { getEmail: function () { return SESSION_EMAIL; } };
  }
};
var Logger = { log: function () {} };

// Knows only the date patterns the script asks for, so changing one fails here instead
// of returning something that happens to look right.
var Utilities = {
  formatDate: function (date, zone, pattern) {
    if (zone !== 'UTC' && zone !== 'Etc/UTC') throw new Error('stub speaks UTC only: ' + zone);
    var pad = function (n) { return (n < 10 ? '0' : '') + n; };
    var parts = {
      yyyy: String(date.getUTCFullYear()), MM: pad(date.getUTCMonth() + 1),
      dd: pad(date.getUTCDate()), HH: pad(date.getUTCHours()),
      mm: pad(date.getUTCMinutes()), ss: pad(date.getUTCSeconds())
    };
    var known = ["yyyy-MM-dd'T'HH:mm'Z'", 'yyyy-MM-dd', 'HH:mm:ss'];
    if (known.indexOf(pattern) < 0) throw new Error('stub does not know the pattern: ' + pattern);
    return pattern.replace(/yyyy|MM|dd|HH|mm|ss/g, function (token) { return parts[token]; })
      .replace(/'/g, '');
  },
  // Decoded for real rather than waved through, so a button icon that was mis-typed or
  // mis-wrapped into the source fails here instead of installing as a broken image.
  base64Decode: function (text) {
    if (typeof Buffer === 'undefined') throw new Error('no base64 decoder in this runtime');
    var clean = String(text).replace(/[^A-Za-z0-9+/=]/g, '');
    if (clean.length !== String(text).replace(/\\s/g, '').length) {
      throw new Error('the embedded base64 holds characters that are not base64');
    }
    return Buffer.from(clean, 'base64');
  },
  newBlob: function (bytes, contentType, name) {
    return {
      bytes: bytes, contentType: contentType, name: name,
      getData: function () { return this.bytes; },
      getContentType: function () { return this.contentType; },
      getName: function () { return this.name; }
    };
  }
};
"""


def node_eval(body: str, payload=None, *, tmp_path, grids=None, prelude: str = ""):
    """Run the script in node with `body` as the driver, and return what it returns.

    `prelude` goes between the stubs and the script, so it can replace a stub before the
    script is even loaded -- which is how a service that must not be reachable during load
    is made genuinely unreachable.
    """
    enums = "var schemaEnums = " + json.dumps(
        {f"{t.name}.{f.name}": list(f.enum)
         for t in schema.TABLES for f in t.fields if f.enum}) + ";"
    boot = (f"WORKBOOK = new FakeWorkbook({json.dumps(grids or {})})"
            if grids is not None else "WORKBOOK = new FakeWorkbook({})")
    script = (STUBS + prelude + SCRIPT.read_text() + "\n" + enums + "\n" + boot
              + "\nfunction __driver__(input) {\n" + body + "\n}\n"
              # `payload or {}` would quietly turn a falsy payload -- "" or 0, both of
              # which a box can hold -- into an object the driver then reads as a value.
              + "console.log(JSON.stringify(__driver__("
              + json.dumps(payload if payload is not None else {}) + ")));\n")
    target = tmp_path / "ui_probe.js"
    target.write_text(script)
    result = subprocess.run([NODE, str(target)], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, (
        f"the script did not run:\n{result.stderr}\n--- driver ---\n{body}")
    return json.loads(result.stdout.strip().splitlines()[-1])


# ---------------------------------------------------------------------------
# a workbook built the way the generator builds one
# ---------------------------------------------------------------------------

HEADER = {t.name: [f.name for f in t.fields] for t in schema.TABLES}


def row(table: str, **values) -> dict:
    """A record with every column present, blank unless named."""
    filled = {name: "" for name in HEADER[table]}
    filled.update(values)
    return filled


def default_seed() -> dict:
    """A small database with somewhere to put things and something already in it."""
    assays = [
        row("assays", assay_id="asy_aaaaaaaa", code="qubit_dsdna", name="Qubit dsDNA",
            quantity="concentration_mass", canonical_unit="ng/ul", result_type="numeric"),
        row("assays", assay_id="asy_bbbbbbbb", code="gel_qc", name="Gel QC",
            quantity="other", canonical_unit="n/a", result_type="text"),
    ]
    return {
        "containers": [
            row("containers", container_id="ctr_aaaaaaaa", kind="box", label="Box One",
                rows="8", cols="12"),
            row("containers", container_id="ctr_bbbbbbbb", kind="rack", label="Rack Full",
                capacity="1"),
            row("containers", container_id="ctr_cccccccc", kind="box", label="Shelf A",
                parent_id="ctr_bbbbbbbb"),
            row("containers", container_id="ctr_dddddddd", kind="box", label="Box Two",
                rows="4", cols="4"),
        ],
        "items": [
            row("items", item_id="smp_aaaaaaaa", name="First tube", **{"class": "sample"},
                status="present", location_id="ctr_aaaaaaaa", position="A1"),
            row("items", item_id="rgt_aaaaaaaa", name="Enzyme", **{"class": "reagent"},
                status="present"),
            row("items", item_id="smp_bbbbbbbb", name="Gone", **{"class": "sample"},
                status="consumed", deleted_at="2026-01-01T00:00Z"),
        ],
        "samples": [row("samples", item_id="smp_aaaaaaaa", sample_type="dna"),
                    row("samples", item_id="smp_bbbbbbbb", sample_type="dna")],
        "reagents": [row("reagents", item_id="rgt_aaaaaaaa")],
        "kits": [], "consumables": [],
        "patients": [row("patients", patient_id="pat_aaaaaaaa", label="P-01")],
        "projects": [row("projects", project_id="prj_aaaaaaaa", name="Study One",
                         status="active")],
        "patient_projects": [row("patient_projects", link_id="ppl_aaaaaaaa",
                                 patient_id="pat_aaaaaaaa", project_id="prj_aaaaaaaa")],
        "item_links": [],
        "measurements": [
            row("measurements", measurement_id="msm_aaaaaaaa", item_id="smp_aaaaaaaa",
                assay_id="asy_aaaaaaaa", measured_on="2026-01-01T00:00Z",
                value_num="10.0", unit="ng/ul"),
        ],
        "_assays_for_options": assays,
    }


def build_grids(seed: dict | None = None) -> dict:
    """Every tab as cells, generated rather than typed -- the same call publish() makes."""
    seed = seed or default_seed()
    start = sheetui.build_start()
    options, _places = sheetui.render_options(seed.get("_assays_for_options", []), start)
    errors = sheetui.build_errors({"error_count": 0, "warning_count": 0,
                                   "errors": [], "warnings": []})
    grids = {"Start": [list(r) for r in start.grid], "options": options,
             "database_errors": [list(r) for r in errors]}
    for table in HEADER:
        grids[table] = [HEADER[table][:]] + [
            [str(cell) for cell in [record.get(name, "") for name in HEADER[table]]]
            for record in seed.get(table, [])]
    return grids


def input_cells() -> dict:
    """action -> {target: cell}, read out of the same generator that lays the sheet out."""
    start = sheetui.build_start()
    _actions, inputs = sheetui.form_blocks(start)
    cells: dict[str, dict[str, str]] = {}
    for action_key, _label, target, _kind, _required, cell, _hint in inputs.rows:
        cells.setdefault(action_key, {})[target] = cell
    return cells


def typed(action: str, values: dict[str, str], seed: dict | None = None, *,
          who: str = "") -> dict:
    """A workbook with the boxes filled in, exactly as the published map places them."""
    grids = build_grids(seed)
    if who:
        grids["Start"][sheetui.build_start().actor_row - 1][1] = who
    for target, cell in input_cells()[action].items():
        if target in values:
            column, digits = 0, ""
            for ch in cell:
                if ch.isalpha():
                    column = column * 26 + (ord(ch.upper()) - ord("A") + 1)
                else:
                    digits += ch
            line = grids["Start"][int(digits) - 1]
            while len(line) < column:
                line.append("")
            line[column - 1] = values[target]
    return grids


def press(action: str, values: dict[str, str], seed: dict | None = None, *,
          tmp_path, who: str = "", account: str | None = None) -> dict:
    """Fill the boxes, press the button, and hand back the tabs as they came out.

    `who` is what the *Who are you?* cell holds; `account` is what the signed-in account
    reports, which is empty unless the script carries the ``userinfo.email`` scope.
    """
    grids = typed(action, values, seed, who=who)
    driver = (
        "runAction_(input.action);\n"
        "var out = {};\n"
        "Object.keys(WORKBOOK.sheets).forEach(function (title) {\n"
        "  out[title] = WORKBOOK.sheets[title].grid;\n"
        "});\n"
        "return out;")
    prelude = "" if account is None else f"SESSION_EMAIL = {json.dumps(account)};"
    result = node_eval(driver, {"action": action}, tmp_path=tmp_path, grids=grids,
                       prelude=prelude)
    return {"grids": result, "result_line": result_line(result, action)}


def result_line(grids: dict, action: str) -> str:
    """What the button left on its Result line -- the only feedback a press produces."""
    start = sheetui.build_start()
    cell = start.actions[action]["result"]
    column, digits = 0, ""
    for ch in cell:
        if ch.isalpha():
            column = column * 26 + (ord(ch.upper()) - ord("A") + 1)
        else:
            digits += ch
    line = grids["Start"][int(digits) - 1]
    return str(line[column - 1]) if len(line) >= column else ""


def records(grids: dict, table: str) -> list[dict]:
    """A tab back as records, so a test can say what was stored rather than where."""
    lines = grids[table]
    header = lines[0]
    out = []
    for line in lines[1:]:
        values = {name: str(line[index]) if index < len(line) else ""
                  for index, name in enumerate(header)}
        if any(v.strip() for v in values.values()):
            out.append(values)
    return out


def find(grids: dict, table: str, key: str) -> dict:
    for record in records(grids, table):
        if record[HEADER[table][0]] == key:
            return record
    raise AssertionError(f"{key} is not on {table}: "
                         f"{[r[HEADER[table][0]] for r in records(grids, table)]}")


# ---------------------------------------------------------------------------


@requires_node
class TestTheScriptStillMatchesThisBuild:
    def test_it_reads_the_layout_this_build_publishes(self, tmp_path):
        got = node_eval("return INVENTORY_LAYOUT_VERSION;", tmp_path=tmp_path)
        assert got == sheetui.LAYOUT_VERSION, (
            "the script reads a different options layout than this build writes")

    def test_every_action_on_the_form_has_a_handler(self, tmp_path):
        keys = [a.key for a in sheetui.FORMS]
        handled = node_eval("return Object.keys(INVENTORY_HANDLERS_);", tmp_path=tmp_path)
        assert sorted(handled) == sorted(keys), (
            "a block on Start has no button behind it, or a button behind nothing")

    def test_every_action_can_be_bound_to_an_image_button(self, tmp_path):
        """An image button names a global function, so each action needs one of its own."""
        keys = [a.key for a in sheetui.FORMS]
        # Apps Script puts a top-level `function` on the global object; node keeps it in
        # module scope, so the names have to be spelled out rather than looked up.
        probes = "\n".join(
            f"try {{ if (typeof btn_{key} !== 'function') out.push('{key}'); }}\n"
            f"catch (e) {{ out.push('{key}'); }}"
            for key in keys)
        missing = node_eval(f"var out = [];\n{probes}\nreturn out;", tmp_path=tmp_path)
        assert not missing, f"no btn_<action> for {missing}"

    def test_it_refuses_a_stale_layout_rather_than_reading_it_wrong(self, tmp_path):
        grids = build_grids()
        for line in grids["options"]:
            for index, cell in enumerate(line):
                if cell == "layout_version":
                    line[index + 1] = str(sheetui.LAYOUT_VERSION + 1)
        outcome = node_eval(
            "try { loadInterface_(); return {loaded: true}; }\n"
            "catch (e) { return {refused: e.message}; }",
            tmp_path=tmp_path, grids=grids)
        assert "refused" in outcome, "a workbook laid out for another version was accepted"
        # Naming the number it found is the proof it read the stamp rather than failing
        # to find it, which is the difference between a version check and an excuse.
        assert str(sheetui.LAYOUT_VERSION + 1) in outcome["refused"], outcome["refused"]


# What a simple trigger may not touch. onOpen runs in AuthMode.LIMITED, and a top-level
# call to any of these would throw while the script is still loading -- which shows up as
# no Extensions menu at all, in every workbook, with nothing on screen to explain it.
LIMITED_PRELUDE = """
var blocked = function (name) {
  return new Proxy({}, {get: function () {
    throw new Error(name + ' is not available in AuthMode.LIMITED');
  }});
};
PropertiesService = blocked('PropertiesService');
UrlFetchApp = blocked('UrlFetchApp');
DriveApp = blocked('DriveApp');
HtmlService = blocked('HtmlService');
ContentService = blocked('ContentService');
"""


def make_action_unprovable(grids: dict) -> int:
    """Set the first published action's `script_can_do_it` to no, returning how many.

    A block is a column of records, so the value sits under its header rather than beside
    it -- writing next to the header edits some other block's cell.
    """
    lines = grids["options"]
    for row, line in enumerate(lines):
        for column, cell in enumerate(line):
            if cell != "script_can_do_it":
                continue
            for below in range(row + 1, len(lines)):
                if column < len(lines[below]) and lines[below][column]:
                    lines[below][column] = "no"
                    return 1
    raise AssertionError("no script_can_do_it column was published on options")


@requires_node
class TestInstallingAndTheMenu:
    def test_the_script_loads_where_a_simple_trigger_runs(self, tmp_path):
        # Loading is the whole test: any top-level use of a service needing authorization
        # throws here, before a single function runs.
        loaded = node_eval("return {loaded: true};", tmp_path=tmp_path,
                           grids=build_grids(), prelude=LIMITED_PRELUDE)
        assert loaded == {"loaded": True}

    def test_the_menu_still_appears_when_properties_are_unavailable(self, tmp_path):
        outcome = node_eval(
            "onOpen();\n"
            "return {menus: UI.menus.map(function (m) { return [m.caption, m.items.length, "
            "!!m.added]; })};",
            tmp_path=tmp_path, grids=build_grids(), prelude=LIMITED_PRELUDE)
        assert len(outcome["menus"]) == 1, outcome
        caption, items, added = outcome["menus"][0]
        assert caption == "Lab inventory" and added, outcome

    def test_the_menu_offers_every_action_the_form_publishes(self, tmp_path):
        titles = [action.title for action in sheetui.FORMS]
        outcome = node_eval(
            "onOpen();\n"
            "var menu = UI.menus[UI.menus.length - 1];\n"
            "return menu.items.filter(function (i) { return !i.separator; })"
            ".map(function (i) { return i.label; });",
            tmp_path=tmp_path, grids=build_grids())
        offered = outcome[:-2]          # the two housekeeping items come last
        assert offered == titles, f"menu offers {offered}, the form publishes {titles}"
        assert any("(refuses)" in label for label in offered) or all(
            action.provable for action in sheetui.FORMS), \
            "an action the script will refuse is offered as though it worked"

    def test_the_menu_names_a_function_that_exists_for_every_item(self, tmp_path):
        """A menu item holds a *name*, resolved when it is clicked -- not a closure.

        So an item can be built happily and still be dead: Apps Script looks the name up at
        press time and reports "Script function not found" to whoever clicked. Nothing else
        in the suite reads the names the way Sheets will.
        """
        items = node_eval(
            "onOpen();\n"
            "var menu = UI.menus[UI.menus.length - 1];\n"
            "return menu.items.filter(function (i) { return !i.separator; }).map("
            "function (i) {\n"
            "  return {label: i.label, name: i.call, resolves: typeof i.call === 'string'"
            " && typeof eval(i.call) === 'function'};\n"
            "});",
            tmp_path=tmp_path, grids=build_grids())
        assert all(item["resolves"] for item in items), [
            item for item in items if not item["resolves"]]

        actions = items[:-2]  # the two housekeeping items come last
        assert [item["name"] for item in actions] == [
            "btn_" + action.key for action in sheetui.FORMS], (
            "each action's menu entry has to reach its own global handler")
        assert [item["name"] for item in items[-2:]] == [
            "inventoryAddButtons_", "inventoryExplainFailure_"]

    def test_the_menu_names_what_it_refuses_rather_than_hiding_it(self, tmp_path):
        grids = build_grids()
        make_action_unprovable(grids)
        outcome = node_eval(
            "onOpen();\n"
            "var menu = UI.menus[UI.menus.length - 1];\n"
            "return menu.items.filter(function (i) { return !i.separator; })"
            ".map(function (i) { return i.label; });",
            tmp_path=tmp_path, grids=grids)
        assert any(label.endswith("(refuses)") for label in outcome), outcome

    def test_the_installed_buttons_land_on_the_published_anchor_cells(self, tmp_path):
        placed = node_eval(
            "inventoryAddButtons_();\n"
            "var ctx = loadInterface_();\n"
            "var start = WORKBOOK.getSheetByName('Start');\n"
            "var out = [];\n"
            "start.getImages().forEach(function (image) {\n"
            "  var key = String(image.getAltTextTitle()).replace('lab-inventory-button:', '');\n"
            "  var want = parseA1_(ctx.actions[key].anchor);\n"
            "  out.push({key: key, column: image.column, row: image.row,\n"
            "            wantColumn: want.col, wantRow: want.row,\n"
            "            assigned: image.getScript(), title: image.getAltTextDescription()});\n"
            "});\n"
            "return out;",
            tmp_path=tmp_path, grids=build_grids())
        provable = [a.key for a in sheetui.FORMS if a.provable]
        assert sorted(p["key"] for p in placed) == sorted(provable)
        for button in placed:
            # insertImage takes column then row, unlike getRange. A swap lands here.
            assert (button["column"], button["row"]) == (button["wantColumn"],
                                                         button["wantRow"]), button
            assert button["assigned"] == "btn_" + button["key"], button
            assert button["title"], "no hover text, so nothing says what it does"

    def test_an_action_that_refuses_gets_no_button(self, tmp_path):
        grids = build_grids()
        make_action_unprovable(grids)
        outcome = node_eval(
            "inventoryAddButtons_();\n"
            "var start = WORKBOOK.getSheetByName('Start');\n"
            "var keys = start.getImages().map(function (image) {\n"
            "  return String(image.getAltTextTitle()).replace('lab-inventory-button:', '');\n"
            "});\n"
            "return {keys: keys, alert: UI.alerts[UI.alerts.length - 1].message};",
            tmp_path=tmp_path, grids=grids)
        expected = len([action for action in sheetui.FORMS if action.provable]) - 1
        assert len(outcome["keys"]) == expected, \
            f"a button was made for the action that now refuses: {outcome['keys']}"
        assert "No button for" in outcome["alert"], outcome["alert"]

    def test_one_insert_that_fails_costs_one_button_not_the_rest(self, tmp_path):
        """A partial install used to look exactly like a finished one.

        The loop was unguarded, so the first image Sheets refused ended it: three buttons
        appeared, six actions got nothing, and the summary reported only what had succeeded.
        """
        outcome = node_eval(
            "inventoryAddButtons_();\n"
            "var start = WORKBOOK.getSheetByName('Start');\n"
            "return {placed: start.getImages().map(function (image) {\n"
            "  return String(image.getAltTextTitle()).replace('lab-inventory-button:', '');\n"
            "}), alert: UI.alerts[UI.alerts.length - 1].message};",
            prelude=(
                "var realInsertImage = FakeSheet.prototype.insertImage;\n"
                "FakeSheet.prototype.insertImage = function (blob, column, row) {\n"
                "  if (row === 61) throw new Error('Simulated refusal at row 61');\n"
                "  return realInsertImage.apply(this, arguments);\n"
                "};\n"),
            tmp_path=tmp_path, grids=build_grids())
        want = sorted(a.key for a in sheetui.FORMS if a.provable)
        want.remove("record_measurement")
        assert sorted(outcome["placed"]) == want, outcome["placed"]
        assert "Could not add" in outcome["alert"], outcome["alert"]
        assert "Simulated refusal at row 61" in outcome["alert"], \
            "the reason has to reach the person looking at the half-finished form"

    def test_installing_twice_moves_buttons_instead_of_duplicating_them(self, tmp_path):
        outcome = node_eval(
            "inventoryAddButtons_();\n"
            "var first = WORKBOOK.getSheetByName('Start').getImages().length;\n"
            "// The layout moved: shift every anchor down three rows, as a rebuilt Start would.\n"
            "var ctx = loadInterface_();\n"
            "Object.keys(ctx.actions).forEach(function (k) { ctx.actions[k].anchor = "
            "ctx.actions[k].anchor.replace(/^([A-Z]+)(\\d+)$/, function (m, c, r) {\n"
            "  return c + (Number(r) + 3);\n"
            "}); });\n"
            "inventoryAddButtons_();\n"
            "var start = WORKBOOK.getSheetByName('Start');\n"
            "return {first: first, second: start.getImages().length,\n"
            "        moved: start.getImages().filter(function (image) {\n"
            "          var key = String(image.getAltTextTitle())"
            ".replace('lab-inventory-button:', '');\n"
            "          var want = parseA1_(loadInterface_().actions[key].anchor);\n"
            "          return image.row === want.row;\n"
            "        }).length,\n"
            "        alert: UI.alerts[UI.alerts.length - 1].message};",
            tmp_path=tmp_path, grids=build_grids())
        assert outcome["second"] == outcome["first"], outcome
        assert outcome["moved"] == outcome["second"], "buttons did not follow the layout"
        assert "already there" in outcome["alert"], outcome["alert"]

    def test_a_button_actually_runs_the_action_it_was_named_for(self, tmp_path):
        """A click resolves by global name, so the name on the image has to do that action.

        Node keeps top-level functions in module scope rather than on globalThis, so the
        name-to-function lookup is spelled out here the way Sheets would perform it.
        """
        dispatch = "\n".join(f"  case 'btn_{action.key}': btn_{action.key}(); break;"
                             for action in sheetui.FORMS)
        outcome = node_eval(
            "inventoryAddButtons_();\n"
            "var image = WORKBOOK.getSheetByName('Start').getImages()[0];\n"
            "var name = image.getScript();\n"
            "switch (name) {\n" + dispatch + "\n}\n"
            "var rows = WORKBOOK.getSheetByName('items').getDataRange().getValues();\n"
            "return {name: name, written: rows[rows.length - 1][2]};",
            tmp_path=tmp_path, grids=typed("add_sample", {
                "items.name": "From the button", "samples.sample_type": "dna"}))
        assert outcome["name"] == "btn_add_sample", outcome
        assert outcome["written"] == "From the button", \
            f"{outcome['name']} did not write the sample its boxes held"

    def test_the_icon_in_the_source_is_the_icon_it_claims_to_be(self, tmp_path):
        header = node_eval(
            "var bytes = Utilities.base64Decode(INVENTORY_BUTTON_PNG);\n"
            "var signature = [137, 80, 78, 71, 13, 10, 26, 10];\n"
            "var ok = true;\n"
            "for (var i = 0; i < 8; i++) ok = ok && (bytes[i] === signature[i]);\n"
            "return {png: ok, width: (bytes[16] << 24) | (bytes[17] << 16) | "
            "(bytes[18] << 8) | bytes[19],\n"
            "        height: (bytes[20] << 24) | (bytes[21] << 16) | (bytes[22] << 8) | "
            "bytes[23],\n"
            "        length: bytes.length};",
            tmp_path=tmp_path)
        assert header["png"], "the embedded constant is not a PNG"
        assert (header["width"], header["height"]) == (96, 26), header
        assert header["length"] < 4096, "an oversized icon bloats a hand-pasted file"


@requires_node
class TestValuesAgreeWithPython:
    def test_ids_it_mints_are_ids_this_server_would_accept(self, tmp_path):
        made = node_eval(
            "var ctx = {constants: {id_alphabet: input.alphabet, id_suffix_length: input.length}, "
            "minted: {}};\n"
            "var out = [];\n"
            "for (var i = 0; i < 50; i++) out.push(mintId_(ctx, 'smp', null));\n"
            "return out;",
            {"alphabet": ids.ALPHABET, "length": ids.SUFFIX_LEN}, tmp_path=tmp_path)
        assert len(set(made)) == len(made), "it handed back the same id twice in one press"
        for candidate in made:
            assert ids.is_valid_id(candidate, "smp"), candidate

    def test_it_stamps_time_in_the_one_spelling_the_server_reads(self, tmp_path):
        stamp = node_eval("return nowStamp_();", tmp_path=tmp_path)
        parsed, problem = validation.parse_datetime(stamp)
        assert parsed is not None, f"{stamp!r}: {problem}"
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}Z", stamp), stamp

    def test_it_keeps_a_typed_number_as_typed(self, tmp_path):
        """`12.50` stays `12.50`, because that is what the server stores."""
        kept = node_eval(
            "var ctx = {values: {'supply.price': text_(input)}, inputs: [{target: "
            "'supply.price', label: 'Price', kind: 'number'}]};\n"
            "return optionalNumber_(ctx, 'supply.price');",
            "12.50", tmp_path=tmp_path)
        assert kept == "12.50"

    @pytest.mark.parametrize("value", ["1,5", "twelve", "12 50", "", " ", "1e3", "-4.75"])
    def test_it_accepts_exactly_the_numbers_python_accepts(self, value, tmp_path):
        # text_ because that is what a cell's contents pass through on the way in; a box
        # holding only a space arrives as blank, exactly as the server would clean it.
        outcome = node_eval(
            "var ctx = {values: {'supply.price': text_(input)}, inputs: [{target: "
            "'supply.price', label: 'Price', kind: 'number'}]};\n"
            "try { return {kept: optionalNumber_(ctx, 'supply.price')}; }\n"
            "catch (e) { return {refused: e.message}; }",
            value, tmp_path=tmp_path)
        stripped = value.strip()
        if not stripped:
            assert outcome == {"kept": ""}, "blank is not a bad number, it is no number"
            return
        parsed, _problem = validation.parse_float(stripped)
        if parsed is None:
            assert "refused" in outcome, f"{value!r} accepted here, rejected by Python"
        else:
            assert "kept" in outcome, f"{value!r} refused here, accepted by Python"


@requires_node
class TestWellsAndCapacityAgreeWithValidation:
    POSITIONS = ["B7", "b7", "1A", "12", "AA13", "Z99", "7", "B", "", "B7C", "13/11", "A0",
                 "ZA1", "10x", "a1"]

    def test_it_splits_a_well_label_exactly_like_validation_does(self, tmp_path):
        split = node_eval(
            "var out = {};\n"
            "for (var i = 0; i < input.length; i++) {\n"
            "  var part = splitWell_(input[i]);\n"
            "  out[input[i]] = part ? [part.row, part.col] : null;\n"
            "}\n"
            "return out;",
            self.POSITIONS, tmp_path=tmp_path)
        for position in self.POSITIONS:
            want = validation.split_position(position)
            got = tuple(split[position]) if split[position] else None
            assert got == want, f"{position!r}: script {got}, validation {want}"

    CONTAINERS = [
        {"rows": "8", "cols": "12"},
        {"rows": "8", "cols": "12", "row_labels": "A,B,C,D,E,F,G,H"},
        {"rows": "8", "cols": "12", "row_labels": "top,bottom"},
        {"rows": "2", "cols": "2", "col_labels": "one two three"},
        {"rows": "", "cols": ""},
        {"rows": "3"},
        {"rows": "2", "cols": "2", "row_labels": "x y z"},
    ]

    @pytest.mark.parametrize("container", CONTAINERS,
                             ids=["96-well", "matching row labels", "short row labels",
                                  "wrong col labels", "no grid", "half a grid", "custom rows"])
    @pytest.mark.parametrize("position", POSITIONS)
    def test_it_calls_a_well_real_or_not_exactly_like_validation_does(
            self, container, position, tmp_path):
        verdict = node_eval(
            "var container = input.container;\n"
            "try { checkWell_(null, container, input.position); return {ok: true}; }\n"
            "catch (e) { return {ok: false, why: e.message}; }",
            {"container": container, "position": position}, tmp_path=tmp_path)
        complaint = validation.validate_position(position, container)
        assert verdict["ok"] == (complaint is None), (
            f"{position!r} in {container}: script ok={verdict['ok']}, "
            f"validation said {complaint!r}")

    def test_it_describes_an_axis_the_way_validation_words_it(self, tmp_path):
        described = node_eval(
            "return [describeAxis_('rows', defaultRowLabels_(12)),\n"
            "        describeAxis_('rows', ['top','bottom']),\n"
            "        describeAxis_('cols', defaultColumnLabels_(9))];",
            tmp_path=tmp_path)
        assert described[0] == validation.describe_axis(
            validation.default_row_labels(12), validation.default_row_labels(12))
        assert described[1] == validation.describe_axis(
            ["top", "bottom"], validation.default_row_labels(2))
        assert described[2] == validation.describe_axis(
            validation.default_col_labels(9), validation.default_col_labels(9))

    def test_capacity_follows_the_databases_own_rule(self, tmp_path):
        cases = [
            {"capacity": "5", "rows": "8", "cols": "12"},   # stated wins
            {"capacity": "", "rows": "8", "cols": "12"},    # else the grid
            {"capacity": "", "rows": "0", "cols": "12"},    # zero is not a grid
            {"capacity": "", "rows": "", "cols": ""},       # else unlimited
            {"capacity": "0", "rows": "3", "cols": "3"},    # zero capacity is no cap
        ]
        guessed = node_eval(
            "var out = [];\n"
            "for (var i = 0; i < input.length; i++) out.push(capacityOf_(input[i]));\n"
            "return out;",
            cases, tmp_path=tmp_path)

        config = Config(backend="local", local_root=str(tmp_path / "db"), actor="tester")
        service, _ = init_database(config)
        header = HEADER["containers"]
        rows = []
        for index, case in enumerate(cases):
            rows.append(row("containers", container_id=f"ctr_{index:08d}", kind="box",
                            label=f"case {index}", **case))
        service.store.write_table("containers", header, rows, None)
        service.reload()
        stated = [service.db.capacity_of(f"ctr_{index:08d}")
                  for index in range(len(cases))]
        assert guessed == stated, f"script {guessed}, database {stated}"


@requires_node
class TestReadingsAgreeWithTheService:
    READINGS = [
        ("msm_aaaaaaaa", "smp_aaaaaaaa", "asy_one", ""),
        ("msm_bbbbbbbb", "smp_aaaaaaaa", "asy_one", ""),
        ("msm_cccccccc", "smp_aaaaaaaa", "asy_two", ""),
        ("msm_dddddddd", "smp_bbbbbbbb", "asy_one", "msm_aaaaaaaa"),
    ]

    @pytest.mark.parametrize("item, assay", [
        ("smp_aaaaaaaa", "asy_one"),
        ("smp_aaaaaaaa", "asy_two"),
        ("smp_bbbbbbbb", "asy_one"),
        ("smp_cccccccc", "asy_one"),
    ])
    def test_it_retires_the_same_reading_the_service_would_retire(self, item, assay, tmp_path):
        """Sheet order, not measured_on: readings are stamped to the minute."""
        rows = [{"measurement_id": mid, "item_id": owner, "assay_id": kind,
                 "supersedes": replaces} for mid, owner, kind, replaces in self.READINGS]
        found = node_eval(
            "records_ = function () { return input.rows; };\n"
            "return currentReadingId_({}, input.item, input.assay);",
            {"rows": rows, "item": item, "assay": assay}, tmp_path=tmp_path)

        config = Config(backend="local", local_root=str(tmp_path / "db"), actor="tester")
        service, _ = init_database(config)
        stored = [row("measurements", measurement_id=mid, item_id=owner, assay_id=kind,
                      measured_on="2026-01-01T00:00Z", supersedes=replaces)
                  for mid, owner, kind, replaces in self.READINGS]
        service.store.write_table("measurements", HEADER["measurements"], stored, None)
        service.reload()
        # The private helper is the predicate under test. The public path around it
        # appends a reading, which is not the thing being compared here.
        assert found == service._current_reading_id(item, assay)


# ---------------------------------------------------------------------------
# pressing buttons
# ---------------------------------------------------------------------------


@requires_node
class TestPressingButtons:
    def test_adding_a_sample_writes_the_pair_and_says_so(self, tmp_path):
        pressed = press("add_sample", {
            "items.name": "Fresh tube",
            "samples.sample_type": "dna",
            "items.location_id": "ctr_aaaaaaaa",
            "items.position": "B3",
            "samples.patient_id": "pat_aaaaaaaa",
            "samples.project_id": "prj_aaaaaaaa",
            "samples.collected_on": "2026-10-18",
        }, tmp_path=tmp_path)
        stored = [r for r in records(pressed["grids"], "items") if r["name"] == "Fresh tube"]
        assert len(stored) == 1, pressed["result_line"]
        item = stored[0]
        assert item["class"] == "sample" and item["status"] == "present"
        assert item["location_id"] == "ctr_aaaaaaaa" and item["position"] == "B3"
        assert item["updated_by"] == "unknown", "no identity box and no signed-in account"
        assert ids.is_valid_id(item["item_id"], "smp")
        paired = find(pressed["grids"], "samples", item["item_id"])
        assert paired["sample_type"] == "dna"
        assert paired["collected_on"] == "2026-10-18"
        assert item["item_id"] in pressed["result_line"], "the id is the useful part"

    def test_the_account_signs_the_press_when_the_box_is_left_blank(self, tmp_path):
        """The middle rung of the chain, which needs the ``userinfo.email`` scope to exist."""
        pressed = press("add_sample", {
            "items.name": "Signed by account",
            "samples.sample_type": "dna",
            "items.location_id": "ctr_aaaaaaaa",
        }, tmp_path=tmp_path, account="y.okafor@example.com")
        stored = [r for r in records(pressed["grids"], "items")
                  if r["name"] == "Signed by account"]
        assert len(stored) == 1, pressed["result_line"]
        assert stored[0]["updated_by"] == "y.okafor@example.com"

    def test_the_box_beats_the_account_because_one_cell_cannot_hold_two_people(self, tmp_path):
        """Whoever is editing may be signing for a colleague's bench, so the cell wins."""
        pressed = press("add_sample", {
            "items.name": "Signed by hand",
            "samples.sample_type": "dna",
            "items.location_id": "ctr_aaaaaaaa",
        }, tmp_path=tmp_path, who="KO", account="y.okafor@example.com")
        stored = [r for r in records(pressed["grids"], "items") if r["name"] == "Signed by hand"]
        assert len(stored) == 1, pressed["result_line"]
        assert stored[0]["updated_by"] == "KO"

    def test_a_box_that_is_not_there_is_refused_and_nothing_is_written(self, tmp_path):
        before = build_grids()
        pressed = press("add_sample", {
            "items.name": "Doomed",
            "samples.sample_type": "dna",
            "items.location_id": "ctr_zzzzzzzz",
        }, tmp_path=tmp_path)
        assert "ctr_zzzzzzzz" in pressed["result_line"]
        assert records(pressed["grids"], "items") == records(before, "items")
        assert records(pressed["grids"], "samples") == records(before, "samples")

    def test_a_well_outside_the_grid_is_refused_with_the_axis_named(self, tmp_path):
        pressed = press("add_sample", {
            "items.name": "Over the edge",
            "samples.sample_type": "dna",
            "items.location_id": "ctr_aaaaaaaa",
            "items.position": "Z99",
        }, tmp_path=tmp_path)
        assert "does not exist" in pressed["result_line"]
        assert "A..H" in pressed["result_line"], pressed["result_line"]
        assert not [r for r in records(pressed["grids"], "items")
                    if r["name"] == "Over the edge"]

    def test_a_value_outside_the_vocabulary_is_refused_and_nothing_is_written(self, tmp_path):
        """The dropdown is the first gate; this is the one that actually holds.

        Validation can be missing, stale, or stepped over by a paste from another tab, so
        the press checks the word against the published vocabulary and names the box.
        """
        before = build_grids()
        pressed = press("add_sample", {
            "items.name": "Banana",
            "samples.sample_type": "banana",
        }, tmp_path=tmp_path)
        assert 'Sample type "banana" is not one of' in pressed["result_line"], \
            pressed["result_line"]
        assert "whole_blood" in pressed["result_line"], "the refusal has to show the choices"
        assert records(pressed["grids"], "items") == records(before, "items")
        assert records(pressed["grids"], "samples") == records(before, "samples")

    def test_an_unexpected_failure_still_answers_and_stays_explainable(self, tmp_path):
        """A bug inside the script owes the person an answer, then has to remember itself.

        The message goes into Script Properties for `Say why the interface is wrong`, and
        storing it used to call the legacy ``setString`` -- which the V8 runtime no longer
        defines. So the catch block threw on its way to answering, and whatever went wrong
        was replaced by a bare TypeError naming neither the action nor the value.
        """
        grids = typed("add_sample", {"items.name": "Unlucky",
                                     "samples.sample_type": "dna"})
        outcome = node_eval(
            "appendRecord_ = function () { throw new Error('a bug inside the script'); };\n"
            "runAction_('add_sample');\n"
            "inventoryExplainFailure_();\n"
            "return {alerts: UI.alerts, stored: SCRIPT_PROPERTIES._all()};",
            {"action": "add_sample"}, tmp_path=tmp_path, grids=grids)

        assert "a bug inside the script" in outcome["stored"]["inventory_interface_error"], (
            "setProperty has to be the method the runtime actually offers")
        explained = outcome["alerts"][-1]["message"]
        assert "a bug inside the script" in explained, (
            "and reading it back is what the menu item is for: " + explained)
        assert "loads fine now" in explained, outcome["alerts"]

    def test_a_project_the_participant_is_not_in_is_refused(self, tmp_path):
        seed = default_seed()
        seed["projects"] = seed["projects"] + [
            row("projects", project_id="prj_bbbbbbbb", name="Study Two", status="active")]
        pressed = press("add_sample", {
            "items.name": "Misfiled",
            "samples.sample_type": "dna",
            "samples.patient_id": "pat_aaaaaaaa",
            "samples.project_id": "prj_bbbbbbbb",
        }, seed=seed, tmp_path=tmp_path)
        assert "not enrolled" in pressed["result_line"]
        assert not [r for r in records(pressed["grids"], "items") if r["name"] == "Misfiled"]

    def test_a_sample_needs_its_extension_row_even_when_it_is_otherwise_empty(self, tmp_path):
        pressed = press("add_sample", {
            "items.name": "Bare",
            "samples.sample_type": "other",
        }, tmp_path=tmp_path)
        stored = [r for r in records(pressed["grids"], "items") if r["name"] == "Bare"]
        assert stored, pressed["result_line"]
        assert find(pressed["grids"], "samples", stored[0]["item_id"])["sample_type"] == "other"

    def test_a_supply_lands_on_the_tab_its_class_names(self, tmp_path):
        pressed = press("add_supply", {
            "items.class": "consumable",
            "items.name": "Tips",
            "supply.price": "12.50",
        }, tmp_path=tmp_path)
        stored = [r for r in records(pressed["grids"], "items") if r["name"] == "Tips"]
        assert stored, pressed["result_line"]
        listed = find(pressed["grids"], "consumables", stored[0]["item_id"])
        assert listed["price"] == "12.50", "typed spelling preserved"
        assert not records(pressed["grids"], "reagents")[:0]  # (reagents untouched below)
        assert [r["item_id"] for r in records(pressed["grids"], "reagents")] == \
            ["rgt_aaaaaaaa"]

    def test_a_sample_is_not_offered_by_the_supply_block(self, tmp_path):
        pressed = press("add_supply", {
            "items.class": "sample",
            "items.name": "Confused",
        }, tmp_path=tmp_path)
        assert "not something this block makes" in pressed["result_line"]
        assert not [r for r in records(pressed["grids"], "items") if r["name"] == "Confused"]

    def test_an_opening_quantity_becomes_a_reading_and_names_its_assay(self, tmp_path):
        pressed = press("add_supply", {
            "items.class": "reagent",
            "items.name": "Mastermix",
            "supply.assay_code": "qubit_dsdna",
            "supply.quantity": "40",
            "supply.unit": "ng/ul",
        }, tmp_path=tmp_path)
        stored = [r for r in records(pressed["grids"], "items") if r["name"] == "Mastermix"]
        assert stored, pressed["result_line"]
        readings = [r for r in records(pressed["grids"], "measurements")
                    if r["item_id"] == stored[0]["item_id"]]
        assert len(readings) == 1
        assert readings[0]["assay_id"] == "asy_aaaaaaaa"
        assert readings[0]["value_num"] == "40" and readings[0]["unit"] == "ng/ul"
        assert readings[0]["supersedes"] == "", "an opening reading replaces nothing"
        assert readings[0]["notes"] == "initial quantity at registration"

    def test_a_unit_from_the_wrong_dimension_is_refused(self, tmp_path):
        pressed = press("add_supply", {
            "items.class": "reagent",
            "items.name": "Wrong dimension",
            "supply.assay_code": "qubit_dsdna",
            "supply.quantity": "40",
            "supply.unit": "ng",
        }, tmp_path=tmp_path)
        assert "not a concentration_mass unit" in pressed["result_line"]
        assert not [r for r in records(pressed["grids"], "items")
                    if r["name"] == "Wrong dimension"]

    def test_recording_a_reading_stores_what_was_observed_and_retires_the_last(self, tmp_path):
        pressed = press("record_measurement", {
            "measurements.item_id": "smp_aaaaaaaa",
            "supply.assay_code": "qubit_dsdna",
            "measurements.value_num": "23.5",
            "measurements.unit": "ng/ul",
        }, tmp_path=tmp_path)
        readings = [r for r in records(pressed["grids"], "measurements")
                    if r["value_num"] == "23.5"]
        assert readings, pressed["result_line"]
        assert readings[0]["supersedes"] == "msm_aaaaaaaa"
        assert "msm_aaaaaaaa" in pressed["result_line"], "the retirement has to be visible"
        assert find(pressed["grids"], "measurements", "msm_aaaaaaaa")["value_num"] == "10.0", \
            "readings are append-only; the old row is never edited"

    def test_a_qualitative_assay_takes_text_and_no_unit(self, tmp_path):
        pressed = press("record_measurement", {
            "measurements.item_id": "smp_aaaaaaaa",
            "supply.assay_code": "gel_qc",
            "measurements.value_text": "single band",
        }, tmp_path=tmp_path)
        readings = [r for r in records(pressed["grids"], "measurements")
                    if r["value_text"] == "single band"]
        assert readings, pressed["result_line"]
        assert readings[0]["unit"] == "" and readings[0]["value_num"] == ""

    def test_a_number_for_a_qualitative_assay_is_refused(self, tmp_path):
        pressed = press("record_measurement", {
            "measurements.item_id": "smp_aaaaaaaa",
            "supply.assay_code": "gel_qc",
            "measurements.value_num": "1",
            "measurements.unit": "count",
        }, tmp_path=tmp_path)
        assert "qualitative" in pressed["result_line"]
        assert len(records(pressed["grids"], "measurements")) == 1

    def test_moving_an_item_frees_the_old_well_and_takes_the_new_one(self, tmp_path):
        pressed = press("move_item", {
            "move.what": "smp_aaaaaaaa",
            "move.into": "ctr_aaaaaaaa",
            "items.position": "C5",
        }, tmp_path=tmp_path)
        moved = find(pressed["grids"], "items", "smp_aaaaaaaa")
        assert moved["location_id"] == "ctr_aaaaaaaa" and moved["position"] == "C5"
        assert "C5" in pressed["result_line"]

    def test_re_picking_the_same_box_leaves_the_well_alone(self, tmp_path):
        """An empty Well box cannot say "clear" apart from "unchanged", so the server keeps
        the well when the container is unchanged; the button has to answer the same way."""
        pressed = press("move_item", {
            "move.what": "smp_aaaaaaaa",
            "move.into": "ctr_aaaaaaaa",
        }, tmp_path=tmp_path)
        moved = find(pressed["grids"], "items", "smp_aaaaaaaa")
        assert (moved["location_id"], moved["position"]) == ("ctr_aaaaaaaa", "A1"), \
            pressed["result_line"]

    def test_moving_to_another_box_with_no_well_gives_the_well_up(self, tmp_path):
        pressed = press("move_item", {
            "move.what": "smp_aaaaaaaa",
            "move.into": "ctr_dddddddd",
        }, tmp_path=tmp_path)
        moved = find(pressed["grids"], "items", "smp_aaaaaaaa")
        assert (moved["location_id"], moved["position"]) == ("ctr_dddddddd", "")
        assert "no well" in pressed["result_line"], "the result has to say the well went too"

    def test_status_keeps_the_well_in_the_same_box_and_clears_it_in_another(self, tmp_path):
        kept = press("set_status", {
            "items.item_id": "smp_aaaaaaaa",
            "items.status": "partially_consumed",
            "items.location_id": "ctr_aaaaaaaa",
        }, tmp_path=tmp_path)
        item = find(kept["grids"], "items", "smp_aaaaaaaa")
        assert (item["status"], item["position"]) == ("partially_consumed", "A1"), \
            kept["result_line"]

        cleared = press("set_status", {
            "items.item_id": "smp_aaaaaaaa",
            "items.status": "present",
            "items.location_id": "ctr_dddddddd",
        }, tmp_path=tmp_path)
        item = find(cleared["grids"], "items", "smp_aaaaaaaa")
        assert (item["location_id"], item["position"]) == ("ctr_dddddddd", "")

    def test_a_well_named_without_a_box_is_refused_rather_than_guessed(self, tmp_path):
        seed = default_seed()
        seed["items"][0]["location_id"] = ""
        pressed = press("set_status", {
            "items.item_id": "smp_aaaaaaaa",
            "items.status": "present",
            "items.position": "C3",
        }, seed=seed, tmp_path=tmp_path)
        assert "no box is set" in pressed["result_line"], pressed["result_line"]
        assert find(pressed["grids"], "items", "smp_aaaaaaaa")["position"] == "A1"

    def test_a_full_container_refuses_the_move_and_counts_out_loud(self, tmp_path):
        pressed = press("move_item", {
            "move.what": "smp_aaaaaaaa",
            "move.into": "ctr_bbbbbbbb",
        }, tmp_path=tmp_path)
        assert "no space in" in pressed["result_line"]
        assert "/1" in pressed["result_line"], pressed["result_line"]
        assert find(pressed["grids"], "items", "smp_aaaaaaaa")["location_id"] == "ctr_aaaaaaaa"

    def test_a_container_cannot_be_moved_inside_itself(self, tmp_path):
        pressed = press("move_item", {
            "move.what": "ctr_aaaaaaaa",
            "move.into": "ctr_aaaaaaaa",
        }, tmp_path=tmp_path)
        assert "into itself" in pressed["result_line"]

    def test_a_container_cannot_be_moved_into_its_own_contents(self, tmp_path):
        pressed = press("move_item", {
            "move.what": "ctr_bbbbbbbb",
            "move.into": "ctr_cccccccc",
        }, tmp_path=tmp_path)
        assert "already inside it" in pressed["result_line"]
        assert find(pressed["grids"], "containers", "ctr_bbbbbbbb")["parent_id"] == ""

    def test_consuming_clears_the_slot_and_says_that_it_is_gone(self, tmp_path):
        pressed = press("set_status", {
            "items.item_id": "smp_aaaaaaaa",
            "items.status": "consumed",
        }, tmp_path=tmp_path)
        item = find(pressed["grids"], "items", "smp_aaaaaaaa")
        assert item["status"] == "consumed"
        assert item["location_id"] == "" and item["position"] == ""
        assert "nothing remembers" in pressed["result_line"]

    def test_a_consumed_item_takes_no_box(self, tmp_path):
        pressed = press("set_status", {
            "items.item_id": "smp_aaaaaaaa",
            "items.status": "consumed",
            "items.location_id": "ctr_aaaaaaaa",
        }, tmp_path=tmp_path)
        assert "holds no slot" in pressed["result_line"]
        assert find(pressed["grids"], "items", "smp_aaaaaaaa")["status"] == "present"

    def test_renaming_follows_the_prefix_not_a_guess(self, tmp_path):
        pressed = press("rename_item", {
            "any.id": "ctr_aaaaaaaa",
            "any.name": "Freezer box 12",
        }, tmp_path=tmp_path)
        assert find(pressed["grids"], "containers", "ctr_aaaaaaaa")["label"] == "Freezer box 12"
        assert find(pressed["grids"], "items", "smp_aaaaaaaa")["name"] == "First tube"

    def test_a_thing_with_no_name_cannot_be_renamed(self, tmp_path):
        pressed = press("rename_item", {
            "any.id": "msm_aaaaaaaa",
            "any.name": "whatever",
        }, tmp_path=tmp_path)
        assert "no name column" in pressed["result_line"]
        assert find(pressed["grids"], "measurements", "msm_aaaaaaaa")["value_num"] == "10.0"

    def test_notes_can_be_cleared_and_that_is_a_write_not_a_refusal(self, tmp_path):
        pressed = press("edit_notes", {"any.id": "smp_aaaaaaaa", "any.notes": ""},
                        tmp_path=tmp_path)
        assert "Cleared the note" in pressed["result_line"]

    def test_retiring_keeps_the_readings_and_frees_the_well(self, tmp_path):
        pressed = press("retire_item", {"items.item_id": "smp_aaaaaaaa"}, tmp_path=tmp_path)
        item = find(pressed["grids"], "items", "smp_aaaaaaaa")
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}Z", item["deleted_at"])
        assert item["location_id"] == "" and item["position"] == ""
        assert item["status"] == "present", "retiring leaves the status alone on purpose"
        assert find(pressed["grids"], "samples", "smp_aaaaaaaa")["sample_type"] == "dna"
        assert find(pressed["grids"], "measurements", "msm_aaaaaaaa")["item_id"] == "smp_aaaaaaaa"

    def test_a_retired_item_refuses_to_be_retired_again(self, tmp_path):
        pressed = press("retire_item", {"items.item_id": "smp_bbbbbbbb"}, tmp_path=tmp_path)
        assert "already retired" in pressed["result_line"]

    def test_a_tombstone_refuses_to_be_edited(self, tmp_path):
        pressed = press("rename_item", {"any.id": "smp_bbbbbbbb", "any.name": "Nope"},
                        tmp_path=tmp_path)
        assert "tombstone" in pressed["result_line"]

    def test_a_mistyped_id_is_refused_before_it_writes_to_the_wrong_row(self, tmp_path):
        # 'o' and 'i' are not in the alphabet; this is the transcription error the
        # alphabet exists to catch.
        pressed = press("retire_item", {"items.item_id": "smp_aioooooo"}, tmp_path=tmp_path)
        assert "is not an id" in pressed["result_line"] or "except the end" in pressed["result_line"]
        assert find(pressed["grids"], "items", "smp_aaaaaaaa")["deleted_at"] == ""

    def test_a_press_leaves_the_boxes_empty_and_the_previous_result_behind(self, tmp_path):
        pressed = press("add_sample", {
            "items.name": "Once only",
            "samples.sample_type": "dna",
        }, tmp_path=tmp_path)
        cells = input_cells()["add_sample"]
        grid = pressed["grids"]["Start"]
        for target in ("items.name", "samples.sample_type"):
            cell = cells[target]
            column, digits = 0, ""
            for ch in cell:
                if ch.isalpha():
                    column = column * 26 + (ord(ch.upper()) - ord("A") + 1)
                else:
                    digits += ch
            line = grid[int(digits) - 1]
            assert (line[column - 1] if len(line) >= column else "") == "", \
                f"{target} still holds what was typed"
