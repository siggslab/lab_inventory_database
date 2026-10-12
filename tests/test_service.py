"""End-to-end behaviour through the service, against real files."""

from __future__ import annotations

import pytest

from conftest import read_rows, table_path
from inventory_mcp.service import Conflict, NotFound, ValidationError


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
        item = next(i["item_id"] for i in seeded.search_items(item_class="sample")
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
        item = next(i["item_id"] for i in seeded.search_items(item_class="sample")
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
        item = next(i["item_id"] for i in seeded.search_items(item_class="sample")
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

    def test_values_are_normalised_to_canonical_on_read(self, seeded):
        item = next(i["item_id"] for i in seeded.search_items(item_class="sample"))
        seeded.record_measurement(item, "qubit_dsdna", value="0.05", unit="mg/ml")
        rows = seeded.get_measurements(item_id=item, assay_code="qubit_dsdna", latest_only=True)
        assert rows[0]["canonical"]["value"] == pytest.approx(50.0)
        assert rows[0]["canonical"]["unit"] == "ng/ul"

    def test_correction_appends_and_supersedes(self, seeded):
        item = next(i["item_id"] for i in seeded.search_items(item_class="sample"))
        first = seeded.record_measurement(item, "qubit_dsdna", value="100", unit="ng/ul")
        mid = first.records[0]["measurement_id"]
        seeded.correct_measurement(mid, value="72", unit="ng/ul", notes="dilution error")

        visible = seeded.get_measurements(item_id=item, assay_code="qubit_dsdna")
        assert all(m["measurement_id"] != mid for m in visible)
        history = seeded.get_measurements(item_id=item, assay_code="qubit_dsdna",
                                          include_superseded=True)
        original = next(m for m in history if m["measurement_id"] == mid)
        assert original["superseded"] is True
        assert original["value_num"] == 100.0  # untouched on disk

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
        assert result.records[0]["value_text"] == "faint smear at 20kb"

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
        history = seeded.get_measurements(item_id=enzyme, assay_code="reagent_volume")
        assert [h["value_num"] for h in history] == [500.0, 420.0]
        latest = seeded.get_measurements(item_id=enzyme, assay_code="reagent_volume",
                                         latest_only=True)
        assert latest[0]["value_num"] == 420.0


class TestLineage:
    def test_chain_reads_both_directions(self, seeded):
        dna = next(i["item_id"] for i in seeded.search_items(text="JaCo genomic DNA"))
        graph = seeded.lineage(dna, direction="both", depth=2)
        assert graph["parents"][0]["name"] == "JaCo PBMC"
        assert graph["children"][0]["name"] == "JaCo DNA aliquot 1"

    def test_pooling_needs_no_schema_change(self, seeded):
        """Many parents into one child -- the junction already covers it."""
        inputs = [i["item_id"] for i in seeded.search_items(item_class="sample")][:3]
        pool = seeded.create_sample("Pooled library", "library").records[0]["item_id"]
        for src in inputs:
            seeded.link_parent(pool, src, "pooled_into")
        graph = seeded.lineage(pool, direction="parents")
        assert len(graph["parents"]) == 3

    def test_self_link_refused(self, seeded):
        item = next(i["item_id"] for i in seeded.search_items(item_class="sample"))
        with pytest.raises(ValidationError):
            seeded.link_parent(item, item, "aliquot_of")


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

    def test_merge_repoints_measurements_and_lineage(self, seeded):
        keep = seeded.create_sample("Keep me", "dna").records[0]["item_id"]
        lose = seeded.create_sample("Duplicate", "dna").records[0]["item_id"]
        seeded.record_measurement(lose, "qubit_dsdna", value="33", unit="ng/ul")
        child = seeded.create_sample("Derived", "dna").records[0]["item_id"]
        seeded.link_parent(child, lose, "extracted_from")

        seeded.merge_items(keep, lose, reason="double-registered at the bench")

        assert seeded.get_measurements(item_id=keep, assay_code="qubit_dsdna")[0]["value_num"] == 33.0
        assert seeded.lineage(child, direction="parents")["parents"][0]["item_id"] == keep
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
