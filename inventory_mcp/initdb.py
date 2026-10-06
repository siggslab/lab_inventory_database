"""Create an empty database: every table written with its schema header.

Going through the service rather than writing files by hand means the empty
database has already passed the same validation everything else must survive.
"""

from __future__ import annotations

from typing import Optional

from . import schema
from .config import Config
from .service import Service


def init_database(config: Config, store=None) -> tuple[Service, list[str]]:
    svc = Service(config, store=store)
    underlying = svc.store
    root = getattr(underlying, "root", None)
    ensure = getattr(underlying, "ensure_dirs", None)
    if callable(ensure):
        ensure()

    created: list[str] = []
    for spec in schema.TABLES:
        if underlying.exists(spec.name):
            continue
        underlying.write_table(spec.name, spec.column_names, [], None)
        created.append(spec.name)

    svc.reload()
    return svc, created
