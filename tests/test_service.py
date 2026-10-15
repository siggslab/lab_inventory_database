"""End-to-end behaviour through the service, against real files."""

from __future__ import annotations

import pytest

from conftest import read_rows, table_path
from inventory_mcp.query import run_sql
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
    """move: one slot, whether the thing being moved is a tube or a box."""

    @staticmethod
    def _one(seeded, label, kind="box"):
        return next(c["container_id"] for c in seeded.list_containers(kind=kind)
                    if c["label"] == label)

    def test_move_between_boxes_drops_the_stale_well(self, seeded):
        boxes = [c["container_id"] for c in seeded.list_containers(kind="box")]
        item = next(i["item_id"] for i in seeded._matching_items(item_class="sample")
                    if i["location"]["position"])
        row = seeded.db.items_by_id()[item]
        source, old_pos = row["location_id"], row["position"]
        target = next(b for b in boxes if b != source)
        out = seeded.move(item, target)
        stored = seeded.db.items_by_id()[item]
        assert stored["location_id"] == target
        # an unasserted position is not carried over into a differently-shaped box
        assert stored["position"] == ""
        # and the answer is only where it came from and where it went
        labels = {c["container_id"]: c["label"] for c in seeded.list_containers()}
        assert out["id"] == item and out["kind"] == "sample"
        assert out["from"] == {"container_id": source, "label": labels[source],
                               "position": old_pos}
        assert out["to"] == {"container_id": target, "label": labels[target], "position": ""}

    def test_explicit_position_survives_the_move(self, seeded):
        boxes = [c for c in seeded.list_containers(kind="box") if c["geometry"] == "10x10"]
        item = next(i["item_id"] for i in seeded._matching_items(item_class="sample")
                    if not i["location"]["position"])
        seeded.move(item, boxes[0]["container_id"], position="H8")
        assert seeded.db.items_by_id()[item]["position"] == "H8"

    def test_repositioning_in_one_box_keeps_the_well_it_had(self, seeded):
        item = next(i["item_id"] for i in seeded._matching_items(item_class="sample")
                    if i["location"]["position"])
        row = seeded.db.items_by_id()[item]
        out = seeded.move(item, row["location_id"])
        assert seeded.db.items_by_id()[item]["position"] == row["position"]
        assert out["from"]["position"] == out["to"]["position"] == row["position"]

    def test_an_empty_position_forgets_the_well_and_stays_put(self, seeded):
        item = next(i["item_id"] for i in seeded._matching_items(item_class="sample")
                    if i["location"]["position"])
        row = seeded.db.items_by_id()[item]
        seeded.move(item, row["location_id"], position="")
        after = seeded.db.items_by_id()[item]
        assert after["location_id"] == row["location_id"] and after["position"] == ""

    def test_a_container_moves_and_takes_one_slot(self, seeded):
        box = self._one(seeded, "A5 ARBC PBMCs")
        rack = self._one(seeded, "Rack 3", kind="rack")
        out = seeded.move(box, rack)
        assert seeded.db.containers_by_id()[box]["parent_id"] == rack
        assert out["kind"] == "container"
        assert out["from"] == {"container_id": self._one(seeded, "Rack C8", kind="rack"),
                               "label": "Rack C8", "position": ""}
        assert out["to"]["label"] == "Rack 3"
        # the two racks now disagree about how many boxes they hold
        assert len(seeded.get_contents(rack)["containers"]) == 3
        assert len(seeded.get_contents(self._one(seeded, "Rack C8", kind="rack"))["containers"]) == 0

    def test_a_container_cannot_move_into_itself_or_what_it_contains(self, seeded):
        freezer = self._one(seeded, "-80 Freezer #7085", kind="freezer")
        deep_box = self._one(seeded, "VEXAS DNA Box 1")
        with pytest.raises(Conflict, match="itself"):
            seeded.move(freezer, freezer)
        with pytest.raises(Conflict, match="already inside"):
            seeded.move(freezer, deep_box)
        assert seeded.db.containers_by_id()[freezer]["parent_id"] == ""

    def test_no_space_in_the_target_refuses_before_anything_is_written(self, seeded):
        seeded.create_box("One spot", rows=1, cols=1,
                          parent_id=self._one(seeded, "Rack 3", kind="rack"))
        tight = self._one(seeded, "One spot")
        tubes = [i for i in seeded._matching_items(item_class="sample")
                 if not i["location"]["position"] and i["status"] != "consumed"]
        first, second = tubes[0]["item_id"], tubes[1]["item_id"]
        seeded.move(first, tight)
        before = seeded.db.items_by_id()[second]["location_id"]
        with pytest.raises(Conflict, match="no space in 'One spot'"):
            seeded.move(second, tight)
        assert seeded.db.items_by_id()[second]["location_id"] == before
        assert seeded.get_contents(tight)["count"] == 1

    def test_a_full_container_refuses_a_box_too(self, seeded):
        seeded.create_box("Snug", rows=1, cols=1,
                          parent_id=self._one(seeded, "Rack 3", kind="rack"))
        snug = self._one(seeded, "Snug")
        seeded.move(self._one(seeded, "-70C Misc"), snug)
        with pytest.raises(Conflict, match="no space in 'Snug'"):
            seeded.move(self._one(seeded, "VEXAS DNA Box 1"), snug)

    def test_a_consumed_item_holds_no_slot(self, seeded):
        item = next(i["item_id"] for i in seeded._matching_items(item_class="sample")
                    if i["location"]["container_id"])
        seeded.set_status(item, "consumed")
        with pytest.raises(Conflict, match="consumed"):
            seeded.move(item, self._one(seeded, "-70C Misc"))

    def test_either_kind_of_id_is_resolved_or_said_so(self, seeded):
        with pytest.raises(NotFound, match="nothing with id"):
            seeded.move("ctr_nosuchid", self._one(seeded, "Rack 3", kind="rack"))
        with pytest.raises(ServiceError, match="id is required"):
            seeded.move("", self._one(seeded, "Rack 3", kind="rack"))

    def test_the_target_has_to_be_a_container(self, seeded):
        item = next(i["item_id"] for i in seeded._matching_items(item_class="sample"))
        with pytest.raises(NotFound, match="no container with id"):
            seeded.move(item, item)

    def test_position_is_for_items_only(self, seeded):
        with pytest.raises(ServiceError, match="for items"):
            seeded.move(self._one(seeded, "-70C Misc"),
                        self._one(seeded, "Rack 3", kind="rack"), position="A1")

    def test_undoing_a_consume_needs_a_place_to_put_it(self, seeded):
        """Consuming releases the slot and forgets the well, so coming back out of it has
        to name a container. Marking something present while it holds nothing is a claim
        on no shelf, which is exactly the state the guard prevents."""
        item = next(i["item_id"] for i in seeded.search_items(status="partially_consumed")["rows"])
        home = seeded.db.items_by_id()[item]["location_id"]
        seeded.set_status(item, "consumed")
        with pytest.raises(ServiceError, match="holds no slot"):
            seeded.set_status(item, "present")
        seeded.set_status(item, "present", location_id=home)
        row = seeded.db.items_by_id()[item]
        assert row["status"] == "present" and row["location_id"] == home
        assert row["position"] == "", "the well went with the slot"

    def test_a_consumed_item_takes_no_location(self, seeded):
        item = next(i["item_id"] for i in seeded.search_items(status="present")["rows"])
        with pytest.raises(ServiceError, match="takes no location_id"):
            seeded.set_status(item, "consumed", location_id=self._one(seeded, "-70C Misc"))

    def test_the_container_it_names_has_to_exist(self, seeded):
        item = next(i["item_id"] for i in seeded.search_items(status="present")["rows"])
        with pytest.raises(NotFound, match="no container with id"):
            seeded.set_status(item, "partially_consumed", location_id="ctr_nosuchid")

    def test_an_item_is_not_an_obstacle_to_its_own_placement(self, seeded):
        """Naming the container it already sits in is a no-op move, so the occupant being
        re-placed has to be excluded from the count -- otherwise a full box refuses the
        very tube that is standing in it."""
        seeded.create_box("Tight", rows=1, cols=1,
                          parent_id=self._one(seeded, "Rack 3", kind="rack"))
        tight = self._one(seeded, "Tight")
        free = [i for i in seeded._matching_items(item_class="sample")
                if not i["location"]["position"] and i["status"] != "consumed"]
        tube, other = free[0]["item_id"], free[1]["item_id"]
        seeded.move(tube, tight)
        seeded.set_status(tube, "partially_consumed", location_id=tight)
        with pytest.raises(Conflict, match="no space in 'Tight'"):
            seeded.set_status(other, "partially_consumed", location_id=tight)


class TestDescriptiveEdits:
    """The three edit tools resolve the table from the id rather than being told it, so
    what is under test is the resolution: which column a rename lands in, whether prose
    finds the table that keeps it, and whether a field the row cannot hold is refused by
    fact instead of accepted on the caller's declaration."""

    def test_one_id_renames_whatever_it_belongs_to(self, seeded):
        item = seeded.create_sample("Temporary name", "dna").records[0]["item_id"]
        box = seeded.db.t("containers").rows[0]["container_id"]
        patient = seeded.db.t("patients").rows[0]["patient_id"]
        project = seeded.db.t("projects").rows[0]["project_id"]
        assay = seeded.db.t("assays").rows[0]["assay_id"]

        assert seeded.edit_name(item, "Renamed tube").operation == "edit_name"
        assert seeded.db.items_by_id()[item]["name"] == "Renamed tube"
        seeded.edit_name(box, "Renamed box")
        assert seeded.db.containers_by_id()[box]["label"] == "Renamed box"
        seeded.edit_name(patient, "Trial participant B")
        assert seeded.db.t("patients").get(patient)["label"] == "Trial participant B"
        seeded.edit_name(project, "Renamed project")
        assert seeded.db.t("projects").get(project)["name"] == "Renamed project"
        seeded.edit_name(assay, "renamed_qc")
        assert seeded.db.t("assays").get(assay)["name"] == "renamed_qc"

    def test_prose_lands_where_that_table_keeps_it(self, seeded):
        item = seeded.db.t("items").rows[0]["item_id"]
        project = seeded.db.t("projects").rows[0]["project_id"]

        seeded.edit_notes(item, "thawed twice, do not freeze again")
        assert seeded.db.items_by_id()[item]["notes"] == "thawed twice, do not freeze again"

        # projects have no notes column; their prose is the description
        seeded.edit_notes(project, "withdrawn pending consent re-signature")
        assert seeded.db.t("projects").get(project)["description"]

        seeded.edit_notes(item, "")
        assert seeded.db.items_by_id()[item]["notes"] == ""

    def test_a_name_cannot_be_blank_but_a_note_can(self, seeded):
        item = seeded.db.t("items").rows[0]["item_id"]
        with pytest.raises(ServiceError, match="cannot be blank"):
            seeded.edit_name(item, "   ")
        seeded.edit_notes(item, "")  # clearing free text is a legitimate edit

    def test_the_class_on_the_row_decides_which_fields_exist(self, seeded):
        sample = next(i["item_id"] for i in seeded.search_items(item_class="sample")["rows"])
        kit = next(i["item_id"] for i in seeded.search_items(item_class="kit")["rows"])

        seeded.edit_data(sample, "collected_on", "2024-03-02")
        assert seeded.db.t("samples").get(sample)["collected_on"] == "2024-03-02"

        with pytest.raises(ServiceError, match=r"price belongs to reagents, kits or "
                                               r"consumables, and .* is a sample"):
            seeded.edit_data(sample, "price", "12.50")

        seeded.edit_data(kit, "price", "12.50")
        assert float(seeded.db.t("kits").get(kit)["price"]) == 12.5

        for oid in (sample, kit):  # handler is on the supertype, so any item takes it
            seeded.edit_data(oid, "handler", "R. Okafor")
            assert seeded.db.items_by_id()[oid]["handler"] == "R. Okafor"

    def test_an_unusable_id_or_field_says_what_would_work(self, seeded):
        sample = next(i["item_id"] for i in seeded.search_items(item_class="sample")["rows"])
        with pytest.raises(ServiceError, match=r"editable are .*collected_on"):
            seeded.edit_data(sample, "colour", "blue")
        with pytest.raises(NotFound, match="nothing editable with id"):
            seeded.edit_notes("pat_nosuchid", "orphan note")
        with pytest.raises(NotFound, match="no item with id"):
            seeded.edit_data("ctr_nosuchid", "handler", "whoever")
        with pytest.raises(ServiceError, match="id is required"):
            seeded.edit_name("", "x")

    def test_a_tombstone_refuses_its_text_being_edited(self, seeded):
        item = next(i["item_id"] for i in seeded.search_items(status="present")["rows"])
        seeded.delete_item(item)
        with pytest.raises(Conflict, match="tombstone"):
            seeded.edit_notes(item, "still editing")
        with pytest.raises(Conflict, match="tombstone"):
            seeded.edit_data(item, "handler", "whoever")
        with pytest.raises(Conflict, match="tombstone"):
            seeded.edit_name(item, "Resurrected")


