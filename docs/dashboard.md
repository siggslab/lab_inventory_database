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
| `occupancy.csv` | One row per container: capacity, occupied, free, percent, how many wells are actually recorded |
| `measurements_long.csv` | Every measurement including superseded ones, with the value converted to the assay's canonical unit |
| `lineage.csv` | Flat parent→child edges with both names resolved |
| `patient_projects.csv` | Enrolments with participant and project names resolved |
| `inventory.sqlite` | The twelve authoritative tables plus the denormalised views above, ready for anything that speaks SQL |
| `manifest.json` | When this was generated, which revision of each source table it reflects, and the integrity state at that moment |

Point Metabase / Grafana / Superset / Datasette / a static site generator at
`inventory.sqlite` and you are done. Row counts in the mirror match the CSV
tables exactly — there is a test asserting it.

### Manifest, and why freshness is explicit

```json
{
  "schema_version": 1,
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
svc.search_items(item_class="sample", status="present", container_subtree=True)
svc.occupancy(container_id="ctr_8kv1f3kc")
svc.run_query("select name, container_path from items_wide where class='sample'")
```

This is the same code the MCP tools call, so you get identical filtering,
identical unit conversion, identical derived fields — and the validators, which
matters if your dashboard also writes. Nothing here knows MCP exists.

Exceptions worth catching: `ValidationError` (carries `.violations`),
`NotFound`, `Conflict`, `ServiceError`.

## Route C — SQL

`run_query` mirrors the tables into in-memory SQLite and runs one statement:

- `SELECT` and `WITH` only. Insert/update/delete/drop/alter/create/attach and
  pragmas are refused before parsing.
- Single statement — `select 1; drop table items` is rejected as two statements.
- Rows capped by `limit` (default 500), with `truncated` reported honestly.
- Views available: `items_wide`, `latest_measurements`, plus the twelve tables.

Against the exported file there is no guard, because SQLite opened read-only
needs none:

```bash
sqlite3 -header -column .devdb/exports/inventory.sqlite \
  "select project_name, count(*) from items_wide where class='sample' group by 1"
```

Objects in the mirror: the twelve tables, then `items_wide`, `occupancy`,
`measurements_long`, `lineage` as materialised tables; `latest_measurements` and
`patient_projects_named` as views. `patient_projects` stays the raw junction
table — the denormalised version is `patient_projects_named`, because shadowing
an authoritative table with a derived copy is how dashboards end up lying.

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
- **Superseded measurements are still there.** `get_measurements` hides them by
  default; `measurements_long.csv` keeps them with `superseded=yes|no` and
  `supersedes` pointing at the row it replaced. Sum without filtering and you'll
  double-count.
- **Positions are sparse.** Most rows are recorded at box level. `occupied` counts
  tubes; `with_known_well` counts tubes anyone can point at. An empty position is
  not a free well, and no query can tell you otherwise.
- **Units are canonical on read.** Stored values keep whatever unit was entered;
  `canonical_value` / `canonical_unit` carry the converted form. Chart the
  canonical pair, or your axis means three things at once.
- **Dates normalise to ISO.** `YYYY-MM-DD` and `YYYY-MM-DDTHH:MM`, UTC minute
  precision. An ambiguous day/month like `03/04/1980` is flagged rather than
  guessed, so expect to see it in `doctor` output until someone fixes the source.
- **A consumed item holds no slot.** Setting status to `consumed` clears location
  deliberately; the tube is in someone's hand or the bin, not in the box.
