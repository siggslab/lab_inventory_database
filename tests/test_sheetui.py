"""The generated interface tabs.

Three things are worth pinning here, and none of them is "the wording is right":

* **Completeness.** Every enum, foreign key and required flag in ``schema.py`` has to
  reach the sheet. A missing one is invisible -- the form simply fails to offer a
  choice -- so it is asserted against the schema rather than against a list written by
  whoever wrote the test.
* **Agreement.** The cells, the dropdown requests that point at them, and the map on
  ``options`` that the Apps Script reads all come out of one generation pass. If they
  stop agreeing, a button writes to the wrong column -- which looks like a bug in the
  script and is really a bug in the layout.
* **Freshness.** The digest has to notice a schema change, because a workbook whose
  dropdowns describe an older schema is confidently wrong.
"""

from __future__ import annotations

import re

import pytest

from inventory_mcp import schema, sheetui, units
from inventory_mcp.config import Config
from inventory_mcp.initdb import init_database
from test_drive import FakeDrive, sheets_store


@pytest.fixture
def drive():
    return FakeDrive()


@pytest.fixture
def config():
    return Config(backend="drive", drive_folder_id="FOLDERID", actor="tester",
                  cache_ttl_seconds=0)


@pytest.fixture
def built(config, drive):
    """A database made the normal way, which now includes building its interface."""
    svc, _ = init_database(config, store=sheets_store(config, drive))
    return svc


# ---------------------------------------------------------------------------
# reading back what was published
# ---------------------------------------------------------------------------


def expected_options(assay_rows=()):
    """Re-render the reference tab, so the published copy is read by known offsets."""
    start = sheetui.build_start()
    return sheetui.render_options(assay_rows, start)


def block_index(grid, places):
    """The ``blocks`` index as it reads off the tab: name -> (first, last, first_row, last_row)."""
    spot = places["blocks"]
    out = {}
    for line in range(spot["first_row"], spot["last_row"] + 1):
        cells = [str(c) for c in grid[line - 1][spot["first_col"] - 1:spot["last_col"]]]
        if not cells[0].strip():
            continue
        out[cells[0]] = tuple(int(v) for v in cells[1:])
    return out


def block_rows(grid, places, name):
    """The value rows of one block, read out of a rendered or published grid."""
    spot = places[name]
    rows = []
    for line in range(spot["first_row"], spot["last_row"] + 1):
        cells = [str(c) for c in grid[line - 1][spot["first_col"] - 1:spot["last_col"]]]
        if any(c.strip() for c in cells):
            rows.append(cells)
    return rows


def cell(grid, where):
    """The cell an A1 reference names; "" when the grid never reached that far."""
    column, digits = 0, ""
    for ch in where:
        if ch.isalpha():
            column = column * 26 + (ord(ch.upper()) - ord("A") + 1)
        else:
            digits += ch
    row = int(digits)
    if row > len(grid) or column > len(grid[row - 1]):
        return ""
    return str(grid[row - 1][column - 1])


def published_inputs(grid, places):
    """action -> {target: (cell, required)} straight off the published reference tab."""
    out: dict[str, dict[str, tuple[str, str]]] = {}
    for action, label, target, kind, required, where, _hint in block_rows(grid, places, "inputs"):
        out.setdefault(action, {})[target] = (where, required)
    return out


# ---------------------------------------------------------------------------


