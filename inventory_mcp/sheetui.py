"""Generate the workbook's interface tabs from the schema, never by hand.

Three tabs come with the database: ``Start`` (the form a person at the bench uses),
``database_errors`` (what the server currently thinks is broken), and ``options``
(hidden reference data for the dropdowns and the attached script). All three are
*derived*. Every value on them -- every enum, every foreign-key target, every legal
unit, every required flag -- is read out of ``schema.py``, ``units.py`` and ``ids.py``
at generation time, because a hand-copied list is a second rulebook, and the second
rulebook is the one that goes stale while everyone keeps trusting it.

The same generation produces three things that have to agree exactly: the cells, the
formatting requests that point at those cells, and the machine-readable map on
``options`` that the Apps Script reads. They come out of one pass over the same
description of the forms (:data:`FORMS`), so the sheet cannot advertise a cell that
the script then fails to find.

What is deliberately *not* here: validation logic. The rules stay in
``validation.py``, and the script either proves an edit satisfies them or refuses.
Reimplementing them in JavaScript is how you end up with two rulebooks disagreeing
about whether a tube exists.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

from . import SCHEMA_VERSION, ids, schema, units
from .service import ServiceError
from .store.base import Row
from .store.drive import column_letter
from .validation import format_datetime, now_utc_minute

#: Bumped whenever the generated layout changes shape, so a workbook laid out by an
#: older release is recognisably stale rather than subtly different. Combined with
#: the schema digest into :func:`layout_stamp`.
#: 2: ``options`` gained the ``blocks`` index the attached script reads its map from.
LAYOUT_VERSION = 2

#: How far down a data tab the vocabulary dropdowns reach. Generous because a
#: validation rule over empty cells costs nothing, and because re-running the command
#: to cover row 3001 is a strange thing to ask of someone adding tubes.
DATA_ROWS_COVERED = 3000

#: Column widths on ``Start``: label, input, explanation, machine target. Fixed
#: rather than auto-resized -- auto-sizing is another request, and a form whose
#: columns shift between runs is worse than one that is slightly too wide.
START_WIDTHS = (190, 300, 380, 210)

ERROR_WIDTHS = (110, 130, 150, 150, 170, 620)

_GREY = {"red": 0.94, "green": 0.94, "blue": 0.94}
_MUTED = {"red": 0.5, "green": 0.5, "blue": 0.5}


# ---------------------------------------------------------------------------
# what the forms are
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Input:
    """One labelled cell on ``Start``.

    ``target`` is where the script writes the value: a ``tab.column`` pair, or one of
    the documented indirections (:data:`INDIRECTIONS`) when the column depends on
    which row is being touched. An input with no target is consumed by the script
    itself and never stored.

    ``required`` left as ``None`` means "whatever the schema says", which is the
    common case and the one worth delegating: when a column stops being mandatory,
    the form follows without anyone remembering to edit a second file.
    """

    label: str
    target: str = ""
    kind: str = ""
    required: Optional[bool] = None
    hint: str = ""

    def slug(self) -> str:
        return (self.target or self.label).replace(".", "_").replace(" ", "_").lower()


@dataclass(frozen=True)
class Action:
    """One block on ``Start``: what it writes, what it asks for, what to warn about."""

    key: str
    title: str
    writes: str
    inputs: tuple[Input, ...]
    #: Whether the attached script can carry this out itself. False is not a TODO:
    #: it means the button refuses and points at the manual route, which is what
    #: keeps the workbook usable on its own.
    provable: bool = True
    warning: str = ""


#: The everyday bench actions, mapped onto columns. Ordered by how often they get
#: used, because that is the order they appear on the sheet.
FORMS: tuple[Action, ...] = (
    Action(
        key="add_sample",
        title="Add a sample",
        writes="one row in items, one row in samples",
        inputs=(
            Input("Name", "items.name", hint="Any label you would say out loud."),
            Input("Sample type", "samples.sample_type"),
            Input("Box", "items.location_id",
                  hint="A container id from the containers tab, not its name."),
            Input("Well", "items.position",
                  hint="Like B7. Optional; most entries stop at the box."),
            Input("Participant", "samples.patient_id", hint="Optional."),
            Input("Project", "samples.project_id",
                  hint="The participant must be enrolled in this exact project."),
            Input("Source material", "samples.source_material",
                  hint="Optional, e.g. 'Venous blood'."),
            Input("Collected on", "samples.collected_on", hint="YYYY-MM-DD."),
            Input("Handler", "items.handler", hint="Blank uses your name above."),
            Input("Notes", "items.notes", hint="Optional."),
        ),
        warning="Both rows are written together. Half a pair is refused everywhere, "
                "not just here.",
    ),
    Action(
        key="add_supply",
        title="Add a supply",
        writes="one row in items, one row in reagents, kits or consumables, plus one "
               "in measurements if an opening quantity is given",
        inputs=(
            Input("Class", "items.class",
                  hint="reagent, kit or consumable. A tube of material is a sample, "
                       "made in the block above."),
            Input("Name", "items.name"),
            Input("Box", "items.location_id", hint="Optional."),
            Input("Well", "items.position", hint="Optional."),
            Input("Quantity assay", "supply.assay_code",
                  hint="Only with an opening quantity. Its code, like qubit_dsdna."),
            Input("Opening quantity", "supply.quantity", kind="number",
                  hint="Leave blank to register the item without a reading."),
            Input("Unit", "supply.unit", hint="Must suit the assay, like ng/ul."),
            Input("Expires on", "supply.expires_on", hint="YYYY-MM-DD."),
            Input("Price", "supply.price", kind="number", hint="Optional."),
            Input("Storage", "supply.storage_requirements", hint="Optional handling note."),
            Input("Handler", "items.handler", hint="Blank uses your name above."),
            Input("Notes", "items.notes", hint="Optional."),
        ),
    ),
    Action(
        key="add_container",
        title="Register a box, rack or shelf",
        writes="one row in containers",
        inputs=(
            Input("Label", "containers.label", hint="What it says on the side of it."),
            Input("Kind", "containers.kind"),
            Input("Inside", "containers.parent_id",
                  hint="The container holding this one. Blank for top level."),
            Input("Rows", "containers.rows", kind="number",
                  hint="Wells down the side. Blank for furniture."),
            Input("Columns", "containers.cols", kind="number",
                  hint="Wells across. Blank for furniture."),
            Input("Capacity", "containers.capacity", kind="number",
                  hint="Only when there is no grid: a rack that holds five boxes, say."),
            Input("Notes", "containers.notes", hint="Optional."),
        ),
        warning="Rows and columns describe wells. A rack that holds boxes takes a "
                "capacity instead.",
    ),
    Action(
        key="record_measurement",
        title="Record a measurement",
        writes="one new row in measurements",
        inputs=(
            Input("Item", "measurements.item_id", hint="The tube's id."),
            Input("Assay", "supply.assay_code", hint="Its code, like qubit_dsdna."),
            Input("Value", "measurements.value_num", kind="number",
                  hint="For a numeric assay. Use the text line below for a qualitative one."),
            Input("Result text", "measurements.value_text", hint="For a qualitative assay."),
            Input("Unit", "measurements.unit", hint="Required for a numeric assay."),
            Input("Measured on", "measurements.measured_on",
                  hint="YYYY-MM-DDTHH:MMZ, UTC. Blank means now."),
            Input("Instrument", "measurements.instrument", hint="Optional."),
            Input("Handler", "measurements.handler", hint="Blank uses your name above."),
            Input("Corrects", "measurements.supersedes",
                  hint="The reading this one replaces. Blank retires whichever reading "
                       "is current for this item and assay now."),
            Input("Notes", "measurements.notes", hint="Optional."),
        ),
        warning="A reading is never edited, only replaced. The old row stays exactly "
                "as it was.",
    ),
    Action(
        key="move_item",
        title="Move something",
        writes="location_id and position on one items row, or parent_id on one "
               "containers row",
        inputs=(
            Input("What", "move.what", hint="An item id or a container id."),
            Input("Into", "move.into", hint="The container to move it into."),
            Input("Well", "items.position", hint="Only for an item. Blank clears the well."),
        ),
        warning="A full box, a taken well, and a container moved inside itself are all "
                "refused.",
    ),
    Action(
        key="set_status",
        title="Change a status",
        writes="status on one items row, and its location when the new status needs no slot",
        inputs=(
            Input("Item", "items.item_id"),
            Input("Status", "items.status"),
            Input("Box", "items.location_id",
                  hint="Only to put a present item somewhere new."),
            Input("Well", "items.position", hint="Optional, with a box."),
        ),
        warning="Consumed clears the location, and nothing remembers where it was.",
    ),
    Action(
        key="rename_item",
        title="Rename",
        writes="the name column of whichever table the id belongs to",
        inputs=(
            Input("Id", "any.id"),
            Input("New name", "any.name"),
        ),
    ),
    Action(
        key="edit_notes",
        title="Edit notes",
        writes="the notes column of whichever table the id belongs to",
        inputs=(
            Input("Id", "any.id"),
            Input("Notes", "any.notes"),
        ),
        warning="Clearing the box empties the note; nothing here keeps the old text.",
    ),
    Action(
        key="retire_item",
        title="Retire an item",
        writes="deleted_at on one items row, clearing its location and well",
        inputs=(
            Input("Item", "items.item_id"),
        ),
        warning="This retires the row rather than removing it: measurements and links "
                "keep pointing at it, which is the point.",
    ),
)

#: Targets that are not a literal ``tab.column``, spelled out on ``options`` so the
#: script and whoever reads the same cell agree on what they mean.
INDIRECTIONS = (
    ("supply.<field>", "the extension table named by the class chosen above: "
                       "reagents, kits or consumables"),
    ("supply.assay_code", "looked up as assays.code, case-insensitively; the row stores "
                          "the assay_id that comes back"),
    ("supply.quantity", "measurements.value_num, written only when an assay is named"),
    ("move.what", "an item id or a container id; which one decides which tab gets the write"),
    ("move.into", "items.location_id for an item, containers.parent_id for a container"),
    ("any.id", "decides the table from the id's prefix; nothing else is written anywhere else"),
    ("any.name", "items.name, projects.name, assays.name, containers.label or "
                 "patients.label, chosen by the id's prefix"),
    ("any.notes", "items.notes, containers.notes, patients.notes, assays.notes or "
                  "projects.description, chosen by the id's prefix"),
)

#: Where attribution comes from. Stated once here so the sheet, the script and the
#: documentation describe one fallback chain: the cell is an override read afresh on every
#: press, because it is one shared cell and cannot hold two people at once.
ACTOR_HINT = ("Recorded as updated_by on anything you add or change. Left blank, each press "
              "signs itself with your own Google account; fill it in to sign as something "
              "else, such as initials. A press that cannot be identified at all is "
              "stamped 'unknown'.")


# ---------------------------------------------------------------------------
# reference data for the hidden options tab
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Block:
    """A labelled column band on ``options``: title in row 1, headers in row 2.

    Blocks sit side by side rather than stacked so that regenerating one at a
    different length cannot disturb its neighbours, and so each is a rectangle the
    script can read with one range.
    """

    name: str
    header: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]


TITLE_ROW = 1
HEADER_ROW = 2
FIRST_VALUE_ROW = 3

#: Declared type -> the kind of cell the form should offer.
_KIND = {
    "str": "text",
    "text": "long",
    "int": "number",
    "float": "number",
    "date": "date",
    "datetime": "timestamp",
    "id": "id",
    "bool": "text",
}


def enum_fields() -> dict[str, tuple[str, ...]]:
    """Every ``table.column`` carrying an enumerated vocabulary."""
    return {f"{t.name}.{f.name}": tuple(f.enum)
            for t in schema.TABLES for f in t.fields if f.enum}


def required_fields() -> dict[str, bool]:
    return {f"{t.name}.{f.name}": bool(f.required)
            for t in schema.TABLES for f in t.fields}


def field_kinds() -> dict[str, str]:
    return {f"{t.name}.{f.name}": _KIND.get(f.type, "text")
            for t in schema.TABLES for f in t.fields}


def human_fields(table: schema.Table) -> tuple[str, str]:
    """The column a person would call "its name", and the one they would call "its note".

    Derived from the naming convention the schema already follows rather than written out
    table by table, because ``rename`` and ``edit_notes`` take an id of any kind and have
    to know where to write without a lookup table nobody keeps up to date. A table with
    neither simply gets refused -- a measurement has no name to change.
    """
    names = [field.name for field in table.fields]
    name = next((n for n in names if n in ("name", "label")), "")
    notes = next((n for n in names if n in ("notes", "description")), "")
    return name, notes


def fk_targets() -> dict[str, str]:
    return {f"{t.name}.{f.name}": f.fk
            for t in schema.TABLES for f in t.fields if f.fk}


def accepted_units(quantity: str) -> list[str]:
    """The units a reading of this quantity may be recorded in.

    Straight out of ``units.py``, including the spellings it happens to accept as
    well as the tidy ones. Pruning the list to look neat would hide units the server
    quietly accepts, leaving someone to wonder why their entry was refused.
    """
    dim = units.dimension_for(quantity)
    if dim is not None:
        return sorted(dim.factors)
    if quantity in units.UNITLESS:
        return sorted({"", "cells", "%", "count", "n"})
    return []


def layout_stamp() -> str:
    """Digest of everything the generated interface depends on.

    Published on ``options`` and compared on every run. A workbook whose digest
    differs was laid out against a different schema, so its dropdowns are describing
    some other database -- worth saying in words rather than leaving someone to
    notice that a valid-looking value is suddenly refused.
    """
    payload = {
        "layout": LAYOUT_VERSION,
        "schema_version": SCHEMA_VERSION,
        "tables": [
            {
                "name": t.name,
                "primary_key": t.primary_key,
                "id_prefix": t.id_prefix,
                "supertype": t.supertype,
                "fields": [[f.name, f.type, bool(f.required), f.fk or "", list(f.enum)]
                           for f in t.fields],
            }
            for t in schema.TABLES
        ],
        "class_prefix": dict(schema.ITEM_CLASS_PREFIX),
        "supply_classes": list(schema.SUPPLY_CLASSES),
        "quantities": {q: accepted_units(q) for q in schema.QUANTITIES},
        "ids": [ids.ALPHABET, ids.SUFFIX_LEN],
        "forms": [[a.key, a.title, a.writes, int(a.provable), a.warning,
                   [[i.label, i.target, i.kind, i.required, i.hint] for i in a.inputs]]
                  for a in FORMS],
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def options_blocks(assay_rows: Sequence[Row] = ()) -> list[Block]:
    """Everything the sheet and the script need to know, as column bands."""
    blocks: list[Block] = [
        Block("stamp", ("key", "value"), (
            ("layout_version", str(LAYOUT_VERSION)),
            ("schema_version", str(SCHEMA_VERSION)),
            ("digest", layout_stamp()),
            ("generated_at", format_datetime(now_utc_minute())),
        )),
    ]

    for name, values in enum_fields().items():
        blocks.append(Block(f"enum:{name}", ("value",), tuple((v,) for v in values)))

    blocks.append(Block("supply_classes", ("value",),
                        tuple((c,) for c in schema.SUPPLY_CLASSES)))

    blocks.append(Block(
        "id_prefixes", ("what", "prefix", "table", "extension_table", "name_column",
                        "notes_column"),
        tuple((t.name, t.id_prefix, t.name, "", *human_fields(t))
              for t in schema.TABLES if t.id_prefix)
        # An item's prefix names its class, and the class decides which extension row has
        # to exist beside it. Both halves have to be on the sheet or the script has to
        # already know them, which is the knowing-we-are-not-doing -- and it cannot be
        # left to pluralising the class, because "consumable" does not become "consumables".
        + tuple((f"class {item_class}", prefix, "items",
                 schema.CLASS_EXTENSION_TABLE[item_class], "name", "notes")
                for item_class, prefix in schema.ITEM_CLASS_PREFIX.items())))

    blocks.append(Block(
        "foreign_keys", ("table", "column", "points at"),
        tuple((t.name, f.name, f.fk)
              for t in schema.TABLES for f in t.fields if f.fk)))

    blocks.append(Block(
        "fields", ("table", "column", "kind", "required"),
        tuple((t.name, f.name, _KIND.get(f.type, "text"), "yes" if f.required else "no")
              for t in schema.TABLES for f in t.fields)))

    blocks.append(Block("indirections", ("target", "means"), INDIRECTIONS))

    blocks.append(Block(
        "constants", ("key", "value"),
        (
            ("id_alphabet", ids.ALPHABET),
            ("id_suffix_length", str(ids.SUFFIX_LEN)),
            ("id_shape", "three lowercase letters, an underscore, then eight alphabet characters"),
            ("timestamp_format", "YYYY-MM-DDTHH:MMZ -- UTC to the minute, always with the Z"),
            ("date_format", "YYYY-MM-DD"),
            ("well_labels", "rows are A, B, C ... and columns are 1, 2, 3 ... unless that "
                            "container's row_labels or col_labels override them"),
            ("capacity_rule", "capacity if stated, otherwise rows*cols, otherwise unlimited"),
            ("occupancy_counts", "live items inside it plus containers directly inside it"),
            ("tombstone", "a row whose deleted_at is not blank is retired, and is skipped "
                          "by the position, status and pairing rules"),
            ("legal_pairs", "a sample's project must appear beside its participant in "
                            "patient_projects exactly; a subproject of one does not count"),
        )))

    blocks.append(Block(
        "assays", ("code", "assay_id", "name", "quantity", "canonical_unit",
                   "result_type", "accepted_units"),
        tuple(_assay_row(r) for r in assay_rows)))

    return blocks


def _assay_row(r: Row) -> tuple[str, ...]:
    quantity = (r.get("quantity") or "").strip()
    return (
        (r.get("code") or "").strip(),
        (r.get("assay_id") or "").strip(),
        (r.get("name") or "").strip(),
        quantity,
        (r.get("canonical_unit") or "").strip(),
        (r.get("result_type") or "").strip(),
        ", ".join(u if u else "(none)" for u in accepted_units(quantity)),
    )


def render_blocks(blocks: Sequence[Block]) -> tuple[list[list[str]], dict[str, dict]]:
    """Lay the blocks out left to right, returning the grid and where each landed.

    The extents are what makes a dropdown possible: a validation rule has to name the
    cells it reads, and the only trustworthy way to know them is to be the code that
    put them there.
    """
    if not blocks:
        return [], {}
    height = max(FIRST_VALUE_ROW + len(b.rows) for b in blocks)
    grid: list[list[str]] = [[] for _ in range(height)]
    places: dict[str, dict] = {}
    cursor = 0
    for block in blocks:
        width = max([len(block.header), *(len(r) for r in block.rows)] + [1])
        band = [[""] * width for _ in range(height)]
        band[TITLE_ROW - 1][0] = block.name
        for offset, heading in enumerate(block.header):
            band[HEADER_ROW - 1][offset] = heading
        for offset, values in enumerate(block.rows):
            for column, value in enumerate(values):
                band[FIRST_VALUE_ROW - 1 + offset][column] = str(value)
        for row, band_row in zip(grid, band):
            row.extend([""] * (cursor - len(row)))  # spacer columns between blocks
            row.extend(band_row)
        last_row = max(FIRST_VALUE_ROW, FIRST_VALUE_ROW + len(block.rows) - 1)
        places[block.name] = {
            "first_col": cursor + 1,
            "last_col": cursor + width,
            "first_row": FIRST_VALUE_ROW,
            "last_row": last_row,
            "range": (f"options!{column_letter(cursor + 1)}{FIRST_VALUE_ROW}"
                      f":{column_letter(cursor + width)}{last_row}"),
        }
        cursor += width + 1
    return grid, places


def render_options(assay_rows: Sequence[Row] = (),
                   start: Optional[StartSheet] = None) -> tuple[list[list[str]], dict[str, dict]]:
    """Render ``options`` with an index of where every block ended up.

    Without the index, whatever reads the tab has to rediscover the layout -- scan for
    the title, assume the header row spans the block exactly. Both assumptions hold
    today and both break silently: a block whose rows run wider than its header gets
    read short, and the reader never finds out. So the generator states where things
    are, in the same pass that put them there.

    The index is appended last, which is what makes two passes enough: blocks go left
    to right, so adding one on the end cannot move the ones already placed.
    """
    blocks = options_blocks(assay_rows) + (list(form_blocks(start)) if start else [])
    _, probe = render_blocks(blocks)
    first_col = max(place["last_col"] for place in probe.values()) + 2
    header = ("block", "first_col", "last_col", "first_row", "last_row")
    rows = [(name, str(p["first_col"]), str(p["last_col"]),
             str(p["first_row"]), str(p["last_row"])) for name, p in probe.items()]
    # Its own extent is knowable only because nothing follows it to shift it.
    rows.append(("blocks", str(first_col), str(first_col + len(header) - 1),
                 str(FIRST_VALUE_ROW), str(FIRST_VALUE_ROW + len(rows))))
    grid, places = render_blocks(blocks + [Block("blocks", header, tuple(rows))])
    for name, spot in probe.items():
        if places[name] != spot:
            raise ServiceError(f"internal: block {name!r} moved while laying out options")
    return grid, places


# ---------------------------------------------------------------------------
# the Start sheet
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StartSheet:
    grid: list[list[str]]
    actor_cell: str
    actor_row: int
    title_rows: tuple[int, ...]
    actions: dict[str, dict]
    last_row: int


def _kind_of(target: str, given: str) -> str:
    return given or field_kinds().get(target, "text")


def _is_required(inp: Input) -> bool:
    return bool(required_fields().get(inp.target, False)) if inp.required is None else inp.required


def build_start() -> StartSheet:
    """The form, as cells plus the coordinates everything else refers to.

    Column A is the label, B the input, C the explanation, D the machine-readable
    target. D looks like clutter and is the reason the script can be written against
    this sheet at all: beside every box it says which column the value ends up in.
    """
    grid: list[list[str]] = []

    def line(*cells: str) -> int:
        grid.append(list(cells))
        return len(grid)

    line("Lab inventory")
    line("", "Fill in a block and press its button. Anything the button refuses says "
             "why on its Result line, and can still be done by hand in the data tabs.")
    line("", "Nothing here waits for approval: a refused action changed nothing, and a "
             "successful one is already saved.")
    actor_row = line("Who are you?", "", ACTOR_HINT)
    line()

    actions: dict[str, dict] = {}
    for action in FORMS:
        title_row = line(action.title.upper(), "", action.writes)
        inputs: dict[str, str] = {}
        for inp in action.inputs:
            star = "*" if _is_required(inp) else ""
            row = line(f"{inp.label}{star}", "", inp.hint, inp.target or "(this sheet only)")
            slug = inp.slug()
            if slug in inputs:
                # Two inputs sharing a target inside one action would silently share a
                # cell, and the published map would name one of them. Refuse to ship
                # that rather than let the last one win.
                raise ServiceError(f"{action.key}: two inputs are both called {slug!r}")
            inputs[slug] = f"B{row}"
        result_row = line("Result", "",
                          "Written by the button: the id it made, or why it refused. "
                          "Cleared the next time you press it.")
        anchor_row = line("(button)", "",
                          "" if action.provable else
                          "No button for this one yet: the script refuses it. Do it by "
                          "hand in the data tabs.")
        if action.warning:
            line("", "", action.warning)
        line()
        actions[action.key] = {
            "title_row": title_row,
            "inputs": inputs,
            "result": f"B{result_row}",
            "anchor": f"A{anchor_row}",
            "provable": action.provable,
        }

    return StartSheet(grid=grid, actor_cell=f"B{actor_row}", actor_row=actor_row,
                      title_rows=tuple(actions[a.key]["title_row"] for a in FORMS),
                      actions=actions, last_row=len(grid))


def form_blocks(start: StartSheet) -> tuple[Block, Block]:
    """The two ``options`` blocks describing the form, generated from the layout.

    This is the contract the Apps Script reads instead of parsing labels off a
    human-facing sheet: one row per action, one per input, each naming the cell it
    lives in. Regenerated alongside the sheet, so it cannot drift from it.
    """
    by_key = {a.key: a for a in FORMS}
    actions = Block(
        "actions",
        ("action", "title", "writes", "button_anchor", "result_cell", "script_can_do_it"),
        tuple((key, by_key[key].title, by_key[key].writes, spec["anchor"], spec["result"],
               "yes" if spec["provable"] else "no")
              for key, spec in start.actions.items()))
    rows: list[tuple[str, ...]] = []
    for action in FORMS:
        spec = start.actions[action.key]
        for inp in action.inputs:
            rows.append((action.key, inp.label, inp.target or "(this sheet only)",
                         _kind_of(inp.target, inp.kind),
                         "yes" if _is_required(inp) else "no",
                         spec["inputs"][inp.slug()], inp.hint))
    return actions, Block("inputs", ("action", "label", "target", "kind",
                                     "required", "cell", "hint"), tuple(rows))


# ---------------------------------------------------------------------------
# the error report
# ---------------------------------------------------------------------------


def build_errors(report: dict, checked_at: Optional[str] = None) -> list[list[str]]:
    """``doctor``'s findings as a tab readable without a terminal.

    Deliberately not refreshed on every write: the value of publishing it is that
    whoever is looking can see when it was produced. A report that refreshes
    silently underneath them is how a problem someone already fixed starts looking
    unfixed.
    """
    stamp = checked_at or format_datetime(now_utc_minute())
    errors = report.get("errors", [])
    warnings = report.get("warnings", [])
    grid: list[list[str]] = [
        ["Database errors"],
        [f"Checked at {stamp}."],
        [f"{report.get('error_count', len(errors))} error(s), "
         f"{report.get('warning_count', len(warnings))} warning(s)."],
        ([f"While any error stands, the app refuses every write touching the tables it "
          f"names -- including the write that would fix it. Clear these first."]
         if errors else ["Nothing is broken. Writes are not blocked."]),
        [],
        ["severity", "table", "row", "column", "code", "what it means"],
    ]
    for v in list(errors) + list(warnings):
        grid.append([v.get("severity", ""), v.get("table", ""), v.get("row_id", ""),
                     v.get("field", ""), v.get("code", ""), v.get("message", "")])
    return grid


# ---------------------------------------------------------------------------
# formatting requests
# ---------------------------------------------------------------------------


def _grid_range(sheet_id: int, row: int, col: int, *, rows: int = 1,
                cols: int = 1, to_bottom: bool = False) -> dict:
    """GridRange from 1-based sheet coordinates.

    Ends are exclusive, unlike everywhere else in this module -- the one API
    convention that has already cost this project a bug, so it gets said out loud
    where it is encoded.
    """
    return {
        "sheetId": sheet_id,
        "startRowIndex": row - 1,
        "endRowIndex": (DATA_ROWS_COVERED + 1) if to_bottom else row - 1 + rows,
        "startColumnIndex": col - 1,
        "endColumnIndex": col - 1 + cols,
    }


def _dropdown(values: Sequence[str], *, strict: bool, message: str = "") -> dict:
    """A dropdown whose allowed values are the ones Python holds.

    Literal rather than a reference into ``options``: every list here is short enough
    to inline, and an inline list survives somebody deleting a row on the hidden tab.
    The price is that a changed vocabulary needs a re-run -- which is what the digest
    on ``options`` exists to catch.
    """
    rule: dict = {
        "condition": {
            "type": "ONE_OF_LIST",
            "values": [{"userEnteredValue": v} for v in values],
        },
        "showCustomUi": True,
        "strict": strict,
    }
    if message:
        # A bare string, not an object: the discovery document types inputMessage as
        # string, and sending {type, text} is rejected outright -- "Starting an object
        # on a scalar field", which fails the whole layout batch, not just this rule.
        rule["inputMessage"] = message
    return rule


def _style(range_: dict, **cell_format) -> dict:
    """One ``repeatCell``. ``fields`` replaces the whole format on purpose.

    Requests are applied in order and each states its complete format, so a later one
    cannot leave a fragment of an earlier one behind on an overlapping cell.
    """
    return {
        "repeatCell": {
            "range": range_,
            "cell": {"userEnteredFormat": cell_format},
            "fields": "userEnteredFormat",
        }
    }


def _freeze(sheet_id: int, rows: int, *, gridlines: Optional[bool] = None) -> dict:
    """Freeze the header rows, optionally dropping the gridlines.

    The field mask names each property individually rather than saying
    ``gridProperties``. With the broad mask Sheets applies the whole GridProperties
    object, and the parts not sent -- ``rowCount``, ``columnCount`` -- arrive as zero,
    which Google answers with "You can't delete all the rows on the sheet" and refuses
    the entire layout batch.
    """
    grid_properties: dict = {"frozenRowCount": rows}
    fields = ["gridProperties.frozenRowCount"]
    if gridlines is not None:
        grid_properties["hideGridlines"] = gridlines
        fields.append("gridProperties.hideGridlines")
    return {"updateSheetProperties": {
        "properties": {"sheetId": sheet_id, "gridProperties": grid_properties},
        "fields": ",".join(fields)}}


def _column_width(sheet_id: int, index: int, pixels: int) -> dict:
    return {"updateDimensionProperties": {
        "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                  "startIndex": index, "endIndex": index + 1},
        "properties": {"pixelSize": pixels},
        "fields": "pixelSize"}}


def layout_requests(tab_ids: dict[str, int], start: StartSheet,
                    places: dict[str, dict]) -> list[dict]:
    """Every dropdown, column width and frozen header, as one batch.

    One call because a layout is one decision applied at once. Half-applied is the
    state where a form has some of its dropdowns and nobody can tell which.
    """
    requests: list[dict] = []

    # -- data tabs: freeze the header, offer the vocabulary without enforcing it ----
    for spec in schema.TABLES:
        sheet_id = tab_ids.get(spec.name)
        if sheet_id is None:
            continue
        requests.append(_freeze(sheet_id, 1))
        for index, field in enumerate(spec.fields, start=1):
            if not field.enum:
                continue
            requests.append({
                "setDataValidation": {
                    "range": _grid_range(sheet_id, 2, index, to_bottom=True),
                    # Not strict: these tabs hold history, and a legacy spelling that
                    # predates an enum change must stay readable while we argue about
                    # it. The validator is what enforces, and it says so in prose.
                    "rule": _dropdown(field.enum, strict=False,
                                      message="Pick one. Free text is accepted by the "
                                              "sheet and refused by the app."),
                }})

    # -- Start: a form, not a grid --------------------------------------------------
    sheet_id = tab_ids.get("Start")
    if sheet_id is not None:
        # Freeze through the Who are you line so it stays reachable while scrolling a
        # long form -- an identity cell you have to scroll to the top for goes unfilled.
        requests.append(_freeze(sheet_id, start.actor_row, gridlines=True))
        requests += [_column_width(sheet_id, index, width)
                     for index, width in enumerate(START_WIDTHS)]
        # Ids and timestamps are text. Without this, typing 2026-01-05 makes the cell
        # a date, which the sheet then renders however the viewer's locale feels about
        # 05/01 versus 01/05 -- exactly the ambiguity the validator refuses to guess at.
        requests.append(_style(_grid_range(sheet_id, 1, 2, rows=start.last_row),
                               numberFormat={"type": "TEXT"}))
        requests.append(_style(_grid_range(sheet_id, 1, 1, rows=start.last_row),
                               textFormat={"bold": True}))
        requests.append(_style(_grid_range(sheet_id, 1, 1, cols=len(START_WIDTHS)),
                               textFormat={"bold": True, "fontSize": 15}))
        for row in start.title_rows:
            requests.append(_style(_grid_range(sheet_id, row, 1, cols=len(START_WIDTHS)),
                                   textFormat={"bold": True}, backgroundColor=_GREY))
        requests.append(_style(_grid_range(sheet_id, 1, 3, rows=start.last_row),
                               wrapStrategy="WRAP"))
        requests.append(_style(_grid_range(sheet_id, 1, 4, rows=start.last_row),
                               textFormat={"foregroundColor": _MUTED}, wrapStrategy="WRAP"))
        # Start is machine-owned, so its validation is stated whole rather than added to.
        # A rule with no `rule` clears the range -- "or empty to clear the data validation
        # in the range", SetDataValidationRequest in the Sheets v4 reference. Without this,
        # a dropdown left by an earlier build -- or made by hand in the UI -- survives every
        # re-publish underneath the ones we send. One such survivor covered B8:B18 of the
        # live test sheet, which is nine inputs *and the Result line* of the first form:
        # the script could read the typed value but died writing the answer about it,
        # because Sheets refuses a strict cell. Clearing first makes what remains exactly
        # the rules below, on every run.
        requests.append({"setDataValidation": {
            "range": _grid_range(sheet_id, 1, 1, rows=start.last_row,
                                 cols=len(START_WIDTHS))}})
        enums = enum_fields()
        for action in FORMS:
            spec = start.actions[action.key]
            for inp in action.inputs:
                allowed = schema.SUPPLY_CLASSES if inp.target == "items.class" \
                    else enums.get(inp.target)
                if not allowed:
                    continue
                row = int(spec["inputs"][inp.slug()][1:])
                requests.append({
                    "setDataValidation": {
                        "range": _grid_range(sheet_id, row, 2),
                        # Strict here, unlike the data tabs: this is a form being
                        # filled in, and catching a typo at the keyboard beats
                        # explaining it afterwards.
                        "rule": _dropdown(allowed, strict=True,
                                          message="Choose one of: " + ", ".join(allowed)),
                    }})

    # -- database_errors ------------------------------------------------------------
    sheet_id = tab_ids.get("database_errors")
    if sheet_id is not None:
        requests.append(_freeze(sheet_id, 6, gridlines=True))
        requests.append(_style(_grid_range(sheet_id, 6, 1, cols=len(ERROR_WIDTHS)),
                               textFormat={"bold": True}, backgroundColor=_GREY))
        requests.append(_style(_grid_range(sheet_id, 6, 6, to_bottom=True),
                               wrapStrategy="WRAP"))
        requests += [_column_width(sheet_id, index, width)
                     for index, width in enumerate(ERROR_WIDTHS)]

    # -- options: bold the titles, so the tab is legible if anyone does open it -----
    sheet_id = tab_ids.get("options")
    if sheet_id is not None and places:
        requests.append(_style(
            _grid_range(sheet_id, 1, 1, rows=HEADER_ROW,
                        cols=max(p["last_col"] for p in places.values())),
            textFormat={"bold": True}))

    return requests


# ---------------------------------------------------------------------------
# publishing
# ---------------------------------------------------------------------------


def digest_of(grid: Sequence[Sequence[str]]) -> str:
    """The digest a published ``options`` grid was generated with; "" if unpublished."""
    for row in list(grid)[FIRST_VALUE_ROW - 1:]:
        key = (row[0] if row else "").strip()
        if not key:
            break  # the stamp block is first, so an empty cell ends it
        if key == "digest":
            return row[1].strip() if len(row) > 1 else ""
    return ""


def read_digest(store) -> str:
    """The digest a published workbook was generated against; "" if unpublished."""
    return digest_of(store.read_grid("options"))


def is_stale(store) -> bool:
    """Whether the workbook's interface was generated against something else."""
    return read_digest(store) != layout_stamp()


