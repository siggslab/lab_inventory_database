# Building a dashboard on this database

Short answer: **don't call the MCP.** Read the exported projections, or import
`inventory_mcp.service` in-process if you need the validators. Both paths are
described below, along with what is guaranteed to stay stable.

## Why not the MCP as an API

MCP is a control plane shaped for a model: stdio transport, one server process
per client, content blocks instead of values, tool surface sized to fit in a
context window. A dashboard needs a data plane — many concurrent readers, plain
values, pagination, cheap polling. Putting a web frontend behind MCP means
keeping a server process alive per viewer and parsing prose out of `content[0].text`
to draw a bar chart. It works, and it is the wrong shape.

The MCP exists so a model can *change* the inventory safely. A dashboard mostly
wants to *look* at it, and looking has a better route.

## Route A — read the exports (recommended)

```bash
inventory-mcp --config /path/to/config.json export
```

Writes to `<root>/exports/`:

| File | What it is |
| --- | --- |
| `items_wide.csv` | One row per item, supertype joined to its extension, container resolved to a path, latest Qubit/Nanodrop/volume flattened into columns |
| `occupancy.csv` | One row per container: capacity in slots, slots taken (`items_held` + `child_containers`), free, percent |
| `measurements_long.csv` | Every measurement including superseded ones, with the value converted to the assay's canonical unit |
| `item_links.csv` | Flat parent→child edges with both names resolved |
| `patient_projects.csv` | Enrolments with participant and project names resolved |
| `inventory.sqlite` | The twelve authoritative tables plus the denormalised views above, ready for anything that speaks SQL |
| `manifest.json` | When this was generated, which revision of each source table it reflects, and the integrity state at that moment |

Point Metabase / Grafana / Superset / Datasette / a static site generator at
`inventory.sqlite` and you are done. Row counts in the mirror match the CSV
tables exactly — there is a test asserting it.

### Manifest, and why freshness is explicit

```json
{
  "schema_version": 3,
  "generated_at": "2026-10-06T09:14:02Z",
  "backend": "local_csv:/srv/lab/db",
  "source_revisions": {"items": "9f2c…", "samples": "b71a…"},
  "files": {"items_wide": {"file": "items_wide.csv", "rows": 13}},
  "integrity": {"open_errors": 0, "open_warnings": 0, "clean": true}
}
```

Exports are derived artifacts, so they sit outside the compare-and-swap
discipline: last writer wins, no precondition. That makes staleness unbounded
across a network sync unless it is stated, hence `generated_at` and
`source_revisions`. A poller should compare the revisions it last saw against
the live ones (`store.current_revision(name)`) and re-poll or warn rather than
silently showing yesterday's freezer.

Re-export refuses to overwrite a generation stamped in the future. If that fires,
either the clock moved or someone restored old exports deliberately.

### Columns worth knowing in `items_wide`

`item_id`, `class`, `name`, `status`, `deleted_at`, `gml_id`, `sample_type`,
`source_material`, `patient_label`, `project_name`, `container_label`,
`container_kind`, `container_path`, `position`, `handler`, `collected_on`,
`received_on`, `processed_on`, `concentration_ng_ul`, `volume_ul`,
`computed_yield_ng`, `notes`, `updated_by`, `updated_at`.

`computed_yield_ng` is `concentration × volume`, computed at export time. It is
**not** stored anywhere authoritative — fix the volume reading and the next export
has the right yield, with nothing left to reconcile.

## Route B — import the service

```python
from inventory_mcp.service import Service

svc = Service.open("/path/to/config.json")       # or a Config object
page = svc.search_items(item_class="sample", status="present")   # {"rows": [...], ...}
svc.search_containers(query="freezer")                         # find a place
svc.get_contents(container_id="ctr_8kv1f3kc")                   # one layer inside it
svc.get_occupancy(container_id="ctr_8kv1f3kc", kind="sample")  # room for a tube here
svc.get_occupancy(container_id="ctr_fzqav521", kind="box")     # room for a box here
svc.run_query("select name, container_path from items_wide where class='sample'")
```

