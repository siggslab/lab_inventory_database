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

from typing import Callable, Optional

from . import schema
from .config import Config
from .service import Service
from .store.base import ForeignTableError


def init_database(config: Config, store=None,
                  progress: Optional[Callable[[str], None]] = None) -> tuple[Service, list[str]]:
    """Write whichever tables are missing; return the service and what was created.

    Two passes, deliberately. Everything already present is classified first, and if
    any of it turns out not to be ours the run raises without having written a thing
    -- so refusing costs nothing, and nobody has to work out afterwards which tables
    made it in before the abort.

    ``progress`` receives one line per unit of work. It is optional because this is a
    library function whose callers include the MCP server; the CLI passes one for the
    Drive backend, where every table here costs several round trips and the whole run
    can take minutes -- long enough that silence stops reading as "fine" and starts
    reading as "stuck".
    """
    svc = Service(config, store=store)
    underlying = svc.store
    ensure = getattr(underlying, "ensure_dirs", None)
    if callable(ensure):
        ensure()

    def say(message: str) -> None:
        if progress is not None:
            progress(message)

    total = len(schema.TABLES)
    foreign: list[str] = []
    missing: list[schema.Table] = []
    for index, spec in enumerate(schema.TABLES, 1):
        say(f"[{index:>2}/{total}] checking {spec.name}")
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
    for index, spec in enumerate(missing, 1):
        say(f"[{index:>2}/{len(missing)}] creating {spec.name}")
        underlying.write_table(spec.name, spec.column_names, [], None)
        created.append(spec.name)

    # The reload is not free -- it reads every table back through the store, which
    # on Drive is another round trip per table -- and it is the phase with nothing
    # else on screen while it runs, including on an attach where nothing was written.
    say("reading the database back")
    svc.reload()
    return svc, created
