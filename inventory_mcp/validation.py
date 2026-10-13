"""Validation: whole-database checks plus scoped pre-write checks.

Violations are data, never exceptions. A corrupt cell must not stop someone
from querying the other 4,000 rows, so reads collect violations and carry on;
only writes refuse, and only for the scope they touch.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Iterable, Optional

from . import ids, schema, units
from .db import EXTRA, Database, Table

SEVERITIES = ("error", "warning")


@dataclass(frozen=True)
class Violation:
    table: str
    row_id: str
    field: str
    code: str
    severity: str
    message: str

    def __str__(self) -> str:
        loc = self.table + (f"[{self.row_id}]" if self.row_id else "")
        if self.field:
            loc += f".{self.field}"
        return f"{self.severity.upper():7} {loc}: {self.message}"

    def as_dict(self) -> dict:
        return {
            "table": self.table,
            "row_id": self.row_id,
            "field": self.field,
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
        }


def errors(violations: Iterable[Violation]) -> list[Violation]:
    return [v for v in violations if v.severity == "error"]


# ---------------------------------------------------------------------------
# scalar parsing
# ---------------------------------------------------------------------------

_DATE_PATTERNS = (
    ("%Y-%m-%d", re.compile(r"^\d{4}-\d{2}-\d{2}$")),
    ("%d/%m/%Y", re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$")),
    ("%d.%m.%Y", re.compile(r"^\d{1,2}\.\d{1,2}\.\d{4}$")),
)

_DATETIME_PATTERNS = (
    ("%Y-%m-%dT%H:%M", re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$")),
    ("%Y-%m-%d %H:%M", re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")),
)

_AMBIGUOUS_SLASH = re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$")


def parse_date(value: str) -> tuple[Optional[date], Optional[str]]:
    """Return (date, problem). Ambiguous day/month ordering is reported, not guessed."""
    v = value.strip()
    if not v:
        return None, None
    if _AMBIGUOUS_SLASH.match(v):
        first, second = v.split("/")[0], v.split("/")[1]
        a, b = int(first), int(second)
        if a <= 12 and b <= 12 and a != b:
            return None, (
                f"{value!r} is ambiguous -- both {a}/{b} and {b}/{a} are valid dates. "
                "Store as YYYY-MM-DD."
            )
    for fmt, pattern in _DATE_PATTERNS:
        if pattern.match(v):
            try:
                return datetime.strptime(v, fmt).date(), None
            except ValueError as e:
                return None, f"{value!r} is not a real date ({e})"
    return None, f"{value!r} is not a recognisable date; expected YYYY-MM-DD"


def parse_datetime(value: str) -> tuple[Optional[datetime], Optional[str]]:
    v = value.strip()
    if not v:
        return None, None
    for fmt, pattern in _DATETIME_PATTERNS:
        if pattern.match(v):
            try:
                return datetime.strptime(v, fmt), None
            except ValueError as e:
                return None, f"{value!r} is not a real timestamp ({e})"
    return None, f"{value!r} is not a recognisable timestamp; expected YYYY-MM-DDTHH:MM"


def parse_float(value: str) -> tuple[Optional[float], Optional[str]]:
    v = value.strip()
    if not v:
        return None, None
    try:
        return float(v), None
    except ValueError:
        return None, f"{value!r} is not a number"


def parse_int(value: str) -> tuple[Optional[int], Optional[str]]:
    v = value.strip()
    if not v:
        return None, None
    try:
        return int(v), None
    except ValueError:
        return None, f"{value!r} is not an integer"


def format_date(d: date) -> str:
    return d.strftime("%Y-%m-%d")


def format_datetime(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M")


def now_utc_minute() -> datetime:
    return datetime.now(timezone.utc).replace(second=0, microsecond=0, tzinfo=None)


# ---------------------------------------------------------------------------
# positions
# ---------------------------------------------------------------------------

_POSITION_RE = re.compile(r"^([A-Za-z]+|\d+)(\d+|[A-Za-z]+)$")


def default_row_labels(n: int) -> list[str]:
    out = []
    for i in range(n):
        s, x = "", i
        while True:
            s = chr(ord("A") + x % 26) + s
            x = x // 26 - 1
            if x < 0:
                break
        out.append(s)
    return out


def split_position(position: str) -> tuple[str, str] | None:
    """Split 'B7' into ('B','7'). Returns None for unparseable input."""
    m = _POSITION_RE.match(position.strip())
    if not m:
        return None
    # Prefer letters-as-row when both parts could be either.
    left, right = m.group(1), m.group(2)
    if left.isdigit() and right.isdigit():
        return None
    return (left, right) if not left.isdigit() else (right, left)


def validate_position(position: str, container: dict) -> Optional[str]:
    if not position.strip():
        return None
    parts = split_position(position)
    if parts is None:
        return f"position {position!r} is not a well-formed well coordinate like 'B7'"
    row_part, col_part = parts

    rows_n = _int_or_none(container.get("rows"))
    cols_n = _int_or_none(container.get("cols"))
    if rows_n is None or cols_n is None:
        return None  # non-grid container; nothing to check against

    row_labels = _labels(container.get("row_labels"), rows_n, default_row_labels)
    col_labels = _labels(container.get("col_labels"), cols_n, lambda n: [str(i + 1) for i in range(n)])

    if row_part.upper() not in {l.upper() for l in row_labels}:
        return (
            f"position {position!r} uses row {row_part!r}, which does not exist in this "
            f"container (rows are {row_labels[0]}..{row_labels[-1]})"
        )
    if col_part.upper() not in {l.upper() for l in col_labels}:
        return (
            f"position {position!r} uses column {col_part!r}, which does not exist in this "
            f"container (columns are {col_labels[0]}..{col_labels[-1]})"
        )
    return None


def _labels(raw: str, count: int, fallback) -> list[str]:
    raw = (raw or "").strip()
    if raw:
        parts = [p.strip() for p in re.split(r"[,;|\s]+", raw) if p.strip()]
        if len(parts) == count:
            return parts
    return fallback(count)


def _int_or_none(value) -> Optional[int]:
    try:
        s = str(value).strip()
        return int(s) if s else None
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# whole-database validation
# ---------------------------------------------------------------------------

def validate(db: Database) -> list[Violation]:
    out: list[Violation] = []
    for table in db.tables.values():
        out.extend(_validate_table_shape(table))
        out.extend(_validate_fields(table))
    out.extend(_validate_foreign_keys(db))
    out.extend(_validate_extensions(db))
    out.extend(_validate_containers(db))
    out.extend(_validate_positions(db))
    out.extend(_validate_status_location(db))
    out.extend(_validate_sample_project_rule(db))
    out.extend(_validate_measurements(db))
    out.extend(_validate_item_links(db))
    return out


def _validate_table_shape(t: Table) -> list[Violation]:
    out: list[Violation] = []
    if t.missing:
        return out
    pk = t.spec.primary_key
    for f in t.spec.fields:
        if f.name not in t.header and f.name != pk:
            out.append(
                Violation(t.name, "", f.name, "column_missing", "warning",
                          f"column {f.name!r} is absent from the file; it will be treated as empty")
            )
    for c in t.unknown_columns():
        out.append(
            Violation(t.name, "", c, "unknown_column", "warning",
                      f"column {c!r} is not part of the schema; its values are preserved but ignored")
            )
    seen: dict[str, int] = {}
    for i, row in enumerate(t.rows):
        key = row.get(pk, "").strip()
        rid = key or f"#{i + 2}"
        if not key:
            out.append(Violation(t.name, rid, pk, "missing_pk", "error",
                                 f"row has no {pk}; it cannot be referenced or updated"))
            continue
        if key in seen:
            out.append(Violation(t.name, rid, pk, "duplicate_pk", "error",
                                 f"{pk} {key!r} also appears on row {seen[key] + 2}; "
                                 "the later row shadows the earlier one"))
        else:
            seen[key] = i
        if row.get(EXTRA):
            out.append(Violation(t.name, rid, "", "extra_cells", "warning",
                                 f"row has more cells than the header describes: {row[EXTRA]!r}"))
        if t.spec.id_prefix:
            parsed = ids.parse_id(key)
            if parsed is None:
                out.append(Violation(t.name, rid, pk, "malformed_id", "error",
                                     f"id {key!r} is not a generated id (expected {t.spec.id_prefix}_xxxxxxxx); "
                                     "ids are assigned by the server"))
            elif parsed[0] != t.spec.id_prefix:
                out.append(Violation(t.name, rid, pk, "wrong_id_prefix", "error",
                                     f"id {key!r} carries prefix {parsed[0]!r}, expected {t.spec.id_prefix!r}"))
    return out


def _validate_fields(t: Table) -> list[Violation]:
    out: list[Violation] = []
    if t.missing:
        return out
    pk = t.spec.primary_key
    for i, row in enumerate(t.rows):
        rid = row.get(pk, "").strip() or f"#{i + 2}"
        for f in t.spec.fields:
            raw = row.get(f.name, "")
            if isinstance(raw, dict):
                continue
            value = (raw or "").strip()
            if not value:
                if f.required and f.name != pk:
                    out.append(Violation(t.name, rid, f.name, "required", "error",
                                         f"{f.name} is required"))
                continue
            if f.enum and value not in f.enum:
                out.append(Violation(t.name, rid, f.name, "bad_enum", "error",
                                     f"{value!r} is not one of {', '.join(f.enum)}"))
            if f.type == "id" and f.fk:
                if ids.parse_id(value) is None:
                    target = f.fk.split(".")[0]
                    hint = ""
                    if ids.looks_like_human_label(value):
                        hint = " -- this looks like a human label; the foreign key needs the generated id"
                    out.append(Violation(t.name, rid, f.name, "bad_id_format", "error",
                                         f"{value!r} is not a valid id for {target}{hint}"))
            elif f.type == "int":
                _, problem = parse_int(value)
                if problem:
                    out.append(Violation(t.name, rid, f.name, "bad_int", "error", problem))
            elif f.type == "float":
                _, problem = parse_float(value)
                if problem:
                    out.append(Violation(t.name, rid, f.name, "bad_float", "error", problem))
            elif f.type == "date":
                _, problem = parse_date(value)
                if problem:
                    out.append(Violation(t.name, rid, f.name, "bad_date", "error", problem))
            elif f.type == "datetime":
                _, problem = parse_datetime(value)
                if problem:
                    out.append(Violation(t.name, rid, f.name, "bad_datetime", "error", problem))
    return out


def _validate_foreign_keys(db: Database) -> list[Violation]:
    out: list[Violation] = []
    targets: dict[str, set[str]] = {
        name: t.pks() for name, t in db.tables.items()
    }
    for t in db.tables.values():
        if t.missing:
            continue
        pk = t.spec.primary_key
        for i, row in enumerate(t.rows):
            rid = row.get(pk, "").strip() or f"#{i + 2}"
            for f in t.spec.fields:
                if not f.fk:
                    continue
                value = (row.get(f.name, "") or "").strip()
                if not value:
                    continue
                target_table, target_col = f.fk.split(".")
                known = targets.get(target_table, set())
                if value in known:
                    continue
                # An extension row's fk to its supertype is reported elsewhere.
                if t.spec.supertype and f.fk_table == t.spec.supertype:
                    continue
                out.append(Violation(t.name, rid, f.name, "fk_dangling", "error",
                                     f"{f.name}={value!r} does not exist in {target_table}.{target_col}"))
    return out


def _validate_extensions(db: Database) -> list[Violation]:
    out: list[Violation] = []
    items = db.items_by_id()
    for iid, item in items.items():
        item_class = (item.get("class", "") or "").strip()
        expected_prefix = schema.ITEM_CLASS_PREFIX.get(item_class)
        if not expected_prefix:
            continue
        parsed = ids.parse_id(iid)
        if parsed is None:
            continue  # malformed_id is already reported by table-shape checks
        if parsed[0] != expected_prefix:
            out.append(Violation("items", iid, "item_id", "class_prefix_mismatch", "error",
                                 f"class is {item_class!r} but the id carries prefix "
                                 f"{parsed[0]!r}; expected {expected_prefix}_"))
    for item_class, ext_name in schema.CLASS_EXTENSION_TABLE.items():
        ext = db.t(ext_name)
        if ext.missing:
            continue
        covered: set[str] = set()
        for row in ext.rows:
            iid = (row.get("item_id", "") or "").strip()
            if not iid:
                continue
            covered.add(iid)
            item = items.get(iid)
            if item is None:
                out.append(Violation(ext_name, iid, "item_id", "orphan_extension", "error",
                                     f"extension row exists but items has no row {iid!r}"))
            elif (item.get("class", "") or "").strip() != item_class:
                out.append(Violation(ext_name, iid, "item_id", "class_mismatch", "error",
                                     f"listed in {ext_name} but items.class is "
                                     f"{item.get('class', '')!r}"))
        for iid, item in items.items():
            if (item.get("class", "") or "").strip() != item_class:
                continue
            if (item.get("deleted_at", "") or "").strip():
                continue
            if iid not in covered:
                out.append(Violation("items", iid, "class", "missing_extension", "error",
                                     f"class is {item_class!r} but {ext_name} has no row for it"))
    return out


def _validate_containers(db: Database) -> list[Violation]:
    out: list[Violation] = []
    t = db.t("containers")
    if t.missing:
        return out
    by_id = db.containers_by_id()
    for cid, row in by_id.items():
        parent = (row.get("parent_id", "") or "").strip()
        if parent and parent == cid:
            out.append(Violation("containers", cid, "parent_id", "self_parent", "error",
                                 "container is its own parent"))
        # walk ancestors looking for a cycle
        seen = {cid}
        cur = parent
        while cur and cur in by_id:
            if cur in seen:
                out.append(Violation("containers", cid, "parent_id", "cycle", "error",
                                     "container hierarchy contains a cycle"))
                break
            seen.add(cur)
            cur = (by_id[cur].get("parent_id", "") or "").strip()
        rows_v = _int_or_none(row.get("rows"))
        cols_v = _int_or_none(row.get("cols"))
        if (rows_v is None) != (cols_v is None):
            out.append(Violation("containers", cid, "rows", "half_geometry", "warning",
                                 "only one of rows/cols is set; neither is used without the other"))
        if rows_v is not None and cols_v is not None:
            if rows_v <= 0 or cols_v <= 0:
                out.append(Violation("containers", cid, "rows", "bad_geometry", "error",
                                     f"geometry {rows_v}x{cols_v} is not positive"))
            elif rows_v * cols_v > 2000:
                out.append(Violation("containers", cid, "rows", "implausible_geometry", "warning",
                                     f"geometry {rows_v}x{cols_v} exceeds 2000 wells; check for a typo"))
            elif row.get("kind", "") != "box":
                # A grid means wells, and positions are checked against it. Furniture
                # that holds boxes has slots, not wells, so its number belongs in
                # capacity -- where counting a slot as a well would quietly lie.
                out.append(Violation(
                    "containers", cid, "rows", "grid_on_furniture", "warning",
                    f"{row.get('kind', '')!r} declares a {rows_v}x{cols_v} well grid; "
                    "grids mean wells and belong on a box. If this holds nested "
                    "containers, record them as capacity instead."
                ))
        cap_v = _int_or_none(row.get("capacity"))
        if cap_v is not None and cap_v <= 0:
            out.append(Violation("containers", cid, "capacity", "bad_capacity", "error",
                                 f"capacity {cap_v} is not positive"))
        elif cap_v is not None and rows_v and cols_v and cap_v != rows_v * cols_v:
            out.append(Violation(
                "containers", cid, "capacity", "capacity_disagrees_with_grid", "warning",
                f"capacity {cap_v} contradicts the {rows_v}x{cols_v} grid "
                f"({rows_v * cols_v} wells); the stated capacity wins"
            ))
    return out


def _validate_positions(db: Database) -> list[Violation]:
    out: list[Violation] = []
    containers = db.containers_by_id()
    items = db.t("items")
    if items.missing:
        return out
    per_well: dict[tuple[str, str], list[str]] = {}
    for iid, row in db.items_by_id().items():
        if (row.get("deleted_at", "") or "").strip():
            continue
        loc = (row.get("location_id", "") or "").strip()
        pos = (row.get("position", "") or "").strip()
        if loc and loc not in containers:
            continue  # already reported by fk_dangling
        if pos:
            if not loc:
                out.append(Violation("items", iid, "position", "position_without_location", "error",
                                     f"well {pos!r} is recorded but no container is set"))
                continue
            container = containers[loc]
            problem = validate_position(pos, container)
            if problem:
                out.append(Violation("items", iid, "position", "bad_position", "error", problem))
            per_well.setdefault((loc, pos.upper()), []).append(iid)
    for (loc, pos), occupants in sorted(per_well.items()):
        if len(occupants) > 1:
            out.append(Violation("items", occupants[0], "position", "double_booked_well", "error",
                                 f"well {pos} of {containers[loc].get('label', loc)!r} is claimed by "
                                 f"{len(occupants)} items: {', '.join(occupants)}"))
    # slots vs capacity
    for cid, container in containers.items():
        cap = db.capacity_of(cid)
        if cap is None:
            continue
        occ = db.occupants(cid)
        kids = db.child_containers(cid)
        used = len(occ) + len(kids)
        if used > cap:
            what = " and ".join(
                f"{n} {word}" for n, word in ((len(occ), "items"), (len(kids), "containers")) if n
            )
            out.append(Violation("containers", cid, "capacity", "over_capacity", "error",
                                 f"{what} are stored here but capacity is {cap}"))
        elif used == cap:
            out.append(Violation("containers", cid, "", "at_capacity", "warning",
                                 f"container is full ({used}/{cap})"))
    return out


def _validate_status_location(db: Database) -> list[Violation]:
    out: list[Violation] = []
    for iid, row in db.items_by_id().items():
        if (row.get("deleted_at", "") or "").strip():
            continue
        status = (row.get("status", "") or "").strip()
        loc = (row.get("location_id", "") or "").strip()
        if status == "consumed" and loc:
            out.append(Violation("items", iid, "location_id", "consumed_with_location", "error",
                                 "a consumed item cannot occupy a storage slot; clear the location"))
    return out


def _validate_sample_project_rule(db: Database) -> list[Violation]:
    """If a sample names both a patient and a project, the patient must belong
    to that project (or to an ancestor of it)."""
    out: list[Violation] = []
    samples = db.t("samples")
    if samples.missing:
        return out
    memberships: dict[str, set[str]] = {}
    for row in db.t("patient_projects").rows:
        pid = (row.get("patient_id", "") or "").strip()
        prid = (row.get("project_id", "") or "").strip()
        if pid and prid:
            memberships.setdefault(pid, set()).add(prid)
    projects = {r["project_id"].strip(): r for r in db.t("projects").rows if r.get("project_id", "").strip()}

    def ancestors(project_id: str) -> set[str]:
        chain, seen = set(), set()
        cur: Optional[str] = project_id
        while cur and cur in projects and cur not in seen:
            seen.add(cur)
            chain.add(cur)
            cur = (projects[cur].get("parent_project_id", "") or "").strip() or None
        return chain

    for row in samples.rows:
        iid = (row.get("item_id", "") or "").strip()
        patient = (row.get("patient_id", "") or "").strip()
        project = (row.get("project_id", "") or "").strip()
        if not (patient and project):
            continue
        allowed = memberships.get(patient, set())
        # Membership at an ancestor of the sample's project counts: a participant
        # enrolled in 'circa-somatic' is enrolled in its subprojects too.
        if allowed & ancestors(project):
            continue
        out.append(Violation("samples", iid, "project_id", "project_not_in_patient_projects", "error",
                             f"sample belongs to project {project!r} but patient {patient!r} is not "
                             "registered under it or any of its ancestors"))
    return out


def _validate_measurements(db: Database) -> list[Violation]:
    out: list[Violation] = []
    t = db.t("measurements")
    if t.missing:
        return out
    assays = db.assays_by_id()
    live_ids: set[str] = set()
    for row in t.rows:
        mid = (row.get("measurement_id", "") or "").strip()
        if mid:
            live_ids.add(mid)
    for row in t.rows:
        mid = (row.get("measurement_id", "") or "").strip() or "?"
        assay_id = (row.get("assay_id", "") or "").strip()
        assay = assays.get(assay_id)
        num = (row.get("value_num", "") or "").strip()
        text = (row.get("value_text", "") or "").strip()
        unit = (row.get("unit", "") or "").strip()
        superseded_by = [r for r in t.rows if (r.get("supersedes", "") or "").strip() == mid]

        if assay is None:
            continue  # fk_dangling covers this
        rtype = assay.get("result_type", "numeric")
        if rtype == "numeric" and not num:
            if not superseded_by:
                out.append(Violation("measurements", mid, "value_num", "missing_numeric_value", "error",
                                     f"assay {assay.get('code', assay_id)!r} is numeric but value_num is empty"))
        if rtype == "text" and not text:
            out.append(Violation("measurements", mid, "value_text", "missing_text_value", "error",
                                 f"assay {assay.get('code', assay_id)!r} is textual but value_text is empty"))
        if num and unit:
            quantity = assay.get("quantity", "other")
            if not units.is_known_unit(quantity, unit):
                hints = ", ".join(sorted(units.compatible_quantity(unit))) or "none"
                out.append(Violation("measurements", mid, "unit", "unit_mismatch", "error",
                                     f"unit {unit!r} is not a {quantity} unit (it fits: {hints})"))
        sup = (row.get("supersedes", "") or "").strip()
        if sup and sup == mid:
            out.append(Violation("measurements", mid, "supersedes", "self_supersede", "error",
                                 "a measurement cannot supersede itself"))
    return out


def _validate_item_links(db: Database) -> list[Violation]:
    out: list[Violation] = []
    t = db.t("item_links")
    if t.missing:
        return out
    children_to_parents: dict[str, set[str]] = {}
    recorded: dict[tuple[str, str], str] = {}
    for row in t.rows:
        lid = (row.get("link_id", "") or "").strip()
        child = (row.get("child_item_id", "") or "").strip()
        parent = (row.get("parent_item_id", "") or "").strip()
        if child and parent and child == parent:
            out.append(Violation("item_links", lid, "parent_item_id", "self_link", "error",
                                 "an item cannot be its own parent"))
        if child and parent:
            pair = (child, parent)
            if pair in recorded:
                out.append(Violation("item_links", lid, "parent_item_id", "duplicate_edge", "error",
                                     f"{child} -> {parent} is recorded twice (also "
                                     f"{recorded[pair]}); two item ids name one edge"))
            else:
                recorded[pair] = lid
            children_to_parents.setdefault(child, set()).add(parent)

    def has_cycle() -> Optional[str]:
        state: dict[str, int] = {}

        def visit(node: str) -> bool:
            if state.get(node) == 1:
                return True
            if state.get(node) == 2:
                return False
            state[node] = 1
            for p in children_to_parents.get(node, ()):
                if visit(p):
                    return True
            state[node] = 2
            return False

        for node in list(children_to_parents):
            if visit(node):
                return node
        return None

    cyclic = has_cycle()
    if cyclic:
        out.append(Violation("item_links", cyclic, "parent_item_id", "derivation_cycle", "error",
                             "derivation links contain a cycle -- an item is its own ancestor"))
    return out


# ---------------------------------------------------------------------------
# scoped validation for writes
# ---------------------------------------------------------------------------

def validate_scope(db: Database, touched_tables: Iterable[str]) -> list[Violation]:
    """Validate only what a pending write affects.

    Whole-database checks run against the candidate state, then results are
    filtered to the tables in play -- cheaper than a full audit on every keystroke
    and enough to block a bad write, since every rule we enforce is local to at
    most two tables.
    """
    scope = set(touched_tables)
    all_violations = validate(db)
    relevant = {
        v
        for v in all_violations
        if v.table in scope
        or (v.table == "<storage>")
        or _involves(v, scope)
    }
    return sorted(relevant, key=lambda v: (v.severity != "error", v.table, v.row_id, v.field))


def _involves(v: Violation, scope: set[str]) -> bool:
    """Cross-table violations are filed under one table but caused by another."""
    if v.code == "fk_dangling":
        return True
    if v.code in {"orphan_extension", "missing_extension", "class_mismatch",
                  "class_prefix_mismatch"}:
        return True
    if v.code == "project_not_in_patient_projects":
        return True
    if v.code in {"double_booked_well", "over_capacity", "at_capacity", "bad_position"}:
        return True
    return False