This is the same code the MCP tools call, so you get identical filtering,
identical unit conversion, identical derived fields — and the validators, which
matters if your dashboard also writes. Nothing here knows MCP exists.

### Paging

A capped read answers with an envelope, never a bare list: `{matched, offset, truncated,
rows}`, with `run_query` adding `columns` and `returned`. `matched` is what the filters
found in total regardless of page; `truncated` means *another page exists*, not that this
one came back full — which is what lets a short final page be recognised as the end
instead of a truncated fetch. Walk it by holding the filters still and stepping `offset`
by the page size:

```python
rows, offset = [], 0
while True:
    page = svc.search_items(item_class="sample", limit=100, offset=offset)
    rows += page["rows"]
    if not page["truncated"]:
        break
    offset += 100
```

`limit=0` on `search_items` means every match, so an uncalled `offset` costs nothing; the
id-or-label searches (`search_containers`, `search_patients`, `search_projects`) default
to a page of twenty. An `offset` past the end answers empty rather than raising, so a
poller whose rows were deleted between two reads draws nothing instead of crashing; a
negative window is refused by name. What paging does not give you is a snapshot: each page
is read fresh, so a walk that spans someone else's write may see a row move between pages.
Re-read whole tables you intend to sum.

`search_items` rows are deliberately lean: identity, status, location and the
class ids/labels needed to filter further. Call `get_item(id)` for the rest
(notes, handler, dates, related-object ids, latest readings as
`{assay, measured_on, value, unit, canonical}`); measurement history and derivation links
stay in `get_measurements` and `get_linked_items`, or read `items_wide` for
everything at once.

Exceptions worth catching: `ValidationError` (carries `.violations`),
`NotFound`, `Conflict`, `ServiceError`.

## Route C — SQL

SQL is not on the MCP surface: a query string is arbitrary code over the whole
database, with no per-table boundary and no bound on what one answer costs. This
route is for humans at a terminal (`inventory-mcp query`) and for code someone
owns — `Service.run_query` and `query.run_sql` are unchanged by that decision.

`run_query` mirrors the tables into in-memory SQLite and runs one statement:

- `SELECT` and `WITH` only. Insert/update/delete/drop/alter/create/attach and
  pragmas are refused before parsing. Words inside a string literal or a comment
  are data, so `where notes like '%update%'` and `replace(name, 'a', 'A')` are
  fine — the scan reads the statement's shape, not its contents.
- Single statement — `select 1; drop table items` is rejected as two statements.
- Rows capped by `limit` (default 500), with `truncated` reported honestly.
- **Columns hold text.** The mirror declares no column types, so a bare comparison
  against a number does not do arithmetic: on the development fixture
  `where value_num > 50` matches all 18 readings while
  `where cast(value_num as real) > 50` matches 10. Aggregates coerce on their own
  (`sum(value_num)` is right); comparisons are the trap.
- Views available: `items_wide`, `latest_measurements`, plus the twelve tables.

Against the exported file there is no guard, because SQLite opened read-only
needs none:

```bash
sqlite3 -header -column .devdb/exports/inventory.sqlite \
  "select project_name, count(*) from items_wide where class='sample' group by 1"
```

Objects in the mirror: the twelve tables, then `items_wide`, `occupancy`,
`measurements_long` as materialised tables; `latest_measurements`,
`patient_projects_named` and `item_links_named` as views. The two junctions
`patient_projects` and `item_links` stay raw — their labelled forms are the
`_named` views, because shadowing an authoritative table with a derived copy is
how dashboards end up lying.

## Stability

`SCHEMA_VERSION` in `inventory_mcp/__init__.py` is the contract.

