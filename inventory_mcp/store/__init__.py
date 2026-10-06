from .base import (
    EXTRA_COLUMNS_KEY,
    Row,
    RevisionConflict,
    StoreError,
    TableData,
    TableStore,
    compute_revision,
)
from .local_csv import LocalCsvStore

__all__ = [
    "EXTRA_COLUMNS_KEY",
    "Row",
    "RevisionConflict",
    "StoreError",
    "TableData",
    "TableStore",
    "compute_revision",
    "LocalCsvStore",
]


def make_store(config):  # -> TableStore
    """Instantiate the backend named by config.backend."""
    if config.backend == "local":
        from .local_csv import LocalCsvStore

        return LocalCsvStore(config.local_root)
    if config.backend in ("drive_csv", "drive_sheets"):
        from .drive import DriveCsvStore, DriveSheetsStore

        return DriveCsvStore(config) if config.backend == "drive_csv" else DriveSheetsStore(config)
    raise StoreError(f"unknown backend {config.backend!r}")