class TestMeasurements:
    def test_unit_dimension_is_enforced(self, seeded):
        item = next(i["item_id"] for i in seeded.search_items(item_class="sample")["rows"])
        with pytest.raises(Exception) as e:
            seeded.record_measurement(item, "qubit_dsdna", value_num="40", unit="ul")
        assert "not a recognised concentration_mass unit" in str(e.value)

    def test_a_reading_reports_the_observed_figure_and_the_canonical_one(self, seeded):
        """What was written stays the record; the conversion is reported beside it.

        The tool row carries both, so a model comparing two tubes recorded in different
        units of the same quantity does it from what it was handed rather than by
        arithmetic in its head -- and the exports get the identical figure."""
        item = next(i["item_id"] for i in seeded.search_items(item_class="sample")["rows"])
        seeded.record_measurement(item, "qubit_dsdna", value_num="0.05", unit="mg/ml")
        lean = seeded.get_measurements(item_id=item, assay="qubit_dsdna")[0]
        assert (lean["value"], lean["unit"]) == (pytest.approx(0.05), "mg/ml")
        assert lean["canonical"]["value"] == pytest.approx(50.0)
        assert lean["canonical"]["unit"] == "ng/ul"

        full = seeded._measurement_rows(item_id=item, assay="qubit_dsdna")[0]
        assert full["canonical"] == lean["canonical"]

    def test_correction_appends_and_supersedes(self, seeded):
        item = next(i["item_id"] for i in seeded.search_items(item_class="sample")["rows"])
        first = seeded.record_measurement(item, "qubit_dsdna", value_num="100", unit="ng/ul")
        mid = first.records[0]["measurement_id"]
        seeded.correct_measurement(mid, value_num="72", unit="ng/ul", notes="dilution error")

        visible = seeded.get_measurements(item_id=item, assay="qubit_dsdna")
        assert all(m["measurement_id"] != mid for m in visible)
        history = seeded.get_measurements(item_id=item, assay="qubit_dsdna",
                                          include_superseded=True)
        original = next(m for m in history if m["measurement_id"] == mid)
        assert original["superseded"] is True
        assert original["value"] == 100.0  # untouched on disk

    def test_double_correction_is_refused(self, seeded):
        item = next(i["item_id"] for i in seeded.search_items(item_class="sample")["rows"])
        mid = seeded.record_measurement(item, "qubit_dsdna", value_num="10",
                                        unit="ng/ul").records[0]["measurement_id"]
        seeded.correct_measurement(mid, value_num="11", unit="ng/ul")
        with pytest.raises(Conflict):
            seeded.correct_measurement(mid, value_num="12", unit="ng/ul")

    def test_a_second_reading_retires_the_one_it_overtook(self, seeded):
        """The new row names what it replaced, so nobody has to infer it from order.

        Two values for one question sit in the same spreadsheet a person opens by
        hand; entry order is invisible there, and inferring currency from it is not
        something a reader should have to know this codebase does.
        """
        item = next(i["item_id"] for i in seeded.search_items(item_class="sample")["rows"])
        first = seeded.record_measurement(item, "qubit_dsdna", value_num="10", unit="ng/ul")
        mid = first.records[0]["measurement_id"]
        second = seeded.record_measurement(item, "qubit_dsdna", value_num="12", unit="ng/ul")
        assert second.records[0]["supersedes"] == mid
        # Retiring is a note on the new row: the old reading keeps every byte.
        assert seeded.db.t("measurements").get(mid)["value_num"] == "10"

    def test_the_retired_reading_is_named_back_to_the_caller(self, seeded):
        """Automatic must not be silent -- the caller asked to measure, not to correct."""
        item = next(i["item_id"] for i in seeded.search_items(item_class="sample")["rows"])
        mid = seeded.record_measurement(item, "qubit_dsdna", value_num="10",
                                        unit="ng/ul").records[0]["measurement_id"]
        result = seeded.record_measurement(item, "qubit_dsdna", value_num="12", unit="ng/ul")
        notes = [w for w in result.warnings if w["code"] == "auto_superseded"]
        assert len(notes) == 1
        assert mid in notes[0]["message"]

    def test_a_first_reading_for_an_assay_retires_nothing(self, seeded):
        # A fixture-built item may already carry readings; this wants none at all.
        item = seeded.create_sample("Never measured", "dna").records[0]["item_id"]
        result = seeded.record_measurement(item, "gel_qc", value_text="no smearing")
        assert result.records[0]["supersedes"] == ""
        assert result.warnings == []

    def test_a_named_reading_to_retire_is_taken_as_given(self, seeded):
        item = next(i["item_id"] for i in seeded.search_items(item_class="sample")["rows"])
        mid = seeded.record_measurement(item, "qubit_dsdna", value_num="10",
                                        unit="ng/ul").records[0]["measurement_id"]
        named = seeded.record_measurement(item, "qubit_dsdna", value_num="12", unit="ng/ul",
                                          supersedes=mid)
        assert named.records[0]["supersedes"] == mid
        assert named.warnings == [], "nothing was decided for the caller; say nothing"

    def test_the_series_survives_its_readings_being_retired(self, seeded):
        """History used to mean 'rows nobody retired'; now retirement is the normal
        outcome of measuring twice, so history has to include them or it is one row."""
        item = seeded.create_sample("Measured three times", "dna").records[0]["item_id"]
        for value in ("10", "11", "12"):
            seeded.record_measurement(item, "qubit_dsdna", value_num=value, unit="ng/ul")
        current = seeded.get_measurements(item_id=item, assay="qubit_dsdna")
        assert [(m["value"], m["superseded"]) for m in current] == [(12.0, False)]
        series = seeded.get_measurements(item_id=item, assay="qubit_dsdna",
                                         include_historical=True)
        assert [m["value"] for m in series] == [10.0, 11.0, 12.0]

    def test_the_sql_view_and_the_service_agree_on_what_is_current(self, seeded):
        """Two definitions of 'current' is how one assay ends up with two answers."""
        item = next(i["item_id"] for i in seeded.search_items(item_class="sample")["rows"])
        seeded.record_measurement(item, "qubit_dsdna", value_num="10", unit="ng/ul")
        seeded.record_measurement(item, "qubit_dsdna", value_num="12", unit="ng/ul")
        rows = run_sql(seeded.db, f"select * from latest_measurements where item_id='{item}'"
                                  " and assay_id=(select assay_id from assays where code="
                                  "'qubit_dsdna')", limit=10)
        service_side = seeded.get_measurements(item_id=item, assay="qubit_dsdna")
        assert [r["measurement_id"] for r in rows["rows"]] == [
            m["measurement_id"] for m in service_side]
        assert [r["value_num"] for r in rows["rows"]] == ["12"]

    def test_qualitative_assay_takes_value_text_not_value_num(self, seeded):
        item = next(i["item_id"] for i in seeded.search_items(item_class="sample")["rows"])
        with pytest.raises(Exception):
            seeded.record_measurement(item, "gel_qc", value_num="3", unit="")
        result = seeded.record_measurement(item, "gel_qc", value_text="faint smear at 20kb")
        assert result.records[0]["value"] == "faint smear at 20kb"

    def test_a_zone_spelled_out_lands_as_the_same_minute_in_utc(self, seeded):
        """ISO lets the caller name its zone however it likes; the column answers in UTC."""
        item = next(i["item_id"] for i in seeded.search_items(item_class="sample")["rows"])
        cases = {
            "2024-05-16T09:00": "2024-05-16T09:00Z",   # no zone named: read as UTC
            "2024-05-16T09:00Z": "2024-05-16T09:00Z",  # UTC said out loud
            "2024-05-16T19:00+10:00": "2024-05-16T09:00Z",
            "2024-05-16T04:00-05:00": "2024-05-16T09:00Z",
            "2024-05-16T09:00+0000": "2024-05-16T09:00Z",
        }
        for sent, stored in cases.items():
            seeded.record_measurement(item, "volume_ul", value_num="10", unit="ul",
                                      measured_on=sent, notes=sent)
            series = seeded.get_measurements(item_id=item, assay="volume_ul",
                                             include_historical=True)
            row = next(r for r in series if r["notes"] == sent)
            assert row["measured_on"] == stored, sent

    def test_a_timestamp_that_is_not_a_timestamp_is_refused_by_name(self, seeded):
        item = next(i["item_id"] for i in seeded.search_items(item_class="sample")["rows"])
        with pytest.raises(ServiceError, match="not a recognisable timestamp"):
            seeded.record_measurement(item, "volume_ul", value_num="10", unit="ul",
                                      measured_on="last Tuesday, morning")

    def test_the_server_stamps_its_own_minutes_with_the_zone(self, seeded):
        """Nobody hand-writes these, so they have no excuse for leaving the zone to be
        inferred -- including the tombstone, which outlives every other field on the row."""
        made = seeded.create_sample("Stamped on write", "dna").records[0]
        assert made["updated_at"].endswith("Z")
        seeded.delete_item(made["item_id"])
        assert seeded.db.items_by_id()[made["item_id"]]["deleted_at"].endswith("Z")

    def test_an_older_row_naming_no_zone_warns_instead_of_blocking(self, seeded):
        """Rows written before the marker landed are still legal -- read as UTC -- but
        a column holding two spellings is worth keeping in view until they are rewritten.
        A warning never refuses a write, so the database stays usable around them."""
        from conftest import write_rows
        from inventory_mcp import schema

        path = table_path(seeded.local_root(), "measurements")
        rows = read_rows(path)
        target = rows[0]["measurement_id"]
        for r in rows:
            if r["measurement_id"] == target:
                r["measured_on"] = r["measured_on"].rstrip("Z")
        write_rows(path, rows, list(schema.MEASUREMENTS.column_names))
        seeded.reload()

        report = seeded.doctor()
        flagged = [w for w in report["warnings"] if w["code"] == "datetime_without_zone"]
        assert [w["row_id"] for w in flagged] == [target]
        assert report["error_count"] == 0, "an unmarked timestamp is ambiguous, not illegal"

        tube = next(i["item_id"] for i in seeded.search_items(item_class="sample")["rows"])
        seeded.record_measurement(tube, "volume_ul", value_num="10", unit="ul")
        assert seeded.doctor()["warning_count"] == 1, "the untouched rows stayed clean"

    def test_yield_is_computed_not_stored(self, seeded):
        """Concentration x volume is derived on read; nothing caches it."""
        wide = seeded.export(formats=("csv",))
        rows = read_rows(seeded.local_root() / "exports" / "items_wide.csv")
        ja_co = next(r for r in rows if r["gml_id"] == "GML001994")
        assert float(ja_co["concentration_ng_ul"]) == pytest.approx(47.2)
        assert float(ja_co["computed_yield_ng"]) == pytest.approx(47.2 * 20)
        assert "yield_ng" not in read_rows(table_path(seeded.local_root(), "measurements"))[0]

    def test_reagent_quantity_is_a_time_series(self, seeded):
        enzyme = next(i["item_id"] for i in seeded.search_items(item_class="reagent")["rows"])
        history = seeded.get_measurements(item_id=enzyme, assay="reagent_volume",
                                          include_historical=True)
        assert [h["value"] for h in history] == [500.0, 420.0]
        latest = seeded.get_measurements(item_id=enzyme, assay="reagent_volume")
        assert latest[0]["value"] == 420.0

    def test_the_series_is_opt_in(self, seeded):
        """One row per item and assay by default: the answer to "how much is left"
        should not have to carry every reading that led here."""
        enzyme = next(i["item_id"] for i in seeded.search_items(item_class="reagent")["rows"])
        current = seeded.get_measurements(item_id=enzyme, assay="reagent_volume")
        assert len(current) == 1

    def test_superseded_rows_never_win_the_latest_slot(self, seeded):
        """A correction that moves the date backwards must not hand the newest
        reading back to the invalidated row it replaced."""
        item = next(i["item_id"] for i in seeded.search_items(item_class="sample")["rows"])
        mid = seeded.record_measurement(
            item, "qubit_dsdna", value_num="100", unit="ng/ul").records[0]["measurement_id"]
        seeded.correct_measurement(mid, value_num="72", unit="ng/ul",
                                   measured_on="2019-01-01T09:00")

        current = seeded.get_measurements(item_id=item, assay="qubit_dsdna")
        assert [r["value"] for r in current] == [72.0]
        audit = seeded.get_measurements(item_id=item, assay="qubit_dsdna",
                                        include_superseded=True)
        assert sorted(r["value"] for r in audit) == [72.0, 100.0]

    def test_unknown_assay_code_is_refused(self, seeded):
        """Silence here would read as "no readings", not "you typed the code wrong"."""
        item = next(i["item_id"] for i in seeded.search_items(item_class="sample")["rows"])
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
                    "unit", "canonical", "handler", "instrument", "supersedes",
                    "superseded", "notes"}
        assert rows and all(set(r) == expected for r in rows)

    def test_a_qualitative_reading_arrives_as_value_too(self, seeded):
        """The key set stays constant; only the type of ``value`` follows the assay.

        A qualitative reading has no magnitude, so ``canonical`` is null rather than
        absent -- the shape a caller reads once is the shape it reads every time."""
        item = next(i["item_id"] for i in seeded.search_items(item_class="sample")["rows"])
        seeded.record_measurement(item, "gel_qc", value_text="faint smear at 20kb")
        row = seeded.get_measurements(item_id=item, assay="gel_qc")[0]
        assert row["value"] == "faint smear at 20kb"
        assert row["unit"] == ""
        assert row["canonical"] is None


