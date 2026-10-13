"""End-to-end behaviour through the service, against real files."""

from __future__ import annotations

import pytest

from conftest import read_rows, table_path
from inventory_mcp.service import Conflict, NotFound, ServiceError, ValidationError


class TestCreation:
    def test_sample_writes_supertype_and_extension_together(self, svc):
        freezer = svc.create_container("freezer", "F1")
        b = svc.create_box("Test Box", 10, 10, parent_id=freezer.records[0]["container_id"])
        result = svc.create_sample(
            "Sample A", "dna",
            location_id=b.records[0]["container_id"], position="B2",
            gml_id="GML009999", handler="tester",
        )
        iid = result.records[0]["item_id"]
        items = read_rows(table_path(svc.local_root(), "items"))
        samples = read_rows(table_path(svc.local_root(), "samples"))
        assert any(r["item_id"] == iid for r in items)
        assert any(r["item_id"] == iid and r["sample_type"] == "dna" for r in samples)

    def test_created_item_carries_server_written_audit_fields(self, svc):
        result = svc.create_sample("Audit me", "dna")
        item = result.records[0]
        assert item["updated_by"] == "tester"
        assert item["updated_at"]

    def test_ids_are_opaque_and_class_prefixed(self, svc):
        s = svc.create_sample("s", "dna").records[0]["item_id"]
        r = svc.create_supply("reagent", "r").records[0]["item_id"]
        k = svc.create_supply("kit", "k").records[0]["item_id"]
        c = svc.create_supply("consumable", "c").records[0]["item_id"]
        assert [x.split("_")[0] for x in (s, r, k, c)] == ["smp", "rgt", "kit", "csm"]

    def test_null_optional_fields_do_not_crash(self, svc):
        result = svc.create_sample("Nulls", "dna", patient_id=None, project_id=None,
                                   gml_id=None, notes=None)
        assert result.records[0]["sample"]["gml_id"] == ""

    def test_unknown_sample_type_is_rejected(self, svc):
        with pytest.raises(ValidationError) as e:
            svc.create_sample("bad type", "plasma_smoothie")
        assert "bad_enum" in {v.code for v in e.value.violations}

    def test_box_requires_a_parent(self, svc):
        with pytest.raises(Exception):
            svc.create_box("homeless", 9, 9, parent_id="")


class TestMovementAndStatus:
    def test_move_between_boxes_drops_the_stale_well(self, seeded):
        boxes = [c["container_id"] for c in seeded.list_containers(kind="box")]
        item = next(i["item_id"] for i in seeded._matching_items(item_class="sample")
                    if i["location"]["position"])
        source = seeded.db.items_by_id()[item]["location_id"]
        target = next(b for b in boxes if b != source)
        seeded.move_item(item, location_id=target)
        stored = seeded.db.items_by_id()[item]
        assert stored["location_id"] == target
        # an unasserted position is not carried over into a differently-shaped box
        assert stored["position"] == ""

    def test_explicit_position_survives_the_move(self, seeded):
        boxes = [c for c in seeded.list_containers(kind="box") if c["geometry"] == "10x10"]
        item = next(i["item_id"] for i in seeded._matching_items(item_class="sample")
                    if not i["location"]["position"])
        seeded.move_item(item, location_id=boxes[0]["container_id"], position="H8")
        assert seeded.db.items_by_id()[item]["position"] == "H8"

    def test_status_cannot_go_backwards_without_consent(self, seeded):
        item = next(i["item_id"] for i in seeded.search_items(status="partially_consumed"))
        with pytest.raises(Conflict):
            seeded.set_status(item, "present")
        seeded.set_status(item, "present", allow_backwards=True)
        assert seeded.db.items_by_id()[item]["status"] == "present"

    def test_clear_location_models_putting_it_down(self, seeded):
        item = next(i["item_id"] for i in seeded._matching_items(item_class="sample")
                    if i["location"]["container_id"])
        seeded.move_item(item, clear_location=True)
        stored = seeded.db.items_by_id()[item]
        assert stored["location_id"] == "" and stored["status"] == "present"
        # still counted, and reported as unlocated rather than gone
        assert len(seeded.search_items(unlocated=True)) >= 1


