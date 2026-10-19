"""In-memory view of the database, built from whatever the store returns.

Two rules govern this layer, and they are the reason the whole thing survives
hand-edited spreadsheets:

1. Files are authoritative and never blindly trusted. A bad cell does not raise;
   it becomes a Violation attached to its row, and every other row still loads.
2. Unknown columns are preserved rather than dropped. If someone adds a column
   in Excel, we do not quietly delete their work on the next write.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Iterator, Optional

from . import schema
from .store.base import EXTRA_COLUMNS_KEY, StoreError, TableData

#: reserved key carrying cells that had no header slot
EXTRA = EXTRA_COLUMNS_KEY


@dataclass
class Table:
    name: str
    spec: schema.Table
    header: tuple[str, ...]
    rows: list[dict] = field(default_factory=list)
    revision: str = ""
    missing: bool = False

    @property
    def known_header(self) -> tuple[str, ...]:
        return self.spec.column_names

    def unknown_columns(self) -> tuple[str, ...]:
        return tuple(c for c in self.header if c not in self.known_header)

    def get(self, pk_value: str) -> Optional[dict]:
        for r in self.rows:
            if r.get(self.spec.primary_key, "").strip() == pk_value:
                return r
        return None

    def pks(self) -> set[str]:
        pk = self.spec.primary_key
        return {r.get(pk, "").strip() for r in self.rows if r.get(pk, "").strip()}

    def live(self) -> Iterator[dict]:
        """Rows excluding tombstones (tables without deleted_at yield all)."""
        for r in self.rows:
            if r.get("deleted_at", "").strip():
                continue
            yield r


@dataclass
class Database:
    tables: dict[str, Table]
    #: tables present in storage but not part of the schema
    stray_files: tuple[str, ...] = ()

    # -- convenience accessors ---------------------------------------------

    def t(self, name: str) -> Table:
        return self.tables[name]

    def items_by_id(self) -> dict[str, dict]:
        return {r["item_id"].strip(): r for r in self.t("items").rows if r.get("item_id", "").strip()}

    def containers_by_id(self) -> dict[str, dict]:
        return {
            r["container_id"].strip(): r
            for r in self.t("containers").rows
            if r.get("container_id", "").strip()
        }

    def assays_by_id(self) -> dict[str, dict]:
        return {r["assay_id"].strip(): r for r in self.t("assays").rows if r.get("assay_id", "").strip()}

    def extension_rows(self, item_class: str) -> list[dict]:
        return self.t(schema.CLASS_EXTENSION_TABLE[item_class]).rows

    def container_path(self, container_id: str) -> list[str]:
        """Label path from root container down to this one. Cycle-safe."""
        by_id = self.containers_by_id()
        out: list[str] = []
        seen: set[str] = set()
        cid: Optional[str] = container_id
        while cid and cid in by_id and cid not in seen:
            seen.add(cid)
            out.append(by_id[cid].get("label", "").strip() or cid)
            parent = by_id[cid].get("parent_id", "").strip()
            cid = parent or None
        return list(reversed(out))

    def capacity_of(self, container_id: str) -> Optional[int]:
        """How many things fit here, or None when nobody recorded it.

        A slot holds one thing, which may be a tube or a nested container, so the
        number is unitless on purpose. Never add capacities of containers that sit
        at different tiers: five box-slots and a hundred wells are not two hundred
        of anything.
        """
        by_id = self.containers_by_id()
        c = by_id.get(container_id)
        if not c:
            return None
        stated = _to_int(c.get("capacity", ""))
        if stated is not None:
            return stated
        rows = _to_int(c.get("rows", ""))
        cols = _to_int(c.get("cols", ""))
        if rows and cols:
            return rows * cols
        return None

    def occupants(self, container_id: str) -> list[dict]:
        return [
            r
            for r in self.t("items").live()
            if r.get("location_id", "").strip() == container_id
        ]

    def child_containers(self, container_id: str) -> list[str]:
        return [
            cid
            for cid, c in self.containers_by_id().items()
            if (c.get("parent_id", "") or "").strip() == container_id
        ]

    def slot_use(self, container_id: str) -> int:
        """Slots taken here: every resident item and every nested container."""
        return len(self.occupants(container_id)) + len(self.child_containers(container_id))


def empty_table(name: str) -> Table:
    spec = schema.get_table(name)
    return Table(name=name, spec=spec, header=spec.column_names, rows=[], missing=True)


def load(store) -> tuple[Database, list["Violation"]]:  # noqa: F821
    """Read every schema table and validate. Never raises on bad content."""
    from .validation import Violation, validate

    tables: dict[str, Table] = {}
    violations: list[Violation] = []

    # One bulk read where the backend can do one. A backend that cannot -- a file per
    # table -- raises on the first absent file rather than answering, and falling back
    # to the table-by-table path below keeps a missing file a warning instead of
    # turning the whole database unreadable.
    try:
        bulk = store.read_all()
    except StoreError:
        bulk = {}

    for spec in schema.TABLES:
        data: Optional[TableData] = bulk.get(spec.name)
        if data is None:
            try:
                data = store.read_table(spec.name)
            except StoreError:
                tables[spec.name] = empty_table(spec.name)
                violations.append(
                    Violation(
                        table=spec.name,
                        row_id="",
                        field="",
                        code="table_missing",
                        severity="warning",
                        message=f"{spec.filename} not present in storage; treating as empty",
                    )
                )
                continue
        tables[spec.name] = _build(spec, data)

    strays = tuple(sorted(set(store.list_tables()) - set(tables)))
    db = Database(tables=tables, stray_files=strays)
    for fname in strays:
        violations.append(
            Violation(
                table="<storage>",
                row_id="",
                field=fname,
                code="stray_file",
                severity="warning",
                message=f"{fname} is not part of the schema and will be ignored",
            )
        )
    violations.extend(validate(db))
    return db, violations


def _build(spec, data: TableData) -> Table:
    header = tuple(data.header)
    rows: list[dict] = []
    for raw in data.rows:
        row = {c: (raw.get(c, "") or "").strip() if isinstance(raw.get(c, ""), str) else raw.get(c, "") for c in header}
        extra = raw.get(EXTRA_COLUMNS_KEY)
        if extra:
            row[EXTRA] = extra
        rows.append(row)
    return Table(name=spec.name, spec=spec, header=header, rows=rows, revision=data.revision)


def _to_int(value) -> Optional[int]:
    try:
        s = str(value).strip()
        return int(s) if s else None
    except (TypeError, ValueError):
        return None


def clone_table(t: Table) -> Table:
    return Table(
        name=t.name,
        spec=t.spec,
        header=t.header,
        rows=[dict(r) for r in t.rows],
        revision=t.revision,
        missing=t.missing,
    )


def clone(db: Database) -> Database:
    return Database(
        tables={k: clone_table(v) for k, v in db.tables.items()},
        stray_files=db.stray_files,
    )
