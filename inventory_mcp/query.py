"""Guarded read-only SQL over an in-memory mirror of the loaded tables.

Fixed tools answer the questions we predicted; this answers the ones we did not.
The mirror is built from the same in-memory tables the rest of the service reads,
so it inherits whatever validation accepted -- a malformed date stays a string
rather than poisoning a typed column.

Deliberately narrow: single statement, SELECT/WITH only, no ATTACH, row-capped.
It is a window onto the data, not a database server.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from .db import Database

_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|attach|detach|pragma|vacuum|"
    r"reindex|savepoint|begin|commit|rollback|replace)\b",
    re.IGNORECASE,
)
_ALLOWED_START = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)


class QueryError(RuntimeError):
    pass


def run_sql(db: Database, sql: str, limit: int = 500) -> dict[str, Any]:
    text = sql.strip().rstrip(";").strip()
    if not text:
        raise QueryError("empty query")
    if ";" in text:
        raise QueryError("only one statement per query")
    if not _ALLOWED_START.match(text):
        raise QueryError("only SELECT or WITH queries are permitted")
    if _FORBIDDEN.search(text):
        raise QueryError("query contains a statement that would modify data")

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        _materialise(conn, db)
        allowed = {t.name for t in db.tables.values()}
        try:
            cursor = conn.execute(text)
        except sqlite3.Error as e:
            hint = _unknown_table_hint(str(e), allowed)
            raise QueryError(f"{e}{hint}") from e
        columns = [d[0] for d in (cursor.description or [])]
        rows = [dict(zip(columns, r)) for r in cursor.fetchmany(limit + 1)]
        truncated = len(rows) > limit
        return {
            "columns": columns,
            "rows": rows[:limit],
            "returned": min(len(rows), limit),
            "truncated": truncated,
            "tables_available": sorted(allowed),
        }
    finally:
        conn.close()


def _materialise(conn: sqlite3.Connection, db: Database) -> None:
    for name, table in db.tables.items():
        cols = list(table.spec.column_names)
        cols += [c for c in table.header if c not in cols]
        ddl_cols = ", ".join(f'"{c}"' for c in cols)
        conn.execute(f'CREATE TABLE "{name}" ({ddl_cols})')
        if not table.rows:
            continue
        placeholders = ", ".join("?" * len(cols))
        stmt = f'INSERT INTO "{name}" ({ddl_cols}) VALUES ({placeholders})'
        conn.executemany(
            stmt,
            [[_clean(r.get(c, "")) for c in cols] for r in table.rows],
        )
    # Convenience views so consumers do not have to re-derive the joins.
    conn.executescript(
        """
        CREATE VIEW IF NOT EXISTS items_wide AS
        SELECT i.*,
               s.gml_id, s.sample_type, s.source_material,
               s.collected_on, s.received_on, s.processed_on,
               s.patient_id, p.label AS patient_label,
               s.project_id, pr.name AS project_name,
               c.label AS container_label, c.kind AS container_kind
        FROM items i
        LEFT JOIN samples s ON s.item_id = i.item_id
        LEFT JOIN patients p ON p.patient_id = s.patient_id
        LEFT JOIN projects pr ON pr.project_id = s.project_id
        LEFT JOIN containers c ON c.container_id = i.location_id
        WHERE i.deleted_at = '' OR i.deleted_at IS NULL;

        CREATE VIEW IF NOT EXISTS latest_measurements AS
        SELECT m.*
        FROM measurements m
        WHERE NOT EXISTS (
            SELECT 1 FROM measurements n WHERE n.supersedes = m.measurement_id
        );
        """
    )


def _clean(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, dict):  # EXTRA_COLUMNS_KEY payload
        return str(value)
    return str(value)


def _unknown_table_hint(message: str, allowed: set[str]) -> str:
    m = re.search(r"no such table: (\w+)", message)
    if not m:
        return ""
    return f". Available tables/views: {', '.join(sorted(allowed | {'items_wide', 'latest_measurements'}))}"