def publish(svc, *, layout: bool = True, problems: bool = True, reset: bool = False,
            progress: Optional[Callable[[str], None]] = None) -> dict:
    """Write the interface tabs into the workbook and format them.

    Safe to re-run: the grids replace what was there, and every formatting request
    overwrites the rule it set last time, so twice is the same as once. Nothing here
    touches a data tab's contents.

    Either half can be skipped. Refreshing the error report should not rewrite a form
    nobody asked it to touch, and rebuilding the form should not overwrite a report
    someone is in the middle of reading.

    ``reset`` deletes those tabs first instead of writing over them, which is the only way
    to lose a merged range, a conditional format, a protection or a hand-made dropdown --
    things a generator cannot overwrite away. It costs the press buttons on Start, which
    are images on that tab, so the caller says so out loud.
    """
    store = svc.store
    if not hasattr(store, "apply_layout"):
        raise ServiceError(
            f"the interface lives in the Drive workbook, and this database is on "
            f"{store.describe()}, which has no tabs to lay out")
    if not layout and not problems:
        raise ServiceError("nothing to do: ask for the layout, the problem report, or both")

    def say(message: str) -> None:
        if progress is not None:
            progress(message)

    start = build_start()
    grids: dict[str, Sequence[Sequence[str]]] = {}
    places: dict[str, dict] = {}

    if layout:
        assay_rows = [dict(r) for r in svc.db.tables["assays"].rows]
        grid, places = render_options(assay_rows, start)
        grids["options"], grids["Start"] = grid, start.grid

    report: Optional[dict] = None
    if problems:
        say("checking the database")
        # The already-loaded violations rather than ``doctor``: doctor reloads, and on
        # Drive that is a whole-database fetch. Called from ``init`` the database was
        # read moments ago and from ``ui`` this is the first read anyway, so reloading
        # here would pay for a second one to ask a question already answered.
        found = svc.violations
        errors = [v.as_dict() for v in found if v.severity == "error"]
        warnings = [v.as_dict() for v in found if v.severity != "error"]
        report = {"error_count": len(errors), "warning_count": len(warnings),
                  "errors": errors, "warnings": warnings}
        grids["database_errors"] = build_errors(report)

    discarded: dict[str, int] = {}
    if reset:
        say("recreating " + ", ".join(sorted(grids)) + " from nothing")
        discarded = store.reset_tabs(list(grids))

    say("writing " + ", ".join(sorted(grids)))
    # What was there before comes back with the write rather than costing a read of
    # its own: the store already fetched those tabs to work out how far to pad.
    before = digest_of(store.write_grids(grids).get("options", [])) if layout else ""

    count = 0
    if layout:
        say("applying the layout")
        requests = layout_requests(store.tab_ids(), start, places)
        store.apply_layout(requests)
        count = len(requests)

    return {
        "tabs": sorted(grids),
        "layout_requests": count,
        "discarded": discarded,
        "digest": layout_stamp(),
        "was_stale": bool(before) and before != layout_stamp(),
        "fresh": not before,
        "errors": (report or {}).get("error_count"),
        "warnings": (report or {}).get("warning_count"),
        "start_rows": start.last_row,
    }
