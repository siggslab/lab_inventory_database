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
    if config.backend == "drive":
        from .drive import DriveSheetsStore

        return DriveSheetsStore(config)
    raise StoreError(f"unknown backend {config.backend!r}")