- **Within a version**, changes are additive: new columns may appear at the end
  of a CSV, new tables and new views may appear. Columns are not renamed,
  removed, or re-typed. Select by name, never by index, and ignore what you don't
  recognise.
- **A version bump means something moved**: a rename, a semantic change, a
  removal. It appears in `manifest.json` before you notice it in a chart.
- **Unknown columns are preserved.** If someone adds a column by hand, the
  validator warns and the value round-trips through the next write intact. Don't
  rely on that being cleaned up automatically; rely on it not being lost.
- **Files remain the source of truth.** Exports are rebuildable; delete the
  directory and regenerate. Anything you build on top should be able to say the
  same.

## Things that will otherwise bite you

- **Empty string means absent.** There are no NULLs in CSV. `""` is how "not
  recorded" arrives, in both directions.
- **Deleted rows are still there.** `items.deleted_at` non-empty means tombstoned.
  `search_items` hides them; `items_wide` includes them with the timestamp so
  history doesn't silently shrink. Filter explicitly.
- **Enrolment does not inherit.** `patient_projects` names, per participant, the exact
  projects their material may sit in. So a subproject can honestly report zero
  participants while holding tubes, and joining samples to enrolments through the project
  tree finds pairs the database never asserted. Roll the tree up only for a chart of
  *reach* — it is not a record of consent. A database written before this was enforced
  may hold tubes whose pairing has no row: `doctor` names them, and
  `inventory-mcp enrol --from-ancestors` writes the missing rows.
- **Superseded measurements are still there.** `get_measurements` answers with one row
  per item and assay — the newest reading the database currently believes. Recording a
  reading retires the one it overtook, so any second measurement of the same tube and
  assay moves the earlier value out of the answer on its own; `include_historical` brings
  the series back, and `include_superseded` adds just the retired rows to the current
  ones. `measurements_long.csv` keeps everything, with `superseded=yes|no` and
  `supersedes` pointing at the row it replaced. Sum without filtering and you'll
  double-count.
- **Positions are convenience, not accounting.** Most rows are recorded at box level.
  `position` in `items_wide` is whatever someone typed, and occupancy ignores it: an
  unlabelled tube fills a slot exactly like one filed under A1. An empty position is
  not a free well, and no query can tell you otherwise.
- **Capacity is slots, and a nested container fills one.** `occupied` = `items_held`
  + `child_containers`, so a rack holding six of its six box positions reads full
  while holding zero tubes. Don't add `capacity` down a subtree: a freezer's four
  shelf positions and its boxes' 181 wells are different units, and only containers
  holding the same kind of thing are ever summed by the tools.
- **Units are canonical on read.** Stored values keep whatever unit was entered;
  `canonical_value` / `canonical_unit` carry the converted form. Chart the
  canonical pair, or your axis means three things at once. The MCP's reading rows
  carry both spellings under different names — `value`/`unit` as observed and
  `canonical` in the assay's unit — so a model comparing two tubes never does the
  arithmetic in its head, while anything quoting a reading back quotes the number
  someone actually wrote down.
- **Dates normalise to ISO.** `YYYY-MM-DD` and `YYYY-MM-DDTHH:MMZ`, UTC minute precision
  with ISO's zone marker written into the column, so a stored timestamp says which clock
  it belongs to instead of leaving it to be inferred. Input may carry the marker or an
  offset (`...+10:00`) or neither — an unmarked value is read as UTC and left for `doctor`
  to flag as `datetime_without_zone`, which is how rows written before the marker landed
  stay visible without blocking anything. An ambiguous day/month like `03/04/1980` is
  flagged rather than guessed, so expect to see it in `doctor` output until someone fixes
  the source.
- **A consumed item holds no slot.** Setting status to `consumed` clears location
  deliberately; the tube is in someone's hand or the bin, not in the box. The well label
  goes with it and nothing remembers it, so marking the item present again has to name a
  container — which is also how a mis-recorded consumption gets undone in a single write.