class TestMeasurements:
    def test_unit_dimension_is_enforced(self, seeded):
        item = next(i["item_id"] for i in seeded.search_items(item_class="sample"))
        with pytest.raises(Exception) as e:
            seeded.record_measurement(item, "qubit_dsdna", value="40", unit="ul")
        assert "not a recognised concentration_mass unit" in str(e.value)

    def test_values_are_normalised_to_canonical_for_export_only(self, seeded):
        """A reading row reports the number as observed. Conversion is arithmetic,
        so it lives where the maths happens: the exports and the SQLite mirror."""
        item = next(i["item_id"] for i in seeded.search_items(item_class="sample"))
        seeded.record_measurement(item, "qubit_dsdna", value="0.05", unit="mg/ml")
        lean = seeded.get_measurements(item_id=item, assay="qubit_dsdna")[0]
        assert (lean["value"], lean["unit"]) == (pytest.approx(0.05), "mg/ml")
        assert "canonical" not in lean

        full = seeded._measurement_rows(item_id=item, assay="qubit_dsdna")[0]
        assert full["canonical"]["value"] == pytest.approx(50.0)
        assert full["canonical"]["unit"] == "ng/ul"

    def test_correction_appends_and_supersedes(self, seeded):
        item = next(i["item_id"] for i in seeded.search_items(item_class="sample"))
        first = seeded.record_measurement(item, "qubit_dsdna", value="100", unit="ng/ul")
        mid = first.records[0]["measurement_id"]
        seeded.correct_measurement(mid, value="72", unit="ng/ul", notes="dilution error")

        visible = seeded.get_measurements(item_id=item, assay="qubit_dsdna")
        assert all(m["measurement_id"] != mid for m in visible)
        history = seeded.get_measurements(item_id=item, assay="qubit_dsdna",
                                          include_superseded=True)
        original = next(m for m in history if m["measurement_id"] == mid)
        assert original["superseded"] is True
        assert original["value"] == 100.0  # untouched on disk

    def test_double_correction_is_refused(self, seeded):
        item = next(i["item_id"] for i in seeded.search_items(item_class="sample"))
        mid = seeded.record_measurement(item, "qubit_dsdna", value="10", unit="ng/ul").records[0]["measurement_id"]
        seeded.correct_measurement(mid, value="11", unit="ng/ul")
        with pytest.raises(Conflict):
            seeded.correct_measurement(mid, value="12", unit="ng/ul")

    def test_textual_assay_takes_text_not_number(self, seeded):
        item = next(i["item_id"] for i in seeded.search_items(item_class="sample"))
        with pytest.raises(Exception):
            seeded.record_measurement(item, "gel_qc", value="3", unit="")
        result = seeded.record_measurement(item, "gel_qc", text="faint smear at 20kb")
        assert result.records[0]["value"] == "faint smear at 20kb"

    def test_yield_is_computed_not_stored(self, seeded):
        """Concentration x volume is derived on read; nothing caches it."""
        wide = seeded.export(formats=("csv",))
        rows = read_rows(seeded.local_root() / "exports" / "items_wide.csv")
        ja_co = next(r for r in rows if r["gml_id"] == "GML001994")
        assert float(ja_co["concentration_ng_ul"]) == pytest.approx(47.2)
        assert float(ja_co["computed_yield_ng"]) == pytest.approx(47.2 * 20)
        assert "yield_ng" not in read_rows(table_path(seeded.local_root(), "measurements"))[0]

    def test_reagent_quantity_is_a_time_series(self, seeded):
        enzyme = next(i["item_id"] for i in seeded.search_items(item_class="reagent"))
        history = seeded.get_measurements(item_id=enzyme, assay="reagent_volume",
                                          include_historical=True)
        assert [h["value"] for h in history] == [500.0, 420.0]
        latest = seeded.get_measurements(item_id=enzyme, assay="reagent_volume")
        assert latest[0]["value"] == 420.0

    def test_the_series_is_opt_in(self, seeded):
        """One row per item and assay by default: the answer to "how much is left"
        should not have to carry every reading that led here."""
        enzyme = next(i["item_id"] for i in seeded.search_items(item_class="reagent"))
        current = seeded.get_measurements(item_id=enzyme, assay="reagent_volume")
        assert len(current) == 1

    def test_superseded_rows_never_win_the_latest_slot(self, seeded):
        """A correction that moves the date backwards must not hand the newest
        reading back to the invalidated row it replaced."""
        item = next(i["item_id"] for i in seeded.search_items(item_class="sample"))
        mid = seeded.record_measurement(
            item, "qubit_dsdna", value="100", unit="ng/ul").records[0]["measurement_id"]
        seeded.correct_measurement(mid, value="72", unit="ng/ul", measured_on="2019-01-01T09:00")

        current = seeded.get_measurements(item_id=item, assay="qubit_dsdna")
        assert [r["value"] for r in current] == [72.0]
        audit = seeded.get_measurements(item_id=item, assay="qubit_dsdna",
                                        include_superseded=True)
        assert sorted(r["value"] for r in audit) == [72.0, 100.0]

    def test_unknown_assay_code_is_refused(self, seeded):
        """Silence here would read as "no readings", not "you typed the code wrong"."""
        item = next(i["item_id"] for i in seeded.search_items(item_class="sample"))
        with pytest.raises(ServiceError) as e:
            seeded.get_measurements(item_id=item, assay="qubit_dsdna_typo")
        assert "qubit_dsdna" in str(e.value)  # names the codes that do exist

    def test_an_unknown_item_is_reported_not_ignored(self, seeded):
        with pytest.raises(NotFound):
            seeded.get_measurements(item_id="smp_00000000")

    def test_reading_rows_always_carry_the_same_keys(self, seeded):
        """One shape for every row of this tool: no per-row conditionals, and no
        column a caller can derive from the assay code or already named."""
        rows = seeded.get_measurements(include_superseded=True, include_historical=True)
        expected = {"measurement_id", "assay", "assay_name", "measured_on", "value",
                    "unit", "handler", "instrument", "supersedes", "superseded", "notes"}
        assert rows and all(set(r) == expected for r in rows)

    def test_a_textual_reading_arrives_as_value_too(self, seeded):
        """The key set stays constant; only the type of ``value`` follows the assay."""
        item = next(i["item_id"] for i in seeded.search_items(item_class="sample"))
        seeded.record_measurement(item, "gel_qc", text="faint smear at 20kb")
        row = seeded.get_measurements(item_id=item, assay="gel_qc")[0]
        assert row["value"] == "faint smear at 20kb"
        assert row["unit"] == ""


