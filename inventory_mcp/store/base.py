"""Storage abstraction.

Everything above this layer speaks in rows (list of dicts) and never learns
whether those came from a local file, a Drive CSV, or a tab in a Google Sheet.
Keeping the seam here -- rather than at "read bytes" -- is what makes csv vs
sheets a config flag instead of a code path.

A *revision* is an opaque token identifying the exact content last read. It is
the precondition for compare-and-swap writes. Note the honest limit: for Sheets
there is no per-tab precondition available, so CAS means "detect a concurrent
change and abort", not "concurrent writes are safe". One-writer-at-a-time is
assumed.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Iterable, Optional

Row = dict[str, str]

#: key holding cells that arrived without a header slot, so that a spreadsheet
#: with extra columns surfaces as a violation instead of losing data
EXTRA_COLUMNS_KEY = "__extra_columns__"


class StoreError(RuntimeError):
    pass


class RevisionConflict(StoreError):
    """Raised when a write's expected revision no longer matches storage."""


@dataclass(frozen=True)
class TableData:
    name: str
    header: tuple[str, ...]
    rows: tuple[Row, ...]
    revision: str

    def as_dicts(self) -> list[Row]:
        return [dict(r) for r in self.rows]


def compute_revision(header: Iterable[str], rows: Iterable[Row]) -> str:
    """Content hash used as a revision token by content-addressed backends."""
    h = hashlib.sha256()
    h.update("\x1f".join(header).encode("utf-8"))
    for row in rows:
        h.update(b"\x1e")
        h.update("\x1f".join(row.get(c, "") for c in header).encode("utf-8"))
    return "sha256:" + h.hexdigest()[:32]


class TableStore(ABC):
    """Read/write access to the logical tables backing the database."""

    #: short identifier used in diagnostics
    kind: str = "abstract"

    @abstractmethod
    def list_tables(self) -> list[str]:
        """Logical table names present in storage."""

    @abstractmethod
    def read_table(self, name: str) -> TableData:
        """Return the table's raw contents. Missing table raises StoreError."""

    @abstractmethod
    def write_table(
        self,
        name: str,
        header: tuple[str, ...],
        rows: list[Row],
        expected_revision: Optional[str],
    ) -> str:
        """Persist ``rows``, aborting if the stored revision differs from
        ``expected_revision``. Returns the new revision.

        ``expected_revision=None`` means create-or-overwrite with no precondition
        (used only for derived artifacts such as exports).
        """

    @abstractmethod
    def current_revision(self, name: str) -> Optional[str]:
        """Current revision without materialising rows; None if absent."""

    def exists(self, name: str) -> bool:
        try:
            return self.current_revision(name) is not None
        except StoreError:
            return False

    def describe(self) -> str:
        return self.kind
