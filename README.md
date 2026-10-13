# inventory-mcp

An on-device MCP server over a lab's biological-sample inventory, stored as
plain CSV so the files stay readable, diffable and auditable without a database
engine.

The server is the layer that makes CSVs safe to treat as a database: identifiers
are minted rather than typed, foreign keys are checked on every write, box
geometry is validated against what actually fits, measurements carry units that
get checked against the assay they belong to, and every write either applies
completely or does not happen at all.

## Status

| | |
| --- | --- |
| Data model, validation, service layer | done |
| Local CSV backend (flock + compare-and-swap) | done |
| MCP tools (31), CLI, exports, guarded SQL | done |
| Google Drive backends (`drive_csv`, `drive_sheets`) | done — verified live: seed, doctor, reads, export, and adversarial cases (browser edits, concurrent writers, deleted files, renamed tabs) |
| Test suite | 110 passing |
| Legacy Airtable importer | deferred by design |

Nothing here is shaped around the old spreadsheet. The import is a separate task
with its own problem — thousands of rows with dangling references — and folding
that in would have bent the schema for the wrong reason.

## Install

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'          # add ,[drive] for the Google Drive backends
```

Python 3.10+. The only hard dependency is the MCP SDK; the Drive backends pull
`google-api-python-client` through the `drive` extra.

## Quickstart

```bash
# create the table files, optionally with development data
inventory-mcp --config ./db.config.json init --root ./db --seed --actor you

# look at it
inventory-mcp --config ./db.config.json ls                  # items
inventory-mcp --config ./db.config.json ls --containers
inventory-mcp --config ./db.config.json occupancy           # fill levels
inventory-mcp --config ./db.config.json doctor              # integrity report

# ask a question in SQL
inventory-mcp --config ./db.config.json query \
  "select project_name, count(*) n from items_wide where class='sample' group by 1"

# publish flat views + a SQLite mirror for anything else to read
inventory-mcp --config ./db.config.json export
```

Then point an MCP client at it:

```json
{
  "mcpServers": {
    "lab-inventory": {
      "command": "/path/to/.venv/bin/inventory-mcp",
      "args": ["--config", "/path/to/db.config.json", "serve"]
    }
  }
}
```

`inventory-mcp tools` prints the tool catalogue as the server actually registers
it. See [docs/tools.md](docs/tools.md) for the conventions behind it.

## What gets stored

Twelve tables under `tables/`, one CSV each, header row included:

```
containers        the storage tree: freezer > shelf > rack > box, plus fridge/bench
items             supertype: every storable thing, one row each
samples           extends items     reagents    extends items
kits              extends items     consumables extends items
patients          participants, kept separate from samples
projects          optionally nested via parent_project_id
patient_projects  who is enrolled in what
item_links        derivation edges: derived_from, extracted_from, aliquot_of, pooled_into
assays            registry of measurement kinds, with quantity and canonical unit
measurements      immutable observations referencing an assay
```

Design decisions worth knowing before changing anything:

- **Supertype + extension tables.** An item is an `items` row plus exactly one
  class row. Adding a new kind of thing means adding a table, not inventing a
  side channel. Reagents, kits and consumables share the same four supply fields
  but stay three tables because a kit's remaining-uses and a reagent's remaining
  volume are different questions.
- **Opaque ids.** `<prefix>_<8 chars>` from an alphabet missing `i l o u`. Ids
  carry no meaning on purpose — encoding the project or the freezer means having
  to change the id when someone moves the tube. Human naming lives in `name`,
  `label` and `gml_id`, all freely mutable.
- **Storage is one foreign key.** `items.location_id` → `containers`, plus an
  optional `position`. There is no path string to keep in sync. Boxes declare
  `rows`/`cols` (and optionally custom `row_labels`/`col_labels` for the box whose
  grid skips `I`), so `Z20` in a 9×9 box is refused rather than written.
- **Measurements never change.** A correction appends a new row and marks the old
  one superseded. Derived quantities like total yield are computed on read, so
  there is nothing to invalidate.
- **Derivation is a junction table.** Aliquots today, pools later — many parents into
  one child needs no migration because the shape was already there.

## Integrity contract

The files are authoritative and are never blindly trusted, because someone will
open them in a spreadsheet.

- **Reads degrade.** A corrupt row doesn't crash the reader. Valid rows come back
  with the problems listed as violations.
- **Writes refuse.** Validation runs against a candidate state first. If anything
  in scope is broken, the write is rejected with every violation listed, and
  nothing is touched — no partial file, no backup generation.
- **`doctor` reports everything**, whether or not it blocks a particular write.

Before any accepted write, the pre-write content of the tables being changed is
copied to `backups/<timestamp>-<operation>/`. That is the undo story: no event
journal, but any write can be reverted by copying files back. `keep_backups`
caps how many generations survive. A Drive backend has no local directory to
copy into, so the write result reports `snapshot: none` and the undo story is
Drive's own version history for those files.

Writes also carry a revision token per table. If the file changed underneath —
the classic case being a spreadsheet save while the server holds the database
open — the write aborts instead of clobbering the human's edit. On the local
backend this is genuine compare-and-swap (advisory lock plus atomic replace);
across a network filesystem it degrades to detect-and-abort, which is still the
guarantee that matters.

## Configuration

JSON at `~/.config/inventory-mcp/config.json`, or `$INVENTORY_MCP_CONFIG`, or
`--config <path>` (the flag must come before the subcommand).

| Key | Default | Meaning |
| --- | --- | --- |
| `backend` | `local` | `local`, `drive_csv`, `drive_sheets` |
| `local_root` | — | directory holding `tables/ exports/ backups/` |
| `drive_folder_id` | — | Drive folder id, or a pasted folder URL — both parse |
| `drive_file_ids` | `{}` | logical table name → file id, when names collide |
| `token_path` | `~/.config/inventory-mcp/oauth_token.json` | OAuth token cache |
| `client_secret_path` | `~/.config/inventory-mcp/client_secret.json` | installed-app credentials |
| `service_account_path` | `~/.config/inventory-mcp/service_account.json` | if this key exists it wins over the OAuth flow |
| `cache_ttl_seconds` | `5.0` | how long a remote read may be served from cache |
| `keep_backups` | `20` | pre-write generations retained |
| `actor` | derive | identity written into `updated_by` |

A bad key in the file is reported and ignored rather than silently dropped.

## Running against Google Drive

The tables live in one access-controlled folder; nothing needs to be mounted.
Pass the folder as a URL and it is reduced to an id at load time:

```bash
inventory-mcp --config ./db.config.json init --backend drive_sheets \
  --drive-folder "https://drive.google.com/drive/folders/1AbC..." --seed