class TestGetLinkedItems:
    def test_chain_reads_both_directions(self, seeded):
        dna = next(i["item_id"] for i in seeded.search_items(text="JaCo genomic DNA")["rows"])
        graph = seeded.get_linked_items(dna, direction="both", depth=2)
        assert graph["parents"][0]["name"] == "JaCo PBMC"
        assert graph["children"][0]["name"] == "JaCo DNA aliquot 1"

    def test_pooling_needs_no_schema_change(self, seeded):
        """Many parents into one child -- the junction already covers it."""
        inputs = [i["item_id"] for i in seeded.search_items(item_class="sample")["rows"]][:3]
        pool = seeded.create_sample("Pooled library", "library").records[0]["item_id"]
        for src in inputs:
            seeded.link_items(pool, src, "pooled_into")
        graph = seeded.get_linked_items(pool, direction="parents")
        assert len(graph["parents"]) == 3

    def test_self_link_refused(self, seeded):
        item = next(i["item_id"] for i in seeded.search_items(item_class="sample")["rows"])
        with pytest.raises(ValidationError):
            seeded.link_items(item, item, "aliquot_of")

    def test_one_hop_is_the_default(self, seeded):
        """Depth is not a knob on the tool: going further means asking again about
        an id, so a trace only grows where someone chose to follow it."""
        dna = next(i["item_id"] for i in seeded.search_items(text="JaCo genomic DNA")["rows"])
        parent = seeded.get_linked_items(dna, direction="parents")["parents"][0]
        assert parent["name"] == "JaCo PBMC"
        assert "parents" not in parent

    def test_tombstoned_ends_are_marked_not_hidden(self, seeded):
        """Provenance outlives the tube, but a discarded aliquot must not read as
        material that still exists -- delete leaves status alone, so status lies."""
        keep = next(i["item_id"] for i in seeded.search_items(text="JaCo DNA aliquot 1")["rows"])
        node = seeded.get_linked_items(keep, direction="parents")["parents"][0]
        parent, status_before = node["item_id"], node["status"]

        seeded.delete_item(parent)
        node = seeded.get_linked_items(keep, direction="parents")["parents"][0]
        assert node["deleted"] is True
        assert node["status"] == status_before  # the old claim survives; deleted corrects it
        assert seeded.get_linked_items(parent, direction="children")["children"][0]["deleted"] is False

    def test_edges_are_named_by_their_endpoints_not_a_surrogate(self, seeded):
        """``link_id`` stays in the table. A caller identifies an edge by its two
        item ids, so shipping the surrogate would advertise an address nothing takes."""
        dna = next(i["item_id"] for i in seeded.search_items(text="JaCo genomic DNA")["rows"])
        node = seeded.get_linked_items(dna, direction="parents")["parents"][0]
        assert set(node) == {"item_id", "role", "name", "class", "status", "deleted"}

    def test_a_second_edge_between_the_same_pair_is_refused(self, seeded):
        """A second row between one pair is a duplicate even when its role differs:
        the endpoints name the edge, not the label someone put on it."""
        child = next(i["item_id"] for i in seeded.search_items(text="JaCo DNA aliquot 1")["rows"])
        parent = seeded.get_linked_items(child, direction="parents")["parents"][0]["item_id"]
        with pytest.raises(Conflict) as e:
            seeded.link_items(child, parent, "derived_from")
        assert "aliquot_of" in str(e.value)  # names what is already recorded


