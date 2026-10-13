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
            assert wide == len(seeded.search_items(item_class="sample"))
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


class TestToolSurface:
    def test_tools_are_registered_with_descriptions_and_schemas(self):
        from inventory_mcp.tools import mcp

        tools = {t.name: t for t in mcp._tool_manager.list_tools()}
        expected = {
            "doctor", "search_items", "get_item", "list_containers", "container_tree",
            "get_contents", "get_occupancy", "count_items", "search_patients",
            "rollup_by_project", "get_measurements",
            "get_linked_items",
            "run_query",
            "create_container", "create_box", "create_sample", "create_supply",
            "create_patient", "create_project", "assign_patient_project", "create_assay",
            "move_item", "set_status", "update_fields",
            "record_measurement", "correct_measurement", "link_items",
            "delete_item", "delete_container", "merge_items", "export_data",
        }
        missing = expected - set(tools)
        assert not missing, f"unregistered tools: {sorted(missing)}"
        for name, tool in tools.items():
            assert tool.description, f"{name} has no description for the model to read"
            assert tool.parameters["type"] == "object"

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

    def test_measurements_refuse_to_answer_without_an_item(self, seeded):
        from inventory_mcp.tools import get_measurements, set_service

        set_service(seeded)
        assert get_measurements("").startswith("ERROR")
        assert "NotFound" in get_measurements("smp_00000000")

    def test_write_refusals_come_back_as_prose_not_tracebacks(self, seeded):
        from inventory_mcp.tools import create_sample, set_service

        set_service(seeded)
        out = create_sample("bad type", "plasma_smoothie")
        assert out.startswith("WRITE REFUSED")
        assert "sample_type" in out and "is not one of" in out
        # and the refusal really did not write
        assert "bad type" not in {i["name"] for i in seeded.search_items()}

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
