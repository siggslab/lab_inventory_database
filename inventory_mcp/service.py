"""The service layer: every read and every mutation, transport-agnostic.

``tools.py`` is a thin adapter over this module and a dashboard may import it
directly. Nothing here knows MCP exists -- that is what lets a third-party
consumer reuse the schema knowledge and the validators instead of reimplementing
a second reader that quietly disagrees with the first.

All writes go through :meth:`Service._commit`, which enforces one order:
validate -> compare-and-swap -> snapshot -> write. Validating before touching
storage means a rejected write leaves nothing behind; snapshotting before
writing means an accepted write can always be undone by copying files back.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from . import db as dbmod
from . import ids, schema, units, validation
from .config import Config
from .store import make_store
from .store.base import EXTRA_COLUMNS_KEY, RevisionConflict, StoreError, TableStore, compute_revision

# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------


class ServiceError(RuntimeError):
    pass


class ValidationError(ServiceError):
    """A write that would leave the database invalid.

    ``introduced`` separates the violations this call caused from the ones already
    standing in the files. From the violation set alone the two are indistinguishable,
    and they warrant opposite advice: one is a caller who typed something wrong, the
    other is somebody else's data -- possibly a deliberate edit that merely looks wrong.
    """

    def __init__(self, violations: list[validation.Violation], introduced=None):
        self.violations = violations
        keys = {validation.key(v) for v in (introduced if introduced is not None else violations)}
        self.introduced = [v for v in violations if validation.key(v) in keys]
        self.preexisting = [v for v in violations if validation.key(v) not in keys]
        lines = "\n".join("  - " + str(v) for v in violations)
        super().__init__(f"write refused, {len(violations)} problem(s):\n{lines}")


class NotFound(ServiceError):
    pass


class Conflict(ServiceError):
    """Business-rule conflict that is not a storage race."""


@dataclass
class Snapshot:
    path: Optional[str]
    tables: tuple[str, ...]

    def as_dict(self) -> dict:
        return {"path": self.path, "tables": list(self.tables)}


@dataclass
class CommitResult:
    operation: str
    touched: tuple[str, ...]
    snapshot: Snapshot
    records: list[dict] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "operation": self.operation,
            "touched_tables": list(self.touched),
            "backup": self.snapshot.as_dict(),
            "records": self.records,
            "warnings": self.warnings,
        }


# ---------------------------------------------------------------------------
# query helpers
# ---------------------------------------------------------------------------


@dataclass
class ItemFilter:
    text: str = ""
    item_class: str = ""
    status: str = ""
    project_id: str = ""
    patient_id: str = ""
    container_id: str = ""
    sample_type: str = ""
    gml_id: str = ""
    unlocated: bool = False
    include_deleted: bool = False
    has_position: Optional[bool] = None
    no_project: bool = False
    no_patient: bool = False
    received_since: str = ""
    received_before: str = ""
    expires_before: str = ""
    measured_after: str = ""


#: Dates are ISO strings compared as text, which is why a bare year or a
#: year-month bounds a full date correctly ("2024-11-13" >= "2024"), and why a date
#: bounds a timestamp the same way ("2026-10-01T09:30Z" >= "2026-10-01"). A time is
#: accepted because ``measured_on`` carries one -- pasting a reading's own stamp back
#: as the bound is the common case -- and so is its zone marker. Anything else is a typo
#: that would quietly answer nothing, so it is refused instead. The marker is dropped
#: before comparing; see ``validation.strip_zone``.
_DATE_BOUND = re.compile(
    r"^\d{4}(-\d{2}(-\d{2}([T ]\d{2}:\d{2}(:\d{2})?)?)?)?([zZ]|[+-]\d{2}:?\d{2})?$")

_SORTS = ("name", "status", "class", "received_on", "updated_at")


def _page(rows: list, cap: Optional[int], offset: int | str) -> tuple[list, int, bool]:
    """One page of an ordered result, and whether another page exists.

    ``cap`` of ``None`` means no ceiling. ``truncated`` answers "is there more past this
    page", not "was this page shortened" -- the two differ on the last page, where a short
    answer would otherwise read as though the walk had stopped early. A caller that only
    ever asks for one page learns the same thing it did before.
    """
    try:
        off = int(offset or 0)
    except (TypeError, ValueError):
        raise ServiceError(f"offset {offset!r} is not a row count") from None
    if off < 0:
        raise ServiceError(f"offset must be 0 or more, got {off}")
    if cap is not None and cap < 1:
        raise ServiceError(f"limit must be 1 or more to cap a page, got {cap}")
    page = rows[off:] if cap is None else rows[off:off + cap]
    return page, off, off + len(page) < len(rows)


_STATUS_ORDER = {"present": 0, "partially_consumed": 1, "consumed": 2}

# Which column holds "what this is called" and "the free text about it" for each table a
# model may edit prose on. They differ -- a box has a label, a project keeps its prose in
# ``description`` -- and the caller has no reason to have memorised that, so the id picks
# the column instead of being asked to name it.
_NAME_COLUMN = {"items": "name", "projects": "name", "assays": "name",
                "containers": "label", "patients": "label"}
_NOTES_COLUMN = {"items": "notes", "containers": "notes", "patients": "notes",
                 "assays": "notes", "projects": "description"}

# Class-specific descriptive fields and the tables that hold them. ``handler`` lives on the
# supertype, so it is legal whatever the item turns out to be.
_DATA_FIELDS: dict[str, tuple[str, ...]] = {
    "source_material": ("samples",),
    "collected_on": ("samples",),
    "received_on": ("samples",),
    "processed_on": ("samples",),
    "gml_id": ("samples",),
    "storage_requirements": ("reagents", "kits", "consumables"),
    "expires_on": ("reagents", "kits", "consumables"),
    "price": ("reagents", "kits", "consumables"),
    "handler": ("items",),
}


def _or_join(names: tuple[str, ...]) -> str:
    """``"samples"``, ``"reagents, kits or consumables"`` -- a refusal should read as a
    sentence, not as a tuple repr."""
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + f" or {names[-1]}"


def _id_list(ids: list[str], cap: int = 10) -> str:
    """The ids a refusal is blocking on, spelled out so each one can be acted on.
    Capped -- a full box is a ``get_contents`` call away, not a paragraph."""
    shown = ", ".join(ids[:cap])
    return shown + (f" (+{len(ids) - cap} more)" if len(ids) > cap else "")


def _other_projects(links: list[dict], project_id: str) -> dict[str, list[str]]:
    """Every participant in ``project_id``, mapped to the projects they are enrolled
    in besides it. Sorted, so exactly one alternative reads as a deduction rather than
    an arbitrary pick; empty means this project was their only one."""
    members = {(r.get("patient_id", "") or "").strip() for r in links
               if (r.get("project_id", "") or "").strip() == project_id}
    members.discard("")
    out: dict[str, list[str]] = {who: [] for who in members}
    for r in links:
        who = (r.get("patient_id", "") or "").strip()
        prj = (r.get("project_id", "") or "").strip()
        if who in out and prj and prj != project_id and prj not in out[who]:
            out[who].append(prj)
    for known in out.values():
        known.sort()
    return out


def _order_rows(rows: list[dict], sort: str) -> list[dict]:
    """Order by one named column; a leading ``-`` reverses it.

    Empty is today's behaviour -- name ascending, which is what every existing
    caller sees. ``status`` orders by what the tube has been through rather than
    alphabetically, because partially-consumed-before-present is not an ordering
    anyone means.
    """
    spec = (sort or "").strip().lower()
    if not spec:
        return rows
    reverse = spec.startswith("-")
    key = spec.lstrip("-+")
    if key not in _SORTS:
        raise ServiceError(
            f"sort {sort!r} is not a sortable field; use one of "
            + ", ".join(_SORTS)
            + ", prefixed with - for descending"
        )

    def value(row: dict):
        if key == "status":
            return _STATUS_ORDER.get(row.get("status", ""), 9)
        got = row.get(key) or (row.get("sample") or {}).get(key, "")
        return str(got).lower()

    return sorted(rows, key=value, reverse=reverse)


class Service:
    def __init__(self, config: Config, store: Optional[TableStore] = None):
        self.config = config
        self.store = store or make_store(config)
        self._db: Optional[dbmod.Database] = None
        self._violations: list[validation.Violation] = []
        self._bulk_depth = 0
        self._bulk_snapshot: Optional[Snapshot] = None

    # -- lifecycle -----------------------------------------------------------

    @classmethod
    def open(cls, config: "Config | str | None" = None, path: str | None = None) -> "Service":
        """Accept a Config, a path to one, or nothing (use the default location)."""
        if isinstance(config, (str, os.PathLike)):
            cfg = Config.load(config)
        elif config is not None:
            cfg = config
        else:
            cfg = Config.load(path)
        return cls(cfg)

    def reload(self) -> "Service":
        self._db, self._violations = dbmod.load(self.store)
        return self

    @property
    def db(self) -> dbmod.Database:
        if self._db is None:
            self.reload()
        assert self._db is not None
        return self._db

    @property
    def violations(self) -> list[validation.Violation]:
        _ = self.db
        return list(self._violations)

    def actor(self) -> str:
        return (
            self.config.actor
            or os.environ.get("INVENTORY_MCP_USER")
            or os.environ.get("USER")
            or "unknown"
        )

    def local_root(self) -> Path:
        """The on-disk root, for callers that need to point at the files themselves."""
        if not self.config.local_root:
            raise ServiceError(
                "this database is not on the local filesystem; pass an explicit "
                "directory= where a local copy is wanted"
            )
        return Path(self.config.local_root).expanduser().resolve()

    # -- reads ---------------------------------------------------------------

    def doctor(self) -> dict:
        self.reload()
        db = self.db
        counts = {name: len(t.rows) for name, t in db.tables.items()}
        missing = [name for name, t in db.tables.items() if t.missing]
        errs = [v for v in self._violations if v.severity == "error"]
        warns = [v for v in self._violations if v.severity == "warning"]
        return {
            "backend": self.store.describe(),
            "table_counts": counts,
            "missing_tables": missing,
            "stray_files": list(db.stray_files),
            "error_count": len(errs),
            "warning_count": len(warns),
            "errors": [v.as_dict() for v in errs],
            "warnings": [v.as_dict() for v in warns],
            "ok": not errs,
        }

    def get_item(self, item_id: str) -> dict:
        db = self.db
        row = db.items_by_id().get(item_id.strip())
        if row is None:
            raise NotFound(f"no item with id {item_id!r}")
        return self._merged_item(row)

    def search_items(self, sort: str = "", limit: int = 0, offset: int | str = 0,
                     **kwargs) -> dict:
        # Ordered and capped while the records are still whole -- ``received_on``
        # lives on the extension row, which the lean pass strips away.
        rows = _order_rows(self._matching_items(**kwargs), sort)
        page, off, more = _page(rows, limit or None, offset)
        return {
            "matched": len(rows),
            "offset": off,
            "truncated": more,
            "rows": [self._lean_row(r) for r in page],
        }

    def _matching_items(self, **kwargs) -> list[dict]:
        """Filtered items as complete summary records.

        The untrimmed sibling of ``search_items``: same filters, no context
        diet. Internal consumers (rollups, CLI) read it so that the model-facing
        rows can stay as thin as they want.
        """
        f = ItemFilter(**kwargs)
        db = self.db
        for name in ("received_since", "received_before", "expires_before", "measured_after"):
            bound = getattr(f, name)
            if bound and not _DATE_BOUND.match(bound.strip()):
                raise ServiceError(
                    f"{name} {bound!r} is not a date; use YYYY-MM-DD, or a bare "
                    "YYYY / YYYY-MM to bound a whole year or month"
                )
        items = db.items_by_id()
        ext_index = self._extension_index()
        containers = db.containers_by_id()
        needle = f.text.lower().strip()
        # A reading-bound filter asks "has this item been measured lately", which is
        # a fact about the measurements table, so it is resolved once here rather
        # than per row. live() yields once -- materialise it.
        measured_after = validation.strip_zone(f.measured_after)
        recently_measured = (
            {(m.get("item_id", "") or "").strip()
             for m in db.t("measurements").live()
             if validation.strip_zone(m.get("measured_on", "")) >= measured_after}
            if measured_after else None
        )
        out: list[dict] = []
        for iid, row in items.items():
            deleted = bool((row.get("deleted_at", "") or "").strip())
            if deleted and not f.include_deleted:
                continue
            if f.item_class and row.get("class", "") != f.item_class:
                continue
            if f.status and row.get("status", "") != f.status:
                continue
            loc = (row.get("location_id", "") or "").strip()
            if f.unlocated and loc:
                continue
            if f.container_id and loc != f.container_id:
                continue
            if f.has_position is not None:
                has = bool((row.get("position", "") or "").strip())
                if has != f.has_position:
                    continue
            ext = ext_index.get(iid, {})
            if f.sample_type and ext.get("sample_type", "") != f.sample_type:
                continue
            if f.gml_id and ext.get("gml_id", "").lower() != f.gml_id.lower():
                continue
            if f.patient_id and ext.get("patient_id", "") != f.patient_id:
                continue
            if f.project_id and ext.get("project_id", "") != f.project_id:
                continue
            if f.no_project and (
                row.get("class", "") != "sample" or (ext.get("project_id", "") or "").strip()
            ):
                continue
            if f.no_patient and (
                row.get("class", "") != "sample" or (ext.get("patient_id", "") or "").strip()
            ):
                continue
            # An unrecorded date is not "before" anything, so a bounded row must
            # carry one: a tube nobody dated never shows up as received last spring.
            received = (ext.get("received_on", "") or "").strip()
            if f.received_since and not (received and received >= f.received_since.strip()):
                continue
            if f.received_before and not (received and received < f.received_before.strip()):
                continue
            expires = (ext.get("expires_on", "") or "").strip()
            if f.expires_before and not (expires and expires < f.expires_before.strip()):
                continue
            if recently_measured is not None and iid not in recently_measured:
                continue
            if needle:
                hay = " ".join(
                    filter(
                        None,
                        [
                            row.get("name", ""),
                            row.get("notes", ""),
                            row.get("handler", ""),
                            ext.get("gml_id", ""),
                            ext.get("source_material", ""),
                            containers.get(loc, {}).get("label", ""),
                        ],
                    )
                ).lower()
                if needle not in hay:
                    continue
            out.append(self._merged_item(row, summary=True))
        out.sort(key=lambda r: r["name"].lower())
        return out

    # A search hit answers "is this the one I mean?"; everything else is
    # get_item's job. Ids of related objects are omitted too: wanting them is
    # a decision to look at that object, not a thing to stumble into.
    _LEAN_OMIT = ("class", "handler", "notes", "updated_by")
    _LEAN_OMIT_LOCATION = ("container_id", "position")
    _LEAN_OMIT_SAMPLE = ("source_material", "patient_id", "project_id",
                         "collected_on", "received_on", "processed_on")

    @staticmethod
    def _lean_row(item: dict) -> dict:
        if item.pop("deleted_at", ""):
            item["deleted"] = True
        for key in Service._LEAN_OMIT:
            item.pop(key, None)
        for block, keys in (
            ("location", Service._LEAN_OMIT_LOCATION),
            ("sample", Service._LEAN_OMIT_SAMPLE),
        ):
            sub = item.get(block)
            if sub:
                for key in keys:
                    sub.pop(key, None)
        return item

    def list_containers(self, kind: str = "", parent_id: str = "") -> list[dict]:
        """Every container with its own figures -- what ``ls --containers`` prints.

        No MCP tool wraps this any more; ``search_containers`` answers the model-facing
        question and ``get_occupancy`` the fill figures. The CLI still walks it whole.
        """
        db = self.db
        out = []
        for row in db.t("containers").rows:
            cid = (row.get("container_id", "") or "").strip()
            if not cid:
                continue
            if kind and row.get("kind", "") != kind:
                continue
            if parent_id and (row.get("parent_id", "") or "").strip() != parent_id:
                continue
            out.append(self._container_summary(cid, row))
        out.sort(key=lambda r: " / ".join(r["path"]))
        return out

    def search_containers(
        self, query: str, kind: str = "", within_id: str = "", limit: int = 20,
        offset: int | str = 0
    ) -> dict:
        """Containers whose id or label matches ``query``, as places rather than numbers.

        Matching runs against the containers table, so a box holding nothing still comes
        back -- the difference between "empty" and "no such box". Rows carry identity and
        position only: how full something is belongs to ``get_occupancy``, what is inside
        it to ``get_contents``. Nothing is nested, so no row repeats its ancestry beyond
        ``path``, and there are no child counts to reconcile with a later call.

        ``within_id`` takes the whole subtree, not just the direct children, because
        "what is in freezer 7" means the boxes three levels down too. The named container
        is excluded from its own results -- it is the scope, not a find.

        Rows come back in tree order and stop at ``limit``, reporting ``matched`` and
        ``truncated`` rather than leaving a suspiciously round row count to interpret.
        There is no blank query; every id starts with ``ctr_``, which serves as match-all.
        """
        needle = query.strip().lower()
        if not needle:
            raise ServiceError(
                "query is required; match on a container id or part of a label"
            )
        level = kind.strip()
        if level and level not in schema.CONTAINER_KINDS:
            raise ServiceError(
                f"kind {level!r} is not a container kind "
                f"({' '.join(schema.CONTAINER_KINDS)})"
            )
        try:
            cap = max(1, int(limit))
        except (TypeError, ValueError):
            cap = 20
        db = self.db
        by_id = db.containers_by_id()
        scope: set[str] | None = None
        if within_id.strip():
            wid = within_id.strip()
            if wid not in by_id:
                raise NotFound(f"no container with id {wid!r}")
            scope = self._container_subtree(wid) - {wid}
        found = [
            cid for cid, row in by_id.items()
            if (not level or row.get("kind", "") == level)
            and (scope is None or cid in scope)
            and (needle in cid.lower()
                 or needle in (row.get("label", "") or "").lower())
        ]
        rows = [
            {
                "container_id": cid,
                "kind": by_id[cid].get("kind", ""),
                "label": by_id[cid].get("label", ""),
                "parent_id": (by_id[cid].get("parent_id", "") or "").strip(),
                "path": " / ".join(db.container_path(cid)),
            }
            for cid in sorted(
                found, key=lambda c: " / ".join(db.container_path(c)).lower()
            )
        ]
        page, off, more = _page(rows, cap, offset)
        return {"matched": len(rows), "offset": off, "truncated": more, "rows": page}

    def get_occupancy(self, container_id: str, kind: str = "") -> dict:
        """How full one container is, reported at the granularity ``kind`` names.

        ``rows`` are the things of that kind inside the target: the containers
        directly nested in it when ``kind`` is empty, every container of that kind
        further down, or the items it holds when ``kind`` names an item class. The
        target itself counts as a row when its own kind matches, so asking about a
        box in units of boxes reports the box. Each row carries its own figures.

        ``summary`` measures the containers the rows live in, which is what keeps
        the numbers in the same unit as the question: ask about samples and you get
        well grids, ask about boxes and you get the slots their racks offer. Ask a
        container about its own kind and it measures itself instead, so a box queried
        in units of boxes says how full that box is. Nothing is added across tiers --
        five box slots and a hundred wells are not two hundred of anything. ``holds``
        says what fills the measured slots, since a fill figure that could mean tubes
        or boxes is not an answer. ``occupied`` counts every slot taken in that tier,
        including a box squatting on a bench where you hoped to put a tube, so it can
        exceed the number of rows when the filter leaves something out. Nothing here is
        computed from well positions: they are optional per item and inconsistently
        recorded, so an item row repeats its position where one exists and no figure
        depends on whether anyone filled it in.
        """
        if not container_id.strip():
            raise ServiceError("container_id is required; this reports on one container")
        cid = container_id.strip()
        db = self.db
        by_id = db.containers_by_id()
        if cid not in by_id:
            raise NotFound(f"no container with id {cid!r}")
        level = kind.strip()
        if level and level not in schema.CONTAINER_KINDS and level not in schema.ITEM_CLASSES:
            raise ServiceError(
                f"kind {level!r} is neither a container kind "
                f"({' '.join(schema.CONTAINER_KINDS)}) nor an item class "
                f"({' '.join(schema.ITEM_CLASSES)})"
            )

        nested = self._container_subtree(cid)
        report = {
            "target": {
                "container_id": cid,
                "label": by_id[cid].get("label", ""),
                "kind": by_id[cid].get("kind", ""),
            },
            "kind": level or "children",
            "summary": {},
            "rows": [],
        }

        if not level or level in schema.CONTAINER_KINDS:
            if level:
                # The target matches its own kind, so asking about a box in units
                # of boxes reports the box rather than an empty list.
                found = [c for c in nested if by_id[c].get("kind", "") == level]
            else:
                found = [
                    c for c in nested
                    if (by_id[c].get("parent_id", "") or "").strip() == cid
                ]
            rows = []
            for c in sorted(found, key=lambda c: " / ".join(db.container_path(c))):
                cap = db.capacity_of(c)
                used = db.slot_use(c)
                rows.append(
                    {
                        "container_id": c,
                        "label": by_id[c].get("label", ""),
                        "kind": by_id[c].get("kind", ""),
                        "capacity": cap,
                        "occupied": used,
                        "free": (cap - used) if cap is not None else None,
                    }
                )
            report["rows"] = rows
            # The tier measured is where the rows live -- but asking a container about
            # its own level is a question about that container, so it answers for
            # itself rather than for the shelf or rack it happens to stand on.
            homes = {cid} if cid in found else {
                (by_id[c].get("parent_id", "") or "").strip() for c in found
            } - {""}
            report["summary"] = self._tier_stats(homes)
            return report

        holders: set[str] = set()
        rows = []
        matches = [
            r for r in db.t("items").live()
            if (r.get("location_id", "") or "").strip() in nested
            and r.get("class", "") == level
        ]
        for r in sorted(
            matches,
            key=lambda r: (
                " / ".join(db.container_path((r.get("location_id", "") or "").strip())),
                (r.get("position", "") or "").strip(),
                r.get("name", "").lower(),
                r["item_id"],
            ),
        ):
            loc = (r.get("location_id", "") or "").strip()
            holders.add(loc)
            rows.append(
                {
                    "item_id": r["item_id"].strip(),
                    "class": r.get("class", ""),
                    "name": r.get("name", ""),
                    "status": r.get("status", ""),
                    "position": (r.get("position", "") or "").strip(),
                    "in": " / ".join(db.container_path(loc)) if loc else "",
                }
            )
        report["rows"] = rows
        report["summary"] = self._tier_stats(holders)
        return report

    def _tier_stats(self, containers: set[str]) -> dict:
        """Capacity and fill for exactly these containers -- nothing below them.

        Only containers holding the same kind of thing are ever summed, which is what
        keeps ``capacity`` and ``occupied`` the same unit. Capacity stays ``None``
        when none of them has one recorded: "not measurable" and "measurable and
        empty" are different answers, and collapsing them would make an unlabelled
        rack look identical to a full one. Well positions are deliberately absent --
        they are optional per item and inconsistent in practice, so occupancy counts
        slots and leaves the grid to whoever has it recorded.
        """
        db = self.db
        measured = False
        capacity = occupied = items = nested = 0
        for cid in containers:
            cap = db.capacity_of(cid)
            if cap is not None:
                measured = True
                capacity += cap
            occupants = db.occupants(cid)
            kids = db.child_containers(cid)
            items += len(occupants)
            nested += len(kids)
            occupied += len(occupants) + len(kids)
        holds = "" if not containers else (
            "items" if not nested else ("containers" if not items else "mixed")
        )
        return {
            "holds": holds,
            "capacity": capacity if measured else None,
            "occupied": occupied,
            "free": (capacity - occupied) if measured else None,
        }

    def _occupancy_rows(self, container_id: str = "", kind: str = "") -> list[dict]:
        """Each container's own numbers, untrimmed -- what exports and the CLI read.

        Flat and unrolled by design: a row means "this container, its own slots",
        carrying the path and the derived figures that the MCP answer leaves for the
        caller to compute. ``occupied`` counts every slot taken, nested containers
        included, so a rack full of boxes reads full rather than empty. No well
        columns: positions are optional per item, and a fill rate over a grid nobody
        records consistently is a number no dashboard can act on.
        """
        db = self.db
        by_id = db.containers_by_id()
        targets = sorted(
            self._container_subtree(container_id) if container_id else by_id
        )
        out = []
        for cid in targets:
            if cid not in by_id:
                raise NotFound(f"no container with id {cid!r}")
            if kind and by_id[cid].get("kind", "") != kind:
                continue
            occupants = db.occupants(cid)
            cap = db.capacity_of(cid)
            children = [
                c for c in by_id.values() if (c.get("parent_id", "") or "").strip() == cid
            ]
            used = len(occupants) + len(children)
            out.append(
                {
                    "container_id": cid,
                    "label": by_id[cid].get("label", ""),
                    "kind": by_id[cid].get("kind", ""),
                    "path": " / ".join(db.container_path(cid)),
                    "capacity": cap,
                    "occupied": used,
                    "items_held": len(occupants),
                    "child_containers": len(children),
                    "free": (cap - used) if cap is not None else None,
                    "occupancy_pct": (round(100.0 * used / cap, 1) if cap else None),
                    "has_capacity": cap is not None,
                }
            )
        out.sort(key=lambda r: r["path"])
        return out

    def get_contents(self, container_id: str) -> dict:
        """What sits directly in this container -- tubes and boxes alike -- plus room at the top.

        One layer only, deliberately. A freezer reports its shelves and no tubes, because
        that is where its contents actually live; walking the whole subtree here would
        duplicate what ``search_containers`` with ``within_id`` is for, and would bury the
        box's own free space under everything below it.

        Nested containers are rows in their own right and are counted, because a rack
        holding two boxes holds two things -- a caller reading only ``items`` would report
        that freezer as empty. They carry identity alone: how full each one is belongs to
        ``get_occupancy``, which states its figures in one tier's unit rather than mixing
        wells with box positions. Consumed items hold no slot, so they have no location and
        appear nowhere.
        """
        db = self.db
        by_id = db.containers_by_id()
        if container_id not in by_id:
            raise NotFound(f"no container with id {container_id!r}")
        row = by_id[container_id]
        occupants = [
            i for i in db.items_by_id().values()
            if (i.get("location_id", "") or "").strip() == container_id
            and not (i.get("deleted_at", "") or "").strip()
        ]
        kids = sorted(
            (by_id[c] for c in db.child_containers(container_id)),
            key=lambda k: k.get("label", "").lower(),
        )
        cap = db.capacity_of(container_id)
        items = [
            self._contents_row(i)
            for i in sorted(
                occupants,
                key=lambda i: (
                    i.get("position", ""), i.get("name", "").lower(), i["item_id"]
                ),
            )
        ]
        return {
            "container": {
                "container_id": container_id,
                "kind": row.get("kind", ""),
                "label": row.get("label", ""),
                "path": " / ".join(db.container_path(container_id)),
                "geometry": self._geometry(row),
                "capacity": cap,
                # Slots taken is len(items) + len(containers); free stays because it says
                # whether that is measurable at all -- null is not the same as full.
                "free": (cap - len(items) - len(kids)) if cap is not None else None,
            },
            "count": len(items) + len(kids),
            "items": items,
            "containers": [self._contents_container(k) for k in kids],
        }

    @staticmethod
    def _contents_row(item: dict) -> dict:
        return {
            "item_id": item["item_id"],
            "class": item.get("class", ""),
            "name": item.get("name", ""),
            "status": item.get("status", ""),
            "position": (item.get("position", "") or "").strip(),
        }

    @staticmethod
    def _contents_container(row: dict) -> dict:
        return {
            "container_id": row["container_id"].strip(),
            "kind": row.get("kind", ""),
            "label": row.get("label", ""),
        }

    def count_items(self, **kwargs) -> int:
        # Counts the untrimmed records: a count should not pay for building the
        # lean rows nobody asked for, nor read them back out of a capped page.
        return len(self._matching_items(**kwargs))

    def search_patients(self, query: str, project_id: str = "", limit: int = 20,
                        offset: int | str = 0) -> dict:
        """Participants whose id or label matches ``query``, each with sample counts.

        Matching runs against the participants table, not the samples, so an enrolled
        participant with nothing stored still comes back under ``total: 0`` -- which is
        the difference between "not enrolled yet" and "no such person". Only samples
        carry a participant, so supplies are never counted here; ``project_id`` narrows
        what is counted without narrowing who is listed. Rows come back heaviest first
        and stop at ``limit``, with ``matched`` and
        ``truncated`` reported rather than leaving the caller to infer it from a
        suspiciously round row count. There is no blank query; every id starts with
        ``pat_``, so that string serves as match-all.

        ``unassigned`` sits beside the rows rather than among them: how many samples the
        filters matched with no participant recorded. It is a caveat about the cohort,
        not a search result, so it neither occupies a row nor competes with the limit.
        """
        needle = query.strip().lower()
        if not needle:
            raise ServiceError(
                "query is required; match on a participant id or part of a label"
            )
        try:
            cap = max(1, int(limit))
        except (TypeError, ValueError):
            cap = 20
        db = self.db
        matched = [
            p for p in db.t("patients").live()
            if needle in p.get("patient_id", "").lower()
            or needle in p.get("label", "").lower()
        ]
        empty = {"total": 0, "present": 0, "partially_consumed": 0, "consumed": 0}
        counts: dict[str, dict] = {}
        unassigned = dict(empty)
        for it in self._matching_items(item_class="sample", project_id=project_id):
            pid = (it.get("sample") or {}).get("patient_id", "")
            bucket = counts.setdefault(pid, dict(empty)) if pid else unassigned
            bucket["total"] += 1
            if it.get("status", "") in bucket:
                bucket[it["status"]] += 1

        def by_volume(p: dict) -> tuple:
            return (-counts.get(p["patient_id"], empty)["total"],
                    p.get("label", "").lower())

        people = [
            {"key": p["patient_id"], "label": p.get("label", ""),
             **counts.get(p["patient_id"], empty)}
            for p in sorted(matched, key=by_volume)
        ]
        page, off, more = _page(people, cap, offset)
        return {
            "matched": len(people),
            "offset": off,
            "truncated": more,
            "unassigned": unassigned["total"],
            "rows": page,
        }

    def get_patient(self, patient_id: str) -> dict:
        """One participant in full: the record as enrolled, plus what we hold for them.

        ``search_patients`` answers who; this answers what is known about them. Every
        field comes back -- sex, date of birth, affected status, gene, variant,
        protocol, consent date -- because those are written once at enrolment and asked
        about long afterwards, and nothing else on the surface carries them. The
        enrolments are listed rather than counted: "what is this person in" is the
        question, and a count cannot answer it. Sample figures arrive both whole and
        split by project, since the split is why anyone asks.
        """
        pid = (patient_id or "").strip()
        if not pid:
            raise ServiceError("patient_id is required; see search_patients")
        row = next(
            (p for p in self.db.t("patients").live()
             if (p.get("patient_id", "") or "").strip() == pid),
            None,
        )
        if row is None:
            raise NotFound(f"no participant with id {pid!r}; see search_patients")

        projects = {
            (p.get("project_id", "") or "").strip(): p
            for p in self.db.t("projects").live()
        }
        empty = {"total": 0, "present": 0, "partially_consumed": 0, "consumed": 0}
        by_project: dict[str, dict] = {}
        whole = dict(empty)
        for it in self._matching_items(item_class="sample", patient_id=pid):
            bucket = by_project.setdefault(
                (it.get("sample") or {}).get("project_id", ""), dict(empty)
            )
            bucket["total"] += 1
            whole["total"] += 1
            if it.get("status", "") in bucket:
                bucket[it["status"]] += 1
                whole[it["status"]] += 1

        enrolments = []
        for edge in self.db.t("patient_projects").live():
            if (edge.get("patient_id", "") or "").strip() != pid:
                continue
            key = (edge.get("project_id", "") or "").strip()
            project = projects.get(key)
            enrolments.append({
                "project_id": key,
                "name": project.get("name", "") if project else "",
                "status": project.get("status", "") if project else "",
                "samples": by_project.get(key, dict(empty)),
            })
        enrolments.sort(key=lambda e: e["name"].lower())
        # Held for a project the person is not enrolled in is worth seeing, and so is
        # material with no project at all -- both are why the enrolment rows do not sum
        # to ``samples``, which would otherwise read as arithmetic that went wrong.
        orphaned = {k: v for k, v in by_project.items()
                    if k not in {e["project_id"] for e in enrolments}}
        return {
            "patient_id": pid,
            "label": row.get("label", ""),
            "sex": row.get("sex", ""),
            "dob": row.get("dob", ""),
            "affected_status": row.get("affected_status", ""),
            "gene": row.get("gene", ""),
            "variant": row.get("variant", ""),
            "protocol": row.get("protocol", ""),
            "consented_on": row.get("consented_on", ""),
            "notes": row.get("notes", ""),
            "updated_by": row.get("updated_by", ""),
            "updated_at": row.get("updated_at", ""),
            "samples": whole,
            "enrolments": enrolments,
            "unenrolled_samples": orphaned,
        }

    def list_assays(self) -> dict:
        """The measurement registry: what may be recorded, and in which unit.

        Small and finite, so it comes back whole. ``readings`` counts every row filed
        against the assay, corrections included, which is how an assay that was defined
        and never used tells you so -- and it is the count that decides whether
        ``delete_assay`` can land. Rows keep ``assay_id`` because that is what
        ``edit_name``, ``edit_notes`` and ``delete_assay`` take; a reading names its assay
        by ``code``, which is what ``record_measurement`` accepts.
        """
        used: dict[str, int] = {}
        for m in self.db.t("measurements").live():
            key = (m.get("assay_id", "") or "").strip()
            used[key] = used.get(key, 0) + 1
        rows = [
            {
                "assay_id": (a.get("assay_id", "") or "").strip(),
                "code": a.get("code", ""),
                "name": a.get("name", ""),
                "quantity": a.get("quantity", ""),
                "canonical_unit": a.get("canonical_unit", ""),
                "result_type": a.get("result_type", ""),
                "method": a.get("method", ""),
                "readings": used.get((a.get("assay_id", "") or "").strip(), 0),
            }
            for a in self.db.t("assays").live()
        ]
        rows.sort(key=lambda r: r["code"].lower())
        return {"count": len(rows), "rows": rows}

    def search_projects(self, query: str, limit: int = 20, offset: int | str = 0) -> dict:
        """Projects whose id or name matches ``query``, each with what sits under it.

        Matching runs against the projects table rather than the samples, so a project
        with nothing attached still comes back with zeroed figures -- the difference
        between "not started yet" and "no such project".

        Counts are **direct**: a subproject's material belongs to the subproject's row
        and never to its parent's, because summing rows across a match-all query would
        otherwise count the same tube twice. ``subprojects`` says how many sit below, so
        a caller can see there is more without absorbing it.

        Two things live under a project: enrolled participants (``patient_projects``)
        and samples (``samples.project_id``). Reagents, kits and consumables carry no
        project column at all, so they are absent by construction instead of pooled into
        a pseudo-project -- the old rollup filed every supply under a fake bucket named
        "(none)", which made a project total mean two different things at once. Samples
        with no project recorded surface as ``unassigned_samples`` beside the rows.
        """
        needle = query.strip().lower()
        if not needle:
            raise ServiceError(
                "query is required; match on a project id or part of a name"
            )
        try:
            cap = max(1, int(limit))
        except (TypeError, ValueError):
            cap = 20

        db = self.db
        projects = list(db.t("projects").live())  # live() is single-pass; two walks need a list
        matched = [
            p for p in projects
            if needle in p.get("project_id", "").lower()
            or needle in p.get("name", "").lower()
        ]

        children: dict[str, int] = {}
        for p in projects:
            parent = (p.get("parent_project_id", "") or "").strip()
            if parent:
                children[parent] = children.get(parent, 0) + 1

        living = {p["patient_id"] for p in db.t("patients").live()}
        enrolled: dict[str, set] = {}
        for link in db.t("patient_projects").live():
            pid = (link.get("project_id", "") or "").strip()
            who = (link.get("patient_id", "") or "").strip()
            if pid and who in living:
                enrolled.setdefault(pid, set()).add(who)

        empty = {"total": 0, "present": 0, "partially_consumed": 0, "consumed": 0}
        samples: dict[str, dict] = {}
        unassigned = dict(empty)
        for it in self._matching_items(item_class="sample"):
            pid = (it.get("sample") or {}).get("project_id", "")
            bucket = samples.setdefault(pid, dict(empty)) if pid else unassigned
            bucket["total"] += 1
            if it.get("status", "") in bucket:
                bucket[it["status"]] += 1

        def by_volume(p: dict) -> tuple:
            return (-samples.get(p["project_id"], empty)["total"],
                    p.get("name", "").lower())

        rows = [
            {"key": p["project_id"], "label": p.get("name", ""),
             "subprojects": children.get(p["project_id"], 0),
             "participants": len(enrolled.get(p["project_id"], ())),
             "samples": dict(samples.get(p["project_id"], empty))}
            for p in sorted(matched, key=by_volume)
        ]
        page, off, more = _page(rows, cap, offset)
        return {
            "matched": len(rows),
            "offset": off,
            "truncated": more,
            "unassigned_samples": unassigned["total"],
            "rows": page,
        }

    def get_measurements(self, **kwargs) -> list[dict]:
        """Filtered readings, trimmed for a context window. See ``_measurement_rows``."""
        return [self._tool_reading(r) for r in self._measurement_rows(**kwargs)]

    def _measurement_rows(
        self,
        item_id: str = "",
        assay: str = "",
        include_historical: bool = False,
        include_superseded: bool = False,
    ) -> list[dict]:
        """Readings as currently believed: newest per item and assay.

        The untrimmed sibling of ``get_measurements``: same filters, every column.
        Exports read it so the model-facing rows can stay as thin as they want.

        Two ways to ask for more rows than the current belief, both off by default.
        ``include_historical`` adds every earlier reading for the assay, including
        the ones a later reading took over from -- the whole series instead of its
        endpoint. ``include_superseded`` adds just those taken-over readings, on top
        of the newest-per-assay trim. Since a recording retires the reading it
        overtakes, both flags now reach the same rows; they differ only in whether
        the trim still applies.

        An empty ``item_id`` means every item, which is what the exports iterate.
        The MCP tool requires one; this layer does not.
        """
        db = self.db
        assays = db.assays_by_id()
        # A mistyped code must not fall through as "no filter wanted": returning
        # every reading looks reassuringly like a query that worked.
        wanted_assay = self._assay_id_for_code(assay)
        if item_id:
            self._require_item(item_id)
        superseded = {
            (r.get("supersedes", "") or "").strip()
            for r in db.t("measurements").rows
            if (r.get("supersedes", "") or "").strip()
        }
        rows = []
        retired = []
        # Where each reading sits in the table, so a series comes back in the order it
        # was entered -- the same clock that decides which one is current. Sorting by
        # ``measured_on`` instead split a same-minute re-measurement apart, because
        # readings are stamped to the minute.
        position: dict[str, int] = {}
        for index, r in enumerate(db.t("measurements").rows):
            mid = (r.get("measurement_id", "") or "").strip()
            if not mid:
                continue
            position[mid] = index
            if item_id and (r.get("item_id", "") or "").strip() != item_id:
                continue
            assay_id = (r.get("assay_id", "") or "").strip()
            if wanted_assay and assay_id != wanted_assay:
                continue
            meta = assays.get(assay_id, {})
            quantity = meta.get("quantity", "other")
            unit = (r.get("unit", "") or "").strip()
            num_raw = (r.get("value_num", "") or "").strip()
            canonical = None
            if num_raw and unit:
                try:
                    value, canon_unit = units.to_canonical(quantity, float(num_raw), unit)
                    canonical = {"value": value, "unit": canon_unit}
                except units.UnitError:
                    canonical = None
            row = {
                "measurement_id": mid,
                "item_id": (r.get("item_id", "") or "").strip(),
                "assay": meta.get("code", assay_id),
                "assay_name": meta.get("name", ""),
                "quantity": quantity,
                "measured_on": r.get("measured_on", ""),
                "value_num": _opt_float(num_raw),
                "value_text": r.get("value_text", ""),
                "unit": unit,
                "canonical": canonical,
                "handler": r.get("handler", ""),
                "instrument": r.get("instrument", ""),
                "supersedes": r.get("supersedes", ""),
                "superseded": mid in superseded,
                "notes": r.get("notes", ""),
            }
            if mid in superseded:
                retired.append(row)
            else:
                rows.append(row)
        if not include_historical:
            latest: dict[tuple[str, str], dict] = {}
            for r in rows:
                latest[(r["item_id"], r["assay"])] = r
            rows = list(latest.values())
        # A recording retires the reading it overtakes, so the series lives in
        # ``retired`` now rather than in the untrimmed rows: asking for history has
        # to fetch those, or "the whole series" would mean one row per assay.
        if include_superseded or include_historical:
            rows.extend(retired)
        rows.sort(key=lambda x: (x["item_id"], x["assay"], position[x["measurement_id"]]))
        return rows

    # A reading answers what was measured, when, and whether it is still the
    # belief. The assay code already keys the registry, so quantity is one lookup
    # away; the row echoes no item_id because a reading query names its item.
    # ``value``+``unit`` is the number as observed, which is what prose quotes back,
    # and ``canonical`` is the same observation expressed in the assay's unit -- the
    # arithmetic is done for exports anyway, and a comparison the surface keeps
    # private is a comparison the model cannot make. Null for a qualitative reading,
    # which has no magnitude to convert.
    @staticmethod
    def _tool_reading(m: dict) -> dict:
        return {
            "measurement_id": m["measurement_id"],
            "assay": m["assay"],
            "assay_name": m["assay_name"],
            "measured_on": m["measured_on"],
            "value": m["value_num"] if m["value_num"] is not None else m["value_text"],
            "unit": m["unit"],
            "canonical": m["canonical"],
            "handler": m["handler"],
            "instrument": m["instrument"],
            "supersedes": m["supersedes"],
            "superseded": m["superseded"],
            "notes": m["notes"],
        }

    def get_linked_items(self, item_id: str, direction: str = "both", depth: int = 1) -> dict:
        """Derivation edges around one item, ``depth`` hops out.

        Called ``get_linked_items`` rather than anything with "lineage": an aliquot
        is not a descendant of its tube, it is a piece of it.

        The MCP tool walks a single hop and makes the caller ask again, so a trace
        grows only where someone chose to follow it; deeper values exist for Python
        callers building the whole graph at once. Tombstoned ends still appear --
        provenance outlives the tube -- carrying ``deleted: true`` so a discarded
        aliquot is never mistaken for material that still exists.
        """
        db = self.db
        edges = db.t("item_links").rows
        parents: dict[str, list[dict]] = {}
        children: dict[str, list[dict]] = {}
        for e in edges:
            child = (e.get("child_item_id", "") or "").strip()
            parent = (e.get("parent_item_id", "") or "").strip()
            if not child or not parent:
                continue
            # An edge is named by its two endpoints, so the surrogate link_id stays
            # in the table: handing it out would advertise an address nothing here
            # accepts, and every id shipped is one a caller may try to use.
            entry = {"item_id": parent, "role": e.get("role", "")}
            parents.setdefault(child, []).append(entry)
            entry2 = {"item_id": child, "role": e.get("role", "")}
            children.setdefault(parent, []).append(entry2)
        items = db.items_by_id()

        def decorate(refs: list[dict]) -> list[dict]:
            out = []
            for ref in refs:
                row = items.get(ref["item_id"], {})
                out.append(
                    {
                        **ref,
                        "name": row.get("name", "?"),
                        "class": row.get("class", "?"),
                        "status": row.get("status", "?"),
                        "deleted": bool((row.get("deleted_at", "") or "").strip()),
                    }
                )
            return out

        def walk(start: str, direction: str, remaining: int, seen: set[str]) -> dict:
            row = items.get(start, {})
            node = {
                "item_id": start,
                "name": row.get("name", "?"),
                "class": row.get("class", "?"),
                "status": row.get("status", "?"),
                "deleted": bool((row.get("deleted_at", "") or "").strip()),
            }
            if remaining <= 0:
                return node
            if direction in ("parents", "both"):
                nxt = [p for p in parents.get(start, []) if p["item_id"] not in seen]
                if nxt:
                    seen = seen | {p["item_id"] for p in nxt}
                    node["parents"] = [
                        {**decorate([p])[0], **walk(p["item_id"], "parents", remaining - 1, seen)}
                        for p in nxt
                    ]
            if direction in ("children", "both"):
                nxt = [c for c in children.get(start, []) if c["item_id"] not in seen]
                if nxt:
                    seen = seen | {c["item_id"] for c in nxt}
                    node["children"] = [
                        {**decorate([c])[0], **walk(c["item_id"], "children", remaining - 1, seen)}
                        for c in nxt
                    ]
            return node

        if item_id not in items:
            raise NotFound(f"no item with id {item_id!r}")
        return walk(item_id, direction, depth, {item_id})

    def run_query(self, sql: str, limit: int = 500, offset: int = 0) -> dict:
        """Guarded read-only SQL over an in-memory mirror of the tables.

        Exists because ad-hoc questions ('how many PBMC samples per project
        received since 2024?') are clumsy to express through fixed tools. The
        mirror is built from the same loaded tables, so it inherits whatever the
        validator accepted.
        """
        from .query import run_sql

        return run_sql(self.db, sql, limit=limit, offset=offset)

    # -- mutations -----------------------------------------------------------

    def create_container(
        self,
        kind: str,
        label: str,
        parent_id: str = "",
        rows: int | str = "",
        cols: int | str = "",
        row_labels: str = "",
        col_labels: str = "",
        capacity: int | str = "",
        notes: str = "",
    ) -> CommitResult:
        cid = ids.new_id(schema.CONTAINERS.id_prefix)
        row = {
            "container_id": cid,
            "parent_id": parent_id.strip(),
            "kind": kind,
            "label": label,
            "rows": _s(rows),
            "cols": _s(cols),
            "row_labels": row_labels,
            "col_labels": col_labels,
            "capacity": _s(capacity),
            "notes": notes,
        }
        result = self._apply("create_container", [("containers", [row], [])])
        result.records = [self._container_summary(cid, row)]
        return result

    def create_box(self, label: str, rows: int, cols: int, parent_id: str, **kw) -> CommitResult:
        if not parent_id:
            raise ServiceError("a box must be placed somewhere: pass parent_id")
        return self.create_container("box", label, parent_id=parent_id, rows=rows, cols=cols, **kw)

    def create_sample(
        self,
        name: str,
        sample_type: str,
        location_id: str = "",
        position: str = "",
        gml_id: str = "",
        patient_id: str = "",
        project_id: str = "",
        source_material: str = "",
        collected_on: str = "",
        received_on: str = "",
        processed_on: str = "",
        handler: str = "",
        notes: str = "",
        status: str = "present",
    ) -> CommitResult:
        iid = ids.new_id(schema.ITEM_CLASS_PREFIX["sample"])
        item = self._new_item_row(
            iid, "sample", name, status, location_id, position, handler, notes
        )
        ext = {
            "item_id": iid,
            # _opt() so that a client sending JSON null gets an empty field
            # rather than an AttributeError on someone else's round trip.
            "gml_id": _opt(gml_id),
            "patient_id": _opt(patient_id),
            "project_id": _opt(project_id),
            "sample_type": sample_type,
            "source_material": source_material,
            "collected_on": collected_on,
            "received_on": received_on,
            "processed_on": processed_on,
        }
        result = self._apply(
            "create_sample", [("items", [item], []), ("samples", [ext], [])]
        )
        result.records = [self.get_item(iid)]
        return result

    def create_supply(
        self,
        item_class: str,
        name: str,
        location_id: str = "",
        position: str = "",
        price: str = "",
        expires_on: str = "",
        storage_requirements: str = "",
        handler: str = "",
        notes: str = "",
        initial_quantity: str = "",
        quantity_assay: str = "",
        quantity_unit: str = "",
        quantity_measured_on: str = "",
    ) -> CommitResult:
        if item_class not in schema.SUPPLY_CLASSES:
            raise ServiceError(
                f"{item_class!r} is not a supply class; supply classes are "
                f"{', '.join(schema.SUPPLY_CLASSES)} -- a tube of material is a sample, "
                "made by create_sample"
            )
        table = schema.CLASS_EXTENSION_TABLE[item_class]
        iid = ids.new_id(schema.ITEM_CLASS_PREFIX[item_class])
        item = self._new_item_row(
            iid, item_class, name, "present", location_id, position, handler, notes
        )
        ext = {
            "item_id": iid,
            "price": _s(price),
            "expires_on": expires_on,
            "storage_requirements": storage_requirements,
        }
        changes: list[tuple[str, list, list]] = [
            ("items", [item], []),
            (table, [ext], []),
        ]
        measurements = []
        if initial_quantity != "":
            mid = ids.new_id("msm")
            assay_id = self._assay_id_for_code(quantity_assay) if quantity_assay else ""
            measured_on = self._timestamp(quantity_measured_on)
            measurements.append(
                {
                    "measurement_id": mid,
                    "item_id": iid,
                    "assay_id": assay_id,
                    "measured_on": measured_on,
                    "value_num": _s(initial_quantity),
                    "value_text": "",
                    "unit": quantity_unit,
                    "handler": handler or self.actor(),
                    "instrument": "",
                    "supersedes": "",
                    "notes": "initial quantity at registration",
                }
            )
            changes.append(("measurements", measurements, []))
        result = self._apply(f"create_{item_class}", changes)
        result.records = [self.get_item(iid)]
        return result

    def move(
        self, object_id: str, target_id: str, position: Optional[str] = None
    ) -> dict:
        """Put one thing inside a container: a tube in a box, a box in a rack.

        Items and containers move the same way because a slot is a slot -- ``occupied``
        counts a box squatting on a bench exactly like a tube in a well, so moving either
        takes one and the target's capacity decides whether it fits. That is checked here
        rather than left to the post-write validator, because "no space in Rack 3 (6/6)"
        is the answer the caller needs and the table-wide rule can only say that some row
        now disagrees with its capacity.

        A container cannot be moved into its own descendant, which would detach the
        subtree below it from every path above. Checked before the write, not discovered
        afterwards as a cycle no walk can escape.

        ``position`` is convenience data, as everywhere else: most entries are filed at
        box level and nothing is computed from wells. It applies to items only -- a
        container sits in a slot, not a well. Omitting it keeps a well label when the
        container is unchanged and drops it when it isn't, because "A1" means different
        tubes in boxes of different geometry, and carrying it over would assert something
        nobody confirmed. Pass ``""`` to forget a label while staying put.

        Returns where the thing came from and where it went, and nothing else: the record
        itself is ``get_item``'s job, and a move that succeeded has changed no other field.
        """
        db = self.db
        kind, row = self._require_movable(object_id)
        oid = (row.get("item_id") or row.get("container_id") or "").strip()

        containers = db.containers_by_id()
        target = (target_id or "").strip()
        if not target:
            raise ServiceError("target_id is required; name the container to move it into")
        if target not in containers:
            raise NotFound(
                f"no container with id {target!r}; target_id names the destination "
                f"container (see search_containers)"
            )

        if kind == "container":
            if position is not None:
                raise ServiceError(
                    "position is for items; a container takes a slot in its parent, not a well"
                )
            if target == oid:
                raise Conflict("cannot move a container into itself")
            if target in self._container_subtree(oid):
                raise Conflict(
                    f"cannot move {row.get('label', oid)!r} into "
                    f"{containers[target].get('label', target)!r}: that is already inside it"
                )
            old_loc = (row.get("parent_id", "") or "").strip()
            old_pos = ""
            updated = dict(row)
            updated["parent_id"] = target
            table = "containers"
        else:
            old_loc = (row.get("location_id", "") or "").strip()
            old_pos = (row.get("position", "") or "").strip()
            new_pos = (old_pos if position is None and target == old_loc
                       else (position or "").strip())
            if row.get("status", "") == "consumed":
                raise Conflict(
                    f"{oid} is consumed and holds no slot; set it back to present with a "
                    "location_id first"
                )
            updated = dict(row)
            updated["location_id"] = target
            updated["position"] = new_pos
            table = "items"

        self._require_space(target, oid)

        updated.update(self._audit())
        self._apply("move", [(table, [], [updated])])

        out = {
            "id": oid,
            "kind": row.get("class", "") if kind == "item" else "container",
            "from": self._place(old_loc, old_pos),
            "to": self._place(target, updated.get("position", "")),
        }
        return out

    def _require_space(self, container_id: str, moving_id: str) -> None:
        """Refuse a placement the target's geometry cannot hold.

        Checked by whoever is placing, not left to the post-write validator: "no space in
        Rack 3 (6/6)" is the answer the caller needs, while the table-wide rule can only
        say that some row now disagrees with its capacity.
        """
        cap = self.db.capacity_of(container_id)
        if cap is None:
            return
        held = [i for i in self.db.occupants(container_id) if i.get("item_id", "") != moving_id]
        kids = [c for c in self.db.child_containers(container_id) if c != moving_id]
        used = len(held) + len(kids)
        if used + 1 > cap:
            label = self.db.containers_by_id().get(container_id, {}).get("label", container_id)
            raise Conflict(f"no space in {label!r}: {used}/{cap} slots taken and this needs one")

    def _place(self, container_id: str, position: str) -> dict:
        """Where something sits, as far as anyone recorded it."""
        if not container_id:
            return {"container_id": "", "label": "", "position": ""}
        row = self.db.containers_by_id().get(container_id, {})
        return {
            "container_id": container_id,
            "label": row.get("label", ""),
            "position": position,
        }

    def _require_movable(self, object_id: str) -> tuple[str, dict]:
        """``("item" | "container", row)`` for anything that occupies a slot."""
        oid = (object_id or "").strip()
        if not oid:
            raise ServiceError("id is required; pass an item id or a container id")
        item = self.db.items_by_id().get(oid)
        if item is not None:
            if (item.get("deleted_at", "") or "").strip():
                raise NotFound(f"{oid} was deleted; it has no live row to move")
            return "item", item
        container = self.db.containers_by_id().get(oid)
        if container is not None:
            return "container", container
        raise NotFound(
            f"nothing with id {oid!r}; pass an item id (smp_, rgt_, kit_, csm_) "
            f"or a container id (ctr_)"
        )

    def set_status(self, item_id: str, status: str, location_id: str = "") -> CommitResult:
        """Mark an item present, partially_consumed, or consumed, in either direction.

        Consuming releases the slot, because a tube that is used up cannot keep a well
        occupied -- and the well label goes with it, since it describes a place the tube
        is no longer in. Nothing remembers it. Coming back out of ``consumed`` therefore
        needs a container named, which is also how a mis-recorded consumption gets undone
        in one write instead of leaving the item floating unlocated between two calls.
        """
        row = self._require_item(item_id)
        if (row.get("deleted_at", "") or "").strip():
            raise Conflict(
                f"{item_id} was deleted and remains only as a tombstone for provenance; "
                "its status is not changed"
            )
        if status not in schema.ITEM_STATUSES:
            raise ServiceError(f"status must be one of {schema.ITEM_STATUSES}")
        updated = dict(row)
        updated["status"] = status
        located = (row.get("location_id", "") or "").strip()
        target = (location_id or "").strip()
        if status == "consumed":
            if target:
                raise ServiceError(
                    "a consumed item holds no slot, so it takes no location_id; leave it "
                    "out, or mark the item present to put it somewhere"
                )
            # A consumed item cannot occupy a slot. Dropping the position here is
            # deliberate: keeping it would claim a well that is physically free.
            updated["location_id"] = ""
            updated["position"] = ""
        elif target:
            if target not in self.db.containers_by_id():
                raise NotFound(
                    f"no container with id {target!r}; location_id names the container to "
                    f"file it in (see search_containers)"
                )
            self._require_space(target, item_id.strip())
            updated["location_id"] = target
            if target != located:
                updated["position"] = ""
        else:
            if not located:
                raise ServiceError(
                    f"{item_id} holds no slot, and {status!r} needs one: pass location_id "
                    "(see search_containers). Consuming drops the location and nothing "
                    "remembers it."
                )
        updated.update(self._audit())
        result = self._apply("set_status", [("items", [], [updated])])
        result.records = [self.get_item(item_id)]
        return result

    def record_measurement(
        self,
        item_id: str,
        assay: str = "",
        measured_on: str = "",
        value_num: str = "",
        unit: str = "",
        value_text: str = "",
        handler: str = "",
        instrument: str = "",
        supersedes: str = "",
        notes: str = "",
        assay_id: str = "",
    ) -> CommitResult:
        self._require_item(item_id)
        assay_row = self._resolve_assay(assay, assay_id)
        assay_label = assay_row.get("code", "") or assay_row.get("name", "")
        if not value_num and not value_text:
            raise ServiceError(
                "pass either value_num= (numeric assays) or value_text= (qualitative assays)")
        if assay_row.get("result_type") == "numeric":
            if not value_num:
                raise ServiceError(
                    f"assay {assay_label!r} is numeric; pass value_num= not value_text="
                )
            try:
                float(value_num)
            except ValueError:
                raise ServiceError(f"value_num {value_num!r} is not a number") from None
            quantity = assay_row.get("quantity", "other")
            if not unit:
                if quantity in units.UNITLESS:
                    # Counts and percentages have no unit to state; fill the
                    # canonical placeholder so stored rows stay self-describing.
                    unit = assay_row.get("canonical_unit", "") or "count"
                else:
                    raise ServiceError(
                        f"assay {assay_label!r} needs a unit (its canonical unit is "
                        f"{assay_row.get('canonical_unit', '?')})"
                    )
            try:
                units.to_canonical(quantity, float(value_num), unit)
            except units.UnitError as e:
                raise ServiceError(str(e)) from None
        else:
            if not value_text:
                raise ServiceError(
                    f"assay {assay_label!r} is qualitative; pass value_text= not value_num=")
        when = self._timestamp(measured_on)
        mid = ids.new_id("msm")
        # A second reading for the same item and assay takes over from whichever one
        # stood, unless the caller named a different row to retire. Without this the
        # sheet holds two values for one question and only entry order separates
        # them -- true everywhere else in this codebase, but invisible to whoever
        # opens the spreadsheet. Retiring is a note on the new row, not an edit of
        # the old one, so readings stay append-only.
        took_over = ""
        if not supersedes.strip():
            took_over = self._current_reading_id(item_id.strip(), assay_row["assay_id"])
            supersedes = took_over
        row = {
            "measurement_id": mid,
            "item_id": item_id.strip(),
            "assay_id": assay_row["assay_id"],
            "measured_on": when,
            "value_num": _s(value_num),
            "value_text": value_text,
            "unit": unit,
            "handler": handler or self.actor(),
            "instrument": instrument,
            "supersedes": supersedes,
            "notes": notes,
        }
        result = self._apply("record_measurement", [("measurements", [row], [])])
        # Fetch the row we just wrote by id. Selecting "the last measurement for
        # this item" would be wrong whenever an item has several assays, because
        # the history is sorted by assay name.
        result.records = [
            m for m in self.get_measurements(item_id=item_id.strip(), include_superseded=True)
            if m["measurement_id"] == mid
        ]
        if took_over:
            result.warnings.append(validation.Violation(
                table="measurements", row_id=mid, field="supersedes",
                code="auto_superseded", severity="warning",
                message=f"{took_over} was the current {assay_label} reading for "
                        f"{item_id.strip()}; this one retires it. Pass supersedes= to "
                        "retire a different reading instead.",
            ).as_dict())
        return result

    def _current_reading_id(self, item_id: str, assay_id: str) -> str:
        """What a new reading of this item and assay would take over from.

        The last row written that nobody has retired, which is how ``get_measurements``
        and the ``latest_measurements`` view both define the current value. Empty when
        there is nothing to retire -- an item's first reading for an assay must not
        claim to replace something.
        """
        t = self.db.t("measurements")
        retired = {(r.get("supersedes", "") or "").strip() for r in t.rows} - {""}
        current = ""
        for r in t.rows:
            if (r.get("item_id", "") or "").strip() != item_id:
                continue
            if (r.get("assay_id", "") or "").strip() != assay_id:
                continue
            mid = (r.get("measurement_id", "") or "").strip()
            if mid and mid not in retired:
                current = mid
        return current

    def correct_measurement(self, measurement_id: str, **changes) -> CommitResult:
        """Measurements are immutable; a correction appends and invalidates."""
        db = self.db
        t = db.t("measurements")
        old = t.get(measurement_id)
        if old is None:
            raise NotFound(f"no measurement with id {measurement_id!r}")
        for r in t.rows:
            if (r.get("supersedes", "") or "").strip() == measurement_id:
                raise Conflict(
                    f"{measurement_id} is already superseded by {r['measurement_id']}"
                )
        new_kwargs = {
            "item_id": old["item_id"],
            # Reuse the assay by id, not by code: the row already names it, and a
            # code round-trip would break if an assay were ever renamed.
            "assay_id": (old.get("assay_id", "") or "").strip(),
            "measured_on": changes.pop("measured_on", old.get("measured_on", "")),
            "value_num": changes.pop("value_num", old.get("value_num", "")),
            "unit": changes.pop("unit", old.get("unit", "")),
            "value_text": changes.pop("value_text", old.get("value_text", "")),
            "handler": changes.pop("handler", self.actor()),
            "instrument": changes.pop("instrument", old.get("instrument", "")),
            "notes": changes.pop("notes", old.get("notes", "")),
            "supersedes": measurement_id,
        }
        if changes:
            raise ServiceError(f"unknown fields: {sorted(changes)}")
        return self.record_measurement(**new_kwargs)

    def link_items(self, child_item_id: str, parent_item_id: str, role: str, notes: str = "") -> CommitResult:
        self._require_item(child_item_id)
        self._require_item(parent_item_id)
        if role not in schema.LINK_ROLES:
            raise ServiceError(f"role must be one of {schema.LINK_ROLES}")
        pair = (child_item_id.strip(), parent_item_id.strip())
        # The two endpoints name the edge, so a second row between the same pair is
        # a duplicate rather than a second relationship -- whatever its role claims.
        for r in self.db.t("item_links").rows:
            existing = ((r.get("child_item_id", "") or "").strip(),
                        (r.get("parent_item_id", "") or "").strip())
            if existing == pair:
                raise Conflict(
                    f"{pair[0]} is already linked to {pair[1]} as {r.get('role', '')!r}"
                    " -- two item ids name one edge"
                )
        lid = ids.new_id("lin")
        row = {
            "link_id": lid,
            "child_item_id": child_item_id.strip(),
            "parent_item_id": parent_item_id.strip(),
            "role": role,
            "notes": notes,
        }
        result = self._apply("link_items", [("item_links", [row], [])])
        result.records = [row]
        return result

    def update_fields(self, table: str, record_id: str, *,
                      operation: str = "update_fields", **changes) -> CommitResult:
        """Edit mutable descriptive fields. Ids, foreign keys and status have
        their own operations because they carry structural consequences."""
        spec = schema.get_table(table)
        protected = {spec.primary_key, "class", "status", "item_id", "deleted_at"}
        blocked = protected & set(changes)
        if blocked:
            raise ServiceError(
                f"{sorted(blocked)} cannot be edited here -- use the dedicated operation"
            )
        t = self.db.t(table)
        row = t.get(record_id)
        if row is None:
            raise NotFound(f"no {table} row with id {record_id!r}")
        updated = dict(row)
        for k, v in changes.items():
            if spec.field(k) is None:
                raise ServiceError(f"{table} has no field {k!r}")
            updated[k] = _s(v)
        if t.spec.field("updated_by"):
            updated.update(self._audit())
        result = self._apply(operation, [(table, [], [updated])])
        result.records = [dict(updated)]
        return result

    def _descriptive_target(self, oid: str, columns: dict[str, str]) -> tuple[str, dict, str]:
        """``(table, row, column)`` for whichever table holds this id.

        The id decides the table by itself: they are minted with a per-table prefix and are
        looked up by primary key, which is already how ``move`` dispatches. So a caller that
        knows what it is holding never has to declare it, and cannot declare it wrongly.
        """
        if not oid:
            raise ServiceError(
                "id is required; pass one from search_items, search_containers, "
                "search_patients or search_projects")
        for table, column in columns.items():
            row = self.db.t(table).get(oid)
            if row is not None:
                if (row.get("deleted_at") or "").strip():
                    raise Conflict(
                        f"{oid} was deleted and remains only as a tombstone for provenance; "
                        "its text is not edited")
                return table, row, column
        raise NotFound(
            f"nothing editable with id {oid!r}; expected an item (smp_, rgt_, kit_, csm_), "
            "a container (ctr_), a participant (pat_), a project (prj_) or an assay (asy_)")

    def item_class(self, item_id: str) -> str:
        """``sample`` | ``reagent`` | ``kit`` | ``consumable`` for an item id."""
        return self._require_item(item_id).get("class", "").strip()

    def edit_name(self, object_id: str, name: str) -> CommitResult:
        """Rename whatever the id points at -- an item's name, a box's label, a project's name."""
        oid = (object_id or "").strip()
        if not str(name).strip():
            raise ServiceError(
                "name cannot be blank: someone has to read this entry aloud, and there is no "
                "unnamed state to move it to")
        table, _, column = self._descriptive_target(oid, _NAME_COLUMN)
        return self.update_fields(table, oid, operation="edit_name", **{column: name})

    def edit_notes(self, object_id: str, notes: str) -> CommitResult:
        """Replace the free text on an entry; a project keeps its prose in ``description``."""
        oid = (object_id or "").strip()
        table, _, column = self._descriptive_target(oid, _NOTES_COLUMN)
        return self.update_fields(table, oid, operation="edit_notes", **{column: notes})

    def edit_data(self, object_id: str, field: str, value: str) -> CommitResult:
        """Set one class-specific field on an item, resolved from what the item actually is.

        The class comes off the ``items`` row rather than from the caller, so asking a
        sample for a reagent's price is refused by fact, with both sides named, instead of
        being accepted on the caller's say-so and landing in the wrong table.
        """
        oid = (object_id or "").strip()
        row = self._require_item(oid)
        if (row.get("deleted_at") or "").strip():
            raise Conflict(
                f"{oid} was deleted and remains only as a tombstone for provenance; its "
                "fields are not edited")
        allowed = _DATA_FIELDS.get((field or "").strip())
        if allowed is None:
            raise ServiceError(
                f"no editable field {field!r}; editable are {', '.join(sorted(_DATA_FIELDS))}")
        cls = row.get("class", "").strip()
        table = "items" if field == "handler" else schema.CLASS_EXTENSION_TABLE.get(cls, "")
        if table not in allowed:
            raise ServiceError(
                f"{field} belongs to {_or_join(allowed)}, and {oid} is a "
                f"{cls or 'record of unknown class'}")
        return self.update_fields(table, oid, operation="edit_data", **{field: value})

    def create_patient(self, label: str, project_id: str, **kw) -> CommitResult:
        """Register a participant and enrol them in their first project.

        Both rows commit together. A participant with no enrolment is a participant
        whose material cannot be filed anywhere, so there is no useful state where
        the patient exists and the pairing does not.
        """
        if not str(project_id).strip():
            raise ServiceError(
                "project_id is required -- a participant is registered inside a project; "
                "see search_projects"
            )
        pid = ids.new_id("pat")
        row = {"patient_id": pid, "label": label, **self._audit()}
        for k, v in kw.items():
            if schema.PATIENTS.field(k) is None:
                raise ServiceError(f"patients has no field {k!r}")
            row[k] = _s(v)
        link = {"link_id": ids.new_id("ppl"), "patient_id": pid, "project_id": _s(project_id)}
        result = self._apply("create_patient",
                             [("patients", [row], []), ("patient_projects", [link], [])])
        result.records = [row, link]
        return result

    def create_project(self, name: str, status: str = "active", parent_project_id: str = "", **kw) -> CommitResult:
        prid = ids.new_id("prj")
        row = {
            "project_id": prid,
            "name": name,
            "status": status,
            "parent_project_id": parent_project_id,
            **self._audit(),
        }
        for k, v in kw.items():
            if schema.PROJECTS.field(k) is None:
                raise ServiceError(f"projects has no field {k!r}")
            row[k] = _s(v)
        result = self._apply("create_project", [("projects", [row], [])])
        result.records = [row]
        return result

    def assign_patient_project(self, patient_id: str, project_id: str) -> CommitResult:
        """Add a further project to an existing participant; their first one comes
        from ``create_patient``."""
        for r in self.db.t("patient_projects").rows:
            if (
                r.get("patient_id", "").strip() == patient_id
                and r.get("project_id", "").strip() == project_id
            ):
                raise Conflict("that participant is already enrolled in that project")
        lid = ids.new_id("ppl")
        row = {"link_id": lid, "patient_id": patient_id, "project_id": project_id}
        result = self._apply("assign_patient_project", [("patient_projects", [row], [])])
        result.records = [row]
        return result

    def unassign_patient_project(self, patient_id: str, project_id: str,
                                 move_to: str = "") -> CommitResult:
        """Withdraw a participant from a project, settling their material in the same
        commit.

        Deleting the row on its own would leave every sample naming the pair illegal,
        and because that rule is reported whatever row caused it, the database would
        then refuse every write touching samples -- including the writes needed to fix
        it. So the tubes are dealt with here rather than afterwards, and there is no
        moment where the caller is handed a list they cannot act on.

        Without ``move_to`` their ``project_id`` is cleared. A tube with a participant
        and no project is legal and findable (``search_items(no_project=True)``), so
        withdrawing someone never requires deciding their destination first. With
        ``move_to`` the tubes are re-pointed at another project the participant is
        enrolled in. Tombstoned tubes are left untouched: a deletion is history, not a
        live claim about routing, and the rule ignores them too.
        """
        who, pr = str(patient_id).strip(), str(project_id).strip()
        if not who:
            raise ServiceError("patient_id is required; see search_patients")
        if not pr:
            raise ServiceError("project_id is required; see search_projects")
        target = str(move_to).strip()
        if target and target == pr:
            raise ServiceError("move_to cannot be the project being withdrawn")

        links = self.db.t("patient_projects").rows
        link = next((r for r in links
                     if (r.get("patient_id", "") or "").strip() == who
                     and (r.get("project_id", "") or "").strip() == pr), None)
        if link is None:
            raise NotFound(f"{who!r} is not enrolled in {pr!r}; see get_patient")
        if target:
            known = {r["project_id"].strip() for r in self.db.t("projects").rows
                     if r.get("project_id", "").strip()}
            if target not in known:
                raise NotFound(f"no project with id {target!r}; see search_projects")
            held = {(r.get("project_id", "") or "").strip() for r in links
                    if (r.get("patient_id", "") or "").strip() == who}
            if target not in held:
                raise ServiceError(
                    f"{who!r} is not enrolled in {target!r}; enrol them there first, or "
                    "omit move_to and leave those tubes without a project"
                )

        buried = {iid for iid, row in self.db.items_by_id().items()
                  if (row.get("deleted_at", "") or "").strip()}
        touched_ids: list[str] = []
        updates: list[dict] = []
        for r in self.db.t("samples").rows:
            iid = (r.get("item_id", "") or "").strip()
            if iid in buried:
                continue
            if ((r.get("patient_id", "") or "").strip() == who
                    and (r.get("project_id", "") or "").strip() == pr):
                touched_ids.append(iid)
                updates.append(dict(r, project_id=target))

        remaining = [r for r in links
                     if not ((r.get("patient_id", "") or "").strip() == who
                             and (r.get("project_id", "") or "").strip() == pr)]
        changes: list[tuple] = ([("samples", [], updates)] if updates else []) + [
            ("patient_projects", [], [], remaining)]
        result = self._apply("unassign_patient_project", changes)

        still_in = sorted({(r.get("project_id", "") or "").strip() for r in remaining
                           if (r.get("patient_id", "") or "").strip() == who})
        result.records = [{
            "removed": {"patient_id": who, "project_id": pr, "link_id": link["link_id"]},
            "samples_repointed": len(touched_ids) if target else 0,
            "samples_unpinned": 0 if target else len(touched_ids),
            "sample_ids": touched_ids[:50],
            "sample_ids_truncated": len(touched_ids) > 50,
            "participant_still_in": still_in,
            "was_their_last_project": not still_in,
        }]
        return result

    def backfill_enrolments(self, dry_run: bool = False) -> CommitResult:
        """Write out an explicit enrolment row for every sample whose pairing is
        covered only by an ancestor project.

        This is the migration for databases built while enrolment inherited down the
        tree. Those tubes are legal under the old rule and illegal under exact
        matching, and since any write touching samples reports the whole rule's
        violations, they would block unrelated work until someone added each row by
        hand.

        Only pairs that hold material get a row -- an unused branch stays unenrolled,
        because nobody asserted it. A pair with no coverage anywhere is reported in
        ``warnings`` rather than invented: nothing in the database authorises it, so
        the honest answer is that a human has to decide.

        Nothing is written while such a pair exists. They block every write touching
        samples, this one included, so a partial migration would leave the database
        exactly as stuck as it was and look like it had helped. An empty
        ``touched_tables`` means nothing changed.
        """
        projects = {r["project_id"].strip(): r for r in self.db.t("projects").rows
                    if r.get("project_id", "").strip()}

        def ancestors(project_id: str) -> set[str]:
            chain, seen, cur = set(), set(), project_id
            while cur and cur in projects and cur not in seen:
                seen.add(cur)
                chain.add(cur)
                cur = (projects[cur].get("parent_project_id", "") or "").strip() or None
            return chain

        held: dict[str, set[str]] = {}
        for r in self.db.t("patient_projects").rows:
            who, pr = (r.get("patient_id", "") or "").strip(), (r.get("project_id", "") or "").strip()
            if who and pr:
                held.setdefault(who, set()).add(pr)

        new_rows: list[dict] = []
        pending: set[tuple[str, str]] = set()
        unfixable: list[validation.Violation] = []
        for r in self.db.t("samples").rows:
            iid = (r.get("item_id", "") or "").strip()
            who, pr = (r.get("patient_id", "") or "").strip(), (r.get("project_id", "") or "").strip()
            if not (who and pr):
                continue
            if pr in held.get(who, set()) or (who, pr) in pending:
                continue
            if held.get(who, set()) & ancestors(pr):
                pending.add((who, pr))
                new_rows.append({"link_id": ids.new_id("ppl"), "patient_id": who, "project_id": pr})
            else:
                unfixable.append(validation.Violation(
                    "samples", iid, "project_id", "unbackfillable_pair", "error",
                    f"sample names patient {who!r} and project {pr!r}, which no enrolment row "
                    "or ancestor of one supports -- this command will not invent that consent"))

        if dry_run or not new_rows or unfixable:
            return CommitResult(operation="backfill_enrolments", touched=(),
                                snapshot=Snapshot(path=None, tables=()),
                                records=new_rows,
                                warnings=[v.as_dict() for v in unfixable])

        result = self._apply("backfill_enrolments", [("patient_projects", new_rows, [])])
        result.records = new_rows
        result.warnings = [v.as_dict() for v in unfixable]
        return result

    def create_assay(
        self,
        code: str,
        name: str,
        quantity: str,
        canonical_unit: str,
        result_type: str = "numeric",
        method: str = "",
        notes: str = "",
    ) -> CommitResult:
        for existing in self.db.t("assays").rows:
            if existing.get("code", "").strip().lower() == code.lower():
                raise Conflict(f"an assay with code {code!r} already exists")
        aid = ids.new_id("asy")
        row = {
            "assay_id": aid,
            "code": code,
            "name": name,
            "quantity": quantity,
            "canonical_unit": canonical_unit,
            "result_type": result_type,
            "method": method,
            "notes": notes,
        }
        result = self._apply("create_assay", [("assays", [row], [])])
        result.records = [row]
        return result

    # -- destructive ---------------------------------------------------------

    def delete_item(self, item_id: str) -> CommitResult:
        """Retire an item, whatever state it was in.

        Nothing here depends on status. Throwing away a tube that still holds material
        happens, and no later step would catch it, so there is nothing to gate it
        behind. What retirement does change is the row's claim on a place: the location
        and well go with it, because a retired tube occupies no well. Measurements and
        derivation links keep pointing at the row -- provenance outlives the tube.
        """
        row = self._require_item(item_id)
        if (row.get("deleted_at", "") or "").strip():
            raise Conflict(f"{item_id} is already deleted")
        updated = dict(row)
        updated["deleted_at"] = validation.format_datetime(validation.now_utc_minute())
        updated["location_id"] = ""
        updated["position"] = ""
        updated.update(self._audit())
        result = self._apply("delete_item", [("items", [], [updated])])
        result.records = [self.get_item(item_id)]
        return result

    def delete_container(self, container_id: str) -> CommitResult:
        """Remove a storage location once nothing lives inside it.

        Live residents block the delete and are listed by id, because the caller has to
        act on each one -- move or retire it, then retry. A retired item still naming
        this place is a different case: its row can be neither moved nor edited, so
        refusing over it would make the container permanently undeletable. Its stale
        location is cleared in this same commit instead, which is also what stops the
        reference surviving as a dangling foreign key that refuses every later write.
        """
        cid = str(container_id).strip()
        db = self.db
        by_id = db.containers_by_id()
        if cid not in by_id:
            raise NotFound(f"no container with id {cid!r}; see search_containers")
        occupants = db.occupants(cid)
        children = db.child_containers(cid)
        if occupants or children:
            blocks = []
            if occupants:
                blocks.append(
                    f"it holds {len(occupants)} item(s): {_id_list([r['item_id'] for r in occupants])}"
                )
            if children:
                blocks.append(f"it has {len(children)} container(s) inside it: {_id_list(children)}")
            raise Conflict(
                f"{cid} cannot be deleted yet: {' and '.join(blocks)}"
                " -- empty it, then delete it again"
            )
        changes: list[tuple] = []
        stale = [
            dict(r, location_id="", position="")
            for r in db.t("items").rows
            if (r.get("location_id", "") or "").strip() == cid
        ]
        if stale:
            changes.append(("items", [], stale))
        t = db.t("containers")
        changes.append(
            ("containers", [], [], [r for r in t.rows
                                    if (r.get("container_id", "") or "").strip() != cid])
        )
        result = self._apply("delete_container", changes)
        result.records = [{"deleted_container": cid, "references_cleared": len(stale)}]
        return result

    def delete_patient(self, patient_id: str) -> CommitResult:
        """Remove a participant and everything that existed only to describe them.

        A sample does not need a participant, so their tubes stay and simply stop
        naming them -- including the retired ones, because a foreign key is checked
        whether or not the item is alive, and a leftover reference here would refuse
        every later write that touches samples. Their enrolments go with them: with no
        participant there is no pairing left to mean anything. Nothing can block this,
        which is why there is nothing to force.
        """
        pid = str(patient_id).strip()
        db = self.db
        patients = db.t("patients").rows
        if not any((r.get("patient_id", "") or "").strip() == pid for r in patients):
            raise NotFound(f"no participant with id {pid!r}; see search_patients")

        links = db.t("patient_projects").rows
        removed_links = [r for r in links if (r.get("patient_id", "") or "").strip() == pid]
        samples = db.t("samples").rows
        updates = [
            dict(r, patient_id="")
            for r in samples
            if (r.get("patient_id", "") or "").strip() == pid
        ]
        changes: list[tuple] = [
            ("patients", [], [], [r for r in patients
                                  if (r.get("patient_id", "") or "").strip() != pid]),
            ("patient_projects", [], [], [r for r in links
                                          if (r.get("patient_id", "") or "").strip() != pid]),
        ]
        if updates:
            changes.append(("samples", [], updates))
        result = self._apply("delete_patient", changes)
        result.records = [{
            "patient_id": pid,
            "enrolments_removed": len(removed_links),
            "samples_unlinked": len(updates),
        }]
        return result

    def delete_project(self, project_id: str) -> CommitResult:
        """Remove a project, taking each participant's material to where it can go.

        Samples do not need a project, so a tube whose participant has nowhere else to
        go loses the label rather than blocking the delete. Participants are the other
        way round: the enrolment is what makes a sample's project legal in the first
        place, so withdrawing someone from their only project would leave their tubes
        illegal. That is why this delete lands only when every participant here has
        exactly one unambiguous alternative. Anything else is refused with the ids that
        have to be dealt with first -- subprojects still hanging off this one, someone
        with no other project, or someone with two or more where the destination is a
        decision rather than a deduction.
        """
        pid = str(project_id).strip()
        db = self.db
        projects = db.t("projects").rows
        if not any((r.get("project_id", "") or "").strip() == pid for r in projects):
            raise NotFound(f"no project with id {pid!r}; see search_projects")

        children = sorted(
            (r.get("project_id", "") or "").strip() for r in projects
            if (r.get("parent_project_id", "") or "").strip() == pid
        )
        links = db.t("patient_projects").rows
        others = _other_projects(links, pid)
        enrolled = sorted(others)
        stranded = [w for w in enrolled if not others[w]]
        ambiguous = {w: others[w] for w in enrolled if len(others[w]) > 1}

        blocks = []
        if children:
            blocks.append(
                f"it has {len(children)} subproject(s): {_id_list(children)} -- nesting "
                "inherits nothing, so those stand alone and go first"
            )
        if stranded:
            blocks.append(
                f"it is the only project for {len(stranded)} participant(s): "
                f"{_id_list(stranded)} -- enrol them in another project first"
            )
        if ambiguous:
            detail = "; ".join(f"{w} -> {', '.join(v[:4])}" for w, v in list(ambiguous.items())[:5])
            more = f" (+{len(ambiguous) - 5} more)" if len(ambiguous) > 5 else ""
            blocks.append(
                f"it leaves {len(ambiguous)} participant(s) choosing between projects, so the "
                f"destination is a decision rather than a deduction: {detail}{more} -- "
                "withdraw them down to one other project, or re-point their samples first"
            )
        if blocks:
            raise Conflict(f"{pid} cannot be deleted yet: {'; '.join(blocks)}")

        moves = {w: others[w][0] for w in enrolled if others[w]}
        updates: list[dict] = []
        repointed = 0
        for r in db.t("samples").rows:
            if (r.get("project_id", "") or "").strip() != pid:
                continue
            dest = moves.get((r.get("patient_id", "") or "").strip(), "")
            updates.append(dict(r, project_id=dest))
            repointed += 1 if dest else 0
        removed_links = [r for r in links if (r.get("project_id", "") or "").strip() == pid]
        changes: list[tuple] = [
            ("projects", [], [], [r for r in projects
                                  if (r.get("project_id", "") or "").strip() != pid]),
            ("patient_projects", [], [], [r for r in links
                                          if (r.get("project_id", "") or "").strip() != pid]),
        ]
        if updates:
            changes.append(("samples", [], updates))
        result = self._apply("delete_project", changes)
        result.records = [{
            "project_id": pid,
            "enrolments_removed": len(removed_links),
            "samples_repointed": repointed,
            "samples_cleared": len(updates) - repointed,
            "moved_to": [
                {"patient_id": w, "project_id": dest}
                for w, dest in sorted(moves.items())
                if any((r.get("patient_id", "") or "").strip() == w for r in updates)
            ],
        }]
        return result

    def delete_assay(self, assay_id: str) -> CommitResult:
        """Remove a kind of measurement, once nothing has been measured with it.

        A reading names its assay and readings only compare inside one, so deleting the
        definition would leave every number filed against it meaningless -- and dangling,
        which refuses every later write on its own account. Nothing here can discard a
        reading, so an assay with history stays put and keeps explaining what its numbers
        were; this delete lands on the ones defined and never used, which is what a
        mis-typed definition turns out to be.
        """
        aid = str(assay_id).strip()
        db = self.db
        assays = db.t("assays").rows
        if not any((r.get("assay_id", "") or "").strip() == aid for r in assays):
            raise NotFound(f"no assay with id {aid!r}; see list_assays")

        readings = sorted(
            (r.get("measurement_id", "") or "").strip() for r in db.t("measurements").rows
            if (r.get("assay_id", "") or "").strip() == aid
        )
        if readings:
            raise Conflict(
                f"{aid} cannot be deleted yet: it has {len(readings)} reading(s) "
                f"recorded against it: {_id_list(readings)} -- readings are immutable, "
                "so the assay stays and keeps describing what they mean"
            )

        result = self._apply(
            "delete_assay",
            [("assays", [], [], [r for r in assays
                                 if (r.get("assay_id", "") or "").strip() != aid])])
        result.records = [{"deleted_assay": aid}]
        return result

    def merge_items(self, keep_id: str, discard_id: str, reason: str = "") -> CommitResult:
        """Re-point everything referencing ``discard_id``, then tombstone it."""
        keep = self._require_item(keep_id)
        discard = self._require_item(discard_id)
        # Compared on the resolved ids, so ' smp_x ' and 'smp_x' collide too: the
        # tombstone below lands on discard, which would silently delete the survivor.
        if keep.get("item_id", "") == discard.get("item_id", ""):
            raise ServiceError(
                "keep_id and discard_id name the same item; nothing to merge"
            )
        if keep.get("class", "") != discard.get("class", ""):
            raise Conflict("cannot merge a " + keep.get("class", "?") + " into a " + discard.get("class", "?"))

        changes: list[tuple[str, list, list]] = []
        repointed = 0

        # items.location/position inherited only if keep has none
        merged_keep = dict(keep)
        changed_keep = False
        for f in ("location_id", "position", "handler"):
            if not (merged_keep.get(f, "") or "").strip() and (discard.get(f, "") or "").strip():
                merged_keep[f] = discard[f]
                changed_keep = True
        # Notes are the one field where both rows usually hold something worth keeping,
        # so they join instead of competing -- keep's first, then discard's. The
        # tombstone below still carries the discarded text on its own row, which is how
        # the joined pair stays separable.
        keep_notes = (merged_keep.get("notes", "") or "").strip()
        discard_notes = (discard.get("notes", "") or "").strip()
        if discard_notes and discard_notes not in keep_notes:
            merged_keep["notes"] = f"{keep_notes}\n{discard_notes}".strip()
            changed_keep = True
        merged_keep.update(self._audit())
        if changed_keep:
            changes.append(("items", [], [merged_keep]))

        # Extension tables: fill blanks on keep's row, then drop discard's row.
        # Whole-table form throughout -- two partial changes against one table
        # would otherwise overwrite each other inside _apply.
        ext_name = schema.CLASS_EXTENSION_TABLE[keep["class"]]
        ext = self.db.t(ext_name)
        final: dict[str, list[dict]] = {}

        def working(name: str) -> list[dict]:
            return final.setdefault(name, [dict(r) for r in self.db.t(name).rows])

        keep_ext = ext.get(keep_id)
        discard_ext = ext.get(discard_id)
        if keep_ext and discard_ext:
            for r in working(ext_name):
                if (r.get("item_id", "") or "").strip() != keep_id:
                    continue
                for k, v in discard_ext.items():
                    if k == "item_id":
                        continue
                    if not (r.get(k, "") or "").strip() and (v or "").strip():
                        r[k] = v
                        repointed += 1
        elif discard_ext and not keep_ext:
            moved = dict(discard_ext)
            moved["item_id"] = keep_id
            working(ext_name).append(moved)
            repointed += 1
        if discard_ext:
            final[ext_name] = [
                r for r in working(ext_name)
                if (r.get("item_id", "") or "").strip() != discard_id
            ]

        # Anything referencing discard must be re-pointed at keep, not deleted
        # with it -- a measurement's history is the reason to merge rather than
        # recreate.
        for table, fk_field in (
            ("measurements", "item_id"),
            ("item_links", "child_item_id"),
            ("item_links", "parent_item_id"),
        ):
            source = final.get(table)
            if source is None:
                source = self.db.t(table).rows
            kept, moved = [], 0
            for r in source:
                r = dict(r)
                if (r.get(fk_field, "") or "").strip() != discard_id:
                    kept.append(r)
                    continue
                r[fk_field] = keep_id
                if table == "item_links" and r["child_item_id"] == r["parent_item_id"]:
                    moved += 1
                    continue  # merging would have created a self-edge; drop it
                kept.append(r)
                moved += 1
            if moved:
                final[table] = kept
                repointed += moved

        for name, rows in final.items():
            changes.append((name, [], [], rows))

        tomb = dict(discard)
        tomb["deleted_at"] = validation.format_datetime(validation.now_utc_minute())
        tomb["location_id"] = ""
        tomb["position"] = ""
        tomb["notes"] = (
            ((discard.get("notes", "") or "").strip() + "\n").lstrip("\n")
            + f"merged into {keep_id}" + (f": {reason}" if reason else "")
        ).strip()
        tomb.update(self._audit())
        items_rows = [
            tomb if (r.get("item_id", "") or "").strip() == discard_id else
            (merged_keep if (r.get("item_id", "") or "").strip() == keep_id else r)
            for r in self.db.t("items").rows
        ]
        changes = [c for c in changes if c[0] != "items"]
        changes.insert(0, ("items", [], [], items_rows))

        result = self._apply("merge_items", changes)
        result.records = [{"kept": keep_id, "discarded": discard_id, "references_repointed": repointed}]
        return result

    # -- derived artifacts ---------------------------------------------------

    def export(self, directory: str = "", formats: Iterable[str] = ("csv", "sqlite")) -> dict:
        from .export import export_views

        return export_views(self, directory=directory, formats=formats)

    # -- internals -----------------------------------------------------------

    def _audit(self) -> dict:
        return {
            "updated_by": self.actor(),
            "updated_at": validation.format_datetime(validation.now_utc_minute()),
        }

    @staticmethod
    def _timestamp(value: str = "") -> str:
        """Put a caller's timestamp into the spelling this database stores.

        Blank means now. Anything else is parsed, so a zone written out as ``Z`` or
        ``+10:00`` lands as the same minute in UTC rather than as a second spelling of
        the column -- and a bad one is refused here, naming the value, instead of
        arriving later as a write refused for a row the caller never mentioned.
        """
        value = (value or "").strip()
        if not value:
            return validation.format_datetime(validation.now_utc_minute())
        dt, problem = validation.parse_datetime(value)
        if problem:
            raise ServiceError(problem)
        return validation.format_datetime(dt)

    def _new_item_row(self, iid, cls, name, status, location_id, position, handler, notes) -> dict:
        return {
            "item_id": iid,
            "class": cls,
            "name": name,
            "status": status,
            "location_id": (location_id or "").strip(),
            "position": (position or "").strip(),
            "handler": handler or self.actor(),
            "notes": notes,
            "deleted_at": "",
            **self._audit(),
        }

    def _require_item(self, item_id: str) -> dict:
        row = self.db.items_by_id().get(item_id.strip())
        if row is None:
            raise NotFound(f"no item with id {item_id!r}")
        return row

    def _assay_for_code(self, code: str) -> dict:
        if not code or not code.strip():
            raise ServiceError("no assay named; pass assay=<code> or assay_id=<id>")
        for a in self.db.t("assays").rows:
            if a.get("code", "").strip().lower() == code.strip().lower():
                return a
        known = ", ".join(sorted(a.get("code", "") for a in self.db.t("assays").rows))
        raise ServiceError(f"no assay with code {code!r}. Registered assays: {known or '<none>'}")

    def _resolve_assay(self, code: str = "", assay_id: str = "") -> dict:
        """Accept either the human code or the surrogate id, never a blank."""
        if assay_id and assay_id.strip():
            row = self.db.t("assays").get(assay_id.strip())
            if row is None:
                raise ServiceError(f"no assay with id {assay_id!r}")
            return row
        return self._assay_for_code(code)

    def _assay_id_for_code(self, code: str) -> str:
        if not code:
            return ""
        return self._assay_for_code(code)["assay_id"]

    def _extension_index(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for table in ("samples", "reagents", "kits", "consumables"):
            for r in self.db.t(table).rows:
                iid = (r.get("item_id", "") or "").strip()
                if iid:
                    out[iid] = r
        return out

    def _container_subtree(self, root: str) -> set[str]:
        by_id = self.db.containers_by_id()
        kids: dict[str, list[str]] = {}
        for cid, row in by_id.items():
            kids.setdefault((row.get("parent_id", "") or "").strip(), []).append(cid)
        out, stack = set(), [root]
        while stack:
            cur = stack.pop()
            if cur in out:
                continue
            out.add(cur)
            stack.extend(kids.get(cur, []))
        return out

    def _merged_item(self, row: dict, summary: bool = False) -> dict:
        db = self.db
        iid = row["item_id"].strip()
        ext = self._extension_index().get(iid, {})
        loc = (row.get("location_id", "") or "").strip()
        containers = db.containers_by_id()
        container = containers.get(loc, {})
        out = {
            "item_id": iid,
            "class": row.get("class", ""),
            "name": row.get("name", ""),
            "status": row.get("status", ""),
            "handler": row.get("handler", ""),
            "notes": row.get("notes", ""),
            "deleted_at": row.get("deleted_at", ""),
            "updated_by": row.get("updated_by", ""),
            "updated_at": row.get("updated_at", ""),
            "location": {
                "container_id": loc,
                "label": container.get("label", ""),
                "kind": container.get("kind", ""),
                "path": " / ".join(db.container_path(loc)) if loc else "",
                "position": (row.get("position", "") or "").strip(),
            },
        }
        if row.get("class") == "sample":
            labels = self._labels_for(ext)
            out["sample"] = {
                "gml_id": ext.get("gml_id", ""),
                "sample_type": ext.get("sample_type", ""),
                "source_material": ext.get("source_material", ""),
                "collected_on": ext.get("collected_on", ""),
                "received_on": ext.get("received_on", ""),
                "processed_on": ext.get("processed_on", ""),
                "patient_id": ext.get("patient_id", ""),
                "patient_label": labels.get("patient", ""),
                "project_id": ext.get("project_id", ""),
                "project_name": labels.get("project", ""),
            }
        elif row.get("class") in ("reagent", "kit", "consumable"):
            out[row["class"]] = {
                "price": ext.get("price", ""),
                "expires_on": ext.get("expires_on", ""),
                "storage_requirements": ext.get("storage_requirements", ""),
            }
        if not summary:
            out["latest_measurements"] = [
                self._brief_reading(m)
                for m in self.get_measurements(item_id=iid)
            ]
        return out

    @staticmethod
    def _brief_reading(m: dict) -> dict:
        """Latest readings inline are a glance; history and correction ids are
        ``get_measurements``, which keeps the columns dropped here.

        The canonical figure stays even here because the glance is exactly where two
        tubes get compared, and they are not always quoted in the same unit."""
        return {k: m[k] for k in ("assay", "measured_on", "value", "unit", "canonical")}

    def _labels_for(self, ext: dict) -> dict[str, str]:
        out = {}
        pid = (ext.get("patient_id", "") or "").strip()
        if pid:
            row = self.db.t("patients").get(pid)
            out["patient"] = row.get("label", "") if row else ""
        prid = (ext.get("project_id", "") or "").strip()
        if prid:
            row = self.db.t("projects").get(prid)
            out["project"] = row.get("name", "") if row else ""
        return out

    def _container_summary(self, cid: str, row: dict) -> dict:
        db = self.db
        kids = db.child_containers(cid)
        cap = db.capacity_of(cid)
        return {
            "container_id": cid,
            "kind": row.get("kind", ""),
            "label": row.get("label", ""),
            "parent_id": (row.get("parent_id", "") or "").strip(),
            "path": " / ".join(db.container_path(cid)),
            "geometry": self._geometry(row),
            "capacity": cap,
            # Slots taken, nested containers included: a rack holding five boxes is
            # full, not empty with five boxes somewhere beneath it.
            "occupied": len(db.occupants(cid)) + len(kids),
            "child_containers": len(kids),
            "notes": row.get("notes", ""),
        }

    @staticmethod
    def _geometry(row: dict) -> str:
        """Well grid as ``rows x cols``; empty for furniture, whose slots are ``capacity``."""
        r, c = (row.get("rows", "") or "").strip(), (row.get("cols", "") or "").strip()
        return f"{r}x{c}" if r and c else ""

    # -- the commit funnel ---------------------------------------------------

    @contextlib.contextmanager
    def bulk(self, operation: str = "bulk"):
        """One pre-write copy for a run of commits, instead of one copy per commit.

        Seeding and resetting are single decisions -- fill this database, empty it --
        and thirty workbook copies to undo one of them is both noise and thirty extra
        requests against a quota that is already tight. The copy taken on entry is the
        state before the first commit, which is the state anybody would want back.
        """
        if self._bulk_depth == 0:
            self._bulk_snapshot = self._snapshot(
                [t.name for t in schema.TABLES], operation)
        self._bulk_depth += 1
        try:
            yield self._bulk_snapshot
        finally:
            self._bulk_depth -= 1
            if self._bulk_depth == 0:
                self._bulk_snapshot = None

    def _apply(self, operation: str, changes) -> CommitResult:
        """Single write path.

        ``changes`` is a list of ``(table, insert_rows, update_rows)`` or
        ``(table, [], [], final_rows)`` when the whole table content is computed
        (deletions, merges). The candidate state is validated *before* anything
        touches storage, so a rejected write leaves no trace; the snapshot happens
        after validation but before the first byte is written.
        """
        candidate = dbmod.clone(self.db)
        touched: list[str] = []

        for change in changes:
            table_name, inserts, updates = change[0], change[1], change[2]
            final_rows = change[3] if len(change) > 3 else None
            t = candidate.tables[table_name]
            if table_name not in touched:
                touched.append(table_name)
            if final_rows is not None:
                t.rows = [_as_cells(r) for r in final_rows]
            else:
                for upd in updates:
                    upd = _as_cells(upd)
                    target = t.get(upd[t.spec.primary_key])
                    if target is None:
                        t.rows.append(upd)
                    else:
                        target.clear()
                        target.update(upd)
                for ins in inserts:
                    t.rows.append(_as_cells(ins))

        # Header must cover every column we are about to write, including ones
        # that were absent from a hand-made file until now.
        for name in touched:
            t = candidate.tables[name]
            base = list(t.header) if t.header and not t.missing else list(t.spec.column_names)
            for want in t.spec.column_names:
                if want not in base:
                    base.append(want)
            t.header = tuple(base)

        scope_violations = validation.validate_scope(candidate, touched)
        bad = validation.errors(scope_violations)
        if bad:
            # Only on the failure path: compare against what is already stored so the
            # caller can be told which of these their own write caused.
            standing = {validation.key(v) for v in validation.errors(validation.validate(self.db))}
            raise ValidationError(bad, [v for v in bad if validation.key(v) not in standing])

        snapshot = self._bulk_snapshot or self._snapshot(touched, operation)
        batch = []
        for name in touched:
            t = candidate.tables[name]
            rows = [{k: v for k, v in r.items() if k != EXTRA_COLUMNS_KEY} for r in t.rows]
            batch.append((name, t.header, rows, t.revision or None))
        landed: dict[str, str] = {}
        try:
            # One call for the whole commit where the backend can carry one. A commit
            # that touches items and samples used to pay per-table round trips for
            # its compare-and-swap, its write and its confirmation each.
            new_revisions = self.store.write_batch(batch, written=landed)
        except StoreError as e:
            # A commit that cannot finish must leave nothing half-landed: any table
            # the store reported as landed goes back to its pre-commit content. Live
            # finding: the CAS failure used to surface only after the earlier tables
            # had been written, orphaning rows across tables.
            reverted, stuck = self._rollback(landed)
            detail = ""
            if reverted:
                detail += f"\nReverted: {', '.join(reverted)}."
            if stuck:
                detail += (f"\nCOULD NOT REVERT: {', '.join(stuck)} -- they changed "
                           "again while this commit was aborting. Run doctor before "
                           "writing more; do not overwrite by hand.")
            backup = f"\nBackup of the pre-write state: {snapshot.path or 'n/a'}"
            if isinstance(e, RevisionConflict):
                raise Conflict(f"{e}{detail}{backup}") from e
            if detail:
                raise StoreError(f"{e}{detail}") from e
            raise

        self._db = candidate
        for name in touched:
            self._db.tables[name].revision = new_revisions.get(
                name, compute_revision(self._db.tables[name].header, self._db.tables[name].rows)
            )
        self._violations = validation.validate(self._db)
        warns = [v for v in self._violations if v.table in touched and v.severity == "warning"]
        return CommitResult(
            operation=operation,
            touched=tuple(touched),
            snapshot=snapshot,
            warnings=[v.as_dict() for v in warns],
        )

    def _rollback(self, written: dict[str, str]) -> tuple[list[str], list[str]]:
        """Put tables already pushed by an aborted commit back as they were.

        ``written`` maps table -> the revision our own push produced. We CAS
        against that value, so if a human or another session wrote again in the
        meantime their change wins and the table is reported as stuck rather
        than bulldozed by the undo. A table this commit *created* cannot be
        deleted through the store API; it is emptied instead, which matches the
        freshly-initialised shape.
        """
        reverted: list[str] = []
        stuck: list[str] = []
        for name, rev in written.items():
            t = self.db.tables[name]  # still the pre-commit view
            rows = [{k: v for k, v in r.items() if k != EXTRA_COLUMNS_KEY} for r in t.rows]
            try:
                self.store.write_table(name, t.header, rows, rev)
                reverted.append(name)
            except StoreError:
                stuck.append(name)
        return reverted, stuck

    def _snapshot(self, tables: Iterable[str], operation: str) -> Snapshot:
        """Copy the pre-write content of the tables we are about to change.

        This is the undo affordance: no event journal, but any accepted write can
        be reverted by copying these back. Two shapes, because storage comes two ways:
        a local directory gets a folder of CSV copies, a remote backend that keeps the
        whole database in one file gets a copy of that file. A store offering neither
        reports ``path=None`` rather than pretending.
        """
        names = [t for t in tables]
        remote = getattr(self.store, "snapshot", None)
        if callable(remote):
            return Snapshot(path=remote(names, operation), tables=tuple(names))
        root = getattr(self.store, "root", None)
        if root is None:
            return Snapshot(path=None, tables=tuple(names))
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backups = Path(root) / "backups"
        dest = backups / f"{stamp}-{operation}"
        # A minute of bursty scripting produces many writes in the same second;
        # generations must never overwrite each other or the undo story lies.
        n = 1
        while dest.exists():
            dest = backups / f"{stamp}-{operation}-{n}"
            n += 1
        dest.mkdir(parents=True, exist_ok=False)
        wrote = []
        for name in names:
            src = getattr(self.store, "path_for")(name)
            if Path(src).exists():
                shutil.copy2(src, dest / Path(src).name)
                wrote.append(Path(src).name)
        self._prune_backups(backups)
        return Snapshot(path=str(dest), tables=tuple(names))

    def _prune_backups(self, backups_dir: Path) -> None:
        keep = max(int(self.config.keep_backups), 0)
        if keep <= 0 or not backups_dir.exists():
            return
        generations = [p for p in backups_dir.iterdir() if p.is_dir()]
        # Sort by creation time, not name: two writes in the same second produce
        # names ordered by operation, so a name sort can evict the generation that
        # was just made -- which is the one anyone would want to restore.
        generations.sort(key=lambda p: (p.stat().st_mtime_ns, p.name))
        for old in generations[:-keep]:
            shutil.rmtree(old, ignore_errors=True)


def _s(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip()


def _as_cells(row: dict) -> dict:
    """Coerce a row into CSV cells at the single write boundary.

    A client sending JSON ``null`` for an optional field must not put the literal
    ``None`` into a file or into a revision hash; the unknown-column marker is not
    a cell and passes through untouched.
    """
    return {k: (v if k == EXTRA_COLUMNS_KEY else _s(v)) for k, v in row.items()}


def _opt(value: Any) -> str:
    """Strip a caller-supplied optional string, tolerating null."""
    if value is None:
        return ""
    return str(value).strip()


def _opt_float(value: str) -> float | None:
    try:
        return float(value) if value else None
    except ValueError:
        return None
