"""Create an empty database: every table written with its schema header.

Going through the service rather than writing files by hand means the empty
database has already passed the same validation everything else must survive.

Existing tables are left alone, which is what makes ``init`` safe to re-run and a
usable entry point on a second machine attaching to a database that already exists.
Leaving them alone is not the same as accepting them, though: whatever already sits
in a table's name is inspected first, and anything that is not ours aborts the whole
run before a single file is written. See :func:`init_database`.
"""

from __future__ import annotations

from . import schema
from .config import Config
from .service import Service
from .store.base import ForeignTableError


def init_database(config: Config, store=None) -> tuple[Service, list[str]]:
    """Write whichever tables are missing; return the service and what was created.

    Two passes, deliberately. Everything already present is classified first, and if
    any of it turns out not to be ours the run raises without having written a thing
    -- so refusing costs nothing, and nobody has to work out afterwards which tables
    made it in before the abort.
    """
    svc = Service(config, store=store)
    underlying = svc.store
    ensure = getattr(underlying, "ensure_dirs", None)
    if callable(ensure):
        ensure()

    foreign: list[str] = []
    missing: list[schema.Table] = []
    for spec in schema.TABLES:
        if not underlying.exists(spec.name):
            missing.append(spec)
            continue
        inspect = getattr(underlying, "inspect_existing", None)
        if not callable(inspect):
            continue
        why = inspect(spec.name, spec)
        if why:
            foreign.append(f"{spec.name}: {why}")

    if foreign:
        raise ForeignTableError(
            f"refusing to adopt {len(foreign)} file(s) already in "
            f"{underlying.describe()}:\n  - " + "\n  - ".join(foreign)
            + "\nThose are not this application's table files, and the first write "
              "would replace their contents. Move or rename them, then re-run init. "
              "To bring old data in, import it as data rather than installing it "
              "under a table's name. Nothing was created."
        )

    created: list[str] = []
    for spec in missing:
        underlying.write_table(spec.name, spec.column_names, [], None)
        created.append(spec.name)

    svc.reload()
    return svc, created
