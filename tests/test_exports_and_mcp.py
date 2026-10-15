"""Derived artifacts and the MCP surface itself.

The export contract is what a dashboard author actually depends on, so it is
tested as an interface: row counts must agree with the source tables, the
manifest must describe what was actually written, and a stale generation must
refuse to overwrite a fresh one.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess  # noqa: F401  (kept for the skip message on odd environments)
import sys
from pathlib import Path

import pytest

from conftest import read_rows, table_path
from inventory_mcp import schema
from inventory_mcp.export import EXPORT_VIEWS
from inventory_mcp.service import NotFound, ValidationError


@pytest.fixture
def exported(seeded):
    return seeded.export(formats=("csv", "sqlite"))


class TestExport:
    def test_every_view_lands_on_disk_with_a_manifest(self, seeded, exported):
        out = Path(exported["directory"])
        for name in EXPORT_VIEWS:
            assert (out / f"{name}.csv").exists(), name
        assert (out / "inventory.sqlite").exists()
        manifest = json.loads((out / "manifest.json").read_text())
        assert manifest["schema_version"]
        assert manifest["generated_at"]
        assert set(manifest["files"]) == set(EXPORT_VIEWS) | {"inventory.sqlite"}
        assert manifest["integrity"]["clean"] is True

    def test_manifest_records_the_source_revisions(self, seeded, exported):
        revisions = exported["manifest"]["source_revisions"]
        # the revision of each table is the hash of exactly what we read, so a
        # consumer can tell whether the source moved after the export
        for name in ("items", "samples", "measurements", "containers"):
            assert revisions[name] == seeded.db.t(name).revision, name

    def test_sqlite_mirror_agrees_with_the_csv_row_counts(self, seeded, exported):
        out = Path(exported["directory"])
        conn = sqlite3.connect(out / "inventory.sqlite")
        try:
            for spec in schema.TABLES:
                csv_rows = len(list(seeded.db.t(spec.name).live()))
                (mirror,) = conn.execute(f'SELECT COUNT(*) FROM "{spec.name}"').fetchone()
                assert mirror == csv_rows, spec.name
            # denormalised view available to BI tools that only speak SQL
            (wide,) = conn.execute(
                "SELECT COUNT(*) FROM items_wide WHERE class='sample'"
            ).fetchone()
            assert wide == len(seeded.search_items(item_class="sample")["rows"])
            named = conn.execute(
                "SELECT patient_label, project_name FROM patient_projects_named LIMIT 1"
            ).fetchall()
            assert named
            # the mirror keeps the real item_links table, so the labelled edges are
            # a SQL view rather than the denormalised copy the CSV carries
            edges = conn.execute(
                "SELECT child_name, parent_name FROM item_links_named LIMIT 1"
            ).fetchall()
            assert edges and all(edges[0]), "both ends should be resolved to names"
        finally:
            conn.close()

    def test_items_wide_is_denormalised_but_stores_no_derived_value(self, seeded, exported):
        rows = read_rows(Path(exported["directory"]) / "items_wide.csv")
        aliquot = next(r for r in rows if r["gml_id"] == "GML001994")
        assert aliquot["patient_label"] == "JaCo"
        assert aliquot["container_path"], "path should be resolved, not left as an id"
        assert float(aliquot["computed_yield_ng"]) == pytest.approx(47.2 * 20)
        # ... and nothing like it exists in the authoritative files
        assert "yield" not in "".join(read_rows(table_path(seeded.local_root(), "items"))[0])

    def test_measurements_long_keeps_superseded_history(self, seeded, exported):
        rows = read_rows(Path(exported["directory"]) / "measurements_long.csv")
        nanodrop = [r for r in rows if r["assay"] == "nanodrop_a260"]
        assert len(nanodrop) == 2, "the corrected reading supersedes but does not replace"
        assert {r["superseded"] for r in nanodrop} == {"yes", "no"}

    def test_reexport_refreshes_revisions_and_timestamp(self, seeded, exported):
        first = exported["manifest"]
        seeded.create_sample("Written after the export", "dna")
        second = seeded.export(formats=("csv", "sqlite"))["manifest"]
        assert second["source_revisions"]["items"] != first["source_revisions"]["items"]
        assert second["generated_at"] >= first["generated_at"]
        assert second["integrity"]["clean"] is True

    def test_refuses_to_overwrite_a_newer_generation(self, seeded, exported):
        manifest_path = Path(exported["directory"]) / "manifest.json"
        future = dict(json.loads(manifest_path.read_text()))
        future["generated_at"] = "2099-01-01T00:00:00Z"
        manifest_path.write_text(json.dumps(future))
        with pytest.raises(RuntimeError, match="refusing to overwrite"):
            seeded.export(formats=("csv",))

    def test_integrity_block_reports_open_errors(self, seeded, exported):
        """An export is still produced when the database is sick -- it just says so."""
        from conftest import write_rows

        path = table_path(seeded.local_root(), "items")
        rows = read_rows(path)
        rows[0]["location_id"] = "ctr_zzzzzzzz"
        write_rows(path, rows, list(schema.ITEMS.column_names))
        seeded.reload()
        result = seeded.export(formats=("csv",))
        assert result["manifest"]["integrity"]["open_errors"] >= 1
        assert result["manifest"]["integrity"]["clean"] is False


def strand_a_location(seeded):
    """Hand-damage the files behind the service's back, as a browser edit would."""
    from conftest import read_rows, table_path, write_rows

    path = table_path(seeded.local_root(), "items")
    rows = read_rows(path)
    rows[0]["location_id"] = "ctr_zzzzzzzz"
    write_rows(path, rows, list(schema.ITEMS.column_names))
    seeded.reload()


