from __future__ import annotations

import csv
from pathlib import Path

import pytest

from inventory_mcp.config import Config
from inventory_mcp.initdb import init_database
from inventory_mcp.seed import seed
from inventory_mcp.service import Service


@pytest.fixture
def root(tmp_path) -> Path:
    return tmp_path / "db"


@pytest.fixture
def config(root) -> Config:
    return Config(backend="local", local_root=str(root), actor="tester")


@pytest.fixture
def svc(config) -> Service:
    service, _ = init_database(config)
    return service


@pytest.fixture
def seeded(config) -> Service:
    service, _ = init_database(config)
    seed(service)
    return service


def read_rows(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def write_rows(path: Path, rows: list[dict], header: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=header, lineterminator="\n")
        writer.writeheader()
        for r in rows:
            writer.writerow({c: r.get(c, "") for c in header})


def table_path(root: Path, name: str) -> Path:
    from inventory_mcp import schema

    return root / "tables" / schema.get_table(name).filename
