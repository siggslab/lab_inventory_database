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

from . import schema, sheetui
from .config import Config
from .service import Service, ServiceError
from .store.base import ForeignTableError, StoreError, TableData


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

    # Some backends know things about a folder that no per-table check would catch --
    # an older layout sitting in it, for instance, which reads as an empty database to
    # anything looking only for its own tables. Ask before writing, not after.
    preflight = getattr(underlying, "preflight", None)
    if callable(preflight):
        reasons = preflight()
        if reasons:
            raise ForeignTableError(
                f"refusing to build a database in {underlying.describe()}:\n  - "
                + "\n  - ".join(reasons)
            )

    def say(message: str) -> None:
        if progress is not None:
            progress(message)

    foreign: list[str] = []
    # One question to storage -- which tables are there -- rather than one per table.
    # On a remote backend ``exists`` is a metadata read, so asking it twelve times
    # bought twelve round trips to learn what a single listing already said.
    #
    # The interface tabs are a step only where they can exist. A local database has no
    # workbook to lay out, and a run that announces a fifth step it never reaches
    # teaches people to stop trusting the step counter.
    total = 5 if hasattr(underlying, "apply_layout") else 4
    say(f"[1/{total}] seeing what is already in {underlying.describe()}")
    present_names = set(underlying.list_tables())
    present = [spec for spec in schema.TABLES if spec.name in present_names]
    missing = [spec for spec in schema.TABLES if spec.name not in present_names]

    # Whatever is already there gets inspected before anything is written -- and where
    # one request can carry every table, it is worth spending that one rather than
    # twelve to answer the same question. A backend with no bulk answer raises here,
    # and ``inspect_existing`` reads its own table the way it always did.
    preloaded: dict[str, TableData] = {}
    if present:
        say(f"[2/{total}] checking the {len(present)} table(s) already there")
        try:
            preloaded = underlying.read_all()
        except StoreError:
            preloaded = {}
    else:
        say(f"[2/{total}] nothing to check yet")

    inspect = getattr(underlying, "inspect_existing", None)
    if callable(inspect):
        for spec in present:
            why = inspect(spec.name, spec, preloaded.get(spec.name))
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
    if missing:
        # Named up front rather than as each one lands, because they go over as one
        # batch: on Drive twelve separate writes would be thirty-odd requests where
        # the batch is three. The list is what the operator wants to see anyway.
        say(f"[3/{total}] creating {len(missing)} tables: "
            + ", ".join(spec.name for spec in missing))
        underlying.write_batch([(spec.name, spec.column_names, [], None)
                                for spec in missing])
        created = [spec.name for spec in missing]
    else:
        say(f"[3/{total}] nothing to create")

    # The reload is not free -- on Drive it crosses the network, and any request there
    # can sit in a quota backoff for a minute -- and it is the phase with nothing else
    # on screen while it runs, including on an attach where nothing was written.
    say(f"[4/{total}] reading the database back")
    svc.reload()

    # A database that can only be edited from a terminal is half a database, so the
    # workbook arrives with its form already laid out rather than as twelve empty tabs.
    # Everything published here is derived and re-runnable, which is why failing to
    # build it is reported instead of aborting a run whose tables are already correct.
    if hasattr(underlying, "apply_layout"):
        say(f"[{total}/{total}] building the interface tabs")
        try:
            sheetui.publish(svc, progress=say)
        except (StoreError, ServiceError) as exc:
            say(f"interface tabs not built: {exc}\n  the database itself is fine -- run "
                f"'inventory-mcp ui' whenever you want the form")

    # Remote backends copy the whole file before each write, so surplus generations
    # have to be trimmed somewhere -- and setup is where they pile up. Doing it here
    # rather than on every commit saves a listing per commit to discover a number
    # that almost never changed.
    prune = getattr(underlying, "prune_snapshots", None)
    if callable(prune):
        dropped = prune()
        if dropped:
            say(f"trimmed {dropped} old backup cop{'y' if dropped == 1 else 'ies'}")
    return svc, created
