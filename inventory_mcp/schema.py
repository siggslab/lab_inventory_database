"""Table definitions for the inventory database.

This module is the single source of truth for the on-disk layout. Everything
else -- validation, serialisation, exports, the MCP tool catalogue, the
generated schema reference -- reads from here.

Design notes worth keeping in mind while editing:

* ``items`` is a supertype; every stored thing (sample, reagent, kit,
  consumable) has exactly one row here carrying the fields that are universal
  (name, status, location). Class-specific fields live in an extension table
  keyed by the same id. This keeps each file narrow and diffable instead of
  regrowing the 33-column mastersheet we migrated away from.
* Ids are opaque surrogates. Human-facing labels (box names, GML sample ids)
  are mutable attributes and never keys.
* Every field here is a string on disk. Types below drive parsing, validation
  and export typing -- not storage.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class Field:
    name: str
    type: str  # str | text | int | float | date | datetime | bool | id
    required: bool = False
    fk: Optional[str] = None  # "<table>.<column>"
    enum: tuple[str, ...] = ()
    doc: str = ""

    @property
    def fk_table(self) -> Optional[str]:
        return self.fk.split(".")[0] if self.fk else None


@dataclass(frozen=True)
class Table:
    name: str
    filename: str
    primary_key: str
    id_prefix: Optional[str]  # None for join tables with synthetic keys
    doc: str
    fields: tuple[Field, ...]
    supertype: Optional[str] = None  # extension tables point at their supertype

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(f.name for f in self.fields)

    def field(self, name: str) -> Optional[Field]:
        for f in self.fields:
            if f.name == name:
                return f
        return None


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

CONTAINER_KINDS = (
    "room",
    "freezer",
    "fridge",
    "ln2_tank",
    "ln2_shipper",
    "shelf",
    "rack",
    "box",
    "bench",
    "other",
)

ITEM_CLASSES = ("sample", "reagent", "kit", "consumable")

#: Supplies rather than biological material -- the classes ``create_supply`` can make.
SUPPLY_CLASSES = tuple(c for c in ITEM_CLASSES if c != "sample")

#: Extension table backing each item class.
CLASS_EXTENSION_TABLE = {
    "sample": "samples",
    "reagent": "reagents",
    "kit": "kits",
    "consumable": "consumables",
}

#: Id prefix minted for each class. Aids humans; validated, but never *parsed* --
#  the class column remains authoritative in case an id is ever re-minted.
ITEM_CLASS_PREFIX = {
    "sample": "smp",
    "reagent": "rgt",
    "kit": "kit",
    "consumable": "csm",
}

ITEM_STATUSES = ("present", "partially_consumed", "consumed")

SAMPLE_TYPES = (
    "whole_blood",
    "pbmc",
    "serum",
    "plasma",
    "tissue",
    "bone_marrow",
    "dna",
    "rna",
    "mrna",
    "library",
    "cell_stock",
    "sputum",
    "saliva",
    "buccal",
    "other",
)

PROJECT_STATUSES = ("active", "delivered", "concluded", "on_hold")

SEXES = ("female", "male", "other", "unknown")

AFFECTED_STATUSES = ("affected", "unaffected", "unknown", "cell_line")

RESULT_TYPES = ("numeric", "text")

QUANTITIES = (
    "volume",
    "concentration_mass",
    "concentration_molar",
    "amount",
    "mass",
    "cell_count",
    "viability",
    "temperature",
    "length",
    "ratio",
    "count",
    "other",
)

LINK_ROLES = ("aliquot_of", "extracted_from", "pooled_into", "derived_from", "other")


# ---------------------------------------------------------------------------
# Shared field groups
# ---------------------------------------------------------------------------

_AUDIT_FIELDS = (
    Field("updated_by", "str", doc="Account written by the server, not user-supplied."),
    Field("updated_at", "datetime", doc="Written by the server, not user-supplied. UTC with a trailing Z."),
)

# Supply classes (reagent/kit/consumable) share this shape today. They stay as
# separate tables so divergence has somewhere to go without sparse columns.
_SUPPLY_FIELDS = (
    Field("item_id", "id", required=True, fk="items.item_id", doc="Same id as the items row."),
    Field("price", "float", doc="Purchase price in lab base currency."),
    Field("expires_on", "date", doc="Manufacturer expiry."),
    Field("storage_requirements", "text", doc="Free-text handling note, e.g. 'Aliquot and store at -20C, avoid freeze/three cycles'."),
)


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------

CONTAINERS = Table(
    name="containers",
    filename="containers.csv",
    primary_key="container_id",
    id_prefix="ctr",
    doc=(
        "Storage locations as a variable-depth tree. A freezer contains shelves, "
        "a rack holds boxes, a box holds items. Depth is not fixed: a bench or a "
        "liquid-nitrogen shipper may hang items directly. Every container holds "
        "slots, and a slot is filled by one thing -- a tube or a nested container. "
        "Containers know only where things are; no scientific data belongs here."
    ),
    fields=(
        Field("container_id", "id", required=True),
        Field("parent_id", "id", fk="containers.container_id", doc="Empty for a top-level container."),
        Field("kind", "str", required=True, enum=CONTAINER_KINDS),
        Field("label", "str", required=True, doc="Human-readable name. Mutable, not a key."),
        Field("rows", "int", doc="Grid rows. Wells only -- leave empty for furniture."),
        Field("cols", "int", doc="Grid columns. Wells only -- leave empty for furniture."),
        Field(
            "row_labels",
            "str",
            doc="Row label sequence, default A,B,C... Override for odd conventions.",
        ),
        Field(
            "col_labels",
            "str",
            doc="Column label sequence, default 1,2,3... Override for odd conventions.",
        ),
        Field(
            "capacity",
            "int",
            doc="How many things fit: tubes, boxes, anything. A box grid implies "
            "this from rows*cols, so leave empty there; set it on furniture that "
            "holds nested containers (a rack holding five boxes) and on boxes "
            "recorded without a grid.",
        ),
        Field("notes", "text"),
        *_AUDIT_FIELDS,
    ),
)

ITEMS = Table(
    name="items",
    filename="items.csv",
    primary_key="item_id",
    # No single prefix: item ids carry their class (smp_/rgt_/kit_/csm_) so a bare
    # id in a spreadsheet or a printed pick-list already says what kind of thing it
    # is. ITEM_CLASS_PREFIX below is what validation checks against.
    id_prefix=None,
    doc=(
        "Supertype for everything physically stored. One row per tube, bottle, "
        "kit or box of tips. Class-specific detail lives in samples/reagents/"
        "kits/consumables, keyed by the same item_id."
    ),
    fields=(
        Field("item_id", "id", required=True),
        Field("class", "str", required=True, enum=ITEM_CLASSES),
        Field("name", "str", required=True, doc="Human-readable label."),
        Field("status", "str", required=True, enum=ITEM_STATUSES),
        Field(
            "location_id",
            "id",
            fk="containers.container_id",
            doc="Null means unlocated: we believe it exists but cannot place it.",
        ),
        Field(
            "position",
            "str",
            doc="Well within the containing box, e.g. A1. Optional bonus -- most "
            "entries are recorded at box level only. Never inferred from absence.",
        ),
        Field("handler", "str", doc="Who last had physical custody."),
        Field("notes", "text"),
        Field(
            "deleted_at",
            "datetime",
            doc="Tombstone. Consumed and discarded items are kept for the record. UTC with a trailing Z.",
        ),
        *_AUDIT_FIELDS,
    ),
)

SAMPLES = Table(
    name="samples",
    filename="samples.csv",
    primary_key="item_id",
    id_prefix=None,
    supertype="items",
    doc="Extension table for items.class == 'sample'. One row per physical tube.",
    fields=(
        Field("item_id", "id", required=True, fk="items.item_id"),
        Field(
            "gml_id",
            "str",
            doc="Lab-internal human identifier. Retained but not trusted as a key: "
            "widths vary and maintenance lapses. Unique when present.",
        ),
        Field("patient_id", "id", fk="patients.patient_id", doc="Optional -- many samples have no patient."),
        Field("project_id", "id", fk="projects.project_id"),
        Field("sample_type", "str", required=True, enum=SAMPLE_TYPES),
        Field("source_material", "str", doc="What it came from, e.g. 'Venous blood', 'Bone marrow aspirate'."),
        Field("collected_on", "date"),
        Field("received_on", "date"),
        Field("processed_on", "date"),
    ),
)

REAGENTS = Table(
    name="reagents",
    filename="reagents.csv",
    primary_key="item_id",
    id_prefix=None,
    supertype="items",
    doc="Extension table for items.class == 'reagent'. Persistent stock whose quantity changes over time.",
    fields=_SUPPLY_FIELDS,
)

KITS = Table(
    name="kits",
    filename="kits.csv",
    primary_key="item_id",
    id_prefix=None,
    supertype="items",
    doc=(
        "Extension table for items.class == 'kit'. A kit is a storable item, not "
        "a container -- it is used as a unit and its remaining uses are recorded "
        "as a measurement."
    ),
    fields=_SUPPLY_FIELDS,
)

CONSUMABLES = Table(
    name="consumables",
    filename="consumables.csv",
    primary_key="item_id",
    id_prefix=None,
    supertype="items",
    doc="Extension table for items.class == 'consumable'. Pipette tips, cryovials, media.",
    fields=_SUPPLY_FIELDS,
)

PATIENTS = Table(
    name="patients",
    filename="patients.csv",
    primary_key="patient_id",
    id_prefix="pat",
    doc="Participant records. Deliberately minimal -- clinical detail lives in the clinical system.",
    fields=(
        Field("patient_id", "id", required=True),
        Field("label", "str", required=True, doc="Human-facing participant code."),
        Field("sex", "str", enum=SEXES),
        Field("dob", "date"),
        Field("affected_status", "str", enum=AFFECTED_STATUSES),
        Field("gene", "str"),
        Field("variant", "str"),
        Field("protocol", "str", doc="Ethics/HREC protocol number."),
        Field("consented_on", "date"),
        Field("notes", "text"),
        *_AUDIT_FIELDS,
    ),
)

PROJECTS = Table(
    name="projects",
    filename="projects.csv",
    primary_key="project_id",
    id_prefix="prj",
    doc="Projects, with subprojects expressed as a self-reference.",
    fields=(
        Field("project_id", "id", required=True),
        Field("parent_project_id", "id", fk="projects.project_id"),
        Field("name", "str", required=True),
        Field("status", "str", required=True, enum=PROJECT_STATUSES),
        Field("description", "text"),
        *_AUDIT_FIELDS,
    ),
)

PATIENT_PROJECTS = Table(
    name="patient_projects",
    filename="patient_projects.csv",
    primary_key="link_id",
    id_prefix="ppl",
    doc="Which projects a participant belongs to. A sample's project must be one of "
        "these exactly -- a subproject of one is not.",
    fields=(
        Field("link_id", "id", required=True),
        Field("patient_id", "id", required=True, fk="patients.patient_id"),
        Field("project_id", "id", required=True, fk="projects.project_id"),
    ),
)

ITEM_LINKS = Table(
    name="item_links",
    filename="item_links.csv",
    primary_key="link_id",
    id_prefix="lin",
    doc=(
        "Derivation as a junction rather than a parent_id column: aliquoting is "
        "one-parent-many-children and pooling is many-parent-one-child, and both "
        "are the same relation. Endpoints are any item class."
    ),
    fields=(
        Field("link_id", "id", required=True),
        Field("child_item_id", "id", required=True, fk="items.item_id"),
        Field("parent_item_id", "id", required=True, fk="items.item_id"),
        Field("role", "str", required=True, enum=LINK_ROLES),
        Field("notes", "text"),
    ),
)

ASSAYS = Table(
    name="assays",
    filename="assays.csv",
    primary_key="assay_id",
    id_prefix="asy",
    doc=(
        "Controlled vocabulary of measurement types. Units alone do not make "
        "values comparable -- Qubit and Nanodrop both report ng/ul and disagree "
        "routinely -- so each assay declares the quantity it measures, its "
        "canonical unit, and whether its result is numeric or qualitative."
    ),
    fields=(
        Field("assay_id", "id", required=True),
        Field("code", "str", required=True, doc="Short stable handle, e.g. 'qubit_dsdna'. Unique."),
        Field("name", "str", required=True),
        Field("quantity", "str", required=True, enum=QUANTITIES),
        Field("canonical_unit", "str", required=True),
        Field("result_type", "str", required=True, enum=RESULT_TYPES),
        Field("method", "text"),
        Field("notes", "text"),
    ),
)

MEASUREMENTS = Table(
    name="measurements",
    filename="measurements.csv",
    primary_key="measurement_id",
    id_prefix="msm",
    doc=(
        "Immutable observation events. You cannot un-measure a tube, so a "
        "correction appends a new row naming the invalidated one in 'supersedes'. "
        "Derived values such as yield are computed on read, never stored."
    ),
    fields=(
        Field("measurement_id", "id", required=True),
        Field("item_id", "id", required=True, fk="items.item_id"),
        Field("assay_id", "id", required=True, fk="assays.assay_id"),
        Field("measured_on", "datetime", required=True,
              doc="UTC to the minute, stored with a trailing Z. A zone spelled out on the way in "
                  "(...Z, ...+10:00) is folded into UTC first, so the column keeps one spelling."),
        Field("value_num", "float"),
        Field("value_text", "text"),
        Field("unit", "str", doc="Unit of the recorded value; converted to canonical on read."),
        Field("handler", "str"),
        Field("instrument", "str"),
        Field("supersedes", "id", fk="measurements.measurement_id"),
        Field("notes", "text"),
    ),
)


TABLES: tuple[Table, ...] = (
    CONTAINERS,
    ITEMS,
    SAMPLES,
    REAGENTS,
    KITS,
    CONSUMABLES,
    PATIENTS,
    PROJECTS,
    PATIENT_PROJECTS,
    ITEM_LINKS,
    ASSAYS,
    MEASUREMENTS,
)

_BY_NAME = {t.name: t for t in TABLES}


def get_table(name: str) -> Table:
    try:
        return _BY_NAME[name]
    except KeyError:
        raise KeyError(f"Unknown table {name!r}. Known: {sorted(_BY_NAME)}") from None


def extension_table(item_class: str) -> Table:
    return get_table(CLASS_EXTENSION_TABLE[item_class])


def table_for_filename(filename: str) -> Optional[Table]:
    for t in TABLES:
        if t.filename == filename:
            return t
    return None