class TestReferenceTabIsComplete:
    """Whatever the schema knows, the sheet has to know."""

    def test_every_enum_in_the_schema_reaches_the_reference_tab(self):
        blocks = {b.name: b for b in sheetui.options_blocks()}
        for table in schema.TABLES:
            for field in table.fields:
                if not field.enum:
                    continue
                name = f"enum:{table.name}.{field.name}"
                assert name in blocks, f"{name} is missing from options"
                assert [r[0] for r in blocks[name].rows] == list(field.enum)

    def test_every_foreign_key_is_published_with_its_target(self):
        blocks = {b.name: b for b in sheetui.options_blocks()}
        published = {f"{r[0]}.{r[1]}": r[2] for r in blocks["foreign_keys"].rows}
        assert published == sheetui.fk_targets(), (
            "the reference tab and the schema disagree about which columns point somewhere")

    def test_every_column_reports_its_type_and_its_required_flag(self):
        blocks = {b.name: b for b in sheetui.options_blocks()}
        flags = {(r[0], r[1]): (r[2], r[3]) for r in blocks["fields"].rows}
        for table in schema.TABLES:
            for field in table.fields:
                kind, required = flags[(table.name, field.name)]
                assert kind == sheetui.field_kinds()[f"{table.name}.{field.name}"]
                assert required == ("yes" if field.required else "no")

    def test_id_prefixes_cover_every_table_and_every_item_class(self):
        blocks = {b.name: b for b in sheetui.options_blocks()}
        published = {r[1]: r for r in blocks["id_prefixes"].rows}
        for table in schema.TABLES:
            if table.id_prefix:
                assert published[table.id_prefix][0] == table.name
                # A prefix has to say which tab its rows live on, or an id pasted into
                # the rename box is a guess about where to write.
                assert published[table.id_prefix][2] == table.name, table.name
        for item_class, prefix in schema.ITEM_CLASS_PREFIX.items():
            row = published[prefix]
            assert row[0] == f"class {item_class}"
            assert row[2] == "items", "an item's class prefix resolves to the items tab"
            # The real table name, not the class pluralised: consumable does not
            # become consumables, and a script that guessed would write nowhere.
            assert row[3] == schema.CLASS_EXTENSION_TABLE[item_class]

    def test_the_rename_map_agrees_with_what_the_indirection_says_it_does(self):
        """Two independently written things have to describe the same columns.

        ``any.name`` is explained in prose for whoever reads the tab and resolved from
        the published columns by whatever reads it. The prose was written first and by
        hand, so it is the thing worth checking against the derivation.
        """
        blocks = {b.name: b for b in sheetui.options_blocks()}
        by_prefix = {r[1]: r for r in blocks["id_prefixes"].rows}
        by_table = {r[2]: r for r in blocks["id_prefixes"].rows if r[2]}
        prose = dict(sheetui.INDIRECTIONS)
        for target, slot in (("any.name", 4), ("any.notes", 5)):
            for table, column in re.findall(r"\b(\w+)\.(name|label|notes|description)\b",
                                            prose[target]):
                assert by_table[table][slot] == column, f"{target}: {table}.{column}"
        # A measurement has no name to rename, and the map has to say so rather than
        # point somewhere plausible.
        assert by_prefix["msm"][4] == ""

    def test_published_units_are_exactly_the_ones_the_converter_accepts(self):
        """The sheet's list and the converter's table, compared both ways.

        A unit published but not accepted sends someone typing an entry that gets
        refused; a unit accepted but not published hides a spelling that already
        works. Both are silent, so both get asserted.
        """
        for quantity in schema.QUANTITIES:
            published = sheetui.accepted_units(quantity)
            for unit in published:
                assert units.is_known_unit(quantity, unit), (quantity, unit)
            legal = (sorted(units.DIMENSIONS[quantity].factors)
                     if quantity in units.DIMENSIONS else sorted({"", "cells", "%", "count", "n"}))
            assert published == legal, quantity

    def test_legal_units_reach_the_assay_rows(self):
        assays = [
            {"code": "qubit_dsdna", "assay_id": "asy_abcdefgh", "name": "Qubit dsDNA",
             "quantity": "concentration_mass", "canonical_unit": "ng/ul",
             "result_type": "numeric"},
            {"code": "gel_qc", "assay_id": "asy_kxympzqr", "name": "Gel QC",
             "quantity": "other", "canonical_unit": "n/a", "result_type": "text"},
        ]
        blocks = {b.name: b for b in sheetui.options_blocks(assays)}
        rows = {r[0]: r for r in blocks["assays"].rows}
        assert rows["qubit_dsdna"][6] == ", ".join(sheetui.accepted_units("concentration_mass"))
        # An empty unit is legal for a unitless quantity and invisible in a cell, so it
        # gets a word instead -- and the assay's own "n/a" placeholder stays out of it.
        assert rows["gel_qc"][6].startswith("(none)")
        assert "n/a" not in rows["gel_qc"][6]
        # Pinned because nl and pl were accepted but converted fifteen orders of
        # magnitude wrong until the factor table was corrected. The sheet publishes
        # whatever the converter accepts, so this is where that fix becomes visible.
        assert {"nl", "pl", "ul"} <= set(sheetui.accepted_units("volume"))

    def test_the_indirections_the_form_relies_on_are_written_down(self):
        blocks = {b.name: b for b in sheetui.options_blocks()}
        published = dict(blocks["indirections"].rows)
        for target, meaning in sheetui.INDIRECTIONS:
            assert published.get(target) == meaning

    def test_the_index_points_at_the_blocks_it_lists(self):
        """The map the script reads has to be true, or it reads the wrong column.

        Checked against the grid rather than against ``places``, because ``places`` is
        what the generator used -- agreeing with it would prove nothing. The title cell
        is the independent witness: if it says ``enum:items.status``, the column under
        it is the one the index names.
        """
        grid, places = sheetui.render_options()
        listed = block_index(grid, places)
        assert set(listed) == set(places), "a block went unindexed"
        for name, (first, last, first_row, last_row) in listed.items():
            spot = places[name]
            assert (first, last, first_row, last_row) == (spot["first_col"], spot["last_col"],
                                                          spot["first_row"], spot["last_row"])
            assert str(grid[sheetui.TITLE_ROW - 1][first - 1]) == name
            assert first_row >= sheetui.FIRST_VALUE_ROW, "the index would read the header twice"

    def test_a_reader_that_knows_nothing_about_the_layout_still_gets_every_enum(self):
        grid, places = sheetui.render_options()
        listed = block_index(grid, places)
        for target, values in sheetui.enum_fields().items():
            first, _last, first_row, last_row = listed[f"enum:{target}"]
            got = [str(grid[line - 1][first - 1]) for line in range(first_row, last_row + 1)]
            assert got == list(values), target

    def test_generating_twice_produces_the_same_sheet(self):
        """Only the timestamp may differ; anything else makes a re-run a surprise."""
        def render():
            grid, places = sheetui.render_blocks(sheetui.options_blocks())
            stamp = places["stamp"]
            for line in grid[stamp["first_row"] - 1:]:
                if line[stamp["first_col"] - 1] == "generated_at":
                    line[stamp["last_col"] - 1] = ""
            return grid

        assert render() == render()