class TestGetLinkedItems:
    def test_chain_reads_both_directions(self, seeded):
        dna = next(i["item_id"] for i in seeded.search_items(text="JaCo genomic DNA"))
        graph = seeded.get_linked_items(dna, direction="both", depth=2)
        assert graph["parents"][0]["name"] == "JaCo PBMC"
        assert graph["children"][0]["name"] == "JaCo DNA aliquot 1"

    def test_pooling_needs_no_schema_change(self, seeded):
        """Many parents into one child -- the junction already covers it."""
        inputs = [i["item_id"] for i in seeded.search_items(item_class="sample")][:3]
        pool = seeded.create_sample("Pooled library", "library").records[0]["item_id"]
        for src in inputs:
            seeded.link_items(pool, src, "pooled_into")
        graph = seeded.get_linked_items(pool, direction="parents")
        assert len(graph["parents"]) == 3

    def test_self_link_refused(self, seeded):
        item = next(i["item_id"] for i in seeded.search_items(item_class="sample"))
        with pytest.raises(ValidationError):
            seeded.link_items(item, item, "aliquot_of")

    def test_one_hop_is_the_default(self, seeded):
        """Depth is not a knob on the tool: going further means asking again about
        an id, so a trace only grows where someone chose to follow it."""
        dna = next(i["item_id"] for i in seeded.search_items(text="JaCo genomic DNA"))
        parent = seeded.get_linked_items(dna, direction="parents")["parents"][0]
        assert parent["name"] == "JaCo PBMC"
        assert "parents" not in parent

    def test_tombstoned_ends_are_marked_not_hidden(self, seeded):
        """Provenance outlives the tube, but a discarded aliquot must not read as
        material that still exists -- delete leaves status alone, so status lies."""
        keep = next(i["item_id"] for i in seeded.search_items(text="JaCo DNA aliquot 1"))
        node = seeded.get_linked_items(keep, direction="parents")["parents"][0]
        parent, status_before = node["item_id"], node["status"]

        seeded.delete_item(parent, force=True)
        node = seeded.get_linked_items(keep, direction="parents")["parents"][0]
        assert node["deleted"] is True
        assert node["status"] == status_before  # the old claim survives; deleted corrects it
        assert seeded.get_linked_items(parent, direction="children")["children"][0]["deleted"] is False

    def test_edges_are_named_by_their_endpoints_not_a_surrogate(self, seeded):
        """``link_id`` stays in the table. A caller identifies an edge by its two
        item ids, so shipping the surrogate would advertise an address nothing takes."""
        dna = next(i["item_id"] for i in seeded.search_items(text="JaCo genomic DNA"))
        node = seeded.get_linked_items(dna, direction="parents")["parents"][0]
        assert set(node) == {"item_id", "role", "name", "class", "status", "deleted"}

    def test_a_second_edge_between_the_same_pair_is_refused(self, seeded):
        """A second row between one pair is a duplicate even when its role differs:
        the endpoints name the edge, not the label someone put on it."""
        child = next(i["item_id"] for i in seeded.search_items(text="JaCo DNA aliquot 1"))
        parent = seeded.get_linked_items(child, direction="parents")["parents"][0]["item_id"]
        with pytest.raises(Conflict) as e:
            seeded.link_items(child, parent, "derived_from")
        assert "aliquot_of" in str(e.value)  # names what is already recorded


