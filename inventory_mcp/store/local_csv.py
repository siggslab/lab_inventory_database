"""Local filesystem CSV backend. The development default, and the strictest
backend available: whole-file replacement plus flock gives genuine
compare-and-swap with no race window.
"""

from __future__ import annotations

import csv
import io
import os
import tempfile
from pathlib import Path
from typing import Optional

from .. import schema
from .base import (
    EXTRA_COLUMNS_KEY,
    Row,
    RevisionConflict,
    StoreError,
    TableData,
    TableStore,
    compute_revision,
)

try:  # POSIX only; absence degrades CAS to best-effort
    import fcntl
except ImportError:  # pragma: no cover - windows
    fcntl = None  # type: ignore[assignment]

#: key used by csv.DictReader for cells beyond the header
RESTKEY = EXTRA_COLUMNS_KEY


def _filename(name: str) -> str:
    return schema.get_table(name).filename


class LocalCsvStore(TableStore):
    kind = "local_csv"

    def __init__(self, root: str | os.PathLike[str]):
        self.root = Path(root).expanduser()
        self.tables_dir = self.root / "tables"

    # -- helpers -------------------------------------------------------------

    def path_for(self, name: str) -> Path:
        return self.tables_dir / _filename(name)

    def _lock_path(self, name: str) -> Path:
        return self.tables_dir / f".{name}.lck"

    def ensure_dirs(self) -> None:
        self.tables_dir.mkdir(parents=True, exist_ok=True)
        (self.root / "exports").mkdir(parents=True, exist_ok=True)
        (self.root / "backups").mkdir(parents=True, exist_ok=True)

    # -- reads ---------------------------------------------------------------

    def list_tables(self) -> list[str]:
        if not self.tables_dir.exists():
            return []
        present = {p.name for p in self.tables_dir.glob("*.csv")}
        return [t.name for t in schema.TABLES if t.filename in present]

    def read_table(self, name: str) -> TableData:
        path = self.path_for(name)
        if not path.exists():
            raise StoreError(f"table {name!r} not found at {path}")
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            text = fh.read()
        reader = csv.DictReader(
            io.StringIO(text), restkey=RESTKEY, restval="", quoting=csv.QUOTE_MINIMAL
        )
        header = tuple(reader.fieldnames or ())
        rows: list[Row] = []
        for raw in reader:
            row: Row = {k: (v if v is not None else "") for k, v in raw.items() if k != RESTKEY}
            extra = raw.get(RESTKEY)
            if extra:
                # Never drop cells silently -- surface them for validation.
                row[RESTKEY] = "|".join("" if c is None else c for c in extra)
            rows.append(row)
        return TableData(
            name=name,
            header=header,
            rows=tuple(rows),
            revision=compute_revision(header, rows),
        )

    def current_revision(self, name: str) -> Optional[str]:
        try:
            return self.read_table(name).revision
        except StoreError:
            return None

    # -- writes --------------------------------------------------------------

    def write_table(
        self,
        name: str,
        header: tuple[str, ...],
        rows: list[Row],
        expected_revision: Optional[str],
    ) -> str:
        self.ensure_dirs()
        path = self.path_for(name)
        new_rev = compute_revision(header, rows)
        if expected_revision is not None and new_rev == expected_revision:
            return expected_revision  # nothing to do

        lock = self._acquire_lock(name)
        try:
            if expected_revision is not None:
                actual = self.current_revision(name)
                if actual != expected_revision:
                    raise RevisionConflict(
                        f"table {name!r} changed underneath us "
                        f"(expected {_short(expected_revision)}, found {_short(actual)}). "
                        "Something edited this file -- likely a spreadsheet save or "
                        "another session. Reload and retry."
                    )
            self._atomic_write(path, header, rows)
        finally:
            self._release_lock(lock)
        return new_rev

    def _atomic_write(self, path: Path, header: tuple[str, ...], rows: list[Row]) -> None:
        buf = io.StringIO()
        writer = csv.DictWriter(
            buf, fieldnames=list(header), extrasaction="ignore", lineterminator="\n"
        )
        writer.writeheader()
        for row in rows:
            writer.writerow({c: _cell(row.get(c, "")) for c in header})
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".csv")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
                fh.write(buf.getvalue())
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    # -- advisory locking ----------------------------------------------------

    def _acquire_lock(self, name: str):
        if fcntl is None:  # pragma: no cover
            return None
        self.ensure_dirs()
        fh = self._lock_path(name).open("w")
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        return fh

    @staticmethod
    def _release_lock(fh) -> None:
        if fh is None:
            return
        if fcntl is not None:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()


def _cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _short(rev: Optional[str]) -> str:
    return (rev or "<absent>")[-8:]