class TestDestructive:
    def test_deleting_an_item_needs_no_permission_and_leaves_a_tombstone(self, seeded):
        """Material still in the tube is not a reason to refuse: discarding it happens,
        and there is no later step that would catch it. What retirement takes is the
        row's claim on a place."""
        item = next(i["item_id"] for i in seeded.search_items(status="present")["rows"])
        seeded.delete_item(item)
        stored = seeded.db.items_by_id()[item]
        assert stored["deleted_at"] and stored["location_id"] == ""
        # tombstoned, not erased
        assert item in seeded.db.items_by_id()
        assert item not in {i["item_id"] for i in seeded.search_items()["rows"]}
        with pytest.raises(Conflict, match="already deleted"):
            seeded.delete_item(item)
        assert item in {i["item_id"] for i in seeded.search_items(include_deleted=True)["rows"]}

    def test_non_empty_container_cannot_be_deleted(self, seeded):
        occupied = next(c["container_id"] for c in seeded.list_containers(kind="box")
                        if c["occupied"])
        with pytest.raises(Conflict):
            seeded.delete_container(occupied)

    def test_a_refused_container_delete_lists_every_blocker_by_id(self, seeded):
        """A count says it failed; the ids are what the caller acts on -- each one gets
        moved or retired before the retry."""
        box = next(c["container_id"] for c in seeded.list_containers(kind="box")
                   if c["occupied"])
        inside = [i["item_id"] for i in seeded.get_contents(box)["items"]]
        assert inside
        with pytest.raises(Conflict) as e:
            seeded.delete_container(box)
        assert all(iid in str(e.value) for iid in inside)

    def test_a_shelf_holding_boxes_lists_the_boxes(self, seeded):
        shelf = next(c["container_id"] for c in seeded.list_containers(kind="shelf"))
        nested = [c["container_id"] for c in seeded.get_contents(shelf)["containers"]]
        assert nested
        with pytest.raises(Conflict) as e:
            seeded.delete_container(shelf)
        assert all(cid in str(e.value) for cid in nested)

    def test_a_retired_tube_naming_the_box_is_swept_not_refused(self, seeded):
        """A tombstone can be neither moved nor edited, so refusing over its stale
        location would leave the box undeletable forever. And leaving the reference
        behind would dangle a foreign key over every later write. This arrives through
        a hand edit -- nothing in the surface leaves a retired row holding a place --
        so the box goes and the dead claim is cleared in the same commit."""
        from conftest import write_rows
        from inventory_mcp import schema

        shelf = next(c["container_id"] for c in seeded.list_containers(kind="shelf"))
        box = seeded.create_box("Doomed box", 2, 2, parent_id=shelf).records[0]["container_id"]
        tube = seeded.create_sample("Retired in the box", "dna",
                                    location_id=box).records[0]["item_id"]
        seeded.delete_item(tube)
        path = table_path(seeded.local_root(), "items")
        rows = read_rows(path)
        for r in rows:
            if r["item_id"] == tube:
                r["location_id"] = box
        write_rows(path, rows, list(schema.ITEMS.column_names))
        seeded.reload()

        seeded.delete_container(box)

        assert box not in seeded.db.containers_by_id()
        assert {r["item_id"]: r for r in read_rows(path)}[tube]["location_id"] == ""
        assert seeded.doctor()["error_count"] == 0

    def test_status_refuses_to_rewrite_a_tombstone(self, seeded):
        """``move`` and the edit tools already refuse a retired row. Status matters more,
        not less: filing a dead tube into a well claims a slot that ``occupants()`` will
        never report, so the box reads free while a row says otherwise."""
        item = next(i["item_id"] for i in seeded.search_items(status="present")["rows"])
        box = next(c["container_id"] for c in seeded.list_containers(kind="box"))
        seeded.delete_item(item)
        with pytest.raises(Conflict, match="tombstone"):
            seeded.set_status(item, "consumed")
        with pytest.raises(Conflict, match="tombstone"):
            seeded.set_status(item, "partially_consumed", location_id=box)
        stored = seeded.db.items_by_id()[item]
        assert stored["status"] == "present" and stored["location_id"] == ""

    def test_merge_repoints_measurements_and_derivation_links(self, seeded):
        keep = seeded.create_sample("Keep me", "dna").records[0]["item_id"]
        lose = seeded.create_sample("Duplicate", "dna").records[0]["item_id"]
        seeded.record_measurement(lose, "qubit_dsdna", value_num="33", unit="ng/ul")
        child = seeded.create_sample("Derived", "dna").records[0]["item_id"]
        seeded.link_items(child, lose, "extracted_from")

        seeded.merge_items(keep, lose, reason="double-registered at the bench")

        assert seeded.get_measurements(item_id=keep, assay="qubit_dsdna")[0]["value"] == 33.0
        assert seeded.get_linked_items(child, direction="parents")["parents"][0]["item_id"] == keep
        assert seeded.db.items_by_id()[lose]["deleted_at"]

    def test_merge_joins_the_notes_of_both_rows(self, seeded):
        """Notes are the one field where each duplicate usually holds something the
        other does not, so they concatenate -- keep's first -- instead of one winning."""
        keep = seeded.create_sample("Keep me", "dna", notes="aliquoted on 3 June").records[0]["item_id"]
        lose = seeded.create_sample("Duplicate", "dna",
                                    notes="tube was half-thawed on arrival").records[0]["item_id"]
        seeded.merge_items(keep, lose)
        assert seeded.get_item(keep)["notes"] == ("aliquoted on 3 June\n"
                                                 "tube was half-thawed on arrival")

    def test_a_survivor_with_no_note_takes_the_discarded_one_cleanly(self, seeded):
        """Joining must not leave a leading blank line where the missing note was."""
        keep = seeded.create_sample("Keep me", "dna").records[0]["item_id"]
        lose = seeded.create_sample("Duplicate", "dna", notes="GML said 20 ul").records[0]["item_id"]
        seeded.merge_items(keep, lose)
        assert seeded.get_item(keep)["notes"] == "GML said 20 ul"

    def test_merge_across_classes_refused(self, seeded):
        s = next(i["item_id"] for i in seeded.search_items(item_class="sample")["rows"])
        r = next(i["item_id"] for i in seeded.search_items(item_class="reagent")["rows"])
        with pytest.raises(Conflict):
            seeded.merge_items(s, r)

    def test_merging_an_item_into_itself_is_refused(self, seeded):
        """The tombstone lands on discard, so an equal pair used to delete the
        survivor while reporting success -- and get_item still called it present."""
        item = next(i["item_id"] for i in seeded.search_items(item_class="sample")["rows"])
        with pytest.raises(ServiceError, match="same item"):
            seeded.merge_items(item, item)
        assert item in {i["item_id"] for i in seeded.search_items()["rows"]}

    def test_padded_ids_naming_one_item_are_the_same_item(self, seeded):
        item = next(i["item_id"] for i in seeded.search_items(item_class="sample")["rows"])
        with pytest.raises(ServiceError, match="same item"):
            seeded.merge_items(item, f"  {item} ")

    def test_merge_requires_both_ids_to_exist(self, seeded):
        item = next(i["item_id"] for i in seeded.search_items(item_class="sample")["rows"])
        with pytest.raises(NotFound, match="no item with id"):
            seeded.merge_items(item, "smp_zzzzzzzz")
        assert not seeded.db.items_by_id()[item].get("deleted_at", "")


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

    def test_a_data_word_is_not_a_statement(self, seeded):
        """The keyword scan guards what the query does, so it must not read the
        contents of a string or a comment -- half the notes in this database are
        prose about what someone did to a tube."""
        out = seeded.run_query("select 'update' as word, 1 as n")
        assert out["rows"] == [{"word": "update", "n": 1}]
        # searching for a banned word in someone's prose is a read, whatever it matches
        assert seeded.run_query(
            "select count(*) n from items where notes like '%create%'"
        )["columns"] == ["n"]
        assert seeded.run_query("select name from items -- drop table items\nlimit 1")["rows"]

    def test_the_scalar_function_survives_the_write_keyword_it_shares(self, seeded):
        """``replace`` is a string function here and only a write when it is followed
        by INTO, which the start anchor already refuses. Banning the word outright
        made ordinary queries unwritable."""
        out = seeded.run_query("select replace(name, 'a', 'A') folded from items limit 1")
        assert "A" in out["rows"][0]["folded"]

    def test_numbers_compare_as_text_unless_cast(self, seeded):
        """Pinned because it produces a plausible wrong answer rather than an error:
        a column with no affinity compared against a bare number is always greater,
        so the uncast count comes back as the whole table."""
        plain = seeded.run_query("select count(*) n from measurements where value_num > 50")
        cast = seeded.run_query(
            "select count(*) n from measurements where cast(value_num as real) > 50"
        )
        assert cast["rows"][0]["n"] < plain["rows"][0]["n"]

    def test_the_answer_names_nothing_but_itself(self, seeded):
        """The table list sits in the server instructions and repeats on the error
        that needs it; paying for it on every successful query is pure overhead."""
        out = seeded.run_query("select 1 n")
        assert set(out) == {"columns", "rows", "returned", "offset", "truncated"}

    def test_view_joins_are_available(self, seeded):
        out = seeded.run_query(
            "select name, container_label, position from items_wide where class='sample'"
        )
        assert any(r["container_label"] for r in out["rows"])


class TestGetPatient:
    """The record is written once at enrolment and asked about for years after.

    These tests exist because nothing else on the MCP surface carried consent dates,
    genes or enrolments while ``run_query`` was the only way to read them.
    """

    def _pid(self, seeded, label):
        return next(p["patient_id"] for p in seeded.db.t("patients").live()
                    if p.get("label") == label)

    def test_every_enrolled_field_comes_back(self, seeded):
        row = next(p for p in seeded.db.t("patients").live() if p.get("label") == "JaCo")
        out = seeded.get_patient(row["patient_id"])
        for field in ("label", "sex", "dob", "affected_status", "gene", "variant",
                      "protocol", "consented_on", "notes"):
            assert out[field] == (row.get(field) or ""), field

    def test_enrolments_are_listed_rather_than_counted(self, seeded):
        pid = self._pid(seeded, "ShNo")
        want = {
            next(p["name"] for p in seeded.db.t("projects").live()
                 if p["project_id"].strip() == e["project_id"].strip())
            for e in seeded.db.t("patient_projects").live() if e["patient_id"].strip() == pid
        }
        got = {e["name"] for e in seeded.get_patient(pid)["enrolments"]}
        assert got == want
        assert len(got) >= 2, "the fixture should hold someone in more than one project"

    def test_material_arrives_whole_and_split_by_project(self, seeded):
        pid = self._pid(seeded, "JaCo")
        rows = [r for r in seeded.db.t("samples").live() if r.get("patient_id", "").strip() == pid]
        expect: dict[str, int] = {}
        for r in rows:
            key = r.get("project_id", "").strip()
            expect[key] = expect.get(key, 0) + 1
        out = seeded.get_patient(pid)
        assert out["samples"]["total"] == len(rows)
        split = {e["project_id"]: e["samples"]["total"] for e in out["enrolments"]}
        split.update({k: v["total"] for k, v in out["unenrolled_samples"].items()})
        # Zeroed buckets are allowed and wanted -- an enrolment with nothing stored
        # yet still says what the person is in. Nothing may be miscounted, though.
        assert {k: v for k, v in split.items() if v} == expect
        assert all(split[k] == expect.get(k, 0) for k in split)

    def test_material_held_outside_an_enrolment_is_visible(self, seeded):
        """Otherwise the enrolment rows sum to less than the total and look like a bug
        in the arithmetic rather than a fact about the sample.

        Enrolment matches exactly, so no fresh write can produce this state -- it only
        arrives as data written while scope inherited down the project tree, or from a
        hand edit. Which is precisely why get_patient has to keep showing it.
        """
        pid = self._pid(seeded, "JaCo")
        enrolled = {e["project_id"] for e in seeded.get_patient(pid)["enrolments"]}
        other = next(p["project_id"] for p in seeded.db.t("projects").live()
                     if p["project_id"].strip() not in enrolled)
        tube = seeded.create_sample("Held elsewhere", "dna", patient_id=pid,
                                    project_id=sorted(enrolled)[0]).records[0]["item_id"]
        t = seeded.db.t("samples")
        rows = [dict(r, project_id=other) if r["item_id"] == tube else r for r in t.rows]
        seeded.store.write_table("samples", t.header, rows, t.revision or None)
        seeded.reload()
        out = seeded.get_patient(pid)
        assert out["unenrolled_samples"][other]["total"] == 1
        assert out["samples"]["total"] == sum(
            [e["samples"]["total"] for e in out["enrolments"]]
            + [v["total"] for v in out["unenrolled_samples"].values()])

    def test_blank_and_unknown_ids_refuse_readably(self, seeded):
        with pytest.raises(ServiceError, match="patient_id is required"):
            seeded.get_patient("")
        with pytest.raises(NotFound, match="search_patients"):
            seeded.get_patient("pat_00000000")


class TestParticipantRegistration:
    """A participant arrives inside a project, because a participant with no
    enrolment is a participant whose material cannot be filed anywhere."""

    def _proj(self, seeded, name):
        return next(p["project_id"] for p in seeded.db.t("projects").rows
                    if p["name"] == name)

    def test_the_participant_and_their_first_project_land_together(self, seeded):
        before = len(seeded.db.t("patient_projects").rows)
        result = seeded.create_patient("NewPerson", self._proj(seeded, "GeneAdd registry"),
                                       sex="female")
        pid, link = result.records[0]["patient_id"], result.records[1]["link_id"]
        assert any(r["link_id"] == link and r["patient_id"] == pid
                   for r in seeded.db.t("patient_projects").rows)
        assert len(seeded.db.t("patient_projects").rows) == before + 1
        assert [e["project_id"] for e in seeded.get_patient(pid)["enrolments"]] == \
            [self._proj(seeded, "GeneAdd registry")]

    def test_the_first_project_is_not_optional(self, seeded):
        for blank in ("", "   "):
            with pytest.raises(ServiceError, match="project_id is required"):
                seeded.create_patient("Unhoused", blank)

    def test_an_unusable_project_leaves_no_participant_behind(self, seeded):
        """The commit is one unit. A half-created participant -- registered, enrolled
        in nothing -- is worse than no participant at all."""
        people = len(seeded.db.t("patients").rows)
        links = len(seeded.db.t("patient_projects").rows)
        with pytest.raises(ValidationError):
            seeded.create_patient("Ghost", "prj_00000000")
        assert len(seeded.db.t("patients").rows) == people
        assert len(seeded.db.t("patient_projects").rows) == links

    def test_a_further_project_stays_an_explicit_call(self, seeded):
        pid = next(p["patient_id"] for p in seeded.db.t("patients").rows
                   if p["label"] == "HC_99801143")
        vexas = self._proj(seeded, "VEXAS")
        seeded.assign_patient_project(pid, vexas)
        assert vexas in {e["project_id"] for e in seeded.get_patient(pid)["enrolments"]}
        with pytest.raises(Conflict, match="already enrolled"):
            seeded.assign_patient_project(pid, vexas)


