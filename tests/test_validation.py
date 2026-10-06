"""Validation rules, exercised by corrupting files on disk.

These tests deliberately write bad CSVs behind the service's back, because that
is how the corruption actually arrives: someone opened a file in a spreadsheet.
"""

from __future__ import annotations

import pytest

from conftest import read_rows, table_path, write_rows
from inventory_mcp import schema, validation


def codes(violations) -> set[str]:
    return {v.code for v in violations}


class TestShapeRules:
    def test_missing_primary_key(self, svc, root):
        path = table_path(root, "patients")
        rows = [{"patient_id": "", "label": "nobody"}]
        write_rows(path, rows, list(schema.PATIENTS.column_names))
        assert "missing_pk" in codes(svc.reload().violations)

    def test_duplicate_primary_key(self, svc, root):
        header = list(schema.PATIENTS.column_names)
        write_rows(
            table_path(root, "patients"),
            [{"patient_id": "pat_aaaaaaaa", "label": "one"},
             {"patient_id": "pat_aaaaaaaa", "label": "two"}],
            header,
        )
        assert "duplicate_pk" in codes(svc.reload().violations)

    def test_hand_made_id_is_rejected(self, svc, root):
        header = list(schema.PATIENTS.column_names)
        write_rows(table_path(root, "patients"),
                   [{"patient_id": "PATIENT-7", "label": "hand made"}], header)
        assert "malformed_id" in codes(svc.reload().violations)

    def test_unknown_column_warns_without_losing_data(self, seeded):
        path = table_path(seeded.local_root(), "patients")
        rows = read_rows(path)
        assert rows, "fixture must have rows for this to mean anything"
        header = list(schema.PATIENTS.column_names) + ["notes_backup"]
        for r in rows:
            r["notes_backup"] = "important thing I typed in Excel"
        write_rows(path, rows, header)
        db, violations = seeded.reload().db, seeded.violations
        assert "unknown_column" in codes(violations)
        # the value must survive rather than being dropped on the next write
        assert all(v.get("notes_backup") for v in db.t("patients").rows)

    def test_enum_violation(self, svc, seeded):
        from inventory_mcp.service import ValidationError

        items = seeded.db.t("items")
        row = dict(items.rows[0])
        row["status"] = "sort_of_consumed"
        with pytest.raises(ValidationError) as e:
            seeded._apply("test", [("items", [], [row])])
        assert "bad_enum" in codes(e.value.violations)


class TestRelationalRules:
    def test_dangling_foreign_key(self, svc, root):
        header = list(schema.ITEMS.column_names)
        write_rows(table_path(root, "containers"), [], list(schema.CONTAINERS.column_names))
        write_rows(
            table_path(root, "items"),
            [{"item_id": "smp_aaaaaaaa", "class": "sample", "name": "orphan",
              "status": "present", "location_id": "ctr_zzzzzzzz"}],
            header,
        )
        write_rows(table_path(root, "samples"),
                   [{"item_id": "smp_aaaaaaaa", "sample_type": "dna"}],
                   list(schema.SAMPLES.column_names))
        assert "fk_dangling" in codes(svc.reload().violations)

    def test_human_label_in_a_foreign_key_gets_a_hint(self, svc, root):
        write_rows(table_path(root, "containers"), [], list(schema.CONTAINERS.column_names))
        write_rows(
            table_path(root, "items"),
            [{"item_id": "smp_aaaaaaaa", "class": "sample", "name": "x",
              "status": "present", "location_id": "-70C Misc"}],
            list(schema.ITEMS.column_names),
        )
        write_rows(table_path(root, "samples"),
                   [{"item_id": "smp_aaaaaaaa", "sample_type": "dna"}],
                   list(schema.SAMPLES.column_names))
        violations = [v for v in svc.reload().violations if v.field == "location_id"]
        assert violations and "human label" in violations[0].message

    def test_extension_row_without_supertype(self, svc, root):
        write_rows(table_path(root, "samples"),
                   [{"item_id": "smp_bbbbbbbb", "sample_type": "dna"}],
                   list(schema.SAMPLES.column_names))
        assert "orphan_extension" in codes(svc.reload().violations)

    def test_item_without_extension_row(self, svc, root):
        write_rows(table_path(root, "samples"), [], list(schema.SAMPLES.column_names))
        write_rows(
            table_path(root, "items"),
            [{"item_id": "smp_cccccccc", "class": "sample", "name": "no detail row",
              "status": "present"}],
            list(schema.ITEMS.column_names),
        )
        assert "missing_extension" in codes(svc.reload().violations)

    def test_class_and_id_prefix_must_agree(self, svc, root):
        write_rows(
            table_path(root, "items"),
            [{"item_id": "kit_aaaaaaaa", "class": "sample", "name": "wrong prefix",
              "status": "present"}],
            list(schema.ITEMS.column_names),
        )
        write_rows(table_path(root, "samples"),
                   [{"item_id": "kit_aaaaaaaa", "sample_type": "dna"}],
                   list(schema.SAMPLES.column_names))
        assert "class_prefix_mismatch" in codes(svc.reload().violations)


