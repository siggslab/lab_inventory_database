# Status — 2026-10-13

Working notes for whoever picks this up next. The user-facing description lives in
[README.md](README.md); this file records state, decisions and open questions.

## Where we are

The database design, validation layer, service layer, MCP tools, CLI, exports and
local CSV backend are done and verified. **The Google Drive backends have now been
verified live** against the scratch folder below: full seed, `doctor`, reads
(`ls`, occupancy grids, guarded SQL, export), and an adversarial pass (browser
edits, concurrent writers, vanished files, hand-renamed tabs, `drive_csv`
coexistence). Contact with the real API found five bugs the offline fakes could
not (list below — all fixed, each with a regression test; fakes corrected to
match what Google actually does).

Nothing here is shaped around the legacy Airtable export. Importing it is a separate
task with its own problem (thousands of rows, dangling references), and folding it in
would have bent the schema for the wrong reason.

## Verified right now

```
.venv/bin/python -m pytest -q          # 165 passed
inventory-mcp doctor                   # 0 error(s), 0 warning(s) -> OK   (on .devdb)
inventory-mcp tools                    # 31 tools, real parameter schemas
inventory-mcp init --seed              # items=13 samples=10 measurements=18, integrity clean
inventory-mcp init --reset --seed      # truncate all tables first, then seed (recovery path)
inventory-mcp export                   # items_wide, occupancy, measurements_long,
                                       # item_links, patient_projects, inventory.sqlite
inventory-mcp occupancy                # every tier in its own unit: freezer 1/4 shelf
                                       # positions, Rack 3 2/6 box positions, box 2/81 wells
```

Live against Drive (config at `.devdb/drive.config.json`, backend `drive_sheets`):
seed produced exactly the local tallies; `doctor` reported 0 errors / 0 warnings;
`ls`, `occupancy`, `query` and `export` all matched the seeded fixture; the
adversarial ledger ended 17/18 with the single miss traced to harness litter from
an earlier crashed attempt, not the product.

Use the project venv (`.venv/bin/...`), never the global interpreter.

## Authentication: settled and working

Desktop OAuth (option 1 of the old decision table): Cloud project with Drive +
Sheets APIs enabled, **Internal** consent screen, Desktop-app client. Secret and
consented token live in `./.creds/` (gitignored); `.devdb.config.json` points at
them via `client_secret_path` / `token_path`. `inventory-mcp auth --check` verifies
without prompting. Service-account support exists and takes precedence when
configured; the Drive-for-Desktop-sync route remains available with the `local`
backend. One caveat to remember: **the OAuth token is the human's whole Drive** —
it sees the lab's shared drives too. Testing stayed confined to the scratch
folder; production use wants a dedicated account or at least group-based sharing
with link-sharing off.

## What the live pass found (all fixed, all regression-tested)

1. **Shrink-trim via `deleteDimension` was unknowable.** Spreadsheets created
   through the Drive API report no `gridProperties.rowCount` — even after being
   written through Sheets — so row-count-sized deletes silently no-op'd and
   stale rows survived; verify-after-write caught the divergence. `_push` now
   clears everything under the new content with `values().clear("'tab'!A{n}:ZZZ")`,
   which needs no grid knowledge.
2. **Sheets read quota is 60 requests/min/user**, and every operation costs
   read+write+verify. `_retry` now takes 8 attempts and sleeps ≥30 s on a 429 so
   a quota window reset is waited out rather than fatal. Seeding the folder took
   ~10 minutes because of this; bulk operations want batching (deferred, below).
3. **A hand-renamed tab read as a legitimately empty table** — meaning the next
   commit would "succeed" by recreating the tab and orphaning the real content.
   Missing tab now raises unless the spreadsheet only holds Drive's default
   `Sheet1` (the genuinely-fresh state). Reads refuse, CAS refuses, reload warns.
4. **Multi-table commits were not atomic.** A CAS failure on the *second* touched
   table left the first one's write landed — an orphan row that then blocked every
   later commit via validation. `_apply` now rolls back tables already pushed,
   CASing against its own just-written revision so a concurrent human edit is
   never bulldozed; anything it cannot revert is named in the error and left for
   doctor.
5. **`drive_csv` downloads are plain bytes**, not streams — the fake returned
   `BytesIO` and hid it until live contact. Production decodes bytes; the fake
   now returns bytes.

The offline fakes gained real-Sheets splice semantics for `values().update`
(rows below a short write survive until cleared), a `values().clear`
implementation, honest `get_media`, and no `gridProperties` reliance. When you
change store code, make the fake lie the way Google lies.

## Test folder

`https://drive.google.com/drive/folders/16QfHIYtRsQBmtvs77Ke-zL5uvxBNUEm6`
(id `16QfHIYtRsQBmtvs77Ke-zL5uvxBNUEm6`) — personal-drive scratch folder this tool
may create and delete inside. Current contents: the 12 table spreadsheets, seeded,
plus legitimate history (a few tombstoned test rows; doctor reports clean). The
adversarial harnesses that ran against it are kept at
`.devdb/adversarial_drive.py` and `.devdb/adversarial_round2.py` (gitignored;
rerunnable; they pre-clean their own litter).

## What exists