class TestFormContract:
    """The sheet, the dropdowns and the machine map have to describe one layout."""

    def test_every_action_gets_a_title_and_a_writes_line(self):
        start = sheetui.build_start()
        for action in sheetui.FORMS:
            spec = start.actions[action.key]
            assert cell(start.grid, f"A{spec['title_row']}") == action.title.upper()
            assert cell(start.grid, f"C{spec['title_row']}") == action.writes
            anchor_row = int(spec["anchor"][1:])
            assert cell(start.grid, f"A{anchor_row}") == "(button)"
            assert cell(start.grid, f"A{spec['result'][1:]}") == "Result"

    def test_every_input_names_the_column_it_writes(self):
        start = sheetui.build_start()
        for action in sheetui.FORMS:
            for inp in action.inputs:
                where = start.actions[action.key]["inputs"][inp.slug()]
                row = where[1:]
                assert cell(start.grid, f"A{row}").rstrip("*") == inp.label
                assert cell(start.grid, where) == "", "an input cell ships empty"
                assert cell(start.grid, f"D{row}") == (inp.target or "(this sheet only)")

    def test_the_published_map_points_at_the_same_cells_as_the_sheet(self):
        start = sheetui.build_start()
        grid, places = expected_options()
        published = published_inputs(grid, places)
        for action in sheetui.FORMS:
            for inp in action.inputs:
                target = inp.target or "(this sheet only)"
                assert published[action.key][target][0] \
                    == start.actions[action.key]["inputs"][inp.slug()], \
                    f"{action.key}/{inp.label}: the script would read a different cell"

    def test_the_published_map_marks_required_what_the_schema_requires(self):
        grid, places = expected_options()
        published = published_inputs(grid, places)
        required = sheetui.required_fields()
        for action in sheetui.FORMS:
            for inp in action.inputs:
                target = inp.target or "(this sheet only)"
                want = "yes" if (required.get(target, False) or inp.required) else "no"
                assert published[action.key][target][1] == want, target
        assert published["add_sample"]["items.name"][1] == "yes"
        assert published["add_sample"]["items.notes"][1] == "no"

    def test_the_dropdowns_offer_exactly_the_vocabulary_that_is_legal(self):
        start = sheetui.build_start()
        requests = sheetui.layout_requests({"Start": 1}, start, {})
        offered = {}
        for request in requests:
            put = request.get("setDataValidation")
            if not put or "rule" not in put:
                continue  # the sheet-wide withdrawal, which offers nothing
            rule = put["rule"]
            offered[put["range"]["startRowIndex"] + 1] = (
                [v["userEnteredValue"] for v in rule["condition"]["values"]], rule["strict"])
        enums = sheetui.enum_fields()
        for action in sheetui.FORMS:
            for inp in action.inputs:
                row = int(start.actions[action.key]["inputs"][inp.slug()][1:])
                if inp.target == "items.class":
                    # A tube of material is made by the sample block, so the supply
                    # form must not offer a class it cannot handle.
                    assert offered[row][0] == list(schema.SUPPLY_CLASSES)
                elif inp.target in enums:
                    assert offered[row][0] == list(enums[inp.target])
                else:
                    assert row not in offered, \
                        f"{inp.label} got a dropdown with no vocabulary behind it"
        assert all(strict for _values, strict in offered.values()), \
            "a form refuses a typo at the keyboard; the data tabs are the permissive ones"

    def test_start_validation_is_cleared_before_it_is_stated(self):
        """Whatever a cell carried before has to go, or it survives underneath our own rule.

        A strict dropdown left on Start by an old build or made by hand is not merely
        wrong-looking: Sheets refuses to *write* a value it dislikes, so one covering a
        form's inputs also vetoes the Result line inside its range, and the button dies
        while delivering the message about something else.
        """
        start = sheetui.build_start()
        requests = sheetui.layout_requests({"Start": 1}, start, {})
        order = [(i, "rule" in r["setDataValidation"])
                 for i, r in enumerate(requests) if "setDataValidation" in r]
        cleared = [i for i, stated in order if not stated]
        assert len(cleared) == 1, "Start's validation is withdrawn exactly once"
        whole = requests[cleared[0]]["setDataValidation"]["range"]
        assert (whole["startRowIndex"], whole["endRowIndex"]) == (0, start.last_row)
        assert (whole["startColumnIndex"], whole["endColumnIndex"]) == (
            0, len(sheetui.START_WIDTHS)), "the clear has to cover the whole form"
        assert all(stated for i, stated in order if i > cleared[0]), \
            "the clear has to come first, or it deletes the rules it was meant to replace"
        assert any(i > cleared[0] for i, _ in order), "a clear with nothing after it is a wipe"

    def test_a_dropdown_nobody_published_does_not_survive_the_next_publish(self, built, drive):
        start = sheetui.build_start()
        sheet = drive.here()["sheet"]
        stray = {"sheetId": sheet.ids["Start"], "startRowIndex": 7, "endRowIndex": 18,
                 "startColumnIndex": 1, "endColumnIndex": 2}
        sheet.validations["Start"].append((stray, {
            "condition": {"type": "ONE_OF_LIST",
                          "values": [{"userEnteredValue": "other"}]},
            "strict": True, "showCustomUi": True}))

        sheetui.publish(built)

        enums = sheetui.enum_fields()
        want = set()
        for action in sheetui.FORMS:
            for inp in action.inputs:
                if inp.target == "items.class" or inp.target in enums:
                    row = int(start.actions[action.key]["inputs"][inp.slug()][1:])
                    want.add(row)
        left = [r for r, _ in sheet.validations["Start"]]
        assert sorted(r["startRowIndex"] + 1 for r in left) == sorted(want), \
            "only the cells we hold a vocabulary for may carry a dropdown"
        assert all(r["endRowIndex"] == r["startRowIndex"] + 1 for r in left), \
            "a form rule covers its own input cell and nothing else -- never a block"

    def test_the_identity_cell_is_asked_for_and_kept_visible(self):
        start = sheetui.build_start()
        assert cell(start.grid, f"A{start.actor_row}") == "Who are you?"
        assert cell(start.grid, f"B{start.actor_row}") == ""
        assert cell(start.grid, f"C{start.actor_row}") == sheetui.ACTOR_HINT
        freeze = [r for r in sheetui.layout_requests({"Start": 7}, start, {})
                  if "updateSheetProperties" in r][0]
        properties = freeze["updateSheetProperties"]["properties"]["gridProperties"]
        assert properties["frozenRowCount"] == start.actor_row, \
            "freezing above the identity cell is how it ends up unfilled"
        assert properties["hideGridlines"] is True

    def test_no_two_inputs_in_one_action_share_a_cell(self):
        start = sheetui.build_start()
        for action in sheetui.FORMS:
            cells = list(start.actions[action.key]["inputs"].values())
            assert len(cells) == len(set(cells))