class TestSpatialRules:
    def test_double_booked_well(self, svc, seeded):
        box = next(c["container_id"] for c in seeded.list_containers(kind="box"))
        items = seeded.db.items_by_id()
        target = next(i for i, r in items.items() if r.get("position"))
        other = next(i for i, r in items.items()
                     if i != target and not r.get("position") and r.get("status") != "consumed")
        from inventory_mcp.service import ValidationError

        with pytest.raises(ValidationError) as e:
            seeded.move_item(other, location_id=box, position=items[target]["position"])
        assert "double_booked_well" in codes(e.value.violations)

    def test_position_outside_declared_geometry(self, svc, seeded):
        box = next(c for c in seeded.list_containers(kind="box") if c["geometry"] == "9x9")
        sample = next(i["item_id"] for i in seeded.search_items(item_class="sample")
                      if not i["location"]["position"])
        from inventory_mcp.service import ValidationError

        with pytest.raises(ValidationError) as e:
            seeded.move_item(sample, location_id=box["container_id"], position="Z20")
        assert "bad_position" in codes(e.value.violations)

    def test_custom_row_labels_are_honoured(self, seeded):
        box = next(c for c in seeded.list_containers(kind="box") if c["geometry"] == "12x8")
        # the 12-row box skips 'I' per its override, so I must be invalid and J valid
        container = seeded.db.containers_by_id()[box["container_id"]]
        assert validation.split_position("J5") == ("J", "5")
        problem = validation.validate_position("I5", container)
        assert problem and "does not exist" in problem
        assert validation.validate_position("M8", container) is None

    def test_container_cycle_detected(self, svc, root):
        write_rows(
            table_path(root, "containers"),
            [{"container_id": "ctr_aaaaaaaa", "parent_id": "ctr_bbbbbbbb",
              "kind": "freezer", "label": "a"},
             {"container_id": "ctr_bbbbbbbb", "parent_id": "ctr_aaaaaaaa",
              "kind": "freezer", "label": "b"}],
            list(schema.CONTAINERS.column_names),
        )
        assert "cycle" in codes(svc.reload().violations)

    def test_over_capacity(self, svc, root):
        write_rows(table_path(root, "containers"),
                   [{"container_id": "ctr_aaaaaaaa", "kind": "box", "label": "tiny",
                     "rows": "1", "cols": "1"}],
                   list(schema.CONTAINERS.column_names))
        write_rows(
            table_path(root, "items"),
            [{"item_id": "smp_aaaaaaaa", "class": "sample", "name": "one",
              "status": "present", "location_id": "ctr_aaaaaaaa"},
             {"item_id": "smp_bbbbbbbb", "class": "sample", "name": "two",
              "status": "present", "location_id": "ctr_aaaaaaaa"}],
            list(schema.ITEMS.column_names),
        )
        write_rows(table_path(root, "samples"),
                   [{"item_id": "smp_aaaaaaaa", "sample_type": "dna"},
                    {"item_id": "smp_bbbbbbbb", "sample_type": "dna"}],
                   list(schema.SAMPLES.column_names))
        assert "over_capacity" in codes(svc.reload().violations)