class TestBackfillEnrolments:
    """Exact matching strands databases built while scope inherited down the tree.
    Those rows block every write touching samples, so the migration has to be
    runnable, repeatable, and unwilling to invent consent."""

    def _legacy_shape(self, seeded):
        """JaCo's tubes sit in STAT5B/KRAS; enrol her only in its parent instead."""
        jac = next(p["patient_id"] for p in seeded.db.t("patients").rows
                   if p["label"] == "JaCo")
        stat5b, circa = (self._proj(seeded, "STAT5B/KRAS"), self._proj(seeded, "CIRCA somatic"))
        t = seeded.db.t("patient_projects")
        rows = [r for r in t.rows if not (r["patient_id"] == jac and r["project_id"] == stat5b)]
        rows.append({"link_id": "ppl_aaaaaaaa", "patient_id": jac, "project_id": circa})
        seeded.store.write_table("patient_projects", t.header, rows, t.revision or None)
        seeded.reload()
        return jac, stat5b, circa

    def _proj(self, seeded, name):
        return next(p["project_id"] for p in seeded.db.t("projects").rows
                   if p["name"] == name)

    def test_inherited_scope_becomes_an_explicit_row(self, seeded):
        jac, stat5b, circa = self._legacy_shape(seeded)
        assert seeded.doctor()["error_count"] > 0, "the fixture should be stranded"

        plan = seeded.backfill_enrolments(dry_run=True)
        assert [(r["patient_id"], r["project_id"]) for r in plan.records] == [(jac, stat5b)]
        assert plan.touched == (), "a dry run changes nothing"
        assert seeded.doctor()["error_count"] > 0

        done = seeded.backfill_enrolments()
        assert done.touched == ("patient_projects",)
        assert seeded.doctor()["error_count"] == 0
        assert {stat5b, circa} <= {e["project_id"] for e in seeded.get_patient(jac)["enrolments"]}

    def test_running_it_twice_adds_nothing(self, seeded):
        self._legacy_shape(seeded)
        seeded.backfill_enrolments()
        again = seeded.backfill_enrolments()
        assert again.records == [] and again.touched == ()

    def test_only_branches_that_hold_material_gain_a_row(self, seeded):
        """An unused subproject of a project she is enrolled in stays unenrolled:
        nobody asserted it, so inventing it would be the bug this migration exists
        to remove."""
        jac, stat5b, circa = self._legacy_shape(seeded)
        seeded.backfill_enrolments()
        got = {e["project_id"] for e in seeded.get_patient(jac)["enrolments"]}
        assert got == {circa, stat5b}
        assert self._proj(seeded, "VEXAS") not in got

    def test_a_pair_nothing_authorises_is_reported_not_invented(self, seeded):
        hc = next(p["patient_id"] for p in seeded.db.t("patients").rows
                  if p["label"] == "HC_99801143")
        geneadd = self._proj(seeded, "GeneAdd registry")
        t = seeded.db.t("samples")
        hit = False
        rows = []
        for r in t.rows:
            if not hit and r.get("patient_id", "").strip() == hc:
                rows.append(dict(r, project_id=geneadd))
                hit = True
            else:
                rows.append(r)
        assert hit, "the fixture should hold an HC tube to repoint"
        seeded.store.write_table("samples", t.header, rows, t.revision or None)
        seeded.reload()

        out = seeded.backfill_enrolments()
        assert [w["code"] for w in out.warnings] == ["unbackfillable_pair"]
        assert out.touched == (), "it will not half-migrate around a pair it cannot justify"
        assert geneadd not in {r["project_id"] for r in seeded.db.t("patient_projects").rows}


class TestUnassignPatientProject:
    """Removing an enrolment strands every sample naming the pair, and because that
    rule is reported whatever row caused it, a bare delete would leave the caller
    holding a fix list they cannot act on. So the material settles in the same write."""

    def _pid(self, seeded, label):
        return next(p["patient_id"] for p in seeded.db.t("patients").rows
                    if p["label"] == label)

    def _prj(self, seeded, name):
        return next(p["project_id"] for p in seeded.db.t("projects").rows
                    if p["name"] == name)

    def _tubes(self, seeded, patient, project=None):
        out = []
        for r in seeded.db.t("samples").rows:
            if (r.get("patient_id", "") or "").strip() != patient:
                continue
            pr = (r.get("project_id", "") or "").strip()
            if project is None or pr == project:
                out.append(r["item_id"])
        return out

    def test_withdrawal_moves_their_material_in_the_same_write(self, seeded):
        jac, stat5b, circa = (self._pid(seeded, "JaCo"), self._prj(seeded, "STAT5B/KRAS"),
                              self._prj(seeded, "CIRCA somatic"))
        seeded.assign_patient_project(jac, circa)
        held = set(self._tubes(seeded, jac, stat5b))
        assert held, "the fixture should hold JaCo tubes in STAT5B/KRAS"

        result = seeded.unassign_patient_project(jac, stat5b, move_to=circa)
        rec = result.records[0]
        assert set(result.touched) == {"samples", "patient_projects"}
        assert rec["samples_repointed"] == len(held) and rec["samples_unpinned"] == 0
        assert set(rec["sample_ids"]) == held
        assert rec["participant_still_in"] == [circa] and not rec["was_their_last_project"]
        assert self._tubes(seeded, jac, stat5b) == []
        assert set(self._tubes(seeded, jac, circa)) == held
        assert seeded.doctor()["error_count"] == 0, "the database never enters an illegal state"

    def test_no_destination_leaves_tubes_loose_rather_than_illegal(self, seeded):
        """A tube with a participant and no project is legal and findable, so nobody
        has to invent a destination before withdrawing someone."""
        jac, stat5b = self._pid(seeded, "JaCo"), self._prj(seeded, "STAT5B/KRAS")
        held = set(self._tubes(seeded, jac, stat5b))
        rec = seeded.unassign_patient_project(jac, stat5b).records[0]
        assert rec["samples_unpinned"] == len(held) and rec["samples_repointed"] == 0
        assert rec["was_their_last_project"] is True
        assert seeded.doctor()["error_count"] == 0
        loose = {r["item_id"] for r in seeded._matching_items(item_class="sample",
                                                             no_project=True)}
        assert held <= loose, "they must be findable, not merely unpinned"

    def test_the_pairing_really_goes(self, seeded):
        """Withdrawal that leaves the row behind is the bug this tool exists to avoid
        twice over: filing a tube must fail until they are enrolled again."""
        hc, circa, vexas = (self._pid(seeded, "HC_99801143"), self._prj(seeded, "CIRCA somatic"),
                            self._prj(seeded, "VEXAS"))
        seeded.unassign_patient_project(hc, circa)
        with pytest.raises(ValidationError):
            seeded.create_sample("orphan", "dna", patient_id=hc, project_id=vexas)
        seeded.assign_patient_project(hc, vexas)
        seeded.create_sample("filed again", "dna", patient_id=hc, project_id=vexas)
        assert seeded.doctor()["error_count"] == 0

    def test_refusals_name_the_way_out(self, seeded):
        hc, circa, geneadd = self._pid(seeded, "HC_99801143"), self._prj(seeded, "CIRCA somatic"), \
            self._prj(seeded, "GeneAdd registry")
        with pytest.raises(NotFound, match="get_patient"):
            seeded.unassign_patient_project(hc, geneadd)
        with pytest.raises(ServiceError, match="omit move_to"):
            seeded.unassign_patient_project(hc, circa, move_to=geneadd)
        with pytest.raises(ServiceError, match="cannot be the project being withdrawn"):
            seeded.unassign_patient_project(hc, circa, move_to=circa)
        with pytest.raises(ServiceError, match="patient_id is required"):
            seeded.unassign_patient_project("", circa)

    def test_a_tombstoned_tube_needs_no_permission_to_withdraw(self, seeded):
        """Three other validators already skip deleted rows. A retired tube is history,
        so it neither counts as live material nor blocks the withdrawal -- and its own
        record is left exactly as it was."""
        hc, circa = self._pid(seeded, "HC_99801143"), self._prj(seeded, "CIRCA somatic")
        tubes = self._tubes(seeded, hc, circa)
        assert len(tubes) >= 2, "the fixture needs two HC tubes for this to mean anything"
        buried, live = tubes[0], tubes[1:]
        seeded.delete_item(buried)
        rec = seeded.unassign_patient_project(hc, circa).records[0]
        assert rec["samples_unpinned"] == len(live)
        assert seeded.db.t("samples").get(buried)["project_id"] == circa, "history stays put"
        assert seeded.doctor()["error_count"] == 0, "a dead tube must not strand the database"

    def test_a_blocked_commit_leaves_the_enrolment_in_place(self, seeded):
        """The two tables commit together or not at all: half a withdrawal -- row gone,
        tubes still naming it -- is precisely the state this tool exists to prevent."""
        jac, stat5b = self._pid(seeded, "JaCo"), self._prj(seeded, "STAT5B/KRAS")
        t = seeded.db.t("samples")
        rows = [dict(r, sample_type="plasma_smoothie") if r["item_id"] == self._tubes(
            seeded, jac, stat5b)[0] else r for r in t.rows]
        seeded.store.write_table("samples", t.header, rows, t.revision or None)
        seeded.reload()

        with pytest.raises(ValidationError):
            seeded.unassign_patient_project(jac, stat5b)
        assert any(r["patient_id"] == jac and r["project_id"] == stat5b
                   for r in seeded.db.t("patient_projects").rows)
        assert seeded.db.t("samples").get(self._tubes(seeded, jac, stat5b)[0])["project_id"] == stat5b


