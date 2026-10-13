"""Command line entry point.

``serve`` is what the MCP client launches; the rest exists so that an operator
can set up, inspect and export a database without leaving the terminal -- and so
that the same code paths a dashboard would import are exercised here first.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import SCHEMA_VERSION, __version__
from .auth import AuthError
from .config import Config, ConfigError
from .service import Service, ServiceError
from .store.base import StoreError


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as e:
        print(f"config: {e}", file=sys.stderr)
        return 2
    except ServiceError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except (StoreError, AuthError) as e:
        # Backend and credential failures are operational, not bugs; the message
        # already names the fix (re-authorise, share the folder, enable the API).
        label = "auth" if isinstance(e, AuthError) else "store"
        print(f"{label}: {e}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        return 130


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="inventory-mcp", description=__doc__)
    p.add_argument("--version", action="version", version=f"inventory-mcp {__version__}")
    p.add_argument("--config", help="path to config.json (else $INVENTORY_MCP_CONFIG)")
    sub = p.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="create the table files")
    init.add_argument("--backend", default="local", choices=("local", "drive_csv", "drive_sheets"))
    init.add_argument("--root", help="directory for tables/ exports/ backups/")
    init.add_argument("--drive-folder", help="Drive folder id or URL")
    init.add_argument("--seed", action="store_true", help="fill with development fixture data")
    init.add_argument("--reset", action="store_true",
                      help="discard all existing rows first (for re-seeding a test "
                           "database after an interrupted run; on Drive, version "
                           "history is the only undo)")
    init.add_argument("--actor", default="", help="identity recorded in updated_by")
    init.set_defaults(func=_cmd_init)

    show = sub.add_parser("show-config", help="print resolved configuration")
    show.set_defaults(func=_cmd_show_config)

    serve = sub.add_parser("serve", help="run the MCP server over stdio")
    serve.set_defaults(func=_cmd_serve)

    auth = sub.add_parser(
        "auth", help="authorise Drive access once, interactively (browser consent)"
    )
    auth.add_argument("--manual", action="store_true",
                      help="print the URL and paste the redirect back, for when the "
                           "browser is on another machine")
    auth.add_argument("--port", type=int, default=8080,
                      help="loopback port for the redirect (default 8080)")
    auth.add_argument("--force", action="store_true", help="re-authorise, ignoring any stored token")
    auth.add_argument("--check", action="store_true",
                      help="only verify the stored credentials; never prompt")
    auth.set_defaults(func=_cmd_auth)

    doc = sub.add_parser("doctor", help="report integrity problems")
    doc.add_argument("--json", action="store_true")
    doc.add_argument("--quiet", action="store_true", help="only print the summary line")
    doc.set_defaults(func=_cmd_doctor)

    exp = sub.add_parser("export", help="write denormalised views and a SQLite mirror")
    exp.add_argument("--dir", default="", help="output root (default: the data root)")
    exp.add_argument("--format", choices=("both", "csv", "sqlite"), default="both")
    exp.set_defaults(func=_cmd_export)

    q = sub.add_parser("query", help="read-only SQL against the database")
    q.add_argument("sql")
    q.add_argument("--limit", type=int, default=200)
    q.set_defaults(func=_cmd_query)

    ls = sub.add_parser("ls", help="list items or containers")
    ls.add_argument("--containers", action="store_true")
    ls.add_argument("--class", dest="item_class", default="")
    ls.add_argument("--text", default="")
    ls.set_defaults(func=_cmd_ls)

    occ = sub.add_parser("occupancy", help="capacity report")
    occ.add_argument("container_id", nargs="?", default="")
    occ.add_argument("--kind", default="", help="filter to one container kind")
    occ.set_defaults(func=_cmd_occupancy)

    schema_cmd = sub.add_parser("schema", help="print the schema reference")
    schema_cmd.add_argument("--json", action="store_true")
    schema_cmd.set_defaults(func=_cmd_schema)

    tools_cmd = sub.add_parser(
        "tools", help="print the MCP tool catalogue as registered (no config needed)"
    )
    tools_cmd.add_argument("--json", action="store_true")
    tools_cmd.set_defaults(func=_cmd_tools)

    return p


# ---------------------------------------------------------------------------


def _load_config(args) -> Config:
    return Config.load(args.config)


def _cmd_init(args) -> int:
    from .initdb import init_database

    # Start from the existing config so credential paths survive; override what
    # the user asked for on the command line.
    base = None
    if args.config:
        try:
            base = Config.load(args.config)
        except ConfigError:
            pass

    if args.backend == "local":
        if not args.root:
            print("init: --root is required for a local database", file=sys.stderr)
            return 2
        root = Path(args.root).expanduser()
        cfg = Config(
            backend="local", local_root=str(root), actor=args.actor,
            token_path=getattr(base, "token_path", ""),
            client_secret_path=getattr(base, "client_secret_path", ""),
            service_account_path=getattr(base, "service_account_path", ""),
        )
    else:
        if not args.drive_folder:
            print("init: --drive-folder is required for a Drive backend", file=sys.stderr)
            return 2
        cfg = Config(
            backend=args.backend, drive_folder_id=args.drive_folder, actor=args.actor,
            token_path=getattr(base, "token_path", ""),
            client_secret_path=getattr(base, "client_secret_path", ""),
            service_account_path=getattr(base, "service_account_path", ""),
        )

    svc, created = init_database(cfg)
    wiped = ""
    if args.reset:
        from .seed import reset

        removed = reset(svc)
        wiped = ", ".join(f"{k}={v}" for k, v in removed.items()) or "(nothing to discard)"
    seeded = ""
    if args.seed:
        from .seed import seed

        counts = seed(svc)
        seeded = ", ".join(f"{k}={v}" for k, v in counts.items())
    report = svc.doctor()

    cfg_path = cfg.save(args.config)
    print(f"config written   {cfg_path}")
    print(f"backend          {cfg.backend}")
    print(f"tables created   {', '.join(created) if created else '(all present)'}")
    if wiped:
        print(f"rows discarded   {wiped}")
    if seeded:
        print(f"seeded           {seeded}")
    print(f"integrity        {'clean' if report['ok'] else str(report['error_count']) + ' error(s)'}")
    return 0


def _cmd_show_config(args) -> int:
    cfg = _load_config(args)
    print(json.dumps(cfg.to_dict(), indent=2))
    if getattr(cfg, "unknown_keys", None):
        print(f"note: ignored unknown keys {cfg.unknown_keys}", file=sys.stderr)
    return 0


def _cmd_serve(args) -> int:
    from .tools import mcp, set_service

    set_service(Service.open(args.config))
    mcp.run(transport="stdio")
    return 0


def _cmd_auth(args) -> int:
    """Drive authorisation. Separate from serve on purpose: stdio owns stdin."""
    from .auth import AuthError, authorize, whoami

    cfg = _load_config(args)
    if not cfg.is_drive:
        print(f"note: this config uses backend={cfg.backend!r}; Drive credentials "
              "are only needed for drive_csv / drive_sheets", file=sys.stderr)
    try:
        if args.check:
            info = whoami(cfg)
            print(f"credentials ok   {info.get('account') or info.get('kind')}")
            if info.get("error"):
                print(f"but the API call failed: {info['error']}", file=sys.stderr)
                return 1
            return 0
        path = authorize(cfg, manual=args.manual, port=args.port, force=args.force)
        print(f"credentials saved {path}")
        info = whoami(cfg)
        if info.get("account"):
            print(f"authorised as     {info['account']}")
        if info.get("error"):
            print(f"warning: could not reach the Drive API: {info['error']}", file=sys.stderr)
            return 1
    except AuthError as e:
        print(f"auth: {e}", file=sys.stderr)
        return 2
    return 0


def _cmd_doctor(args) -> int:
    svc = Service.open(args.config)
    report = svc.doctor()
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"backend: {report['backend']}   schema v{SCHEMA_VERSION}")
        for name, count in report["table_counts"].items():
            flag = "  (missing)" if name in report["missing_tables"] else ""
            print(f"  {name:<18}{count:>6}{flag}")
        if report["stray_files"]:
            print(f"unrecognised files: {', '.join(report['stray_files'])}")
        if not args.quiet:
            for v in report["errors"]:
                print(f"ERROR   {v['table']}[{v['row_id']}].{v['field']}: {v['message']}")
            for v in report["warnings"]:
                print(f"warning {v['table']}[{v['row_id']}].{v['field']}: {v['message']}")
        print(f"\n{report['error_count']} error(s), {report['warning_count']} warning(s)"
              f" -> {'OK' if report['ok'] else 'NOT OK'}")
    return 0 if report["ok"] else 1


def _cmd_export(args) -> int:
    svc = Service.open(args.config)
    formats = ("csv", "sqlite") if args.format == "both" else (args.format,)
    result = svc.export(directory=args.dir, formats=formats)
    print(f"wrote {result['directory']}")
    for name, info in result["manifest"]["files"].items():
        print(f"  {name:<24} {info['rows']} row(s)")
    integrity = result["manifest"]["integrity"]
    if not integrity["clean"]:
        print(f"note: exported despite {integrity['open_errors']} open integrity error(s)")
    return 0


def _cmd_query(args) -> int:
    from .query import QueryError
    from .query import run_sql

    svc = Service.open(args.config)
    try:
        result = run_sql(svc.db, args.sql, limit=args.limit)
    except QueryError as e:
        print(f"query: {e}", file=sys.stderr)
        return 1
    cols = result["columns"]
    print("\t".join(cols))
    for row in result["rows"]:
        print("\t".join("" if row[c] is None else str(row[c]) for c in cols))
    suffix = " (truncated)" if result["truncated"] else ""
    print(f"-- {result['returned']} row(s){suffix}")
    return 0


def _cmd_ls(args) -> int:
    svc = Service.open(args.config)
    if args.containers:
        for c in svc.list_containers():
            fill = (
                f"{c['occupied']:>4}/{c['capacity']}"
                if c["capacity"] is not None
                else f"{c['occupied']:>4} item(s)"
            )
            print(f"{c['container_id']:<14} {c['kind']:<12} {c['label']:<28} "
                  f"{fill:<14} {c['path']}")
        return 0
    for it in svc._matching_items(item_class=args.item_class, text=args.text):
        loc = it["location"]["path"] or "unlocated"
        pos = it["location"]["position"]
        print(f"{it['item_id']:<14} {it['class']:<10} {it['status']:<18} "
              f"{it['name']:<36} {loc}{(':' + pos) if pos else ''}")
    return 0


def _cmd_occupancy(args) -> int:
    svc = Service.open(args.config)
    rows = svc._occupancy_rows(container_id=args.container_id, kind=args.kind)
    for r in rows:
        if r["has_capacity"]:
            pct = f"{r['occupancy_pct']}%"
            detail = f"{r['occupied']:>4}/{r['capacity']:<5} free {r['free']:<5} {pct:<7}"
        else:
            # No capacity recorded: report what it holds rather than a fake percentage.
            detail = (f"{r['items_held']:>4} item(s), {r['child_containers']} "
                      f"container(s) inside")
        print(f"{r['container_id']:<14} {r['kind']:<12} {r['label']:<28} {detail}")
    return 0


def _cmd_schema(args) -> int:
    from . import schema as S

    if args.json:
        out = {}
        for t in S.TABLES:
            out[t.name] = {
                "file": t.filename,
                "primary_key": t.primary_key,
                "supertype": t.supertype,
                "doc": t.doc,
                "fields": [
                    {
                        "name": f.name,
                        "type": f.type,
                        "required": f.required,
                        "fk": f.fk,
                        "enum": list(f.enum),
                        "doc": f.doc,
                    }
                    for f in t.fields
                ],
            }
        print(json.dumps({"schema_version": SCHEMA_VERSION, "tables": out}, indent=2))
        return 0
    print(f"schema v{SCHEMA_VERSION}\n")
    for t in S.TABLES:
        sup = f" (extends {t.supertype})" if t.supertype else ""
        print(f"{t.name}{sup}  [{t.filename}]")
        print(f"  {t.doc}")
        for f in t.fields:
            bits = []
            if f.required:
                bits.append("required")
            if f.fk:
                bits.append(f"-> {f.fk}")
            if f.enum:
                bits.append("|".join(f.enum))
            tail = f"  [{'; '.join(bits)}]" if bits else ""
            print(f"    {f.name:<20} {f.type:<9}{tail}")
            if f.doc:
                print(f"    {'':<20} {f.doc}")
        print()
    return 0


def _cmd_tools(args) -> int:
    """Print the tool catalogue from the live registry.

    Generated rather than hand-written so the documentation cannot drift away
    from what the server actually advertises.
    """
    from .tools import mcp

    tools = mcp._tool_manager.list_tools()
    if args.json:
        print(json.dumps(
            [
                {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                }
                for t in tools
            ],
            indent=2,
        ))
        return 0
    print(f"{len(tools)} tools\n")
    for t in sorted(tools, key=lambda t: t.name):
        params = t.parameters.get("properties", {})
        required = set(t.parameters.get("required", []))
        signature = ", ".join(
            f"{k}:{_short_type(v)}{'' if k in required else '='}" for k, v in params.items()
        )
        print(f"{t.name}({signature})")
        for line in (t.description or "").strip().splitlines():
            print(f"    {line}")
        print()
    return 0


def _short_type(schema: dict) -> str:
    names = {"string": "str", "integer": "int", "boolean": "bool", "number": "float",
             "array": "list"}
    if "type" in schema:
        return names.get(schema["type"], schema["type"])
    # Optionals arrive as anyOf; show what is actually acceptable, not "any".
    options = [o.get("type", "") for o in schema.get("anyOf", []) if o.get("type") != "null"]
    if options:
        return "|".join(names.get(o, o) for o in options)
    return "any"


if __name__ == "__main__":
    raise SystemExit(main())