class TestDestructive:
    def test_delete_requires_consumed_or_force(self, seeded):
        item = next(i["item_id"] for i in seeded.search_items(status="present"))
        with pytest.raises(Conflict):
            seeded.delete_item(item)
        seeded.delete_item(item, force=True)
        stored = seeded.db.items_by_id()[item]
        assert stored["deleted_at"] and stored["location_id"] == ""
        # tombstoned, not erased
        assert item in seeded.db.items_by_id()
        assert item not in {i["item_id"] for i in seeded.search_items()}
        assert item in {i["item_id"] for i in seeded.search_items(include_deleted=True)}

    def test_non_empty_container_cannot_be_deleted(self, seeded):
        occupied = next(c["container_id"] for c in seeded.list_containers(kind="box")
                        if c["occupied"])
        with pytest.raises(Conflict):
            seeded.delete_container(occupied)

    def test_merge_repoints_measurements_and_derivation_links(self, seeded):
        keep = seeded.create_sample("Keep me", "dna").records[0]["item_id"]
        lose = seeded.create_sample("Duplicate", "dna").records[0]["item_id"]
        seeded.record_measurement(lose, "qubit_dsdna", value="33", unit="ng/ul")
        child = seeded.create_sample("Derived", "dna").records[0]["item_id"]
        seeded.link_items(child, lose, "extracted_from")

        seeded.merge_items(keep, lose, reason="double-registered at the bench")

        assert seeded.get_measurements(item_id=keep, assay="qubit_dsdna")[0]["value"] == 33.0
        assert seeded.get_linked_items(child, direction="parents")["parents"][0]["item_id"] == keep
        assert seeded.db.items_by_id()[lose]["deleted_at"]

    def test_merge_across_classes_refused(self, seeded):
        s = next(i["item_id"] for i in seeded.search_items(item_class="sample"))
        r = next(i["item_id"] for i in seeded.search_items(item_class="reagent"))
        with pytest.raises(Conflict):
            seeded.merge_items(s, r)


class TestSnapshotAndCas:
    def test_every_write_snapshots_before_touching_storage(self, seeded):
        before = set((seeded.local_root() / "backups").iterdir())
        seeded.create_sample("Snapshotted", "dna")
        after = set((seeded.local_root() / "backups").iterdir())
        new = after - before
        assert len(new) == 1
        generation = new.pop()
        assert (generation / "items.csv").exists()
        assert (generation / "samples.csv").exists()

    def test_rejected_write_leaves_no_trace(self, seeded):
        before = set((seeded.local_root() / "backups").iterdir())
        items_before = (table_path(seeded.local_root(), "items")).read_bytes()
        with pytest.raises(ValidationError):
            seeded.create_sample("bad", "not_a_type")
        assert set((seeded.local_root() / "backups").iterdir()) == before
        assert (table_path(seeded.local_root(), "items")).read_bytes() == items_before

    def test_external_edit_aborts_the_next_write(self, seeded):
        """The spreadsheet-save-underneath-us case.

        CAS covers the tables a write touches -- create_sample writes items and
        samples, so those are the files whose revision is checked. An edit to a
        table this write never touches is reported by doctor(), not blocked here.
        """
        from conftest import write_rows
        from inventory_mcp import schema

        path = table_path(seeded.local_root(), "items")
        rows = read_rows(path)
        for r in rows:
            r["notes"] = ((r["notes"] + " ") if r["notes"] else "") + "typed directly into Excel"
        write_rows(path, rows, list(schema.ITEMS.column_names))

        with pytest.raises(Conflict) as e:
            seeded.create_sample("Racing a human", "dna")
        message = str(e.value)
        assert "changed underneath us" in message
        assert "spreadsheet" in message
        # the abort preserved the hand edit rather than clobbering it
        assert "typed directly into Excel" in read_rows(path)[0]["notes"]

    def test_backup_pruning_keeps_the_recent_generations(self, config, svc):
        config.keep_backups = 3
        for n in range(6):
            svc.create_sample(f"prune {n}", "dna")
        generations = list((svc.local_root() / "backups").iterdir())
        assert len(generations) <= 3