class TestToolSurface:
    def test_tools_are_registered_with_descriptions_and_schemas(self):
        from inventory_mcp.tools import mcp

        tools = {t.name: t for t in mcp._tool_manager.list_tools()}
        expected = {
            "doctor", "search_items", "get_item", "search_containers",
            "get_contents", "get_occupancy", "count_items", "search_patients",
            "get_patient", "list_assays", "search_projects", "get_measurements",
            "get_linked_items",
            "create_container", "create_box", "create_sample", "create_supply",
            "create_patient", "create_project", "assign_patient_project", "create_assay",
            "move", "set_status", "edit_name", "edit_notes", "edit_data",
            "record_measurement", "link_items",
            "delete_item", "delete_container", "delete_patient", "delete_project",
            "delete_assay", "merge_items", "unassign_patient_project",
        }
        missing = expected - set(tools)
        assert not missing, f"unregistered tools: {sorted(missing)}"
        assert set(tools) == expected, f"unexpected tools: {sorted(set(tools) - expected)}"
        for name, tool in tools.items():
            assert tool.description, f"{name} has no description for the model to read"
            assert tool.parameters["type"] == "object"

    def test_no_tool_executes_caller_supplied_code(self):
        """SQL left the surface on purpose: read-only or not, a query string is
        arbitrary code over the whole database, with no per-table boundary and no
        bound on what one answer costs. ``Service.run_query`` stays for the CLI and
        for code someone owns; nothing reachable from a model may reach it."""
        from inventory_mcp.tools import mcp

        names = {t.name for t in mcp._tool_manager.list_tools()}
        assert "run_query" not in names
        import inventory_mcp.tools as tools_module

        assert not hasattr(tools_module, "run_query")

    def test_the_surface_appends_readings_and_never_corrects_one(self):
        """A wrong reading is answered by recording a newer one, not by retracting the
        old one. ``Service.correct_measurement`` stays -- the seed builds its superseded
        Nanodrop pair with it, and a dashboard author importing the service may still
        want it -- but a model holding both tools would have to decide which of two
        readings counts as current, so only one path is offered."""
        from inventory_mcp.tools import mcp

        names = {t.name for t in mcp._tool_manager.list_tools()}
        assert "correct_measurement" not in names
        assert "record_measurement" in names
        import inventory_mcp.tools as tools_module

        assert not hasattr(tools_module, "correct_measurement")

    def test_publishing_data_is_not_something_a_model_does(self):
        """Exports serve dashboards, which want bytes rather than a conversation: the same
        request giving the same answer, no context budget per poll, and no lab credentials
        held by whatever is drawing the chart. ``inventory-mcp export`` and
        ``Service.export`` both remain -- TestExport drives them throughout this file --
        so this is about which door you use, not whether the room exists."""
        from inventory_mcp.tools import mcp

        names = {t.name for t in mcp._tool_manager.list_tools()}
        assert "export_data" not in names
        import inventory_mcp.tools as tools_module

        assert not hasattr(tools_module, "export_data")

    def test_schemas_show_real_parameters_not_the_wrapper(self):
        """The error wrapper must stay transparent to signature introspection."""
        from inventory_mcp.tools import mcp

        tools = {t.name: t for t in mcp._tool_manager.list_tools()}
        schema_ = tools["create_sample"].parameters
        props = schema_["properties"]
        assert {"name", "sample_type", "patient_id"} <= set(props)
        assert "args" not in props and "kwargs" not in props
        assert schema_["required"] == ["name", "sample_type"]
        assert "required" not in props["patient_id"], "optional fields must look optional"
        # a tool with no parameters must accept an empty argument object
        assert tools["doctor"].parameters["properties"] == {}

    def test_advertised_schemas_carry_no_derived_titles(self):
        """pydantic's ``title`` twins restate the field name below them and cost
        context on every listing, so the server strips them at the wire boundary.
        Registration keeps them -- assertions here are on what list_tools returns.
        """
        import asyncio

        from inventory_mcp.tools import mcp

        def count_titles(node):
            if isinstance(node, dict):
                return ("title" in node) + sum(count_titles(v) for v in node.values())
            if isinstance(node, list):
                return sum(count_titles(v) for v in node)
            return 0

        advertised = asyncio.run(mcp.list_tools())
        assert len(advertised) == len(mcp._tool_manager.list_tools())
        for tool in advertised:
            assert count_titles(tool.input_schema) == 0, f"{tool.name} advertises titles"

    def test_descriptions_ship_without_their_source_indentation(self):
        """Docstrings are indented four spaces where they are written, and that
        padding used to travel with every line of every description."""
        import asyncio

        from inventory_mcp.tools import mcp

        for tool in asyncio.run(mcp.list_tools()):
            assert "\n    " not in tool.description, f"{tool.name} ships indentation"
            assert tool.description == tool.description.strip()

    def test_direction_offers_only_the_values_it_accepts(self):
        """A constrained parameter is a shorter conversation than a prose list a
        caller can ignore and then wonder why the answer came back empty."""
        import asyncio

        from inventory_mcp.tools import mcp

        tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
        direction = tools["get_linked_items"].input_schema["properties"]["direction"]
        assert direction["enum"] == ["parents", "children", "both"]
        assert direction["default"] == "both"

    def test_only_the_item_is_required_of_a_measurement_query(self):
        """Annotated defaults are easy to lose, and the one argument that must stay
        required is the subject: without an item this answers for every tube."""
        import asyncio

        from inventory_mcp.tools import mcp

        advertised = {t.name: t for t in asyncio.run(mcp.list_tools())}
        tool = advertised["get_measurements"]
        props = tool.input_schema["properties"]
        assert tool.input_schema["required"] == ["item_id"]
        assert props["assay"]["default"] == ""
        assert props["include_historical"]["default"] is False
        assert all(p.get("description") for p in props.values()), "every param should say what it wants"

    def test_move_advertises_position_as_optional(self):
        """A default on an Annotated parameter is the easiest thing in a rewritten
        signature to lose, and losing it turns an optional well into an argument the
        caller has no way to leave out."""
        import asyncio

        from inventory_mcp.tools import mcp

        tool = {t.name: t for t in asyncio.run(mcp.list_tools())}["move"]
        props = tool.input_schema["properties"]
        assert tool.input_schema["required"] == ["id", "target_id"]
        assert props["position"]["default"] is None
        assert all(p.get("description") for p in props.values())

    def test_recording_a_reading_says_which_field_it_wants(self):
        """Whether an assay takes a number or words is a property of the assay, not of
        the caller's preference, and ``unit`` is required only for assays that have one.
        Neither rule is guessable, so both are advertised per parameter. The two inputs
        keep their storage names; the reading comes back as ``value``, whichever way it
        went in."""
        import asyncio

        from inventory_mcp.tools import mcp

        tool = {t.name: t for t in asyncio.run(mcp.list_tools())}["record_measurement"]
        props = tool.input_schema["properties"]
        assert tool.input_schema["required"] == ["item_id", "assay"]
        assert all(p.get("description") for p in props.values()), "every param should say what it wants"
        assert "value" not in props, "the input is value_num/value_text; value is the read spelling"
        assert props["value_num"]["default"] == "" and props["measured_on"]["default"] == ""
        assert "qualitative" in props["value_text"]["description"]
        assert "numeric" in props["value_num"]["description"]
        assert "list_assays" in props["assay"]["description"]

    def test_recording_a_reading_answers_with_the_reading_only(self, seeded):
        """An append used to hand back the whole commit envelope -- operation, tables,
        backup path, empty warnings -- around a value the caller had just supplied. What
        comes back now is the reading: when it landed, since "now" is only one option, and
        the canonical figure, since the unit sent is not always the assay's own."""
        import json

        from inventory_mcp.tools import record_measurement, set_service

        set_service(seeded)
        tube = seeded.search_items(item_class="sample", status="present")["rows"][0]["item_id"]
        out = json.loads(record_measurement(
            item_id=tube, assay="qubit_dsdna", value_num="0.0515", unit="mg/ml"))
        assert set(out) == {"measurement_id", "assay", "assay_name", "measured_on",
                            "value", "unit", "canonical", "instrument", "notes",
                            "supersedes"}
        # 'supersedes' earns its place by naming what the recording retired on the
        # caller's behalf -- an automatic change with no other channel to the model.
        assert "supersedes" in out
        assert out["measurement_id"].startswith("msm_")
        assert out["assay"] == "qubit_dsdna" and out["assay_name"]
        assert float(out["value"]) == pytest.approx(0.0515) and out["unit"] == "mg/ml"
        assert out["measured_on"]
        assert out["canonical"]["value"] == pytest.approx(51.5)
        assert out["canonical"]["unit"] == "ng/ul"
        assert len(json.dumps(out, separators=(",", ":"))) < 300

    def test_marking_an_item_says_only_that_it_is_marked(self, seeded):
        """The caller named the id and the status, and got the name from the search that
        found the tube. What came back was the whole item record -- class row, audit
        fields, freezer path, readings -- wrapped in the commit envelope, around a value
        the caller supplied itself."""
        import json

        from inventory_mcp.tools import set_service, set_status

        set_service(seeded)
        tube = next(r["item_id"] for r in seeded.db.t("items").rows
                    if r.get("status") == "present" and r.get("location_id", "").strip()
                    and not r.get("deleted_at", "").strip())
        home = seeded.db.items_by_id()[tube]["location_id"]
        out = json.loads(set_status(item_id=tube, status="consumed"))
        assert set(out) == {"item_id", "name", "new_status"}
        assert out["item_id"] == tube and out["new_status"] == "consumed"
        assert seeded.db.items_by_id()[tube]["location_id"] == ""
        assert len(json.dumps(out, separators=(",", ":"))) < 120

        # Coming back needs somewhere to be, and the refusal has to say so in terms a
        # model can act on rather than a stack trace about a missing field.
        refused = set_status(item_id=tube, status="present")
        assert "location_id" in refused and "holds no slot" in refused
        back = json.loads(set_status(item_id=tube, status="present", location_id=home))
        assert back["new_status"] == "present"
        assert seeded.db.items_by_id()[tube]["location_id"] == home

    def test_marking_lists_the_statuses_it_accepts_and_says_when_location_is_needed(self):
        """Three statuses exist, so the schema carries them rather than letting a model
        invent a fourth and spend a round trip finding out. ``location_id`` is optional
        except when the item holds no slot, which the parameter has to say because the
        schema cannot express it."""
        import asyncio

        from inventory_mcp.tools import mcp

        tool = {t.name: t for t in asyncio.run(mcp.list_tools())}["set_status"]
        props = tool.input_schema["properties"]
        assert set(props) == {"item_id", "status", "location_id"}
        assert tool.input_schema["required"] == ["item_id", "status"]
        assert all(p.get("description") for p in props.values())
        assert sorted(props["status"]["enum"]) == ["consumed", "partially_consumed", "present"]
        assert props["location_id"]["default"] == ""
        assert "search_containers" in props["location_id"]["description"]

    def test_editing_prose_is_three_tools_that_need_no_table(self):
        """``update_fields`` offered thirteen fields against twelve tables and left the
        caller to already know that a tube's notes sit on ``items`` while its collection
        date sits on ``samples``. Each replacement implies its own table, and the id says
        which row, so neither a table nor a kind is a parameter anyone can get wrong."""
        import asyncio

        from inventory_mcp.tools import mcp

        tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
        assert "update_fields" not in tools
        assert tools["edit_name"].input_schema["required"] == ["id", "name"]
        assert tools["edit_notes"].input_schema["required"] == ["id", "notes"]
        assert tools["edit_data"].input_schema["required"] == ["id", "field", "value"]
        for name in ("edit_name", "edit_notes", "edit_data"):
            props = tools[name].input_schema["properties"]
            assert all(p.get("description") for p in props.values()), f"{name} has a bare param"
            assert not ({"table", "kind"} & set(props))

    def test_edit_data_carries_the_fields_and_says_which_class_has_which(self):
        """The one place the table matrix still has to live: nine names, and one sentence
        saying who holds them, instead of thirteen undescribed parameters."""
        import asyncio

        from inventory_mcp.tools import mcp

        props = ({t.name: t for t in asyncio.run(mcp.list_tools())}
                 ["edit_data"].input_schema["properties"])
        assert sorted(props["field"]["enum"]) == sorted(
            ["source_material", "collected_on", "received_on", "processed_on", "gml_id",
             "storage_requirements", "expires_on", "price", "handler"])
        said = props["field"]["description"].lower()
        for who in ("samples", "reagents", "kits", "consumables", "handler"):
            assert who in said, f"the description never says who holds {who}"

    def test_the_edit_tools_answer_with_the_edit_and_nothing_else(self, seeded):
        """Not the row, not the commit envelope: what was written, and what the entry
        turned out to be -- which is the one thing the caller did not already know."""
        import json

        from inventory_mcp.tools import edit_data, edit_name, edit_notes, set_service

        set_service(seeded)
        kit = seeded.search_items(item_class="kit")["rows"][0]["item_id"]
        project = seeded.db.t("projects").rows[0]["project_id"]

        renamed = json.loads(edit_name(id=kit, name="Kit A, re-housed"))
        assert renamed == {"id": kit, "kind": "kit", "name": "Kit A, re-housed"}
        noted = json.loads(edit_notes(id=project, notes="paused over winter"))
        assert noted == {"id": project, "kind": "project", "notes": "paused over winter"}
        edited = json.loads(edit_data(id=kit, field="price", value="12.50"))
        assert (edited["id"], edited["kind"], edited["field"]) == (kit, "kit", "price")
        assert float(edited["value"]) == 12.5
        for body in (renamed, noted, edited):
            assert len(json.dumps(body, separators=(",", ":"))) < 140

    def test_registration_carries_the_project_a_participant_needs(self):
        """A participant registered outside a project holds nothing filable, so the
        requirement belongs on the wire rather than in a sentence. And the tool that
        adds project two has to point back at the one that made project one."""
        import asyncio

        from inventory_mcp.tools import mcp

        tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
        assert "project_id" in tools["create_patient"].input_schema["required"]
        assign = tools["assign_patient_project"].input_schema
        assert all(p.get("description") for p in assign["properties"].values())
        assert "create_patient" in tools["assign_patient_project"].description

    def test_withdrawal_offers_a_destination_without_requiring_one(self):
        """``move_to`` is the difference between a withdrawal and a dead end: with it
        the tubes are re-pointed, without it they lose their project. Losing the
        default would make every withdrawal demand an answer the caller may not have."""
        import asyncio

        from inventory_mcp.tools import mcp

        tool = {t.name: t for t in asyncio.run(mcp.list_tools())}["unassign_patient_project"]
        props = tool.input_schema["properties"]
        assert tool.input_schema["required"] == ["patient_id", "project_id"]
        assert props["move_to"]["default"] == ""
        assert all(p.get("description") for p in props.values())

    def test_move_answers_with_the_move_and_nothing_else(self, seeded):
        """The wrapper is where argument marshalling breaks, so the compact readback is
        asserted through the tool surface rather than only at the service layer."""
        import json

        from inventory_mcp.tools import get_contents, move, search_containers, set_service

        set_service(seeded)
        box = lambda label: json.loads(search_containers(query=label))["rows"][0]["container_id"]
        tube = json.loads(get_contents(container_id=box("-70C Misc")))["items"][0]["item_id"]
        out = json.loads(move(id=tube, target_id=box("VEXAS DNA Box 1")))
        assert set(out) == {"id", "kind", "from", "to"}
        assert out["id"] == tube and out["kind"] == "sample"
        assert out["to"]["label"] == "VEXAS DNA Box 1"
        assert len(json.dumps(out, separators=(",", ":"))) < 300

    def test_measurements_refuse_to_answer_without_an_item(self, seeded):
        from inventory_mcp.tools import get_measurements, set_service

        set_service(seeded)
        assert get_measurements("").startswith("ERROR")
        assert "NotFound" in get_measurements("smp_00000000")

    def test_a_refusal_names_the_entries_the_caller_broke(self, seeded):
        """Bad arguments get specific feedback. The caller can act on this and nothing
        else, so it is never withheld -- and nothing belonging to another row appears."""
        from inventory_mcp.tools import create_sample, set_service

        set_service(seeded)
        out = create_sample("bad type", "plasma_smoothie")
        assert out.startswith("WRITE REFUSED")
        assert "sample_type" in out and "is not one of" in out
        assert "locked" not in out, "nothing here is pre-existing"
        assert "bad type" not in {i["name"] for i in seeded.search_items()["rows"]}

    def test_a_locked_database_says_so_and_blames_nobody(self, seeded):
        """Damage somebody else left must not arrive dressed as the caller's mistake, and
        must not name rows: a list of unrelated entries reads as permission to tidy them,
        which is how a deliberate hand edit gets quietly reverted."""
        from inventory_mcp.tools import create_sample, set_service

        set_service(seeded)
        strand_a_location(seeded)
        out = create_sample("Perfectly Fine Sample", "dna")
        assert out.startswith("WRITE REFUSED")
        assert "Nothing in this call is at fault" in out and "doctor" in out
        assert "deliberate" in out, "the message has to warn against unilateral repair"
        for leak in ("zzzzzzzz", "[smp_", "[items[", "item_id"):
            assert leak not in out, f"the refusal leaked {leak}"
        assert "Perfectly Fine Sample" not in {i["name"] for i in seeded.search_items()["rows"]}

    def test_a_mixed_refusal_separates_your_damage_from_theirs(self, seeded):
        """Both at once: the caller's own error is named in full while the standing damage
        is counted and sent to doctor, unnamed."""
        from inventory_mcp.tools import create_sample, set_service

        set_service(seeded)
        strand_a_location(seeded)
        out = create_sample("bad type", "plasma_smoothie")
        assert "sample_type" in out and "is not one of" in out
        assert "predate it" in out and "doctor" in out
        assert "zzzzzzzz" not in out, "the standing damage is counted, not quoted"

    def test_the_filters_that_took_over_sqls_job_are_described(self):
        """The bounded filters are what a model has instead of SQL, so each one has
        to say what it matches. The two defaults that read oddly are pinned here
        because losing a default on an ``Annotated`` parameter is the easiest thing
        in a rewritten signature to do -- and it did happen once."""
        import asyncio

        from inventory_mcp.tools import mcp

        tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
        shared = ("no_project", "no_patient", "received_since",
                  "received_before", "expires_before", "measured_after")
        for name in ("search_items", "count_items"):
            props = tools[name].input_schema["properties"]
            assert all(p.get("description") for p in props.values()), f"{name} has a bare param"
            assert set(shared) <= set(props), f"{name} lost a filter"
        items = tools["search_items"].input_schema
        assert items.get("required", []) == []
        assert items["properties"]["limit"]["default"] == 0, "0 means every match"
        assert items["properties"]["sort"]["default"] == "", "empty means by name"

    def test_a_bad_filter_is_refused_rather_than_answering_nothing(self, seeded):
        """``received_since='last may'`` would otherwise return an empty list, which
        reads as an empty freezer rather than as the typo it is."""
        from inventory_mcp.tools import search_items, set_service

        set_service(seeded)
        assert "not a date" in search_items(received_since="last may")
        assert "not a sortable field" in search_items(sort="volume")

    def test_every_capped_search_offers_an_offset(self):
        """A limit with no offset is a ceiling on what a caller can ever see. With one,
        the cap still protects the context window without hiding the tail."""
        import asyncio

        from inventory_mcp.tools import mcp

        tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
        for name in ("search_items", "search_containers", "search_patients",
                     "search_projects"):
            props = tools[name].input_schema["properties"]
            assert props["offset"]["default"] == 0, name
            words = props["offset"]["description"]
            assert "truncated" in words and "matched" in words, f"{name} pages blind"

    def test_paging_through_the_tool_reaches_the_whole_result(self, seeded):
        from inventory_mcp.tools import search_items, set_service

        set_service(seeded)
        whole = json.loads(search_items(item_class="sample"))
        first = json.loads(search_items(item_class="sample", limit=2))
        second = json.loads(search_items(item_class="sample", limit=2, offset=2))
        assert whole["matched"] > 2, "the fixture needs enough rows to page"
        assert first["matched"] == second["matched"] == whole["matched"]
        assert [i["item_id"] for i in first["rows"]] == [
            i["item_id"] for i in whole["rows"][:2]]
        assert not ({i["item_id"] for i in first["rows"]}
                    & {i["item_id"] for i in second["rows"]}), "pages must not overlap"
        assert first["truncated"] is True

    def test_missing_ids_are_reported_readably(self, seeded):
        from inventory_mcp.tools import get_item, set_service

        set_service(seeded)
        assert "NotFound" in get_item("smp_00000000")

    def test_guard_translates_every_service_exception(self):
        from inventory_mcp.tools import _guard

        def boom():
            raise ValidationError([])

        assert _guard(boom)().startswith("WRITE REFUSED")

        def gone():
            raise NotFound("nope")

        assert _guard(gone)() == "NotFound: nope"

    def test_every_advertised_parameter_says_what_it_wants(self):
        """A bare name and a type is a guess the caller has to make about somebody else's
        database, and seven of the create tools asked for fifty-eight of them. The
        description is the only place that can say where an id comes from, what a blank
        means, and which format the field takes."""
        import asyncio

        from inventory_mcp.tools import mcp

        silent = []
        for tool in asyncio.run(mcp.list_tools()):
            for name, prop in (tool.input_schema.get("properties") or {}).items():
                if not prop.get("description"):
                    silent.append(f"{tool.name}.{name}")
        assert not silent, f"parameters shipped without a description: {silent}"

    def test_required_means_the_caller_really_has_to_supply_it(self):
        """``required`` is derived from the Python default, so a default lost in a rewrite
        silently turns an optional field into an argument the caller cannot leave out --
        which is invisible until someone tries to register a tube without saying where it
        is. Checked across the whole surface rather than per tool, because the mistake is
        never the one you just looked at."""
        import asyncio

        from inventory_mcp.tools import mcp

        for tool in asyncio.run(mcp.list_tools()):
            schema_ = tool.input_schema
            props = schema_.get("properties") or {}
            nodefault = sorted(k for k, v in props.items() if "default" not in v)
            assert sorted(schema_.get("required", [])) == nodefault, (
                f"{tool.name}: required={schema_.get('required')} but these carry no "
                f"default: {nodefault}")

    def test_closed_vocabularies_travel_as_enum_and_refuse_as_prose(self, seeded):
        """The values a field accepts live in validation, where a model cannot see them:
        two of the seven create tools burned a round trip guessing 'M' for sex and
        'concentration' for a quantity. So the set goes out in the schema, where a client
        can read it before sending -- but the check stays in the service, because pydantic
        rejects a ``Literal`` mismatch before the tool runs and the caller gets the shape
        of a crash instead of the row and rule at fault."""
        import asyncio

        from inventory_mcp import schema
        from inventory_mcp.tools import create_patient, create_sample, mcp, set_service

        set_service(seeded)
        props = {t.name: t for t in asyncio.run(mcp.list_tools())}
        wanted = {
            ("create_container", "kind"): schema.CONTAINER_KINDS,
            ("create_sample", "sample_type"): schema.SAMPLE_TYPES,
            ("create_supply", "item_class"): schema.SUPPLY_CLASSES,
            ("create_patient", "sex"): schema.SEXES,
            ("create_patient", "affected_status"): schema.AFFECTED_STATUSES,
            ("create_project", "status"): schema.PROJECT_STATUSES,
            ("create_assay", "quantity"): schema.QUANTITIES,
            ("create_assay", "result_type"): schema.RESULT_TYPES,
            ("link_items", "role"): schema.LINK_ROLES,
        }
        for (tool_name, field), vocabulary in wanted.items():
            advertised = props[tool_name].input_schema["properties"][field]
            assert advertised["enum"] == list(vocabulary), f"{tool_name}.{field} enum drifted"

        project = seeded.search_projects("prj_")["rows"][0]["key"]
        refused = create_patient(label="P-900", project_id=project, sex="M")
        assert refused.startswith("WRITE REFUSED"), refused
        assert "female" in refused and "Error executing tool" not in refused
        # and the vocabulary on the wire is the one the service actually enforces
        assert create_sample(name="A tube", sample_type="plasma_smoothie").startswith(
            "WRITE REFUSED")

    def test_the_grid_belongs_to_create_box_alone(self):
        """Two tools could build a box, and only the narrower one insisted on a parent.
        Now furniture takes a declared capacity and a gridded box owns its geometry,
        including the boxes whose rows skip I."""
        import asyncio

        from inventory_mcp.tools import mcp

        tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
        container = tools["create_container"].input_schema["properties"]
        assert not {"rows", "cols", "row_labels", "col_labels"} & set(container)
        box = tools["create_box"].input_schema["properties"]
        assert {"rows", "cols", "row_labels", "col_labels"} <= set(box)
        assert box["rows"]["type"] == box["cols"]["type"] == "integer"
        assert tools["create_box"].input_schema["required"] == ["label", "rows", "cols",
                                                                "parent_id"]

    def test_a_supply_can_be_filed_and_stamped_at_creation(self):
        """Three arguments the service accepted were missing from the tool, so a kit could
        not be put in a well, given a custodian, or dated -- each needing a second call."""
        import asyncio

        from inventory_mcp.tools import mcp

        props = {t.name: t for t in asyncio.run(mcp.list_tools())}[
            "create_supply"].input_schema["properties"]
        assert {"position", "handler", "quantity_measured_on"} <= set(props)
        # the opening amount is a reading, and readings want a minute, not a day
        assert "YYYY-MM-DDTHH:MM" in props["quantity_measured_on"]["description"]

    def test_creating_something_answers_with_the_receipt(self, seeded):
        """Every write used to return the commit envelope around the row it had just been
        handed: the operation it was called by (``create_box`` reported
        ``create_container``, because it delegates), the tables touched, a backup path on a
        server filesystem, and warnings that no tool call ever fills. What a caller needs is
        the id that was minted and anything the server worked out for itself."""
        import json

        from inventory_mcp import tools as t
        from inventory_mcp.tools import set_service

        set_service(seeded)
        envelope = {"operation", "touched_tables", "backup", "warnings", "records"}
        shelf = seeded.search_containers("Shelf")["rows"][0]["container_id"]
        projects = seeded.search_projects("prj_")["rows"]
        project = projects[0]["key"]
        # a leaf nobody else depends on, so the delete at the end of this flow lands
        sibling = next(p["key"] for p in projects if p["label"] == "GeneAdd registry")

        rack = json.loads(t.create_container(kind="rack", label="Rack 9", parent_id=shelf,
                                             capacity=3))
        assert set(rack) == {"container_id", "kind", "label", "path", "capacity"}
        assert rack["capacity"] == 3 and rack["container_id"].startswith("ctr_")

        box = json.loads(t.create_box(label="Box 12", rows=9, cols=9,
                                      parent_id=rack["container_id"],
                                      row_labels="A,B,C,D,E,F,G,H,J"))
        assert box["geometry"] == "9x9" and box["capacity"] == 81

        tube = json.loads(t.create_sample(name="DNA-900-01", sample_type="dna",
                                          location_id=box["container_id"], position="B2"))
        assert set(tube) == {"item_id", "class", "name", "status"}
        assert tube["status"] == "present"

        patient = json.loads(t.create_patient(label="P-901", project_id=project))
        assert set(patient) == {"patient_id", "label", "project_id"}

        assay = json.loads(t.create_assay(code="qubit_rna", name="Qubit RNA HS",
                                         quantity="concentration_mass",
                                         canonical_unit="ng/ul"))
        assert assay["assay_id"].startswith("asy_") and assay["result_type"] == "numeric"

        kept = json.loads(t.create_supply(item_class="reagent", name="Ethanol"))
        assert set(kept) == {"item_id", "class", "name", "status"}, "no reading, no receipt line"
        stocked = json.loads(t.create_supply(item_class="reagent", name="Glycerol",
                                            initial_quantity="500",
                                            quantity_assay="reagent_volume",
                                            quantity_unit="ml"))
        assert stocked["opening_reading"]["unit"] == "ml"
        assert stocked["opening_reading"]["canonical"] == {"value": 500000.0, "unit": "ul"}

        duplicate = json.loads(t.create_supply(item_class="reagent", name="Ethanol again"))
        merged = json.loads(t.merge_items(keep_id=kept["item_id"],
                                          discard_id=duplicate["item_id"],
                                          reason="Logged twice."))
        assert set(merged) == {"kept", "discarded", "references_repointed"}

        daughter = json.loads(t.create_sample(name="DNA-900-02", sample_type="dna",
                                              location_id=box["container_id"], position="B3"))
        edge = json.loads(t.link_items(child_item_id=daughter["item_id"],
                                       parent_item_id=tube["item_id"],
                                       role="extracted_from"))
        assert set(edge) == {"link_id", "child_item_id", "parent_item_id", "role"}

        enrolment = json.loads(t.assign_patient_project(patient_id=patient["patient_id"],
                                                        project_id=sibling))
        assert set(enrolment) == {"patient_id", "project_id", "link_id"}
        withdrawal = json.loads(t.unassign_patient_project(patient_id=patient["patient_id"],
                                                           project_id=project))
        assert withdrawal["removed"]["patient_id"] == patient["patient_id"]
        assert withdrawal["was_their_last_project"] is False

        tombstone = json.loads(t.delete_item(item_id=tube["item_id"]))
        assert set(tombstone) == {"item_id", "deleted_at"}, "an id and when it went away"
        assert tombstone["deleted_at"]
        json.loads(t.delete_item(item_id=daughter["item_id"]))
        for cid in (box["container_id"], rack["container_id"]):
            retired = json.loads(t.delete_container(container_id=cid))
            assert retired == {"container_id": cid}, "a delete says what it deleted, nothing more"

        swept = json.loads(t.delete_patient(patient_id=patient["patient_id"]))
        assert set(swept) == {"patient_id", "enrolments_removed", "samples_unlinked"}
        assert swept["enrolments_removed"] == 1

        dropped = json.loads(t.delete_project(project_id=sibling))
        assert set(dropped) == {"project_id", "enrolments_removed", "samples_repointed",
                                "samples_cleared", "moved_to"}

        unused = json.loads(t.delete_assay(assay_id=assay["assay_id"]))
        assert unused == {"assay_id": assay["assay_id"]}

        for payload in (rack, box, tube, patient, assay, kept, stocked, merged, edge,
                        enrolment, withdrawal, tombstone, retired, swept, dropped, unused):
            assert not envelope & set(payload)

    def test_a_reading_names_the_minute_it_was_taken(self, seeded):
        """The parameter description promised a bare date was fine. It is not: readings are
        ordered by minute, so a date alone is refused rather than answered with midnight,
        which would assert a minute nobody recorded. A zone spelled out is honoured, and the
        answer comes back in UTC with the marker on it."""
        import asyncio
        import json

        from inventory_mcp.tools import mcp, record_measurement, set_service

        set_service(seeded)
        props = {t.name: t for t in asyncio.run(mcp.list_tools())}[
            "record_measurement"].input_schema["properties"]
        assert "bare date is refused" in props["measured_on"]["description"]
        assert "UTC" in props["measured_on"]["description"]

        tube = seeded.search_items(item_class="sample", status="present")["rows"][0]["item_id"]
        assert "not a recognisable timestamp" in record_measurement(
            item_id=tube, assay="qubit_dsdna", value_num="10", unit="ng/ul",
            measured_on="2026-10-13")

        stored = json.loads(record_measurement(
            item_id=tube, assay="nanodrop_a260", value_num="10", unit="ng/ul",
            measured_on="2026-10-13T20:00+11:00"))
        assert stored["measured_on"] == "2026-10-13T09:00Z"


class TestStdioHandshake:
    """A real client session against a real server process over stdio."""

    def test_initialize_list_tools_and_call_one(self, seeded, tmp_path):
        mcp = pytest.importorskip("mcp")
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        cfg_path = tmp_path / "serve-config.json"
        seeded.config.save(cfg_path)
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "inventory_mcp.cli", "--config", str(cfg_path), "serve"],
            cwd=str(Path(sys.modules["inventory_mcp"].__file__).parent.parent),
        )

        async def session():
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as client:
                    info = await client.initialize()
                    tools = await client.list_tools()
                    result = await client.call_tool("doctor", {})
                    text = result.content[0].text
                    return info, [t.name for t in tools.tools], text

        info, names, doctor_text = asyncio.run(asyncio.wait_for(session(), timeout=60))
        assert info.server_info.name == "lab-inventory"
        assert "search_items" in names and "record_measurement" in names
        payload = json.loads(doctor_text)
        assert payload["ok"] is True
        assert payload["table_counts"]["samples"] == len(list(seeded.db.t("samples").live()))
