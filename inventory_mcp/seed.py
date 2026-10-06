"""Realistic development seed data.

Built by calling the service, not by writing files -- so a bug in the write path
shows up as a seed failure, and the fixture is a live example of every rule.
The content mirrors the shape of a real sample inventory: freezers and liquid
nitrogen shippers, boxes of mixed geometry, lineage chains from blood to DNA,
and reagents with quantities that decrease over time.
"""

from __future__ import annotations

from . import schema
from .service import Service


def seed(svc: Service) -> dict:
    # -- assays ---------------------------------------------------------------
    assays = [
        ("qubit_dsdna", "Qubit dsDNA HS concentration", "concentration_mass", "ng/ul", "numeric",
         "fluorometric, dsDNA-selective"),
        ("nanodrop_a260", "Nanodrop A260 concentration", "concentration_mass", "ng/ul", "numeric",
         "absorbance; over-reads on RNA-contaminated DNA"),
        ("tapeguard", "TapeGuard fragment analysis", "length", "bp", "numeric", "capillary electrophoresis"),
        ("volume_ul", "Measured volume", "volume", "ul", "numeric", "pipette or tube reading"),
        ("cell_count", "Viable cell count", "cell_count", "cells", "numeric", "haemocytometer"),
        ("cell_viability", "Trypan blue viability", "viability", "%", "numeric", ""),
        ("kit_uses_remaining", "Kit uses remaining", "count", "count", "numeric", ""),
        ("reagent_volume", "Reagent volume remaining", "volume", "ul", "numeric", ""),
        ("gel_qc", "Agarose gel QC", "other", "n/a", "text", "qualitative assessment"),
    ]
    for code, name, quantity, unit, rtype, method in assays:
        svc.create_assay(code=code, name=name, quantity=quantity, canonical_unit=unit,
                         result_type=rtype, method=method)

    # -- containers -----------------------------------------------------------
    freezer7085 = svc.create_container("freezer", "-80 Freezer #7085").records or None
    f7085 = _id(freezer7085)
    shelf3 = _id(svc.create_container("shelf", "Shelf 3", parent_id=f7085).records)
    rack3 = _id(svc.create_container("rack", "Rack 3", parent_id=shelf3).records)

    ln2_vp4 = _id(svc.create_container("ln2_shipper", "Liquid Nitrogen VP4").records)
    rack_c8 = _id(svc.create_container("rack", "Rack C8", parent_id=ln2_vp4).records)

    box_misc = _id(svc.create_box("-70C Misc", 9, 9, parent_id=rack3,
                                  notes="Labelled 'GML Etienne' on the side").records)
    box_vexas = _id(svc.create_box("VEXAS DNA Box 1", 10, 10, parent_id=rack3).records)
    # an odd-geometry box: 12 rows labelled A-L, 8 columns, to exercise overrides
    box_pbmc = _id(svc.create_box("A5 ARBC PBMCs", 12, 8, parent_id=rack_c8,
                                  row_labels="A,B,C,D,E,F,G,H,J,K,L,M",
                                  notes="Bottom two rows hold 1.8ml vials only").records)

    fridge = _id(svc.create_container("fridge", "4C Fridge #100664").records)
    bench = _id(svc.create_container("bench", "Bench 100666 (temporary)", notes="Not for overnight storage").records)

    # -- projects -------------------------------------------------------------
    circa = _id(svc.create_project("CIRCA somatic", status="active",
                                   description="Somatic mutation cohort").records)
    vexas = _id(svc.create_project("VEXAS", status="active", parent_project_id=circa).records)
    stat5b = _id(svc.create_project("STAT5B/KRAS", status="delivered", parent_project_id=circa).records)
    geneadd = _id(svc.create_project("GeneAdd registry", status="active").records)

    # -- patients -------------------------------------------------------------
    jacobs = _id(svc.create_patient("JaCo", sex="female", dob="2005-08-20",
                                    affected_status="affected", gene="STAT5B",
                                    variant="T628S", protocol="HREC 2022/041").records)
    shno = _id(svc.create_patient("ShNo", sex="male", dob="1961-04-28",
                                  affected_status="affected", gene="UBA1",
                                  protocol="HREC 2022/041").records)
    hc = _id(svc.create_patient("HC_99801143", sex="female", affected_status="unaffected",
                                protocol="HREC 2020/118").records)
    svc.assign_patient_project(jacobs, circa)
    svc.assign_patient_project(shno, circa)
    svc.assign_patient_project(shno, vexas)
    svc.assign_patient_project(hc, circa)

    # -- samples with lineage -------------------------------------------------
    blood = _id(svc.create_sample(
        "JaCo whole blood 2024-11-13", "whole_blood", location_id=box_misc, position="A1",
        gml_id="GML001631", patient_id=jacobs, project_id=stat5b,
        source_material="Venous blood", collected_on="2023-11-13", received_on="2023-11-14",
        processed_on="2023-11-14", handler="Julia",
        notes="Tube provided to Igor for HMW DNA extraction").records)
    _track(svc.record_measurement(blood, "volume_ul", value="100", unit="ul",
                                  measured_on="2023-11-14T09:00"))

    pbmc = _id(svc.create_sample(
        "JaCo PBMC", "pbmc", location_id=box_pbmc, position="B3",
        gml_id="GML000487", patient_id=jacobs, project_id=stat5b,
        source_material="Venous blood", collected_on="2022-03-03", handler="AMR").records)
    svc.link_parent(pbmc, blood, "derived_from", "PBMC isolation from the 2022 draw")
    _track(svc.record_measurement(pbmc, "cell_count", value="5.3", unit="",
                                  measured_on="2022-03-03T10:00", notes="x10^6 per ml"))
    _track(svc.record_measurement(pbmc, "cell_viability", value="94", unit="%",
                                  measured_on="2022-03-03T10:05"))

    dna = _id(svc.create_sample(
        "JaCo genomic DNA", "dna", location_id=box_misc, position="C4",
        gml_id="GML000495", patient_id=jacobs, project_id=stat5b,
        source_material="PBMC", handler="Julia").records)
    svc.link_parent(dna, pbmc, "extracted_from")
    _track(svc.record_measurement(dna, "qubit_dsdna", value="49.0", unit="ng/ul",
                                  measured_on="2024-05-16T11:00", instrument="Qubit 4"))
    _track(svc.record_measurement(dna, "volume_ul", value="40", unit="ul",
                                  measured_on="2024-05-16T11:05"))
    # Nanodrop read, then a correction appended rather than edited in place --
    # measurements are immutable, so the original stays visible.
    first_read = svc.record_measurement(dna, "nanodrop_a260", value="132.0", unit="ng/ul",
                                        measured_on="2024-05-16T11:20")
    svc.correct_measurement(
        first_read.records[-1]["measurement_id"],
        value="88.5", unit="ng/ul", measured_on="2024-05-17T09:00",
        notes="re-read after dilution error",
    )

    aliquot = _id(svc.create_sample(
        "JaCo DNA aliquot 1", "dna", location_id=box_vexas, position="D7",
        gml_id="GML001994", patient_id=jacobs, project_id=stat5b,
        source_material="genomic DNA", handler="Julia").records)
    svc.link_parent(aliquot, dna, "aliquot_of")
    _track(svc.record_measurement(aliquot, "qubit_dsdna", value="47.2", unit="ng/ul",
                                  measured_on="2024-06-01T09:00"))
    _track(svc.record_measurement(aliquot, "volume_ul", value="20", unit="ul",
                                  measured_on="2024-06-01T09:05"))

    # statuses across the board
    consumed = _id(svc.create_sample(
        "GeneAdd S00366F DNA (exhausted)", "dna", gml_id="GML001996",
        project_id=geneadd, source_material="Venous blood",
        notes="Fully consumed during library prep", status="consumed").records)
    _track(svc.record_measurement(consumed, "qubit_dsdna", value="96.0", unit="ng/ul",
                                  measured_on="2024-04-22T14:00"))

    partial = _id(svc.create_sample(
        "ShNo plasma", "plasma", location_id=box_vexas, position="E2",
        gml_id="GML001644", patient_id=shno, project_id=vexas,
        source_material="Venous blood", status="partially_consumed").records)
    _track(svc.record_measurement(partial, "volume_ul", value="1500", unit="ul",
                                  measured_on="2023-12-01T10:00"))
    _track(svc.record_measurement(partial, "volume_ul", value="1200", unit="ul",
                                  measured_on="2024-02-10T10:00",
                                  notes="300 ul taken for methylation assay"))

    unlocated = _id(svc.create_sample(
        "EmSn plasma (location uncertain)", "plasma", gml_id="GML001632",
        patient_id=None, project_id=circa,
        notes="Not located in -70 Misc; may have been consumed. Recorded as present with no slot.").records)

    healthy = _id(svc.create_sample(
        "HC_99801143 PBMC", "pbmc", location_id=box_pbmc, position="A1",
        gml_id="GML000664", patient_id=hc, project_id=circa,
        source_material="Venous blood", handler="AMR").records)
    svc.link_parent(healthy, _id(svc.create_sample(
        "HC_99801143 whole blood", "whole_blood", location_id=bench,
        gml_id="GML000663", patient_id=hc, project_id=circa,
        notes="temporary bench storage, move to LN2").records), "derived_from")

    # -- supplies -------------------------------------------------------------
    # The opening quantity carries an explicit timestamp rather than "now" so the
    # time series reads in registration order no matter when the seed is run.
    enzyme = _id(svc.create_supply(
        "reagent", "Q5 High-Fidelity Polymerase", location_id=fridge,
        price="312.50", expires_on="2026-03-31",
        storage_requirements="-20C, avoid repeated freeze/thaw",
        initial_quantity="500", quantity_assay="reagent_volume", quantity_unit="ul",
        quantity_measured_on="2026-08-04T10:00").records)
    _track(svc.record_measurement(enzyme, "reagent_volume", value="420", unit="ul",
                                  measured_on="2026-09-12T15:00",
                                  notes="80 ul used for the STAT5B amplicon panel"))

    kit = _id(svc.create_supply(
        "kit", "NEBNext Ultra II DNA Library Prep", location_id=fridge,
        price="845.00", expires_on="2027-01-15",
        storage_requirements="Store all components at -20C; equilibration at room temp before use",
        initial_quantity="48", quantity_assay="kit_uses_remaining", quantity_unit="count",
        quantity_measured_on="2026-09-02T09:00").records)
    _track(svc.record_measurement(kit, "kit_uses_remaining", value="41", unit="count",
                                  measured_on="2026-10-01T09:30"))

    tips = _id(svc.create_supply(
        "consumable", "200 ul filtered tips, sterile", location_id=bench,
        storage_requirements="Room temperature, autoclaved racks",
        initial_quantity="96", quantity_assay="kit_uses_remaining", quantity_unit="count",
        quantity_measured_on="2026-09-20T11:00").records)

    qc = _id(svc.create_sample("JaCo DNA post-library QC", "library", location_id=box_vexas,
                               position="F1", gml_id="GML002001", patient_id=jacobs,
                               project_id=stat5b, source_material="genomic DNA").records)
    svc.record_measurement(qc, "gel_qc", text="single high-molecular-weight band, no smearing",
                           measured_on="2024-06-05T13:00", handler="Julia")

    # Counted from what landed rather than tallied as we go; a hand-maintained
    # tally drifts silently (it claimed 9 samples while writing 10) and this
    # number is printed back to whoever runs `init --seed`.
    return {t.name: len(svc.db.t(t.name).rows) for t in schema.TABLES}


def _id(records):
    if not records:
        raise RuntimeError("expected a record back from the service")
    r = records[0]
    for key in ("item_id", "container_id", "patient_id", "project_id", "assay_id"):
        if key in r:
            return r[key]
    raise RuntimeError(f"could not find an id in {r!r}")


def _track(result):
    return result


def _last_measurement_id(svc: Service, item_id: str, assay_code: str):
    rows = svc.get_measurements(item_id=item_id, assay_code=assay_code)
    return rows[-1]["measurement_id"] if rows else None