class TestQueryGuard:
    def test_aggregate_question_works(self, seeded):
        out = seeded.run_query(
            "select status, count(*) n from items where class='sample' group by status"
        )
        assert {r["status"]: r["n"] for r in out["rows"]}["present"] >= 1

    def test_mutating_statement_refused(self, seeded):
        from inventory_mcp.query import QueryError

        with pytest.raises(QueryError):
            seeded.run_query("delete from items")
        with pytest.raises(QueryError):
            seeded.run_query("select 1; drop table items")
        with pytest.raises(QueryError):
            seeded.run_query("pragma table_info(items)")

    def test_unknown_table_gets_a_hint(self, seeded):
        from inventory_mcp.query import QueryError

        with pytest.raises(QueryError) as e:
            seeded.run_query("select * from specimens")
        assert "Available tables/views" in str(e.value)

    def test_view_joins_are_available(self, seeded):
        out = seeded.run_query(
            "select name, container_label, position from items_wide where class='sample'"
        )
        assert any(r["container_label"] for r in out["rows"])


class TestReset:
    """`init --reset --seed` recovery path: truncate, then seed from clean."""

    def test_reset_empties_tables_and_allows_reseed(self, config):
        from inventory_mcp import schema
        from inventory_mcp.initdb import init_database
        from inventory_mcp.seed import reset, seed

        service, _ = init_database(config)
        seed(service)
        removed = reset(service)
        assert removed["samples"] == 10
        assert sum(len(service.db.t(t.name).rows) for t in schema.TABLES) == 0
        # the duplicate guard that motivated reset() no longer trips
        counts = seed(service)
        assert counts["samples"] == 10

    def test_reset_keeps_columns_someone_added_by_hand(self, seeded):
        from inventory_mcp.seed import reset

        t = seeded.db.t("assays")
        header = t.header + ("lab_note",)
        rows = [dict(r, lab_note="kept") for r in t.rows]
        seeded.store.write_table("assays", header, rows, t.revision or None)
        seeded.reload()
        reset(seeded)
        first_line = table_path(seeded.local_root(), "assays").read_text().splitlines()[0]
        assert "lab_note" in first_line.split(",")
        assert read_rows(table_path(seeded.local_root(), "assays")) == []


class TestCommitAtomicity:
    """Multi-table commits that abort mid-way must leave nothing half-landed."""

    def test_aborted_commit_rolls_back_the_table_already_written(self, svc):
        from inventory_mcp.service import Conflict

        _ = svc.db.t("samples").revision  # eager load, like a session at work
        rev = svc.store.current_revision("samples")
        # External change to the *second* table create_sample writes: items
        # lands first, samples then fails CAS -- without rollback the items
        # row survives as an orphan (live Drive finding).
        svc.store.write_table("samples", ("sample_id",), [], rev)
        with pytest.raises(Conflict, match="Reverted: items"):
            svc.create_sample("Half-written ghost", "dna")
        fresh = read_rows(table_path(svc.local_root(), "items"))
        assert all(r["name"] != "Half-written ghost" for r in fresh)


class TestSearchRows:
    """A search hit answers "is this the one I mean?" -- nothing more."""

    def test_rows_carry_only_identity_and_location(self, seeded):
        rows = seeded.search_items(item_class="sample")
        assert rows
        for r in rows:
            assert set(r) == {
                "item_id", "name", "status", "updated_at", "location", "sample",
            } | ({"deleted"} if r.get("deleted") else set())
            assert set(r["location"]) == {"label", "kind", "path"}
            assert set(r["sample"]) == {
                "gml_id", "sample_type", "patient_label", "project_name",
            }

    def test_class_is_readable_from_the_extension_block(self, seeded):
        hit = seeded.search_items(item_class="reagent")[0]
        assert "class" not in hit and "reagent" in hit

    def test_rollups_still_see_the_fields_search_hides(self, seeded):
        buckets = seeded.rollup_by_project()
        named = [b for b in buckets if b["key"] != "(none)"]
        assert named and all(b["total"] > 0 for b in named)

    def test_tombstones_are_marked_only_when_included(self, seeded):
        victim = seeded.search_items(item_class="sample")[0]["item_id"]
        seeded.delete_item(victim, force=True)
        assert all("deleted" not in r for r in seeded.search_items())
        flagged = next(
            r for r in seeded.search_items(include_deleted=True)
            if r["item_id"] == victim
        )
        assert flagged["deleted"] is True

    def test_what_search_hides_get_item_shows(self, seeded):
        hit = seeded.search_items(item_class="sample")[0]
        full = seeded.get_item(hit["item_id"])
        assert {"class", "handler", "notes", "updated_by"} <= set(full)
        assert {"container_id", "position"} <= set(full["location"])
        assert {"source_material", "patient_id", "project_id",
                "collected_on", "received_on", "processed_on"} <= set(full["sample"])