| Path | Role |
| --- | --- |
| `inventory_mcp/schema.py` | the 12 table definitions, field by field |
| `inventory_mcp/validation.py` | reads degrade / writes refuse; `doctor` reports everything |
| `inventory_mcp/service.py` | every mutation, all through one `_apply` commit path (now with rollback) |
| `inventory_mcp/store/` | `base` (revisions, `TableStore`), `local`, `drive` |
| `inventory_mcp/auth.py` | installed-app OAuth flow, manual relay, service-account precedence |
| `inventory_mcp/tools.py` | the 32 MCP tools |
| `inventory_mcp/query.py` | guarded read-only SQL over an in-memory SQLite mirror |
| `inventory_mcp/exports.py` | denormalised views + SQLite mirror + manifest |
| `inventory_mcp/seed.py` | development fixture, built by calling the service; `reset()` truncates |
| `docs/tools.md` | tool conventions |
| `docs/dashboard.md` | how a third party reads this (not via MCP as an API) |

## Deliberate limits, restated so they don't get mistaken for bugs

- On Drive, concurrency is **detect-and-abort**, not true CAS: no per-range
  precondition exists in the API. Two sessions that loaded the same revision both
  pass and the later write wins. Drive version history is the backstop.
- CAS covers only the tables a commit touches. A hand edit to `patients` while we
  write `items` is invisible until the next load.
- No local `backups/` on a Drive backend — the write result reports
  `snapshot: none`; aborted commits roll back what they can, but Drive version
  history is the real undo.
- Reads may be `cache_ttl_seconds` stale; `0` buys read-your-writes at one API
  call per table per operation.
- Sheets re-parses cell values on input. Unrecognised renderings raise on the
  post-write verification read rather than persisting a silent mutation — this
  contract earned its keep twice during the live pass.

## Open threads

- Commit identity: git resolves to a placeholder (`yves <e@mail.com>`); amend or set
  `user.email` before pushing real history.
- `tmp.py` / `tmp2.py` (scratch credential inspectors) are untracked but not
  gitignored — delete them or add to `.gitignore` before the next `git add -A`.
- Whether `.devdb.config.json` should flip its default backend to `drive_sheets`
  for the lab's real folder, or whether each deployment gets its own config.
- Bulk-operation quota strategy: per-row read+write+verify will not survive a
  thousand-row import at 60 reads/min. Batching (multi-table value updates,
  skip-verify behind a flag) is the obvious shape; needed before real data lands.
- `assays.canonical_unit` is `required=True` unconditionally (`schema.py`), so a
  **textual** assay cannot be registered without inventing a placeholder unit —
  "gel smear quality" has none. Found while pinning the text-assay path in
  `TestGetItem`; the test passes `"score"` and says so. Fix is to make the
  requirement conditional on `result_type == "numeric"` (column-level `required`
  can express that, so the rule moves into `_validate_assays`). No textual assay
  exists in the seed today, which is why this stayed hidden.
- Schema v2 renamed `item_parents` → `item_links`, `link_parent` → `link_items`, the
  `lineage` export view → `item_links`, and the doctor codes `self_lineage` /
  `lineage_cycle` → `self_link` / `derivation_cycle`. Local `.devdb` was migrated by
  renaming its CSV (4 edges intact). **The Drive folder still holds `item_parents`**:
  rename the file — or the sheet tab, under `drive_sheets` — before pointing a
  Drive-backed config at this build, or the edges read as empty and `init` quietly
  creates a second, empty table beside the old one.
- Two read tools left the surface during the same pass. **`rollup_by_container` is
  deleted**: grouping items by their direct container can only ever produce leaf boxes,
  so it could not answer anything about a freezer, and `get_occupancy`, `list_containers`
  and `run_query` cover what it said. `_rollup` now serves the project axis alone.
  **`rollup_by_patient` became `search_patients`**, because the census shape had two
  faults: `(none)` mixed "not a sample" with "sample with no participant", and cost grew
  with the cohort. It now requires a `query` matched against participant ids and labels,
  enumerates from the participants table (so an enrolled person with no tubes returns
  `total: 0` rather than vanishing), caps rows at `limit` (default 20) reporting
  `matched`/`truncated`, and carries `unassigned` — samples held with no participant —
  beside the rows instead of as a fake row that would compete with the limit. No blank
  query is accepted; `"pat_"` matches every id, which is the only way to ask for the
  whole cohort. If that census should be first-class, give `query` a wildcard value.
- Schema v3 made capacity a property of every container, not just gridded ones:
  `containers.capacity_override` → `capacity`, meaning **slots** — one tube in a well
  or one box in a rack, never both summed together. `occupied` now counts nested
  containers (`occupancy.csv` gained `items_held` and `has_capacity`, lost `grid`,
  `with_known_well` and `wells_resolved` — positions are convenience data, so no
  occupancy figure is computed from them any more),
  `get_occupancy` reports the tier the rows live in so a freezer answers "1 of 4 shelf
  positions used" and Rack 3 answers "2 of 6 box positions", and doctor added
  `grid_on_furniture` (a well grid on a non-box kind) plus `bad_capacity` and
  `capacity_disagrees_with_grid`. The seed sets slot counts on the freezer, shelf,
  racks and LN2 shipper; fridge and bench stay unmeasured so the null path survives.
  **A pre-v3 `containers` sheet reads with two warnings** (`column_missing` for
  `capacity`, `unknown_column` for `capacity_override`) and every furniture tier
  reports `capacity: null` until the header is renamed there — verified against a
  copy of `.devdb` with the old header, so it degrades rather than lying.

## Deferred by agreement

Legacy Airtable importer, bulk import, sequencing runs/datasets/pooling entities, an
HTTP facade for dashboards.