```

`drive_sheets` keeps one spreadsheet per table with the tab named after the
table, which is what people actually open and read. `drive_csv` keeps real CSV
files in the same folder — fewer conversions, but Drive will convert them to
spreadsheets on upload unless the MIME type is declared, so the server declares
it and refuses to read back a file that got converted anyway.

Credentials are for a **Desktop** OAuth client (an institute Google account is
enough; no GCP project per user beyond the one consent screen). Drop the
downloaded JSON at `~/.config/inventory-mcp/client_secret.json`, then:

```bash
inventory-mcp auth            # opens a browser; the token is cached at 0600
inventory-mcp auth --manual   # prints the URL, you paste the redirect back
inventory-mcp auth --check    # verify the stored token without prompting
```

Authorisation is a separate command because `serve` owns stdin — an MCP server
that blocked on a browser consent would look like a hang. A service-account key
at `service_account_path` takes precedence, which suits a shared machine where
you would rather an RA not have to consent at all.

What changes once the files are remote, honestly stated:

- **Concurrency is detect-and-abort.** There is no per-range precondition in the
  Sheets API or Drive's upload endpoint, so the check compares the revision read
  at load time with the revision fetched just before writing. It catches an edit
  that landed before the commit began; two sessions that both loaded the same
  revision will both pass and the later write wins. Drive's version history is
  the backstop for that case, not this code.
- **Sheets coerces.** A spreadsheet cell has types and a CSV field does not, so
  values come back re-parsed: numbers normalise (`1.50` → `1.5`), ISO timestamps
  can arrive as locale date strings, `TRUE` arrives as `true`. The store converts
  what it recognises on the way in, and every write is verified by reading back
  what Drive actually stored and re-rendering it — a mismatch raises rather than
  persisting a silent mutation. The live pass proved this contract earns its
  keep: real divergences were caught and aborted instead of landing quietly.
- **Reads may be up to `cache_ttl_seconds` stale.** Set it to `0` for a
  read-your-writes guarantee at the cost of one API call per table per operation.
- **The Sheets read quota is 60 requests/minute/user** and every operation costs
  read + write + verify. Transient 429s are waited out (≥30 s, so a quota window
  resets), which makes bulk work like `init --seed` slow-but-fatal-free — a real
  thousand-row import will want batched writes before it happens.
- **Shrinking a table** writes the new rows first and clears the leftover rows
  under them afterwards (`values().clear`; Drive-created spreadsheets report no
  grid size, so sized row-deletes are not available), so there is no moment
  where the sheet is empty.
- Hand-editing a table the server is about to write aborts that write. Editing a
  *different* table while a write is in flight goes unnoticed — CAS covers only
  the tables the commit touches.

## Command line

| Command | Purpose |
| --- | --- |
| `init` | create tables (`--root`, `--seed`, `--reset`, `--backend`, `--drive-folder`, `--actor`) |
| `auth` | Drive consent once (`--manual`, `--port`, `--force`, `--check`) |
| `serve` | run the MCP server over stdio |
| `doctor` | integrity report; exit 1 when errors exist (`--json`, `--quiet`) |
| `ls` | list items, or `--containers` |
| `occupancy` | fill levels per container (`[container_id]`, `--kind`) |
| `query` | guarded read-only SQL |
| `export` | flat CSV views + SQLite mirror + manifest |
| `schema` | the field-by-field reference (`--json`) |
| `tools` | the live tool catalogue (`--json`) |
| `show-config` | resolved configuration |

## Building a dashboard on this

Not through MCP — that's a control plane for a model, not a data plane for many
readers. Read the exports or import `inventory_mcp.service`. See
[docs/dashboard.md](docs/dashboard.md) for the view-by-view reference, the
freshness rules, and the stability guarantees a consumer may rely on.

## Tests

```bash
pytest
```

Unit tests for unit arithmetic and id minting; validation tests that corrupt the
CSVs behind the service's back, because that is how corruption really arrives;
integration tests driving full mutation paths against real files; assertions that
a rejected write leaves no trace and that an external edit aborts the next write;
an export round-trip checking manifest freshness and SQLite/CSV row-count
agreement; and a real MCP handshake over stdio against a spawned server process.

The Drive tests run with no network and no credentials, against fakes shaped like
the discovery client (`files()` / `spreadsheets()` / `values()`, each returning a
request object that fails when executed, so retry logic sees failures where the
real client sees them). They cover upload conversion, cell coercion, revision
conflicts, shrink-trim, missing tabs and transient HTTP status handling — and
then drive `init`, `seed`, `doctor` and a commit through the whole stack on both
backends. Four production bugs came out of writing them. What they cannot test
is what Google actually does to a value on the way in, which is why the live pass
is listed as pending rather than done.