class TestDeleteParticipantAndProject:
    """The same discipline as retiring a tube: either the write settles everything that
    named the row, or it refuses and hands back the ids to deal with first. What counts
    as settleable differs by what the reference meant -- a sample does not need a
    participant or a project, but a participant needs at least one project, because the
    enrolment is what makes their sample's project legal at all."""

    def _pid(self, seeded, label):
        return next(p["patient_id"] for p in seeded.db.t("patients").rows
                    if p["label"] == label)

    def _prj(self, seeded, name):
        return next(p["project_id"] for p in seeded.db.t("projects").rows
                    if p["name"] == name)

    def _tubes(self, seeded, patient, project=None):
        out = []
        for r in seeded.db.t("samples").rows:
            if (r.get("patient_id", "") or "").strip() != patient:
                continue
            pr = (r.get("project_id", "") or "").strip()
            if project is None or pr == project:
                out.append(r["item_id"])
        return out

    def test_deleting_a_participant_keeps_their_tubes_and_drops_the_pairing(self, seeded):
        hc, circa = self._pid(seeded, "HC_99801143"), self._prj(seeded, "CIRCA somatic")
        held = self._tubes(seeded, hc)
        assert held and circa

        rec = seeded.delete_patient(hc).records[0]

        assert rec == {"patient_id": hc, "enrolments_removed": 1, "samples_unlinked": len(held)}
        assert hc not in {p["patient_id"] for p in seeded.db.t("patients").rows}
        assert not [r for r in seeded.db.t("patient_projects").rows
                    if r["patient_id"] == hc]
        for iid in held:
            assert iid in seeded.db.items_by_id(), "the tubes were never the thing being deleted"
            assert seeded.db.t("samples").get(iid)["patient_id"] == ""
            assert seeded.db.t("samples").get(iid)["project_id"] == circa
        assert seeded.doctor()["error_count"] == 0

    def test_a_retired_tube_is_swept_too_because_a_foreign_key_does_not_care(self, seeded):
        """``unassign`` deliberately leaves retired tubes pinned -- the pairing rule
        skips them. Deleting the participant is different: the row they point at is
        gone, and ``fk_dangling`` checks every row alive or dead. Leaving one behind
        would refuse every later write that touches samples."""
        hc = self._pid(seeded, "HC_99801143")
        buried = self._tubes(seeded, hc)[0]
        seeded.delete_item(buried)

        seeded.delete_patient(hc)

        assert seeded.db.t("samples").get(buried)["patient_id"] == ""
        assert buried in seeded.db.items_by_id(), "still a tombstone, still provenance"
        assert seeded.doctor()["error_count"] == 0

    def test_deleting_an_unknown_participant_says_so(self, seeded):
        with pytest.raises(NotFound, match="search_patients"):
            seeded.delete_patient("pat_zzzzzzzz")

    def test_deleting_a_project_moves_their_material_to_the_project_left(self, seeded):
        """One alternative is a deduction, so the delete lands and nobody has to hold
        the tubes in limbo between two calls."""
        jac, stat5b, circa = (self._pid(seeded, "JaCo"), self._prj(seeded, "STAT5B/KRAS"),
                              self._prj(seeded, "CIRCA somatic"))
        seeded.assign_patient_project(jac, circa)
        held = set(self._tubes(seeded, jac, stat5b))
        assert held

        rec = seeded.delete_project(stat5b).records[0]

        assert rec["moved_to"] == [{"patient_id": jac, "project_id": circa}]
        assert rec["samples_repointed"] == len(held) and rec["samples_cleared"] == 0
        assert stat5b not in {p["project_id"] for p in seeded.db.t("projects").rows}
        assert not [r for r in seeded.db.t("patient_projects").rows
                    if r["project_id"] == stat5b]
        for iid in held:
            assert seeded.db.t("samples").get(iid)["project_id"] == circa
        assert seeded.doctor()["error_count"] == 0

    def test_a_participant_with_nowhere_to_go_blocks_the_delete(self, seeded):
        """JaCo's only enrolment is this project. Withdrawing them would leave their
        four tubes naming a project they are not enrolled in -- illegal, and reported
        against whatever row tripped it, which is how a database gets locked."""
        jac, stat5b = self._pid(seeded, "JaCo"), self._prj(seeded, "STAT5B/KRAS")
        with pytest.raises(Conflict) as e:
            seeded.delete_project(stat5b)
        assert jac in str(e.value) and stat5b in str(e.value)
        assert stat5b in {p["project_id"] for p in seeded.db.t("projects").rows}
        assert all(seeded.db.t("samples").get(i)["project_id"] == stat5b
                   for i in self._tubes(seeded, jac, stat5b))

    def test_two_alternatives_are_a_decision_not_a_deduction(self, seeded):
        """Where the tubes go when there is a choice is the caller's call; guessing
        would move material into a study nobody agreed to."""
        jac, stat5b = self._pid(seeded, "JaCo"), self._prj(seeded, "STAT5B/KRAS")
        circa, geneadd = self._prj(seeded, "CIRCA somatic"), self._prj(seeded, "GeneAdd registry")
        seeded.assign_patient_project(jac, circa)
        seeded.assign_patient_project(jac, geneadd)
        with pytest.raises(Conflict) as e:
            seeded.delete_project(stat5b)
        message = str(e.value)
        # the participant and both candidate destinations, all by id -- names would be
        # a label the caller cannot act on
        assert jac in message and circa in message and geneadd in message
        assert stat5b in {p["project_id"] for p in seeded.db.t("projects").rows}

    def test_a_subproject_stands_alone_and_blocks_until_it_is_gone(self, seeded):
        """Nesting organises work and inherits nothing, so CIRCA's children are not
        covered by it -- deleting the parent would orphan them with no route back."""
        circa = self._prj(seeded, "CIRCA somatic")
        vexas, stat5b = self._prj(seeded, "VEXAS"), self._prj(seeded, "STAT5B/KRAS")
        with pytest.raises(Conflict) as e:
            seeded.delete_project(circa)
        assert vexas in str(e.value) and stat5b in str(e.value)

    def test_a_project_no_one_enrolled_in_just_loses_its_tubes(self, seeded):
        """A sample with no participant needs no project either, so there is nothing
        here to protect and nothing to ask."""
        geneadd = self._prj(seeded, "GeneAdd registry")
        loose = seeded.create_sample("Registry spare", "dna",
                                     project_id=geneadd).records[0]["item_id"]
        held = [r["item_id"] for r in seeded.db.t("samples").rows
                if (r.get("project_id", "") or "").strip() == geneadd]
        rec = seeded.delete_project(geneadd).records[0]
        assert rec["samples_cleared"] == len(held) and rec["samples_repointed"] == 0
        assert loose in held
        for iid in held:
            assert seeded.db.t("samples").get(iid)["project_id"] == ""
        assert seeded.doctor()["error_count"] == 0

    def test_deleting_an_unknown_project_says_so(self, seeded):
        with pytest.raises(NotFound, match="search_projects"):
            seeded.delete_project("prj_zzzzzzzz")


class TestListAssays:
    def test_the_registry_comes_back_with_its_units(self, seeded):
        out = seeded.list_assays()
        codes = [r["code"] for r in out["rows"]]
        assert codes == sorted(codes, key=str.lower)
        assert {"qubit_dsdna", "nanodrop_a260"} <= set(codes)
        qubit = next(r for r in out["rows"] if r["code"] == "qubit_dsdna")
        assert qubit["canonical_unit"] == "ng/ul"
        assert qubit["quantity"] and qubit["result_type"]

    def test_usage_counts_match_the_measurements_table(self, seeded):
        want: dict[str, int] = {}
        for m in seeded.db.t("measurements").live():
            key = m["assay_id"].strip()
            want[key] = want.get(key, 0) + 1
        rows = seeded.list_assays()["rows"]
        assert {r["assay_id"]: r["readings"] for r in rows if r["readings"]} == want
        assert all(r["readings"] >= 0 for r in rows)

    def test_an_assay_nobody_measured_with_says_so(self, seeded):
        seeded.create_assay(code="gel_smear", name="Gel smear quality",
                            quantity="other", canonical_unit="score",
                            result_type="text")
        row = next(r for r in seeded.list_assays()["rows"] if r["code"] == "gel_smear")
        assert row["readings"] == 0


class TestDeleteAssay:
    """delete_assay: clean or refused, like every other delete."""

    def test_an_assay_nobody_measured_with_is_simply_gone(self, seeded):
        aid = seeded.create_assay(code="gel_smear", name="Gel smear quality",
                                 quantity="other", canonical_unit="score",
                                 result_type="text").records[0]["assay_id"]
        rec = seeded.delete_assay(aid).records[0]
        assert rec == {"deleted_assay": aid}
        assert aid not in {r["assay_id"] for r in seeded.list_assays()["rows"]}
        assert seeded.doctor()["error_count"] == 0

    def test_the_other_assays_survive_a_delete(self, seeded):
        before = {r["code"] for r in seeded.list_assays()["rows"]}
        spare = next(r for r in seeded.list_assays()["rows"] if r["readings"] == 0)
        seeded.delete_assay(spare["assay_id"])
        assert {r["code"] for r in seeded.list_assays()["rows"]} == before - {spare["code"]}

    def test_an_assay_with_readings_lists_them_and_stays(self, seeded):
        """Readings are immutable and nothing discards one from the surface, so the
        refusal cannot send the caller off to clear something they cannot clear. It says
        why, and names the rows that hold the definition open."""
        qubit = next(r for r in seeded.list_assays()["rows"] if r["code"] == "qubit_dsdna")
        with pytest.raises(Conflict) as e:
            seeded.delete_assay(qubit["assay_id"])
        msg = str(e.value)
        assert msg.startswith(f"{qubit['assay_id']} cannot be deleted yet")
        assert f"{qubit['readings']} reading(s)" in msg
        listed = [m["measurement_id"] for m in
                  seeded.get_measurements(include_historical=True, include_superseded=True)
                  if m["assay"] == "qubit_dsdna"]
        assert all(mid in msg for mid in listed[:3])
        assert qubit["assay_id"] in {r["assay_id"] for r in seeded.list_assays()["rows"]}

    def test_a_reading_on_a_retired_tube_still_holds_the_assay_open(self, seeded):
        """The foreign key is checked whether the item lives or not, so a dangling assay
        would refuse every later write -- the delete has to agree with that."""
        tube = seeded.create_sample("Measured then discarded", "dna").records[0]["item_id"]
        assay = seeded.create_assay(code="one_off", name="One-off read",
                                    quantity="volume", canonical_unit="ul").records[0]
        seeded.record_measurement(tube, "one_off", value_num="12", unit="ul")
        seeded.delete_item(tube)
        with pytest.raises(Conflict, match="immutable"):
            seeded.delete_assay(assay["assay_id"])

    def test_deleting_an_unknown_assay_says_so(self, seeded):
        with pytest.raises(NotFound, match="list_assays"):
            seeded.delete_assay("asy_zzzzzzzz")