class TestPublishing:
    def test_init_builds_the_interface_along_with_the_tables(self, built, drive):
        tabs = drive.tabs()
        for name in ("Start", "database_errors", "options"):
            assert any(any(str(c).strip() for c in row) for row in tabs[name]), \
                f"{name} came out empty"
        assert "options" in drive.here()["sheet"].hidden, "reference data is not for scrolling"

    def test_the_published_reference_tab_is_the_generated_one(self, built, drive):
        grid, places = expected_options()
        published = drive.tabs()["options"]
        assert len(published) >= len(grid)
        for line in range(1, len(grid) + 1):
            want = grid[line - 1]
            got = published[line - 1][:len(want)]
            if want[:1] == ["stamp"]:
                continue  # row 1 is titles; the timestamp differs by construction
            if got != want:
                pytest.fail(f"options row {line} differs:\n  generated {want}\n  published {got}")

    def test_the_digest_round_trips_and_then_notices_drift(self, built, monkeypatch):
        assert sheetui.read_digest(built.store) == sheetui.layout_stamp()
        assert not sheetui.is_stale(built.store)
        monkeypatch.setattr(sheetui, "LAYOUT_VERSION", sheetui.LAYOUT_VERSION + 1)
        assert sheetui.is_stale(built.store), "a layout change went unnoticed"

    def test_rerunning_leaves_the_sheet_and_the_formatting_alone(self, built, drive):
        before = [list(r) for r in drive.tabs()["Start"]]
        validations = {k: len(v) for k, v in drive.here()["sheet"].validations.items()}
        formats = {k: len(v) for k, v in drive.here()["sheet"].formats.items()}
        sheetui.publish(built)
        assert drive.tabs()["Start"] == before
        # Re-applying a rule over the same range replaces it. If these counts double,
        # every cell now carries two dropdowns and only one of them is the editable one.
        assert {k: len(v) for k, v in drive.here()["sheet"].validations.items()} == validations
        assert {k: len(v) for k, v in drive.here()["sheet"].formats.items()} == formats

    def test_reset_loses_what_writing_over_the_tab_cannot_reach(self, built, drive):
        """The reason `--reset` deletes rather than overwrites.

        A plain publish replaces values and the formats this module sends. Anything else
        a person left on the tab -- a bolded column out at E, a conditional format, a
        merged header -- is still there afterwards, which is precisely the state that
        makes a form behave oddly for reasons nobody can point at.
        """
        sheet = drive.here()["sheet"]
        hand_made = {"sheetId": sheet.ids["Start"], "startRowIndex": 0, "endRowIndex": 99,
                     "startColumnIndex": 4, "endColumnIndex": 6}
        sheet.formats["Start"].append(
            (hand_made, {"userEnteredFormat": {"textFormat": {"bold": True}}}))

        sheetui.publish(built)
        assert any(pair[0] == hand_made for pair in sheet.formats["Start"]), \
            "an ordinary publish is expected to leave it alone"

        again = sheetui.publish(built, reset=True)
        assert not any(pair[0] == hand_made for pair in sheet.formats.get("Start", []))
        assert again["discarded"]["Start"] > 0, "it has to say what it threw away"
        assert list(drive.tabs())[0] == "Start", "the rebuilt tab keeps its place at the front"

    def test_the_layout_goes_over_in_one_request(self, built, drive):
        drive.forget_calls()
        result = sheetui.publish(built)
        assert drive.counts()["spreadsheets.batchUpdate"] == 1
        assert result["layout_requests"] > 1

    def test_the_error_report_says_when_it_was_written(self, built, drive):
        rows = drive.tabs()["database_errors"]
        assert rows[0][0] == "Database errors"
        assert rows[1][0].startswith("Checked at ")
        assert "error(s)" in rows[2][0]
        assert rows[5][:5] == ["severity", "table", "row", "column", "code"]

    def test_a_broken_row_shows_up_and_then_disappears_without_leaving_debris(
            self, built, drive):
        spec = schema.get_table("items")
        data = built.store.read_table("items")
        ghost = {name: "" for name in spec.column_names}
        ghost.update({"item_id": "smp_kxympzqr", "class": "sample", "name": "phantom",
                      "status": "present", "location_id": "ctr_zzzzzzzz"})
        built.store.write_table("items", data.header, list(data.rows) + [ghost], None)
        built.reload()
        sheetui.publish(built)
        rows = drive.tabs()["database_errors"]
        listed = [r for r in rows if r and r[4] == "fk_dangling"]
        assert listed and listed[0][1] == "items", "the report missed a dangling reference"

        built.store.write_table("items", data.header, list(data.rows), None)
        built.reload()
        sheetui.publish(built)
        fresh = drive.tabs()["database_errors"]
        assert not [r for r in fresh if len(r) > 4 and r[4] == "fk_dangling"], \
            "the old finding is still on the tab after the fix"
        assert "Nothing is broken" in fresh[3][0]
        assert not [r for r in fresh[6:] if any(str(c).strip() for c in r)], \
            "a shorter report left its leftovers below the header"

    def test_a_backend_without_tabs_is_refused_clearly(self, tmp_path):
        from inventory_mcp.service import ServiceError

        local = Config(backend="local", local_root=str(tmp_path / "db"), actor="tester")
        svc, _ = init_database(local)
        with pytest.raises(ServiceError) as exc:
            sheetui.publish(svc)
        assert "no tabs to lay out" in str(exc.value)