class TestGetItem:
    """One call deep: latest readings inline and brief, everything else separate."""

    def test_latest_readings_are_inlined_briefly(self, seeded):
        iid = next(i["item_id"] for i in seeded.search_items(text="aliquot"))
        out = seeded.get_item(iid)
        assert out["latest_measurements"]
        for m in out["latest_measurements"]:
            assert set(m) == {"assay", "measured_on", "value", "unit"}
        qubit = next(m for m in out["latest_measurements"] if m["assay"] == "qubit_dsdna")
        assert qubit["value"] == 47.2 and qubit["unit"] == "ng/ul"

    def test_derivation_links_stay_behind_their_own_tool(self, seeded):
        iid = next(i["item_id"] for i in seeded.search_items(text="aliquot"))
        assert "parents" not in seeded.get_item(iid)
        assert seeded.get_linked_items(iid, direction="parents", depth=1)["parents"]

    def test_textual_assays_surface_their_value_as_text(self, seeded):
        # canonical_unit is required even for textual assays, so a placeholder
        # is unavoidable today -- see STATUS open thread.
        seeded.create_assay("smear_score", "Gel smear quality", "other", "score",
                            result_type="text")
        iid = next(i["item_id"] for i in seeded.search_items(item_class="sample"))
        seeded.record_measurement(iid, assay="smear_score", text="slight smearing")
        reading = next(m for m in seeded.get_item(iid)["latest_measurements"]
                       if m["assay"] == "smear_score")
        assert reading["value"] == "slight smearing" and reading["unit"] == ""


class TestContents:
    """get_contents: lean rows for what is physically inside a container."""

    @staticmethod
    def _box(seeded, label):
        return next(c["container_id"] for c in seeded.list_containers(kind="box")
                    if c["label"] == label)

    def test_direct_contents_are_lean_and_counted(self, seeded):
        box = self._box(seeded, "-70C Misc")
        out = seeded.contents(box)
        assert [i["position"] for i in out["items"]] == ["A1", "C4"]
        assert out["count"] == 2
        assert out["container"]["capacity"] == 81
        assert out["container"]["free"] == 79
        assert all("in" not in i for i in out["items"])  # no padding when not needed
        assert set(out["items"][0]) == {"item_id", "class", "name", "status", "position"}

    def test_recursive_reaches_nested_boxes_and_names_their_container(self, seeded):
        freezer = next(c["container_id"] for c in seeded.list_containers(kind="freezer")
                       if c["label"] == "-80 Freezer #7085")
        assert seeded.contents(freezer)["count"] == 0
        out = seeded.contents(freezer, recursive=True)
        names = {i["name"] for i in out["items"]}
        assert "JaCo DNA aliquot 1" in names  # lives in a box under a rack
        nested = [i for i in out["items"] if i["in"]]
        assert nested and all("Rack 3" in i["in"] or "Shelf 3" in i["in"]
                              for i in nested)

    def test_consumed_items_vanish_from_their_old_box(self, seeded):
        box = self._box(seeded, "-70C Misc")
        victim = seeded.contents(box)["items"][0]["item_id"]
        seeded.set_status(victim, "consumed")
        assert victim not in {i["item_id"] for i in seeded.contents(box)["items"]}

    def test_tombstoned_items_are_excluded(self, seeded):
        box = self._box(seeded, "VEXAS DNA Box 1")
        before = seeded.contents(box)["count"]
        victim = seeded.contents(box)["items"][0]["item_id"]
        seeded.delete_item(victim, force=True)
        assert seeded.contents(box)["count"] == before - 1

    def test_unknown_container_is_not_found(self, seeded):
        with pytest.raises(NotFound):
            seeded.contents("ctr_nosuchid")


