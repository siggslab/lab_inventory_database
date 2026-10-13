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

import os
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
    def __init__(self, violations: list[validation.Violation]):
        self.violations = violations
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


_STATUS_ORDER = {"present": 0, "partially_consumed": 1, "consumed": 2}


class Service:
    def __init__(self, config: Config, store: Optional[TableStore] = None):
        self.config = config
        self.store = store or make_store(config)
        self._db: Optional[dbmod.Database] = None
        self._violations: list[validation.Violation] = []

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

    def search_items(self, **kwargs) -> list[dict]:
        return [self._lean_row(r) for r in self._matching_items(**kwargs)]

    def _matching_items(self, **kwargs) -> list[dict]:
        """Filtered items as complete summary records.

        The untrimmed sibling of ``search_items``: same filters, no context
        diet. Internal consumers (rollups, CLI) read it so that the model-facing
        rows can stay as thin as they want.
        """
        f = ItemFilter(**kwargs)
        db = self.db
        items = db.items_by_id()
        ext_index = self._extension_index()
        containers = db.containers_by_id()
        needle = f.text.lower().strip()
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

    def container_tree(self, root_id: str = "") -> list[dict]:
        db = self.db
        children: dict[str, list[str]] = {}
        by_id = db.containers_by_id()
        for cid, row in by_id.items():
            parent = (row.get("parent_id", "") or "").strip()
            children.setdefault(parent, []).append(cid)
        for kids in children.values():
            kids.sort(key=lambda c: by_id[c].get("label", "").lower())

        def build(cid: str) -> dict:
            node = self._container_summary(cid, by_id[cid])
            node["children"] = [build(c) for c in children.get(cid, [])]
            return node

        roots = children.get("", [])
        if root_id:
            if root_id not in by_id:
                raise NotFound(f"no container with id {root_id!r}")
            return [build(root_id)]
        return [build(c) for c in roots]

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

    def contents(self, container_id: str, recursive: bool = False) -> dict:
        """What is stored in a container: lean rows, plus room at the top."""
        db = self.db
        by_id = db.containers_by_id()
        if container_id not in by_id:
            raise NotFound(f"no container with id {container_id!r}")
        targets = self._container_subtree(container_id) if recursive else {container_id}
        row = by_id[container_id]
        occupants = [
            i for i in db.items_by_id().values()
            if (i.get("location_id", "") or "").strip() in targets
            and not (i.get("deleted_at", "") or "").strip()
        ]
        cap = db.capacity_of(container_id)
        kids = db.child_containers(container_id)
        here = len(db.occupants(container_id))
        return {
            "container": {
                "container_id": container_id,
                "kind": row.get("kind", ""),
                "label": row.get("label", ""),
                "path": " / ".join(db.container_path(container_id)),
                "geometry": self._geometry(row),
                "capacity": cap,
                "occupied_here": here,
                # Nested containers take slots too, so free is not capacity minus the
                # items listed below -- otherwise a bench reports room that isn't there.
                "child_containers": len(kids),
                "free": (cap - here - len(kids)) if cap is not None else None,
            },
            "recursive": recursive,
            "count": len(occupants),
            "items": [
                self._contents_row(i, db, container_id, recursive)
                for i in sorted(
                    occupants,
                    key=lambda i: (
                        i.get("position", ""), i.get("name", "").lower(), i["item_id"]
                    ),
                )
            ],
        }

    def _contents_row(self, item: dict, db, top: str, recursive: bool) -> dict:
        row = {
            "item_id": item["item_id"],
            "class": item.get("class", ""),
            "name": item.get("name", ""),
            "status": item.get("status", ""),
            "position": (item.get("position", "") or "").strip(),
        }
        if recursive:
            loc = (item.get("location_id", "") or "").strip()
            row["in"] = (
                " / ".join(db.container_path(loc)) if loc != top else ""
            )
        return row

    def count_items(self, **kwargs) -> int:
        return len(self.search_items(**kwargs))

    def search_patients(self, query: str, project_id: str = "", limit: int = 20) -> dict:
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
        return {
            "matched": len(people),
            "truncated": len(people) > cap,
            "unassigned": unassigned["total"],
            "rows": people[:cap],
        }

    def rollup_by_project(self, **kwargs) -> list[dict]:
        return self._rollup("project", **kwargs)

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

        Two independent axes of extra rows, both off by default.
        ``include_superseded`` adds readings a correction invalidated; those are
        never candidates for "the latest", so they arrive on top of whatever the
        other flags chose. ``include_historical`` adds valid readings that a newer
        one has overtaken -- the whole series instead of its endpoint.

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
        for r in db.t("measurements").rows:
            mid = (r.get("measurement_id", "") or "").strip()
            if not mid:
                continue
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
        if include_superseded:
            rows.extend(retired)
        rows.sort(key=lambda x: (x["item_id"], x["assay"], x["measured_on"]))
        return rows

    # A reading answers what was measured, when, and whether it is still the
    # belief. The assay code already keys the registry, so quantity is one lookup
    # away; the row echoes no item_id because a reading query names its item.
    # Canonical conversion stays in the exports, where the arithmetic belongs --
    # value+unit here is the number as observed, which is what prose quotes back.
    @staticmethod
    def _tool_reading(m: dict) -> dict:
        return {
            "measurement_id": m["measurement_id"],
            "assay": m["assay"],
            "assay_name": m["assay_name"],
            "measured_on": m["measured_on"],
            "value": m["value_num"] if m["value_num"] is not None else m["value_text"],
            "unit": m["unit"],
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

    def run_query(self, sql: str, limit: int = 500) -> dict:
        """Guarded read-only SQL over an in-memory mirror of the tables.

        Exists because ad-hoc questions ('how many PBMC samples per project
        received since 2024?') are clumsy to express through fixed tools. The
        mirror is built from the same loaded tables, so it inherits whatever the
        validator accepted.
        """
        from .query import run_sql

        return run_sql(self.db, sql, limit=limit)

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
        if item_class not in ("reagent", "kit", "consumable"):
            raise ServiceError(f"{item_class!r} is not a supply class")
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
            measured_on = _s(quantity_measured_on)
            if not measured_on:
                measured_on = validation.format_datetime(validation.now_utc_minute())
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

    def move_item(
        self,
        item_id: str,
        location_id: Optional[str] = None,
        position: Optional[str] = None,
        clear_location: bool = False,
    ) -> CommitResult:
        db = self.db
        row = self._require_item(item_id)
        updated = dict(row)
        if clear_location:
            updated["location_id"] = ""
            updated["position"] = ""
        else:
            moved_container = (
                location_id is not None
                and location_id.strip() != (row.get("location_id", "") or "").strip()
            )
            if location_id is not None:
                updated["location_id"] = location_id.strip()
            if position is not None:
                updated["position"] = position.strip()
            elif moved_container:
                # A well label belongs to a particular grid. Carrying "A1" into a
                # box with different geometry -- or one whose row labels skip I --
                # would assert something nobody confirmed, so it becomes unknown.
                updated["position"] = ""
        if updated.get("status", "") == "consumed" and updated.get("location_id", ""):
            raise Conflict(
                f"{item_id} is consumed and holds no slot; set it back to present first"
            )
        updated.update(self._audit())
        result = self._apply("move_item", [("items", [], [updated])])
        result.records = [self.get_item(item_id)]
        return result

    def set_status(self, item_id: str, status: str, allow_backwards: bool = False) -> CommitResult:
        row = self._require_item(item_id)
        current = row.get("status", "")
        if status not in schema.ITEM_STATUSES:
            raise ServiceError(f"status must be one of {schema.ITEM_STATUSES}")
        if _STATUS_ORDER.get(status, 0) < _STATUS_ORDER.get(current, 0) and not allow_backwards:
            raise Conflict(
                f"{item_id} is already {current!r}; moving back to {status!r} needs "
                "allow_backwards=true (it usually means something was mis-recorded)"
            )
        updated = dict(row)
        updated["status"] = status
        if status == "consumed":
            # A consumed item cannot occupy a slot. Dropping the position here is
            # deliberate: keeping it would claim a well that is physically free.
            updated["location_id"] = ""
            updated["position"] = ""
        updated.update(self._audit())
        result = self._apply("set_status", [("items", [], [updated])])
        result.records = [self.get_item(item_id)]
        return result

    def record_measurement(
        self,
        item_id: str,
        assay: str = "",
        measured_on: str = "",
        value: str = "",
        unit: str = "",
        text: str = "",
        handler: str = "",
        instrument: str = "",
        supersedes: str = "",
        notes: str = "",
        assay_id: str = "",
    ) -> CommitResult:
        self._require_item(item_id)
        assay_row = self._resolve_assay(assay, assay_id)
        assay_label = assay_row.get("code", "") or assay_row.get("name", "")
        if not value and not text:
            raise ServiceError("pass either value= (numeric assays) or text= (textual assays)")
        if assay_row.get("result_type") == "numeric":
            if not value:
                raise ServiceError(
                    f"assay {assay_label!r} is numeric; pass value= not text="
                )
            try:
                float(value)
            except ValueError:
                raise ServiceError(f"value {value!r} is not a number") from None
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
                units.to_canonical(quantity, float(value), unit)
            except units.UnitError as e:
                raise ServiceError(str(e)) from None
        else:
            if not text:
                raise ServiceError(f"assay {assay_label!r} is textual; pass text= not value=")
        when = measured_on or validation.format_datetime(validation.now_utc_minute())
        mid = ids.new_id("msm")
        row = {
            "measurement_id": mid,
            "item_id": item_id.strip(),
            "assay_id": assay_row["assay_id"],
            "measured_on": when,
            "value_num": _s(value),
            "value_text": text,
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
        return result

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
            "value": changes.pop("value", old.get("value_num", "")),
            "unit": changes.pop("unit", old.get("unit", "")),
            "text": changes.pop("text", old.get("value_text", "")),
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

    def update_fields(self, table: str, record_id: str, **changes) -> CommitResult:
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
        result = self._apply("update_fields", [(table, [], [updated])])
        result.records = [dict(updated)]
        return result

    def create_patient(self, label: str, **kw) -> CommitResult:
        pid = ids.new_id("pat")
        row = {"patient_id": pid, "label": label, **self._audit()}
        for k, v in kw.items():
            if schema.PATIENTS.field(k) is None:
                raise ServiceError(f"patients has no field {k!r}")
            row[k] = _s(v)
        result = self._apply("create_patient", [("patients", [row], [])])
        result.records = [row]
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
        for r in self.db.t("patient_projects").rows:
            if (
                r.get("patient_id", "").strip() == patient_id
                and r.get("project_id", "").strip() == project_id
            ):
                raise Conflict("that patient is already registered under that project")
        lid = ids.new_id("ppl")
        row = {"link_id": lid, "patient_id": patient_id, "project_id": project_id}
        result = self._apply("assign_patient_project", [("patient_projects", [row], [])])
        result.records = [row]
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

    def delete_item(self, item_id: str, force: bool = False) -> CommitResult:
        row = self._require_item(item_id)
        if (row.get("deleted_at", "") or "").strip():
            raise Conflict(f"{item_id} is already deleted")
        if row.get("status", "") != "consumed" and not force:
            raise Conflict(
                f"{item_id} is {row.get('status', '?')!r}, not consumed. Discarding a "
                "tube that still holds material needs force=true."
            )
        updated = dict(row)
        updated["deleted_at"] = validation.format_datetime(validation.now_utc_minute())
        updated["location_id"] = ""
        updated["position"] = ""
        updated.update(self._audit())
        result = self._apply("delete_item", [("items", [], [updated])])
        result.records = [self.get_item(item_id)]
        return result

    def delete_container(self, container_id: str) -> CommitResult:
        db = self.db
        by_id = db.containers_by_id()
        if container_id not in by_id:
            raise NotFound(f"no container with id {container_id!r}")
        occupants = db.occupants(container_id)
        if occupants:
            raise Conflict(
                f"{by_id[container_id].get('label', container_id)!r} still holds "
                f"{len(occupants)} item(s); move them out first"
            )
        children = [
            c for c in by_id.values() if (c.get("parent_id", "") or "").strip() == container_id
        ]
        if children:
            raise Conflict(f"{container_id} still has {len(children)} child container(s)")
        t = db.t("containers")
        rows = [r for r in t.rows if (r.get("container_id", "") or "").strip() != container_id]
        result = self._apply("delete_container", [("containers", [], [], rows)])
        result.records = [{"deleted_container": container_id}]
        return result

    def merge_items(self, keep_id: str, discard_id: str, reason: str = "") -> CommitResult:
        """Re-point everything referencing ``discard_id``, then tombstone it."""
        keep = self._require_item(keep_id)
        discard = self._require_item(discard_id)
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
        ``get_measurements``, which keeps the columns dropped here."""
        return {k: m[k] for k in ("assay", "measured_on", "value", "unit")}

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

    def _rollup(self, axis: str, **kwargs) -> list[dict]:
        """Group matching items by a flat link and count them per status.

        Only one axis uses this now. ``patient`` moved to ``search_patients``, which
        enumerates people rather than items, and there is no container axis because
        storage is a tree -- grouping by an item's direct container counts boxes and
        never their freezer. ``get_occupancy`` walks that hierarchy; this does not.
        """
        items = self._matching_items(**kwargs)
        key_field = {"project": "project_id"}[axis]
        label_field = "project_name"
        buckets: dict[str, dict] = {}
        for it in items:
            sub = it.get("sample") or {}
            key = sub.get(key_field, "") or "(none)"
            label = sub.get(label_field, "") or key
            b = buckets.setdefault(key, {"key": key, "label": label, "total": 0,
                                         "present": 0, "partially_consumed": 0, "consumed": 0})
            b["total"] += 1
            st = it.get("status", "")
            if st in b:
                b[st] += 1
        return sorted(buckets.values(), key=lambda b: -b["total"])

    # -- the commit funnel ---------------------------------------------------

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
            raise ValidationError(bad)

        snapshot = self._snapshot(touched, operation)
        new_revisions: dict[str, str] = {}
        try:
            for name in touched:
                t = candidate.tables[name]
                rows = [{k: v for k, v in r.items() if k != EXTRA_COLUMNS_KEY} for r in t.rows]
                new_rev = self.store.write_table(name, t.header, rows, t.revision or None)
                new_revisions[name] = new_rev
        except StoreError as e:
            # A commit that cannot finish must leave nothing half-landed: any
            # table already pushed by this loop goes back to its pre-commit
            # content. Live finding: the CAS failure used to surface only after
            # the earlier tables had been written, orphaning rows across tables.
            reverted, stuck = self._rollback(new_revisions)
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
        be reverted by copying these files back. Only available when storage is a
        local directory; remote backends report it honestly rather than pretending.
        """
        names = [t for t in tables]
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