class TestItemFiltersAndOrdering:
    """The bounded filters are what replaced SQL for a model, so they have to be
    exact: a filter that quietly includes an extra row is worse than no filter."""

    def dated(self, seeded):
        return [(r["item_id"].strip(), r.get("received_on", "").strip())
                for r in seeded.db.t("samples").live()
                if r.get("received_on", "").strip()]

    def test_received_bounds_partition_the_dated_samples(self, seeded):
        dated = self.dated(seeded)
        assert dated, "the fixture should hold at least one dated sample"
        pivot = max(d for _, d in dated)
        since = {i["item_id"] for i in seeded.search_items(received_since=pivot)["rows"]}
        before = {i["item_id"] for i in seeded.search_items(received_before=pivot)["rows"]}
        assert since == {i for i, d in dated if d >= pivot}
        assert not since & before, "a date cannot be both on-or-after and before its bound"

    def test_an_undated_sample_matches_no_date_bound(self, seeded):
        undated = [r["item_id"].strip() for r in seeded.db.t("samples").live()
                   if not r.get("received_on", "").strip()]
        assert undated, "the fixture should hold a sample nobody dated"
        hits = {i["item_id"] for i in seeded.search_items(received_since="1900")["rows"]}
        assert not hits & set(undated), "no date recorded is not the same as an old date"

    def test_expires_before_is_strict_and_skips_the_undated(self, seeded):
        seeded.create_supply("reagent", "Undated buffer")
        hits = seeded.search_items(expires_before="2100-01-01")["rows"]
        names = {i["name"] for i in hits}
        assert "Undated buffer" not in names
        assert names, "dated supplies must still match a far-future bound"
        kit = next(r for r in seeded.db.t("kits").live() if r.get("expires_on", "").strip())
        assert kit["item_id"].strip() not in {
            i["item_id"] for i in seeded.search_items(expires_before=kit["expires_on"])["rows"]}

    def test_measured_after_follows_the_readings(self, seeded):
        readings = list(seeded.db.t("measurements").live())
        live = {i["item_id"] for i in seeded.search_items()["rows"]}
        expect = {r["item_id"].strip() for r in readings if r["measured_on"].strip()} & live
        assert {i["item_id"] for i in seeded.search_items(measured_after="1900-01-01")["rows"]} == expect
        newest = max(r["measured_on"].strip() for r in readings)
        late = {i["item_id"] for i in seeded.search_items(measured_after=newest)["rows"]}
        assert late <= expect and late, "the newest reading date must still match itself"
        # A reading's own stamp pastes straight back as a bound, and the marker on it
        # changes nothing: older rows in the same column name no zone at all.
        assert {i["item_id"] for i in seeded.search_items(measured_after=newest.rstrip("Z"))["rows"]} == late

    def test_a_malformed_bound_is_refused_not_answered_with_nothing(self, seeded):
        for kw in ({"received_since": "last may"}, {"received_before": "31-03-2026"},
                   {"expires_before": "next week"}, {"measured_after": "20261301"}):
            with pytest.raises(ServiceError, match="not a date"):
                seeded.search_items(**kw)["rows"]
            with pytest.raises(ServiceError, match="not a date"):
                seeded.count_items(**kw)

    def test_a_bare_year_bounds_the_same_as_its_january(self, seeded):
        dated = self.dated(seeded)
        year = max(d for _, d in dated)[:4]
        assert {i["item_id"] for i in seeded.search_items(received_since=year)["rows"]} == \
            {i for i, d in dated if d[:4] >= year}
        assert seeded.search_items(received_since="1900")["rows"] == \
            seeded.search_items(received_since="1900-01-01")["rows"]

    def test_sort_names_the_column_and_direction_it_understands(self, seeded):
        names = [i["name"] for i in seeded.search_items(sort="name")["rows"]]
        assert names == sorted(names, key=str.lower)
        assert [i["name"] for i in seeded.search_items(sort="-name")["rows"]] == list(reversed(names))
        order = {"present": 0, "partially_consumed": 1, "consumed": 2}
        statuses = [i["status"] for i in seeded.search_items(sort="status")["rows"]]
        assert statuses == sorted(statuses, key=lambda s: order.get(s, 9)), \
            "status orders by what the tube has been through, not alphabetically"

    def test_an_unknown_sort_names_the_ones_that_work(self, seeded):
        with pytest.raises(ServiceError, match="not a sortable field") as e:
            seeded.search_items(sort="volume")["rows"]
        assert "received_on" in str(e.value)

    def test_limit_caps_while_zero_means_everything(self, seeded):
        everything = seeded.search_items()["rows"]
        assert seeded.search_items(limit=2)["rows"] == everything[:2]
        assert len(seeded.search_items(limit=0)["rows"]) == len(everything)

    def test_unassigned_filters_agree_with_the_tallies(self, seeded):
        """``search_projects`` and ``search_patients`` already report unassigned
        material beside their rows; the filters have to count the same tubes."""
        projects = seeded.search_projects(query="prj_")
        assert seeded.count_items(no_project=True) == projects["unassigned_samples"]
        people = seeded.search_patients(query="pat_")
        assert seeded.count_items(no_patient=True) == people["unassigned"]

    def test_unassigned_filters_do_not_reach_supplies(self, seeded):
        """Reagents have no project column, so a missing-project filter that ignored
        class would file every tip in the lab under 'needs a project'."""
        samples = {i["item_id"] for i in seeded.search_items(item_class="sample")["rows"]}
        assert seeded.count_items(item_class="reagent") > 0
        for kwargs in ({"no_project": True}, {"no_patient": True}):
            hits = {i["item_id"] for i in seeded.search_items(**kwargs)["rows"]}
            assert hits <= samples, "only samples can be missing a project or a person"