class TestGetOccupancy:
    """get_occupancy: one container, reported at whichever level was asked for."""

    @staticmethod
    def _find(seeded, label, kind=None):
        return next(c["container_id"] for c in seeded.list_containers(kind=kind or "")
                    if c["label"] == label)

    def test_the_container_is_required_and_must_exist(self, seeded):
        with pytest.raises(ServiceError, match="required"):
            seeded.get_occupancy("")
        with pytest.raises(NotFound):
            seeded.get_occupancy("ctr_nosuchid")

    def test_kind_that_names_nothing_is_refused_with_alternatives(self, seeded):
        freezer = self._find(seeded, "-80 Freezer #7085", "freezer")
        with pytest.raises(ServiceError) as e:
            seeded.get_occupancy(freezer, kind="drawer")
        assert "container kind" in str(e.value) and "item class" in str(e.value)

    def test_without_kind_the_answer_is_the_containers_directly_inside(self, seeded):
        freezer = self._find(seeded, "-80 Freezer #7085", "freezer")
        out = seeded.get_occupancy(freezer)
        assert out["kind"] == "children"
        assert [r["label"] for r in out["rows"]] == ["Shelf 3"]
        # the tier measured is the freezer itself: four shelf positions, one used
        assert out["summary"]["holds"] == "containers"
        assert (out["summary"]["capacity"], out["summary"]["occupied"]) == (4, 1)
        assert out["summary"]["free"] == 3

    def test_kind_reaches_any_depth_and_reports_at_that_level(self, seeded):
        """The boxes are two levels down; kind asks for them wherever they sit."""
        freezer = self._find(seeded, "-80 Freezer #7085", "freezer")
        out = seeded.get_occupancy(freezer, kind="box")
        assert {r["label"] for r in out["rows"]} == {"-70C Misc", "VEXAS DNA Box 1"}
        assert len(out["rows"]) == 2
        # Each row states its own wells; the summary speaks in box slots instead.
        assert [(r["label"], r["capacity"], r["occupied"]) for r in out["rows"]] == [
            ("-70C Misc", 81, 2), ("VEXAS DNA Box 1", 100, 3),
        ]
        assert out["summary"]["holds"] == "containers"
        assert out["summary"]["capacity"] == 6      # Rack 3, where both boxes live
        assert out["summary"]["occupied"] == 2
        assert out["summary"]["free"] == 4

    def test_room_for_another_box_is_answered_in_boxes(self, seeded):
        """'I want to put a new box in here, can I?' -- counted against the rack's
        own positions, which is what a box consumes."""
        rack = self._find(seeded, "Rack 3", "rack")
        seeded.create_box("Fresh 96", 8, 12, parent_id=rack)
        out = seeded.get_occupancy(rack, kind="box")
        assert out["summary"]["occupied"] == 3
        assert out["summary"]["free"] == 3

    def test_a_nested_container_occupies_a_slot(self, seeded):
        """A rack holding six boxes is full, not empty with boxes somewhere below."""
        rack = self._find(seeded, "Rack 3", "rack")
        out = seeded.get_occupancy(rack, kind="rack")
        row = out["rows"][0]
        assert row["occupied"] == 2          # two boxes, no tubes filed in the rack
        assert row["capacity"] == 6          # box positions, not wells
        assert out["summary"]["capacity"] == 6 and out["summary"]["free"] == 4

    def test_numbers_are_never_added_across_tiers(self, seeded):
        """The freezer's four shelf positions and its boxes' 181 wells are different
        units; summing them would print a number that means nothing."""
        freezer = self._find(seeded, "-80 Freezer #7085", "freezer")
        assert seeded.get_occupancy(freezer, kind="sample")["summary"]["capacity"] == 181

    def test_a_container_asked_about_in_its_own_kind_reports_itself(self, seeded):
        box = self._find(seeded, "-70C Misc", "box")
        out = seeded.get_occupancy(box, kind="box")
        assert [r["container_id"] for r in out["rows"]] == [box]
        # ... and answers for itself, not for the rack it happens to stand on
        assert out["summary"]["capacity"] == 81
        assert out["summary"]["occupied"] == 2
        assert out["summary"]["holds"] == "items"

    def test_an_item_class_lists_the_items_and_totals_their_containers(self, seeded):
        freezer = self._find(seeded, "-80 Freezer #7085", "freezer")
        out = seeded.get_occupancy(freezer, kind="sample")
        assert len(out["rows"]) == 5
        assert out["summary"]["capacity"] == 181
        assert out["summary"]["holds"] == "items"
        assert all(i["in"].endswith(("Misc", "Box 1")) for i in out["rows"])
        assert set(out["rows"][0]) == {
            "item_id", "class", "name", "status", "position", "in",
        }

    def test_summary_describes_the_container_not_the_filtered_class(self, seeded):
        """Two samples and a reagent in one box: the list is filtered, the totals
        are the box's, so free stays true rather than counting only samples."""
        box = self._find(seeded, "-70C Misc", "box")
        seeded.create_supply("reagent", "Pipette tips", location_id=box)
        out = seeded.get_occupancy(box, kind="sample")
        assert len(out["rows"]) == 2             # the samples listed
        assert out["summary"]["occupied"] == 3   # everything sharing the box

    def test_occupancy_counts_slots_regardless_of_recorded_wells(self, seeded):
        """Positions are convenience, not accounting: a tube nobody placed fills a slot
        exactly like one filed under A1, and no figure distinguishes them."""
        box = self._find(seeded, "-70C Misc", "box")
        before = seeded.get_occupancy(box, kind="box")["rows"][0]
        seeded.create_sample("Unpositioned tube", "dna", location_id=box)
        after = seeded.get_occupancy(box, kind="box")["rows"][0]
        assert after["occupied"] == before["occupied"] + 1
        assert after["free"] == before["free"] - 1
        assert not [k for k in after if "well" in k or "position" in k]

    def test_container_rows_carry_one_shape(self, seeded):
        freezer = self._find(seeded, "-80 Freezer #7085", "freezer")
        for row in seeded.get_occupancy(freezer, kind="box")["rows"]:
            assert set(row) == {
                "container_id", "label", "kind", "capacity", "occupied", "free",
            }

    def test_space_that_cannot_be_measured_is_null_not_zero(self, seeded):
        bench = self._find(seeded, "Bench 100666 (temporary)", "bench")
        out = seeded.get_occupancy(bench)
        assert set(out["target"]) == {"container_id", "label", "kind"}
        assert out["rows"] == []                      # nothing nested inside
        assert out["summary"]["holds"] == ""          # so nothing was measured at all
        assert out["summary"]["occupied"] == 0
        assert out["summary"]["capacity"] is None
        assert out["summary"]["free"] is None

    def test_tombstoned_items_stop_filling_the_box(self, seeded):
        box = self._find(seeded, "-70C Misc", "box")
        before = seeded.get_occupancy(box, kind="box")["rows"][0]["occupied"]
        victim = seeded.contents(box)["items"][0]["item_id"]
        seeded.delete_item(victim, force=True)
        assert seeded.get_occupancy(box, kind="box")["rows"][0]["occupied"] == before - 1