class TestBusinessRules:
    def test_consumed_item_cannot_hold_a_slot(self, seeded):
        item = next(i["item_id"] for i in seeded.search_items(status="present")
                    if i["location"]["container_id"])
        seeded.set_status(item, "consumed")
        stored = seeded.db.items_by_id()[item]
        assert stored["location_id"] == "" and stored["position"] == ""

    def test_project_outside_the_patient_enrolments_blocks_write(self, seeded):
        # HC is enrolled in CIRCA somatic only. GeneAdd registry is an unrelated
        # root project, so a HC sample filed there must be refused. (VEXAS would
        # not be: it is a child of CIRCA, covered by the ancestor rule below.)
        hc = next(p["patient_id"] for p in seeded.db.t("patients").rows
                  if p["label"] == "HC_99801143")
        geneadd = next(p["project_id"] for p in seeded.db.t("projects").rows
                       if p["name"] == "GeneAdd registry")
        from inventory_mcp.service import ValidationError

        with pytest.raises(ValidationError) as e:
            seeded.create_sample("mislabelled", "dna", patient_id=hc, project_id=geneadd)
        assert "project_not_in_patient_projects" in codes(e.value.violations)

    def test_membership_at_an_ancestor_satisfies_the_rule(self, seeded):
        hc = next(p["patient_id"] for p in seeded.db.t("patients").rows
                  if p["label"] == "HC_99801143")
        circa = next(p["project_id"] for p in seeded.db.t("projects").rows
                     if p["name"] == "CIRCA somatic")
        stat5b = next(p["project_id"] for p in seeded.db.t("projects").rows
                      if p["name"] == "STAT5B/KRAS")
        # JaCo sits under STAT5B/KRAS whose parent CIRCA is where she is enrolled
        jacobs = next(s["patient_id"] for s in seeded.db.t("samples").rows
                      if s.get("patient_id"))
        seeded.create_sample("fine", "dna", patient_id=jacobs, project_id=stat5b)
        seeded.create_sample("also fine", "dna", patient_id=hc, project_id=circa)
        assert seeded.doctor()["error_count"] == 0

    def test_ambiguous_date_is_reported_not_guessed(self, svc, root):
        write_rows(table_path(root, "patients"),
                   [{"patient_id": "pat_aaaaaaaa", "label": "x", "dob": "03/04/1980"}],
                   list(schema.PATIENTS.column_names))
        violations = [v for v in svc.reload().violations if v.code == "bad_date"]
        assert violations and "ambiguous" in violations[0].message

    def test_iso_dates_and_dayfirst_unambiguous_are_accepted(self, svc, root):
        write_rows(table_path(root, "patients"),
                   [{"patient_id": "pat_aaaaaaaa", "label": "x", "dob": "1980-04-03"},
                    {"patient_id": "pat_bbbbbbbb", "label": "y", "dob": "23/04/1980"}],
                   list(schema.PATIENTS.column_names))
        assert not [v for v in svc.reload().violations if v.code == "bad_date"]

    def test_lineage_cycle_detected(self, svc, root):
        items = [
            {"item_id": "smp_aaaaaaaa", "class": "sample", "name": "a", "status": "present"},
            {"item_id": "smp_bbbbbbbb", "class": "sample", "name": "b", "status": "present"},
        ]
        write_rows(table_path(root, "items"), items, list(schema.ITEMS.column_names))
        write_rows(table_path(root, "samples"),
                   [{"item_id": "smp_aaaaaaaa", "sample_type": "dna"},
                    {"item_id": "smp_bbbbbbbb", "sample_type": "dna"}],
                   list(schema.SAMPLES.column_names))
        write_rows(
            table_path(root, "item_parents"),
            [{"link_id": "lin_aaaaaaaa", "child_item_id": "smp_aaaaaaaa",
              "parent_item_id": "smp_bbbbbbbb", "role": "aliquot_of"},
             {"link_id": "lin_bbbbbbbb", "child_item_id": "smp_bbbbbbbb",
              "parent_item_id": "smp_aaaaaaaa", "role": "aliquot_of"}],
            list(schema.ITEM_PARENTS.column_names),
        )
        assert "lineage_cycle" in codes(svc.reload().violations)