class TestPaging:
    """Reads answer one page at a time and say what lies past the page they sent."""

    def test_walking_offsets_returns_every_row_exactly_once(self, seeded):
        every = [i["item_id"] for i in seeded.search_items()["rows"]]
        assert len(every) > 7, "paging needs enough rows to mean anything"
        seen, off, pages = [], 0, 0
        while True:
            out = seeded.search_items(limit=3, offset=off)
            seen += [i["item_id"] for i in out["rows"]]
            pages += 1
            if not out["truncated"]:
                break
            off += 3
        assert seen == every, "no row skipped, repeated, or reordered by the walk"
        assert pages == -(-len(every) // 3)

    def test_matched_counts_the_filters_not_the_page(self, seeded):
        whole = seeded.search_items(item_class="sample")
        page = seeded.search_items(item_class="sample", limit=1)
        assert whole["matched"] > 1
        assert page["matched"] == whole["matched"], "the total cannot shrink with the page"
        assert len(page["rows"]) == 1 and page["truncated"]

    def test_a_short_final_page_says_it_is_the_last(self, seeded):
        """``truncated`` answers 'is there another page', not 'did this one come back
        full'. A caller that read a short page as an interruption would fetch forever."""
        total = seeded.search_items()["matched"]
        near_end = seeded.search_items(limit=4, offset=max(0, total - 2))
        assert 0 < len(near_end["rows"]) <= 2, "a deliberately short page"
        assert near_end["truncated"] is False

    def test_an_offset_past_the_end_answers_empty_rather_than_failing(self, seeded):
        """A poller re-running after rows went away should see nothing, not an error
        it has to special-case before it can draw anything."""
        out = seeded.search_items(limit=5, offset=10_000)
        assert out["rows"] == [] and out["truncated"] is False
        assert out["matched"] == seeded.search_items()["matched"]

    def test_a_negative_or_unparseable_window_is_refused_by_name(self, seeded):
        with pytest.raises(ServiceError, match="offset must be 0 or more"):
            seeded.search_items(offset=-1)
        with pytest.raises(ServiceError, match="limit must be 1 or more"):
            seeded.search_items(limit=-3)
        with pytest.raises(ServiceError, match="not a row count"):
            seeded.search_patients(query="pat_", offset="the first ten")

    def test_the_other_searches_page_in_the_same_words(self, seeded):
        for read, kwargs in (
            ("search_containers", {"query": "ctr_"}),
            ("search_patients", {"query": "pat_"}),
            ("search_projects", {"query": "prj_"}),
        ):
            find = getattr(seeded, read)
            first, second = find(limit=1, **kwargs), find(limit=1, offset=1, **kwargs)
            assert first["matched"] == second["matched"], read
            assert (first["offset"], second["offset"]) == (0, 1), read
            if first["matched"] > 1:
                assert first["rows"] != second["rows"], read
                assert first["truncated"] is True, read

    def test_sql_pages_without_losing_the_honest_count(self, seeded):
        sql = "select item_id from items order by item_id"
        first = seeded.run_query(sql, limit=2)
        second = seeded.run_query(sql, limit=2, offset=2)
        assert (first["offset"], second["offset"]) == (0, 2)
        assert first["rows"] != second["rows"]
        assert first["returned"] == len(first["rows"])
        assert first["truncated"] is True


class TestReset:
    """`init --reset-db --seed` recovery path: truncate, then seed from clean."""

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
        rows = seeded.search_items(item_class="sample")["rows"]
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
        hit = seeded.search_items(item_class="reagent")["rows"][0]
        assert "class" not in hit and "reagent" in hit

    def test_project_counts_see_the_fields_search_hides(self, seeded):
        rows = seeded.search_projects(query="prj_", limit=100)["rows"]
        named = [r for r in rows if r["samples"]["total"] > 0]
        assert named and all(r["key"].startswith("prj_") for r in rows)

    def test_tombstones_are_marked_only_when_included(self, seeded):
        victim = seeded.search_items(item_class="sample")["rows"][0]["item_id"]
        seeded.delete_item(victim)
        assert all("deleted" not in r for r in seeded.search_items()["rows"])
        flagged = next(
            r for r in seeded.search_items(include_deleted=True)["rows"]
            if r["item_id"] == victim
        )
        assert flagged["deleted"] is True

    def test_what_search_hides_get_item_shows(self, seeded):
        hit = seeded.search_items(item_class="sample")["rows"][0]
        full = seeded.get_item(hit["item_id"])
        assert {"class", "handler", "notes", "updated_by"} <= set(full)
        assert {"container_id", "position"} <= set(full["location"])
        assert {"source_material", "patient_id", "project_id",
                "collected_on", "received_on", "processed_on"} <= set(full["sample"])


class TestGetItem:
    """One call deep: latest readings inline and brief, everything else separate."""

    def test_latest_readings_are_inlined_briefly(self, seeded):
        iid = next(i["item_id"] for i in seeded.search_items(text="aliquot")["rows"])
        out = seeded.get_item(iid)
        assert out["latest_measurements"]
        for m in out["latest_measurements"]:
            assert set(m) == {"assay", "measured_on", "value", "unit", "canonical"}
        qubit = next(m for m in out["latest_measurements"] if m["assay"] == "qubit_dsdna")
        assert qubit["value"] == 47.2 and qubit["unit"] == "ng/ul"
        assert qubit["canonical"] == {"value": 47.2, "unit": "ng/ul"}

    def test_derivation_links_stay_behind_their_own_tool(self, seeded):
        iid = next(i["item_id"] for i in seeded.search_items(text="aliquot")["rows"])
        assert "parents" not in seeded.get_item(iid)
        assert seeded.get_linked_items(iid, direction="parents", depth=1)["parents"]

    def test_qualitative_assays_surface_their_value_as_text(self, seeded):
        # canonical_unit is required even for qualitative assays, so a placeholder
        # is unavoidable today -- see STATUS open thread.
        seeded.create_assay("smear_score", "Gel smear quality", "other", "score",
                            result_type="text")
        iid = next(i["item_id"] for i in seeded.search_items(item_class="sample")["rows"])
        seeded.record_measurement(iid, assay="smear_score", value_text="slight smearing")
        reading = next(m for m in seeded.get_item(iid)["latest_measurements"]
                       if m["assay"] == "smear_score")
        assert reading["value"] == "slight smearing" and reading["unit"] == ""


class TestSearchContainers:
    """search_containers: find a place by id or label, and learn nothing else about it."""

    @staticmethod
    def _total(seeded) -> int:
        return len(seeded.list_containers())

    def test_query_is_required(self, seeded):
        with pytest.raises(ServiceError, match="required"):
            seeded.search_containers("")
        with pytest.raises(ServiceError, match="required"):
            seeded.search_containers("   ")

    def test_a_row_is_identity_and_place_only(self, seeded):
        rows = seeded.search_containers("Rack")["rows"]
        assert {r["label"] for r in rows} == {"Rack 3", "Rack C8"}
        for r in rows:
            assert set(r) == {"container_id", "kind", "label", "parent_id", "path"}

    def test_matches_on_id_as_well_as_label(self, seeded):
        cid = seeded.search_containers("Shelf 3")["rows"][0]["container_id"]
        again = seeded.search_containers(cid.upper())["rows"]
        assert [r["container_id"] for r in again] == [cid]

    def test_a_container_holding_nothing_is_still_found(self, seeded):
        """Nothing sits directly in the freezer; that must not make it invisible."""
        rows = seeded.search_containers("Freezer")["rows"]
        assert [r["label"] for r in rows] == ["-80 Freezer #7085"]
        assert seeded.get_contents(rows[0]["container_id"])["items"] == []

    def test_kind_narrows_and_an_unknown_kind_is_refused(self, seeded):
        everything = seeded.search_containers("ctr_")["matched"]
        expect = len(seeded.list_containers(kind="box"))
        boxes = seeded.search_containers("ctr_", kind="box")
        assert expect > 0
        assert boxes["matched"] == expect < everything == self._total(seeded)
        assert {r["kind"] for r in boxes["rows"]} == {"box"}
        with pytest.raises(ServiceError, match="not a container kind"):
            seeded.search_containers("ctr_", kind="freezr")

    def test_within_id_walks_the_subtree_and_excludes_the_scope(self, seeded):
        freezer = seeded.search_containers("Freezer")["rows"][0]["container_id"]
        out = seeded.search_containers("ctr_", within_id=freezer)
        assert {r["label"] for r in out["rows"]} == {
            "Shelf 3", "Rack 3", "-70C Misc", "VEXAS DNA Box 1"}
        assert freezer not in {r["container_id"] for r in out["rows"]}

    def test_within_id_combines_with_kind(self, seeded):
        freezer = seeded.search_containers("Freezer")["rows"][0]["container_id"]
        rows = seeded.search_containers("ctr_", within_id=freezer, kind="box")["rows"]
        assert {r["label"] for r in rows} == {"-70C Misc", "VEXAS DNA Box 1"}
        assert all(r["parent_id"] != freezer for r in rows)  # found at depth, not direct

    def test_within_id_must_name_a_container(self, seeded):
        with pytest.raises(NotFound):
            seeded.search_containers("ctr_", within_id="ctr_nosuchid")

    def test_limit_caps_rows_and_says_so(self, seeded):
        total = self._total(seeded)
        out = seeded.search_containers("ctr_", limit=2)
        assert len(out["rows"]) == 2
        assert out["matched"] == total and out["truncated"] is True
        whole = seeded.search_containers("ctr_")
        assert whole["matched"] == total and whole["truncated"] is False

    def test_rows_arrive_in_tree_order(self, seeded):
        rows = seeded.search_containers("ctr_")["rows"]
        assert [r["label"] for r in rows[:2]] == ["-80 Freezer #7085", "Shelf 3"]


class TestContents:
    """get_contents: lean rows for what is physically inside a container, tubes or boxes."""

    @staticmethod
    def _box(seeded, label):
        return next(c["container_id"] for c in seeded.list_containers(kind="box")
                    if c["label"] == label)

    def test_direct_contents_are_lean_and_counted(self, seeded):
        box = self._box(seeded, "-70C Misc")
        out = seeded.get_contents(box)
        assert [i["position"] for i in out["items"]] == ["A1", "C4"]
        assert out["count"] == 2
        assert out["container"]["capacity"] == 81
        assert out["container"]["free"] == 79
        assert all("in" not in i for i in out["items"])  # no padding when not needed
        assert set(out["items"][0]) == {"item_id", "class", "name", "status", "position"}
        assert out["containers"] == []

    def test_nested_containers_are_rows_and_are_counted(self, seeded):
        rack = next(c["container_id"] for c in seeded.list_containers(kind="rack")
                    if c["label"] == "Rack 3")
        out = seeded.get_contents(rack)
        assert [c["label"] for c in out["containers"]] == ["-70C Misc", "VEXAS DNA Box 1"]
        assert out["items"] == [] and out["count"] == 2  # empty of tubes, not empty
        # identity only: how full each box is belongs to get_occupancy
        assert all(set(c) == {"container_id", "kind", "label"} for c in out["containers"])

    def test_a_mixed_tier_counts_both_kinds_against_capacity(self, seeded):
        box = self._box(seeded, "-70C Misc")
        before = seeded.get_contents(box)
        assert before["count"] == 2 and before["container"]["free"] == 79
        seeded.create_box("Nested tube rack", rows=2, cols=2, parent_id=box)
        after = seeded.get_contents(box)
        assert [c["label"] for c in after["containers"]] == ["Nested tube rack"]
        assert after["count"] == before["count"] + 1
        assert after["count"] == len(after["items"]) + len(after["containers"])
        assert after["container"]["free"] == 81 - after["count"]

    def test_only_the_direct_layer_is_listed(self, seeded):
        """A freezer lists its shelf and no tubes: the tubes live in boxes below."""
        freezer = next(c["container_id"] for c in seeded.list_containers(kind="freezer")
                       if c["label"] == "-80 Freezer #7085")
        out = seeded.get_contents(freezer)
        assert out["items"] == []
        assert [c["label"] for c in out["containers"]] == ["Shelf 3"]
        assert out["count"] == 1  # reporting 0 here would read as an empty freezer
        assert "recursive" not in out  # the subtree walk is search_containers' job now

    def test_consumed_items_vanish_from_their_old_box(self, seeded):
        box = self._box(seeded, "-70C Misc")
        victim = seeded.get_contents(box)["items"][0]["item_id"]
        seeded.set_status(victim, "consumed")
        assert victim not in {i["item_id"] for i in seeded.get_contents(box)["items"]}

    def test_tombstoned_items_are_excluded(self, seeded):
        box = self._box(seeded, "VEXAS DNA Box 1")
        before = seeded.get_contents(box)["count"]
        victim = seeded.get_contents(box)["items"][0]["item_id"]
        seeded.delete_item(victim)
        assert seeded.get_contents(box)["count"] == before - 1

    def test_unknown_container_is_not_found(self, seeded):
        with pytest.raises(NotFound):
            seeded.get_contents("ctr_nosuchid")


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
        victim = seeded.get_contents(box)["items"][0]["item_id"]
        seeded.delete_item(victim)
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
        anywhere = next(p["project_id"] for p in seeded.db.t("projects").live())
        seeded.create_patient("Zz Nobody", anywhere)
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


class TestSearchProjects:
    def test_a_query_is_required(self, seeded):
        for blank in ("", "   "):
            with pytest.raises(ServiceError, match="query is required"):
                seeded.search_projects(query=blank)

    def test_names_match_case_insensitively(self, seeded):
        rows = seeded.search_projects(query="circa")["rows"]
        assert [r["label"] for r in rows] == ["CIRCA somatic"]

    def test_ids_match_too(self, seeded):
        everyone = seeded.search_projects(query="prj_", limit=100)["rows"]
        pid = next(r["key"] for r in everyone if r["samples"]["total"])
        rows = seeded.search_projects(query=pid[4:8])["rows"]
        assert [r["key"] for r in rows] == [pid]

    def test_every_sample_is_counted_exactly_once(self, seeded):
        """Direct counts only: a parent must not absorb its subprojects, or summing
        the rows double-counts whatever hangs below. An unfiled tube is added so the
        identity spans both buckets rather than resting on an empty residue."""
        seeded.create_sample("Orphan tube", sample_type="dna")
        out = seeded.search_projects(query="prj_", limit=100)
        counted = sum(r["samples"]["total"] for r in out["rows"]) + out["unassigned_samples"]
        assert counted == seeded.count_items(item_class="sample")
        assert out["unassigned_samples"] > 0, "the fixture plus the new tube should leave a residue"

    def test_supplies_are_absent_rather_than_pooled_into_a_fake_project(self, seeded):
        """The old rollup filed every reagent, kit and consumable under a bucket named
        "(none)", so a project total meant two different things at once."""
        out = seeded.search_projects(query="prj_", limit=100)
        assert all(r["key"].startswith("prj_") for r in out["rows"])
        assert "(none)" not in {r["label"] for r in out["rows"]}
        assert seeded.count_items() > sum(
            r["samples"]["total"] for r in out["rows"]) + out["unassigned_samples"], \
            "fixture should hold supplies that no project can claim"

    def test_subprojects_are_reported_not_absorbed(self, seeded):
        projects = seeded.db.t("projects").live()
        kids = [p for p in projects
                if (p.get("parent_project_id", "") or "").strip()]
        assert kids, "fixture should nest at least one project"
        rows = {r["key"]: r for r in
                seeded.search_projects(query="prj_", limit=100)["rows"]}
        for parent_id in {k["parent_project_id"] for k in kids}:
            expected = sum(1 for k in kids if k["parent_project_id"] == parent_id)
            assert rows[parent_id]["subprojects"] == expected

    def test_enrolled_participants_are_counted_per_project(self, seeded):
        expected: dict[str, set] = {}
        for link in seeded.db.t("patient_projects").live():
            expected.setdefault(link["project_id"], set()).add(link["patient_id"])
        assert any(expected.values()), "fixture should enrol someone somewhere"
        rows = seeded.search_projects(query="prj_", limit=100)["rows"]
        assert all(r["participants"] == len(expected.get(r["key"], ())) for r in rows)

    def test_a_project_with_nothing_attached_still_appears(self, seeded):
        """'Not started yet' and 'no such project' must not arrive as the same answer."""
        seeded.create_project("Zzz Nothing Yet")
        rows = seeded.search_projects(query="zzz nothing")["rows"]
        assert [r["label"] for r in rows] == ["Zzz Nothing Yet"]
        assert rows[0]["samples"]["total"] == 0
        assert rows[0]["participants"] == 0
        assert rows[0]["subprojects"] == 0

    def test_heaviest_projects_come_first(self, seeded):
        totals = [r["samples"]["total"] for r in
                  seeded.search_projects(query="prj_", limit=100)["rows"]]
        assert totals == sorted(totals, reverse=True)

    def test_unassigned_samples_sit_beside_the_rows_and_survive_the_limit(self, seeded):
        before = seeded.search_projects(query="prj_", limit=100)["unassigned_samples"]
        seeded.create_sample("Orphan tube", sample_type="dna")
        out = seeded.search_projects(query="prj_", limit=1)
        assert out["unassigned_samples"] == before + 1
        assert all(r["key"].startswith("prj_") for r in out["rows"])
        assert len(out["rows"]) == 1 and out["matched"] > 1 and out["truncated"] is True