class TestSearchPatients:
    def test_a_query_is_required(self, seeded):
        for blank in ("", "   "):
            with pytest.raises(ServiceError, match="query is required"):
                seeded.search_patients(query=blank)

    def test_labels_match_case_insensitively(self, seeded):
        rows = seeded.search_patients(query="jaco")["rows"]
        assert [r["label"] for r in rows] == ["JaCo"]

    def test_ids_match_too(self, seeded):
        everyone = seeded.search_patients(query="pat_")["rows"]
        pid = next(r["key"] for r in everyone if r["total"])
        rows = seeded.search_patients(query=pid[4:8])["rows"]
        assert [r["key"] for r in rows] == [pid]

    def test_heaviest_participants_come_first(self, seeded):
        totals = [r["total"] for r in seeded.search_patients(query="pat_")["rows"]]
        assert totals == sorted(totals, reverse=True)

    def test_an_enrolled_participant_with_nothing_still_appears(self, seeded):
        """Otherwise 'nobody by that name' and 'nobody has given us a tube yet' arrive
        as the same empty answer, and only one of them is a fact about the database."""
        seeded.create_patient("Zz Nobody")
        rows = seeded.search_patients(query="zz nobody")["rows"]
        assert [r["label"] for r in rows] == ["Zz Nobody"]
        assert rows[0]["total"] == 0

    def test_only_samples_are_counted(self, seeded):
        out = seeded.search_patients(query="pat_", limit=100)
        counted = sum(r["total"] for r in out["rows"]) + out["unassigned"]
        assert counted == seeded.count_items(item_class="sample")
        assert seeded.count_items() > counted, "fixture should hold supplies too"

    def test_samples_without_a_participant_beside_the_rows_not_among_them(self, seeded):
        """A caveat about the cohort is not a search result: it stays visible at any
        limit and never displaces a person."""
        out = seeded.search_patients(query="pat_", limit=1)
        assert out["unassigned"] == len(
            [i for i in seeded._matching_items(item_class="sample")
             if not (i.get("sample") or {}).get("patient_id", "")]
        )
        assert all(r["key"].startswith("pat_") for r in out["rows"])

    def test_limit_caps_people_and_says_so(self, seeded):
        out = seeded.search_patients(query="pat_", limit=1)
        assert len(out["rows"]) == 1
        assert out["matched"] > 1 and out["truncated"] is True

    def test_project_narrows_the_counts_not_the_roster(self, seeded):
        project = list(seeded.db.t("projects").live())[0]["project_id"]
        all_of_it = seeded.search_patients(query="pat_", limit=100)
        within = seeded.search_patients(query="pat_", project_id=project, limit=100)
        assert sum(r["total"] for r in within["rows"]) <= sum(
            r["total"] for r in all_of_it["rows"])
        assert {r["key"] for r in within["rows"]} >= {
            r["key"] for r in all_of_it["rows"] if r["total"]
        }
