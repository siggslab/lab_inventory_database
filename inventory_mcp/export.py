"""Materialised projections for third-party consumers.

Dashboards should read these rather than call the MCP: MCP is a control plane
shaped for a model (stdio, content blocks, context-sized tool surface), not a
data plane. The exports are flat, denormalised, version-stamped, and paired with
a SQLite mirror because BI tools speak SQL and none of them want to re-derive
the supertype/extension join themselves.

Exports are derived artifacts and therefore sit outside the compare-and-swap
discipline -- last writer wins, no precondition -- but their freshness is made
explicit, because across a network sync there is otherwise no bound on staleness.
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import SCHEMA_VERSION, units
from .validation import Violation

EXPORT_VIEWS = (
    "items_wide",
    "occupancy",
    "measurements_long",
    "item_links",
    "patient_projects",
)


def export_views(service, directory: str = "", formats=("csv", "sqlite")) -> dict[str, Any]:
    root = _resolve_root(service, directory)
    out_dir = root / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)

    db = service.db
    violations = service.violations
    err_count = sum(1 for v in violations if v.severity == "error")

    views = {
        "items_wide": _view_items_wide(service),
        "occupancy": service._occupancy_rows(),
        "measurements_long": _view_measurements(service),
        "item_links": _view_item_links(service),
        "patient_projects": _view_patient_projects(service),
    }

    written: dict[str, dict] = {}
    if "csv" in formats:
        for name, rows in views.items():
            path = out_dir / f"{name}.csv"
            _write_csv(path, rows)
            written[name] = {"file": path.name, "rows": len(rows)}

    sqlite_path = None
    if "sqlite" in formats:
        sqlite_path = out_dir / "inventory.sqlite"
        _write_sqlite(sqlite_path, db, views)
        written["inventory.sqlite"] = {
            "file": sqlite_path.name,
            "rows": _sqlite_rowcount(sqlite_path),
        }

    generated_at = datetime.now(timezone.utc).replace(microsecond=0)
    manifest_path = out_dir / "manifest.json"
    previous = _read_json(manifest_path)
    if previous and _is_newer(previous.get("generated_at", ""), generated_at):
        raise RuntimeError(
            f"refusing to overwrite exports generated at {previous['generated_at']} "
            f"with an older generation ({generated_at.isoformat()}). Check the clock, "
            "or delete exports/manifest.json deliberately."
        )

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "backend": service.store.describe(),
        "source_revisions": {
            name: t.revision for name, t in db.tables.items() if not t.missing
        },
        "files": written,
        "integrity": {
            "open_errors": err_count,
            "open_warnings": len(violations) - err_count,
            "clean": err_count == 0,
        },
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return {"directory": str(out_dir), "manifest": manifest}


# ---------------------------------------------------------------------------
# view builders
# ---------------------------------------------------------------------------


def _view_items_wide(service) -> list[dict]:
    db = service.db
    containers = db.containers_by_id()
    patients = {r["patient_id"].strip(): r for r in db.t("patients").rows if r.get("patient_id", "").strip()}
    projects = {r["project_id"].strip(): r for r in db.t("projects").rows if r.get("project_id", "").strip()}
    ext = service._extension_index()
    latest = {
        (m["item_id"], m["assay"]): m
        for m in service._measurement_rows()
    }
    # group latest measurements per item for compact columns
    per_item: dict[str, dict[str, dict]] = {}
    for (iid, assay), m in latest.items():
        per_item.setdefault(iid, {})[assay] = m

    rows = []
    for iid, item in db.items_by_id().items():
        e = ext.get(iid, {})
        loc = (item.get("location_id", "") or "").strip()
        container = containers.get(loc, {})
        path = " / ".join(db.container_path(loc)) if loc else ""
        patient = patients.get((e.get("patient_id", "") or "").strip(), {})
        project = projects.get((e.get("project_id", "") or "").strip(), {})
        meas = per_item.get(iid, {})
        conc = meas.get("qubit_dsdna") or meas.get("nanodrop_a260") or {}
        vol = meas.get("volume_ul") or {}
        yield_value = None
        if conc.get("canonical") and vol.get("canonical"):
            yield_value = round(conc["canonical"]["value"] * vol["canonical"]["value"], 3)
        rows.append(
            {
                "item_id": iid,
                "class": item.get("class", ""),
                "name": item.get("name", ""),
                "status": item.get("status", ""),
                "deleted_at": item.get("deleted_at", ""),
                "gml_id": e.get("gml_id", ""),
                "sample_type": e.get("sample_type", ""),
                "source_material": e.get("source_material", ""),
                "patient_label": patient.get("label", ""),
                "project_name": project.get("name", ""),
                "container_label": container.get("label", ""),
                "container_kind": container.get("kind", ""),
                "container_path": path,
                "position": (item.get("position", "") or "").strip(),
                "handler": item.get("handler", ""),
                "collected_on": e.get("collected_on", ""),
                "received_on": e.get("received_on", ""),
                "processed_on": e.get("processed_on", ""),
                "concentration_ng_ul": (conc.get("canonical") or {}).get("value", ""),
                "volume_ul": (vol.get("canonical") or {}).get("value", ""),
                "computed_yield_ng": yield_value if yield_value is not None else "",
                "notes": item.get("notes", ""),
                "updated_by": item.get("updated_by", ""),
                "updated_at": item.get("updated_at", ""),
            }
        )
    rows.sort(key=lambda r: r["name"].lower())
    return rows


def _view_measurements(service) -> list[dict]:
    rows = []
    # Both flags: this view is the complete record, not what is currently believed.
    for m in service._measurement_rows(include_historical=True, include_superseded=True):
        canon = m.get("canonical") or {}
        rows.append(
            {
                "measurement_id": m["measurement_id"],
                "item_id": m["item_id"],
                "assay": m["assay"],
                "assay_name": m["assay_name"],
                "quantity": m["quantity"],
                "measured_on": m["measured_on"],
                "value": m["value_num"] if m["value_num"] is not None else m["value_text"],
                "unit": m["unit"],
                "canonical_value": canon.get("value", ""),
                "canonical_unit": canon.get("unit", ""),
                "handler": m["handler"],
                "instrument": m["instrument"],
                "supersedes": m["supersedes"],
                "superseded": "yes" if m["superseded"] else "no",
                "notes": m["notes"],
            }
        )
    return rows


def _view_item_links(service) -> list[dict]:
    db = service.db
    items = db.items_by_id()
    rows = []
    for r in db.t("item_links").rows:
        child = items.get((r.get("child_item_id", "") or "").strip(), {})
        parent = items.get((r.get("parent_item_id", "") or "").strip(), {})
        rows.append(
            {
                "link_id": r.get("link_id", ""),
                "child_item_id": r.get("child_item_id", ""),
                "child_name": child.get("name", ""),
                "child_class": child.get("class", ""),
                "parent_item_id": r.get("parent_item_id", ""),
                "parent_name": parent.get("name", ""),
                "parent_class": parent.get("class", ""),
                "role": r.get("role", ""),
                "notes": r.get("notes", ""),
            }
        )
    return rows


def _view_patient_projects(service) -> list[dict]:
    db = service.db
    patients = {r["patient_id"].strip(): r for r in db.t("patients").rows if r.get("patient_id", "").strip()}
    projects = {r["project_id"].strip(): r for r in db.t("projects").rows if r.get("project_id", "").strip()}
    rows = []
    for r in db.t("patient_projects").rows:
        p = patients.get((r.get("patient_id", "") or "").strip(), {})
        pr = projects.get((r.get("project_id", "") or "").strip(), {})
        rows.append(
            {
                "link_id": r.get("link_id", ""),
                "patient_id": r.get("patient_id", ""),
                "patient_label": p.get("label", ""),
                "project_id": r.get("project_id", ""),
                "project_name": pr.get("name", ""),
            }
        )
    return rows


# ---------------------------------------------------------------------------
# writers
# ---------------------------------------------------------------------------


def _write_csv(path: Path, rows: list[dict]) -> None:
    header: list[str] = []
    for r in rows:
        for k in r:
            if k not in header:
                header.append(k)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=header, lineterminator="\n", extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        writer.writerow({k: ("" if v is None else v) for k, v in r.items()})
    path.write_text(buf.getvalue(), encoding="utf-8")


def _write_sqlite(path: Path, db, views: dict[str, list[dict]]) -> None:
    tmp = path.with_suffix(".tmp")
    if tmp.exists():
        tmp.unlink()
    conn = sqlite3.connect(tmp)
    try:
        from .query import _materialise  # reuse the same projection logic

        _materialise(conn, db)
        for name, rows in views.items():
            if name in db.tables:
                # A real table is already loaded under that name; re-creating it
                # from a denormalised copy would shadow the source of truth.
                # item_links and patient_projects gain labelled SQL views instead.
                continue
            _load_view(conn, name, rows)
        conn.executescript(
            """
            CREATE VIEW IF NOT EXISTS patient_projects_named AS
            SELECT l.*, p.label AS patient_label, pr.name AS project_name
            FROM patient_projects l
            LEFT JOIN patients p ON p.patient_id = l.patient_id
            LEFT JOIN projects pr ON pr.project_id = l.project_id;

            CREATE VIEW IF NOT EXISTS item_links_named AS
            SELECT l.*, c.name AS child_name, c.class AS child_class,
                   p.name AS parent_name, p.class AS parent_class
            FROM item_links l
            LEFT JOIN items c ON c.item_id = l.child_item_id
            LEFT JOIN items p ON p.item_id = l.parent_item_id;
            """
        )
        conn.commit()
    finally:
        conn.close()
    tmp.replace(path)


def _load_view(conn: sqlite3.Connection, name: str, rows: list[dict]) -> None:
    cols: list[str] = []
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)
    conn.execute(f'DROP VIEW IF EXISTS "{name}"')
    conn.execute(f'DROP TABLE IF EXISTS "{name}"')
    if not cols:
        conn.execute(f'CREATE TABLE "{name}" (_empty TEXT)')
        return
    conn.execute(f'CREATE TABLE "{name}" ({", ".join(chr(34) + c + chr(34) for c in cols)})')
    placeholders = ", ".join("?" * len(cols))
    conn.executemany(
        f'INSERT INTO "{name}" ({", ".join(chr(34) + c + chr(34) for c in cols)}) VALUES ({placeholders})',
        [[_num_or_str(r.get(c, "")) for c in cols] for r in rows],
    )


def _num_or_str(value):
    if value == "" or value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    try:
        return float(value)
    except (TypeError, ValueError):
        return str(value)


def _sqlite_rowcount(path: Path) -> int:
    conn = sqlite3.connect(path)
    try:
        total = 0
        for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
        ):
            if name.startswith("sqlite_"):
                continue
            try:
                total += conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            except sqlite3.Error:
                pass
        return total
    finally:
        conn.close()


def _resolve_root(service, directory: str) -> Path:
    if directory:
        return Path(directory).expanduser()
    root = getattr(service.store, "root", None)
    if root is None:
        raise RuntimeError(
            "this backend has no local directory; pass directory= to write exports somewhere"
        )
    return Path(root)


def _read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _is_newer(previous_stamp: str, current: datetime) -> bool:
    try:
        prev = datetime.strptime(previous_stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return prev > current
