# Status — 2026-10-15

Working notes for whoever picks this up next. The user-facing description lives in
[README.md](README.md); this file records state, decisions and open questions.

## A recording retires the reading it overtook — 2026-10-15

Recording a second measurement for an item and assay now writes `supersedes` on the new
row itself, naming whichever reading stood. Before this, retirement happened only when
someone called `Service.correct_measurement` from Python — which is not a tool, so a model
could not express "this value was wrong" at all, and the sheet held two values for one
question with nothing but row order to tell them apart.

**Retirement stays a note on the new row.** Nothing existing is edited, so readings remain
append-only and a write stays a single-row compare-and-swap. Retiring *every* earlier row
would have meant writing into them; retiring the one that stood does not.

**Entry order is the clock.** Currency means "last written", not "latest `measured_on`" —
which is what makes a late-entered correction authoritative. Confirmed before changing
anything: appending a reading dated 2019-01-01 to an item whose previous reading was from
2023 made the 2019 value current. That behaviour is now consistent instead of implicit.

Three consequences, each fixed rather than noted:

- **`latest_measurements` disagreed with the service.** The SQL view stopped at the
  retirement rule, so both readings looked current there while every tool call reported
  one. The view now copies the service's definition (`query.py`). Two definitions of
  "current" is how one assay ends up with two answers.
- **History had to follow.** Since measuring twice is normal, overtaken rows land in the
  retired bucket, so `include_historical` fetches those too now — otherwise "the whole
  series" would mean one row per assay. Both flags reach the same rows and differ only in
  whether the newest-per-assay trim still applies; their descriptions say so.
- **Series order was wrong.** Sorting by `measured_on` split a same-minute re-measurement
  apart — readings are stamped to the minute, so the retired ones sorted after the current
  one. Rows now carry their table position and sort by that, matching the clock that
  decides currency.

**Told, not asked.** The automatic decision goes back two ways: a `warning` on the
`CommitResult` (`code: auto_superseded`, naming the retired id) for Python callers and
dashboards, and `supersedes` in the tool's reply, because `_record` trims the envelope and
that dict is all a model reads. Reply size 259 bytes against the 300-byte budget the test
holds. An explicitly passed `supersedes` is taken as given and produces no warning.

**Not done, deliberately:** storage cannot distinguish *"that value was wrong"* from *"I
measured it again"* — one column, now carrying both meanings. If that distinction is ever
needed for audit, it wants its own field; nothing here can reconstruct it afterwards.

Tests: six new in `TestMeasurements` (retirement named back, first reading retires nothing,
explicit honoured and silent, series survives retirement, view agrees with service), plus
the readback-key assertion in `test_exports_and_mcp.py` extended. **330 passed.**

README's measurements bullet (~line 190, "just make a new one to overwrite its place") is
now literally true and worth a reword to mention the automatic retirement — the user owns
that file.

## Backends: one Drive shape, called `drive` — 2026-10-15

`BACKENDS` is now `("local", "drive")`.

**`drive_csv` is gone.** Its read path opened with a check that Drive had converted
the uploaded CSV into a Google-native file and refused to continue — a backend whose
primary job was detecting that its own storage format had been taken away from it.
Nothing stored anything there: production and the scratch folder were both created as
spreadsheets, so no database needed migrating. What it took with it: `DriveCsvStore`,
`render_csv`, `_parse_csv`, and the `text/csv` declaration dance.

**`drive_sheets` is renamed `drive`.** The class stays `DriveSheetsStore` because how
it stores is the interesting fact; the *config* name is what lost its redundant suffix.
The base class already carried `kind = "drive"`, so `describe()` now reports
`drive:drive-folder:<id>` with no subclass override.

Old names fail loudly rather than silently working. `Config.validate()` keeps a map of
retired names, so a pre-rename config says *"that backend was renamed to 'drive' when
drive_csv was removed"* instead of presenting a list of valid options and letting the
reader guess which one they meant. No alias, so there's no second spelling to keep
honest.

Tests that existed only for the CSV backend went with it, except the four behaviours
that lived in the shared base class — retry on transient status, 403 phrased as advice,
a deleted file refusing an update, and every `files()` call carrying `supportsAllDrives`
— which were ported to the sheets store rather than deleted, since the base class still
provides them and nothing else covered them. The fake now records kwargs of every
`files()` call so the shared-drive assertion can cover all of them at once; production
lives in a shared drive, where omitting the flag answers 404 and 404 looks exactly like
a deleted table.

### Mounted Drive, and why there is no `local_sheets`

A proposal came up: users who mount Drive locally rather than authorising should get a
sheets backend over the filesystem. It doesn't work, and the reason is worth recording
so it isn't proposed again.

A mounted Google Drive does not expose Google-native sheets as data. Drive for Desktop
surfaces them as `.gsheet` stubs holding a URL; rclone and ocamlfuse serve an exported
copy on read and cannot write back into the native file. So a filesystem backend pointed
at the production folder would find either nothing writable or a read-only export. For
it to have real bytes, tables would have to be genuine `.xlsx` files in Drive that nobody
ever converts — the conversion hazard `drive_csv` just died of, relocated.

The deeper objection: **the mount route and the API route cannot share one set of files.**
`drive` writes Google-native sheets; a mount cannot write those. A `local_sheets` backend
would therefore not unify the two groups of users, it would create a third storage shape
that neither existing backend reads, and the team would end up with two databases whose
writers cannot see each other's revisions — the one failure mode this codebase exists to
prevent. Coherent options for mounted users remain: everyone uses `drive`; or they use
`local` against real CSVs on the mount and treat the API route as a different database;
or they read `export` output.

Not verified on this machine — there is no Drive mount or rclone installed here, so the
stub behaviour is known behaviour of those tools rather than something measured. If
somebody wants it confirmed, mount the folder and `ls -la` a table.

### Loading a config that names a retired backend

Smoke-testing the rename showed the retired-name message was unreachable in practice:
`Config.from_dict` parsed `drive_folder_id` before validating, so any config carrying a
pasted URL *and* an old backend name died with a `ValueError` traceback — `'X' is neither
a Drive URL nor a plausible folder id` — before anything could mention the rename. The CLI
prints `config: …` for `ConfigError` only; a `ValueError` escaping a file load is a stack
trace over a file the caller never opened.

Two changes, both found because there were no `Config` tests at all and now there are
(`tests/test_config.py`, 13):

- `from_dict` validates the backend first, then parses the folder id inside
  `try/except ValueError → ConfigError`. Order matters: a config from before the rename
  should be told about the rename, not accused of having a bad folder id.
- `_DRIVE_FOLDER_PATTERNS` matched `/drive/folders/<id>` with the prefix pinned, which
  silently failed on two forms people actually paste — the signed-in
  `/drive/u/1/folders/<id>` and the legacy `/drive/#folder/<id>` that the function's own
  docstring promises to handle. It now matches the `folders`/`folder` segment wherever it
  sits (`[/#]folders?/`). Both forms were broken since the initial commit; the parametrised
  test covers four share-link shapes plus a bare id.

Verified live after the change: `show-config` resolves prod as `backend: "drive"`, and
`doctor` against the production folder reports `drive:drive-folder:1ARN4XL0…`, twelve
tables, 0 errors.

### The destructive-flag brake

`main()` asserts that `--seed`/`--reset` were not requested, so using them means deleting
the line first — deliberate, because both commands truncate a database that other people
are reading. It sits in `main()`, not in `_cmd_init`, on purpose: the point is that the
person running it had to open the file.

As written it read `args.seed` plainly, and those attributes exist only on the `init`
subparser — every other subcommand raised `AttributeError: 'Namespace' object has no
attribute 'seed'` before reaching its handler, `serve` included. Narrowed to
`getattr(args, "seed", False)`; the brake still fires for `init --seed`, and everything
else runs. Suite with the brake active: **321 passed**.

Note for whoever follows: README's local quickstart is `init --root ./db --seed`, which
now stops at the assert until the line is removed.

### How a config comes to exist

`init` is the only writer of `config.json`; there is no template file and no `config`
subcommand. `--config` reads two ways — a **destination** on `init` (it creates the file
and its parent directory), a **source** on every other subcommand — so the quickstart's
`--config ./db.config.json init --root ./db` names where the scratch database's config
should go, not a file that must already exist. Omit it and the default path is written,
after which `serve` needs no arguments. Precedence stays `--config` >
`$INVENTORY_MCP_CONFIG` > `~/.config/inventory-mcp/config.json`.

The sharp edge in that flow: `init` rewrites the whole file from the command line plus
three inherited credential paths, so re-running it resets `cache_ttl_seconds`,
`keep_backups`, `drive_file_ids` and `actor`. Measured, not inferred. `drive_file_ids` is
the one that matters — nothing in the code writes it, it exists for a person to pin a
table name to a file id, and the Drive setup notes tell people to re-run `init` after a
read times out mid-setup. Now announced rather than silently applied:

```
note: init rewrites the config; resetting drive_file_ids={'items': 'FILEID'} -> {},
      cache_ttl_seconds=99.0 -> 5.0, keep_backups=3 -> 20
```

Warned, not prevented — the overwrite still happens, because `init` overwriting its own
output is the expected behaviour for a fresh install. Fields named on the command line
aren't reported as resets. `tests/test_cli.py` covers all three cases (fresh install says
nothing, edited values get named, unedited re-init stays quiet); suite now **324 passed**.

## Push-to-team pass — 2026-10-15

Credentials left the repository tree. Nothing names `./.creds/` any more: the client secret
and the consented token sit at the documented defaults under `~/.config/inventory-mcp/`
(mode `0600`), so no config has to point at them — and no config carries a relative path
that quietly breaks the moment the server is started from somewhere else, which the old
`./.creds/oauth_token.json` did.

Production has a folder: `Inventory_Database` in the *Genomic Medicine Lab* shared drive,
under `Resources`, id `1ARN4XL0DgvLK1hmStk7X_CWKOmTgexKt`. Created the twelve tables there,
then drove the write path against it live — `create_project`, `create_sample`,
`create_assay`, `record_measurement`, read-back, `doctor` clean — and reset it, so prod
starts with headers and nothing else. The scratch folder remains `Inventory_Database_Test`
(`16QfHIYtRsQBmtvs77Ke-zL5uvxBNUEm6`), still seeded, still where experiments belong.

Doing the setup instead of reading about it turned up two bugs:

- **`init` saved a config with empty credential paths.** On a fresh install there is no prior
  config, and `_cmd_init` built its `Config` from `getattr(base, "token_path", "")`, so the
  file it wrote carried `""` where the defaults belong — and `Path("")` is `.`, which the
  Drive layer then reported as `token file . is unreadable`. `init` now reads the resolved
  default config when one exists, so it preserves a working authorisation instead of
  overwriting it, and falls back to the dataclass defaults when there is none. This was the
  first command a new team member would have run.
- **`drive_csv` could not see a shared drive at all.** `files().get_media()` was called
  without `supportsAllDrives`, and Drive replies 404 to that — the same response a deleted
  table gets. Every other Drive call in the store passed the flag; the one that missed it sat
  on the read path of the backend nobody had aimed at a team drive yet. Now passed, with a
  test asserting it is sent.

`.creds/` is gone from disk as well. The one thing that does ship is the app's own OAuth
client, `inventory_mcp/client_secret.json`: an installed-app client secret identifies *which
app* is asking and authorises nothing until a human approves the consent screen, so Google
treats it as distributed with the binary (RFC 8252) and there is no sense in which keeping it
out of the repo protects anyone. Without it a new install cannot start the flow that produces
a token at all, which made onboarding an errand to ask someone for a file. `auth.resolve_client_secret()`
prefers a copy at the configured path, so anyone who registers their own client still uses
theirs; `is_file` rather than `exists`, because an unset path arrives as `""` and `Path("")`
is the working directory. It is declared as package data — verified present in the built
wheel, not merely present in the checkout — and `.gitignore`'s `client_secret*.json` rule got
one negation, with a test asserting the shipped JSON has client fields and none of a token's
(`refresh_token`, `expiry`, `account`), since dropping a real token in that slot would look
like a harmless JSON change in review. Rotating that client secret invalidates every stored
token, which all refresh through it.

`.devdb.config.json` is untracked. It held machine-specific paths and a real folder id, which
is per-machine state rather than project state; it remains on disk for local development,
pointing at the scratch folder and inheriting credentials from the defaults. The README now
carries the first-run instructions, including both folder ids.

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
.venv/bin/python -m pytest -q          # 313 passed
inventory-mcp doctor                   # 0 error(s), 0 warning(s) -> OK   (freshly seeded local root)
                                       # the live Drive scratch folder: 0 errors after
                                       # `enrol --from-ancestors` + 3 header warnings
inventory-mcp tools                    # 35 tools, real parameter schemas
inventory-mcp init --seed              # items=13 samples=10 measurements=18, integrity clean
inventory-mcp init --reset --seed      # truncate all tables first, then seed (recovery path)
inventory-mcp enrol --from-ancestors   # migration for inherited-scope databases; --dry-run plans it,
                                       # reports "nothing to do" on a database already exact
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
consented token live under `~/.config/inventory-mcp/` (`client_secret.json`,
`oauth_token.json`, both `0600`) where the defaults already look, so no config names
them. `inventory-mcp auth --check` verifies without prompting. Service-account support
exists and takes precedence when configured; the Drive-for-Desktop-sync route remains
available with the `local` backend. One caveat to remember: **the OAuth token is the
human's whole Drive** — it sees the lab's shared drives too. Testing stayed confined to
the scratch folder; production use wants a dedicated account or at least group-based sharing
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
| `inventory_mcp/tools.py` | the 35 MCP tools |
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
- **`patient_projects` records no provenance.** No `updated_by`/`updated_at`, so an
  enrolment says neither who made it nor when — odd for a consent-adjacent act, and it
  means "when did this participant enter project X?" is unanswerable. Adding the two
  columns is additive within schema v3; the open question is whether withdrawal should
  leave a trace row rather than nothing.
- **`set_status` no longer writes to tombstones.** `_require_item` reads `items_by_id()`,
  which does not filter `deleted_at`, so marking a deleted item consumed used to succeed —
  and filing it into a well claimed a slot that `occupants()` filters out, leaving a box
  reading free while a row said otherwise. It refuses now, in the wording `move` and the
  edit tools already used. The tempting central fix (`_require_item` refusing to hand back
  a tombstone) is wrong: `get_measurements` answers for retired items on purpose, and
  `link_items` must accept a derivation edge to a tube that was discarded years ago.
  `record_measurement` still appends to a tombstone deliberately — "we measured it, then
  threw it away" is a real sequence, and `measured_on` dates the reading correctly.
- **Receipts dropped `warnings`, which no tool call ever fills.** `CommitResult.warnings` is
  written in exactly one place — `backfill_enrolments`, the CLI's inherited-scope migration —
  and comes back empty from every write reachable from the surface, so the receipts stopped
  carrying it. If a future write needs to say something that is neither a refusal nor a field
  ("this pairing has no consent row behind it"), the receipt has to be given the channel
  deliberately rather than inherit it from the envelope it replaced.
- ~~Whether `.devdb.config.json` should flip its default backend to `drive_sheets`
  for the lab's real folder, or whether each deployment gets its own config.~~ Settled
  the second way: each deployment owns a config at `~/.config/inventory-mcp/config.json`,
  production is `drive_sheets` against the team folder, and `.devdb.config.json` is
  untracked scratch pointing at the test folder. A committed config would have been a
  machine's opinion about somebody else's install.
- Bulk-operation quota strategy: per-row read+write+verify will not survive a
  thousand-row import at 60 reads/min. Batching (multi-table value updates,
  skip-verify behind a flag) is the obvious shape; needed before real data lands.
- `assays.canonical_unit` is `required=True` unconditionally (`schema.py`), so a
  **qualitative** assay cannot be registered without inventing a placeholder unit —
  "gel smear quality" has none. Found while pinning the qualitative-assay path in
  `TestGetItem`; the test passes `"score"` and says so. Fix is to make the
  requirement conditional on `result_type == "numeric"` (column-level `required`
  can express that, so the rule moves into `_validate_assays`). No qualitative assay
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
  so it could not answer anything about a freezer, and `get_occupancy`, `search_containers`
  and `contents` cover what it said (`run_query` covered it too, until SQL left the surface).
  **`rollup_by_patient` became `search_patients`**, because the census shape had two
  faults: `(none)` mixed "not a sample" with "sample with no participant", and cost grew
  with the cohort. It now requires a `query` matched against participant ids and labels,
  enumerates from the participants table (so an enrolled person with no tubes returns
  `total: 0` rather than vanishing), caps rows at `limit` (default 20) reporting
  `matched`/`truncated`, and carries `unassigned` — samples held with no participant —
  beside the rows instead of as a fake row that would compete with the limit. No blank
  query is accepted; `"pat_"` matches every id, which is the only way to ask for the
  whole cohort. If that census should be first-class, give `query` a wildcard value.
- **`rollup_by_project` became `search_projects`**, for the same reason the patient axis
  moved: only `samples` carry a `project_id`, so `(none)` had been pooling every reagent,
  kit and consumable into a fake project and a "project total" silently mixed tubes with
  tips. It takes a mandatory `query` (id or name; `"prj_"` matches all) plus `limit`,
  enumerates from the projects table so an untouched project returns zeroed figures rather
  than vanishing, and reports `participants` (from `patient_projects`) beside sample counts
  by status. Counts are **direct, never subtree** — `CIRCA somatic` keeps its own 3 while
  `STAT5B/KRAS` and `VEXAS` keep theirs, and `subprojects: 2` says there is more below.
  Absorbing children would double-count any tube summed across a match-all query.
  Unaffiliated samples arrive as `unassigned_samples` beside the rows. Supplies stay
  uncountable rather than migrated: a `project_id` on `reagents`/`kits`/`consumables` is a
  schema v4 change, deferred by choice. `_rollup` is deleted with this.
  **Trap for the next read tool:** `Table.live()` returns an *iterator* (`db.py:50`), so a
  second walk over the same value silently sees nothing — it produced a plausible
  `subprojects: 0` until a test caught it. Materialise with `list()`.
- **The three container readers became two.** `list_containers` and `container_tree`
  returned *the same objects* — both built by `_container_summary`, one flat and one nested
  — 2207 B against 2342 B for the ten-container fixture, neither bounded by anything, and
  the nested one charged `path` at every node to repeat an ancestry the nesting already
  showed. Neither could be queried. **`search_containers`** replaces them on the surface:
  mandatory `query` (id or label; `"ctr_"` matches all), `kind`, `within_id` (everything
  under a container at any depth, which is what "what is in freezer 7" actually asks — the
  named container is the scope, not a find), and `limit` with `matched`/`truncated`. Rows
  carry `container_id`, `kind`, `label`, `parent_id`, `path` and nothing else: fill figures
  are `get_occupancy`'s answer and contents are `get_contents`'s. `Service.list_containers`
  stays — `ls --containers` walks the whole set and wants the numbers. `get_contents` lost
  `recursive`: one layer only, since the subtree walk belongs to `within_id`, and recursion
  had been paying a container path per row (`in`) to say which box each tube sat in. That
  one layer now lists **nested containers beside items** and counts both — a rack holding
  two boxes holds two things, and `count: 0` for a freezer with its shelf sitting right there
  read as *empty*. Child rows carry identity alone (fill figures stay `get_occupancy`'s
  answer, stated in one tier's unit), which made the header's `occupied_here` and
  `child_containers` pure list lengths; both are gone, and `free = capacity − count`.
- **`move_item` became `move`, and it moves containers as well as items.** One slot is one
  slot, so shelving a tube and re-racking a box are the same operation and the target's
  capacity decides either way — checked before the write, because "no space in Rack 3 (6/6)"
  is the answer a caller needs while the table-wide `over_capacity` rule can only report that
  some row now disagrees with its own capacity. A container cannot be moved into its own
  descendant (checked up front: a cycle detaches the subtree from every path above it), and
  `position` applies to items only — a container fills a slot, not a well. `clear_location` is
  gone: passing it alongside `location_id` silently discarded the destination and reported
  success, which is the failure shape this pass kept finding — two arguments arguing, and the
  code picking a winner by `if` order. Consequence worth remembering: a *present* item can no
  longer be un-located on purpose (`set_status(consumed)` still clears the slot, and that is
  the case anyone actually means). The response also shrank from 951 B to ~200 B: every write
  used to end with a full `get_item` dump, and a move changes nothing but the location, so
  `move` returns `{id, kind, from, to}`. The other writes still return the whole record — same
  fix available, deliberately not taken under cover of this change. A surface test now pins
  `required == [id, target_id]`: rewriting a signature is precisely how the `= None` on an
  `Annotated` parameter goes missing, and it did here — the wrapper failed as
  `TypeError: move() missing 1 required positional argument: 'position'`, which only an
  end-to-end call sees.
- **SQL left the MCP surface.** `run_query` was deleted as a tool; `query.run_sql`,
  `Service.run_query` and `inventory-mcp query` are untouched, so humans and code
  someone owns keep the capability. The reason is not that the guard was weak — it was
  sound (single statement, `SELECT`/`WITH` only, keywords banned before parsing) — but
  that a query string is arbitrary code over the whole database with no per-table
  boundary and no bound on what an answer costs. Measured: a row of `items_wide` ships
  at 681 B, so the *default* `limit=500` returns **333 KB**, and a disciplined
  three-column select still 51 KB. Worse, the mirror declares no column types, so
  `where value_num > 50` matched **all 18** readings while `cast(... as real) > 50`
  matched 10 — a plausible wrong number with no error attached, which is the failure
  class this pass kept finding. Two bugs surfaced during the audit and were fixed on
  the way out: every guard message was being swallowed because `QueryError` is a
  `RuntimeError` rather than a `ServiceError`, so callers saw only
  `Error executing tool run_query`; and the keyword ban caught the `replace()` string
  function plus any word inside a string literal or comment. Both live on in
  `query.py`'s tests even though nothing on the surface can reach them now.
  **Deletion was gated on a coverage inventory, not on a hunch.** Four things had no
  other read path at all and were built first: **`get_patient`** (sex, dob, affected
  status, gene, variant, protocol, consent date and notes were written once by
  `create_patient`, could not be changed by `update_fields`, and were returned by
  nothing — `get_item('pat_…')` is a `NotFound`; the tool also lists enrolments, which
  no tool had ever enumerated, with sample figures per project and an explicit
  `unenrolled_samples` so the rows cannot silently fail to sum), **`list_assays`**
  (the registry had `create_assay` and no reader, so an assay's quantity, canonical
  unit and method note were unreachable, and an assay defined but never used was
  indistinguishable from one that did not exist — rows now carry `readings` for that),
  **date bounds** on `search_items`/`count_items` (`received_since`,
  `received_before`, `expires_before`, `measured_after` — no read tool had taken a date
  at all, so expiry watches and intake windows were impossible), and **`sort`/`limit`
  plus `no_project`/`no_patient`** (`search_items` had neither ordering nor a cap, and
  the only way to ask "missing a project" was to diff against `search_projects`).
  Facts worth keeping: dates compare as text, so a bare `YYYY` or `YYYY-MM` bounds a
  range correctly and a date bounds a timestamp; **a row with no date matches no
  bound** — nobody dated a tube is not the same as a tube dated long ago; a malformed
  bound is refused rather than answered with an empty list, because an empty list reads
  as an empty freezer; `measured_after` accepts a time component because `measured_on`
  carries one; `no_project`/`no_patient` imply samples, since supplies have no project
  column and a filter that ignored class would file every tip under "needs a project";
  `limit=0` means every match, so truncation only happens when the caller named the
  number and needs no separate flag. Search rows still hide dates by design
  (`test_what_search_hides_get_item_shows`), so sorting by `received_on` orders
  correctly without showing the value — two steps, find then read, as intended.
  Net: 30 → **31 tools**, `INSTRUCTIONS` 1589 → 1373 B (the table list existed to serve
  SQL), tests 207 → **227**. Cross-tabs, trends and derivation-graph census are
  **deliberately** left to the exports and Route C; if the next person reaches for a
  rollup tool to fill them, that is the argument to reopen this, not a bug to fix.
- **Enrolment stopped inheriting down the project tree.** A sample pairing a participant
  with a project used to pass if they were enrolled in that project *or any ancestor of
  it*. On the untouched seed, **5 of 8 paired samples were legal only through that
  closure**, all five sitting in `STAT5B/KRAS` while their participant was enrolled in
  `CIRCA somatic`. So "is X enrolled in Y?" had two answers: `get_patient` and
  `search_projects` read raw rows (`STAT5B/KRAS`: participants 0, samples 5) while
  `create_sample` said yes — and creating a subproject silently widened every umbrella
  participant's scope, with nothing recording the act because `patient_projects` carries
  no audit columns. Exact matching removes the contradiction rather than mirroring the
  closure into reads: the junction table becomes the whole truth and every existing read
  tool stops lying, so no new field was needed. `schema.py` already said "must be one of
  these" — the code was the deviation. **The blast radius shaped the design:**
  `validate_scope._involves()` reports `project_not_in_patient_projects` whatever row
  caused it, so stranded pairs block *every* write touching samples — filing one legal
  tube came back refusing on behalf of five unrelated ones. So the migration ships with
  the rule instead of following it: `Service.backfill_enrolments(dry_run=...)` behind
  `inventory-mcp enrol --from-ancestors [--dry-run]`, adding a row per pair that actually
  holds material (unused branches stay unenrolled — nobody asserted them) and writing
  **nothing** while some pair has no enrolment or ancestor behind it, because inventing
  that consent is the exact bug being removed. Verified end to end on a fixture rewound
  to the legacy shape: dry run leaves the 5 errors, the real run writes 1 row and doctor
  goes clean, a second run adds nothing; an unauthorised pair exits 1 having written
  nothing. Alongside it, `create_patient` now takes its first `project_id` and commits
  participant + enrolment as one unit (a participant enrolled in nothing can hold no
  filable material, and an unusable project leaves no participant behind);
  `assign_patient_project` is documented as the tool for *further* projects with both
  params described; the rule's text reads "not enrolled in it -- nesting does not
  inherit, so add the pair with assign_patient_project", naming the fix because there is
  no longer any implicit route — a model now meets that sentence in `doctor`, not in a
  refusal. Dropped mid-audit: `create_project(enrol_from_parent=...)`, which would
  have copied a parent's roster into a new subproject — precisely the automatic widening
  this change forbids, and a new subproject is empty until someone puts a participant in
  it. Still open: the **audit columns** `patient_projects` lacks — an enrolment is a
  consent-adjacent act and records neither who made it nor when. Tests 227 → **237**.
- **Withdrawal settles the material in the same write.** `unassign_patient_project
  (patient_id, project_id, move_to="")` deletes an enrolment row and handles every live
  sample naming that pair in the *same* commit: re-pointed at `move_to` (which must be
  another project they are already enrolled in), or left with a participant and no
  project — legal, since the rule needs both halves, and findable through
  `search_items(no_project=true)`. The obvious design — remove the row, return the list
  of orphaned samples to reassign — was ruled out by measurement rather than taste:
  `_involves()` reports `project_not_in_patient_projects` whatever row caused it, so the
  moment the row is gone every write touching samples is refused, *including* the writes
  needed to work down the returned list. A to-do list the caller cannot act on is worse
  than a refusal, and there is no other route: `update_fields` takes no foreign keys, so
  nothing else on the surface can re-point a sample's project. Readback names what
  happened per tube (`samples_repointed`, `samples_unpinned`, `sample_ids` capped at 50
  with `sample_ids_truncated`, `participant_still_in`, `was_their_last_project`). Two
  decisions inside it: **the rule now skips tombstoned items**, matching the three
  validators that already do — a retired tube is history, not a live claim about routing,
  and before this it counted as a dependent, meaning deleting tubes made a withdrawal
  *harder* instead of easier; and withdrawal is allowed to take a participant to zero
  projects (flagged in the readback rather than blocked, since nothing in the schema says
  a participant must hold a project). Tools 31 → **32**, tests 237 → **244**.
- **`correct_measurement` left the surface; appending is the overwrite.** A mis-recorded
  number is answered by recording it again, because the read model already collapses to
  one reading per item-and-assay: `_measurement_rows` keeps the last row *written*, so a
  second `record_measurement` retires the first from `get_measurements` without needing a
  second verb. Handing a model both tools would have made it decide which reading is
  current. `Service.correct_measurement` survives — the seed builds its superseded
  Nanodrop pair with it, exports still carry `superseded`, `include_historical=true` still
  reads the chain. Two consequences stated rather than discovered later: nothing on the
  surface can now mark a reading invalid while leaving the older one authoritative, and
  recency is write order, not the date field — backfill a reading dated last year and it
  becomes "the latest". Tools 32 → **31**, tests 244 → **247**.
- **"Textual assay" was never a thing; it is a qualitative assay.** Changed in everything
  a reader sees: the parameter descriptions, both `ServiceError` messages, the
  `missing_text_value` violation text, the `assays` schema blurb, the test names. The
  stored enum stays `result_type=text` and the parameter stays `text=` — renaming those is
  a data migration, not a vocabulary fix, and `gel_qc` rows do not care what they are called.
- **An append answers with the reading, not the commit.** An append returned the whole
  envelope — `operation`, `touched_tables`, a backup path, an empty `warnings` — wrapped
  around a figure the caller had just typed: 604 B, 157 of it indentation. Seven keys come
  back now (`measurement_id`, `assay`, `assay_name`, `value`, `unit`, `instrument`,
  `notes`), 183 B. `measured_on` is absent because unless supplied it is this minute;
  `handler`, `supersedes` and `superseded` are absent because the surface can no longer
  produce either state. Projection sits at the tool layer, unlike `move`, whose readback
  lives in the service — `record_measurement` also answers to the seed and the suite, and
  those need the real `CommitResult`. Description 168 → 67 B with the rules moved onto the
  nine parameters, all now described: the pair used to cost 1104 B per listing and the
  survivor costs 1505 B, so deleting a tool made the catalogue larger on purpose.
- **A refusal now says whose fault it is.** Rehearsed against a corrupted folder, a
  perfectly valid call was refused on behalf of eight entries it never touched, and the
  quickest route back to writable was to overwrite somebody's edits from a snapshot — the
  old message quoted every violation, so unrelated damage arrived looking like the
  caller's mistake and reading as permission to tidy it. `_apply` therefore validates the
  stored state as well as the candidate one *on the failure path only*, partitioning via
  `validation.key()` (table, row, field, code — message excluded, because an over-capacity
  rack says "3 containers" then "4" while remaining the same problem). `ValidationError`
  gained `.introduced` and `.preexisting`; `.violations` still carries everything, so the
  CLI, the suite and any importer are untouched. Three shapes reach the surface: your own
  bad argument names the offending field in full; standing damage names nothing, states
  that the call is not at fault, and says to check with whoever edits the files before
  repairing anything; both at once names yours and counts theirs. The first draft of this
  change suppressed the list unconditionally, which would have left a model holding a typo
  with no way to find it — `doctor` inspects stored state and would have reported a clean
  database. The CLI keeps printing the whole list. Tests 247 → **249**.
- **`export_data` left the surface, and the Publish group with it.** Nothing about
  exporting changed: `Service.export`, `export_views()` and `inventory-mcp export` all
  stand, and `TestExport` still drives them end to end. What changed is who asks. A
  dashboard needs the same request to yield the same bytes, at poll rates a conversation
  cannot hold, without being handed credentials that can write. The tool also carried the
  surface's only filesystem path — `directory=`, resolved as `Path(directory).expanduser()`
  with no containment check — and returned 1587 B of which ~700 was twelve content hashes
  nothing in a conversation can act on. On a Drive-backed config it was worse than
  pointless: there is no local root, so omitting `directory` raised and naming one wrote
  the exports onto the laptop of whoever started the server, invisible to the readers they
  existed for. With `run_query` gone too, the surface now carries neither a query language
  nor a file write. Tools 31 → **30**, tests 249 → **250**. Follow-on recorded where it
  belongs: `docs/dashboard.md` already tells builders *"don't call the MCP"* and describes
  the two routes that remain; the missing piece is an HTTP API over `export_views()` and
  the read methods, to be designed **after** the tools settle, because published URLs
  freeze faster than any other contract.
- **`set_status` shrank to what it changes.** It used to answer a one-word edit with the
  whole `get_item` projection inside the commit envelope: 1384 B where `{item_id, name,
  new_status}` — 101 B — is everything a caller needs to know the write landed. The rest was
  the class row, audit fields, the freezer path and readings, most of it already on screen
  from the search that produced the id. `allow_backwards` is gone; reversing a status is how
  a mis-record gets fixed, and a guard whose only effect was to add a retry taught nothing.
  In its place comes `location_id`, required exactly when the item holds no slot, which is
  every trip out of `consumed` — consuming drops the location and nothing remembers the well,
  so the old route was two calls with the tube marked present and holding nothing in between.
  `status` became a `Literal`, so the three legal values travel in the schema instead of
  arriving as an open string a caller can typo. Entry 418 → **824 B**, body 1384 → **101 B**.
  The capacity check came out of `move` as `_require_space()` so both place things against
  the same geometry, with a test that the tube being re-placed isn't counted as an obstacle
  to itself in a full box. Tests 250 → **255**.
- **`update_fields` became three tools.** Thirteen optional fields against twelve tables asked
  the caller to have already memorised the schema: that a name is `name` except on boxes and
  participants where it is `label`, that prose is `notes` except on projects where it is
  `description`, that a tube's note sits on `items` while its collection date sits on
  `samples`. None of its fifteen parameters carried a description — not from skipping the
  work, but because no sentence about `notes` can say which of six tables it means. Two real
  faults came with the shape: a singular table name reached `KeyError`, which `_guard` does
  not catch and the SDK reduces to `Error executing tool update_fields` — the known-tables
  list it helpfully built never left the process — and it edited `measurements.notes` in
  place, on the one table whose contract is append-only.
  `edit_name(id, name)`, `edit_notes(id, notes)` and `edit_data(id, field, value)` each imply
  their own table, and the id resolves the row: the prefix picks the table, `items.class`
  picks the extension table, which is how `move` has dispatched all along. So there is no
  `kind` argument to get wrong, and asking a sample for a price is refused by fact — *"price
  belongs to reagents, kits or consumables, and smp_x is a sample"* — instead of being
  accepted on the caller's declaration. `handler` sits on the supertype so any item takes it;
  project prose maps to `description`; `item_links.notes` and `measurements.{notes,handler}`
  left the surface, and tombstones now refuse text edits (which `set_status` still permits —
  open thread below). Each answers with `{id, kind, …}` around 90 B rather than the whole row
  inside the commit envelope (422 B measured). All three funnel through `Service.update_fields`,
  which stays for the CLI and for code someone owns, and gained a keyword-only `operation=` so
  the backup directory is named for the edit that ran. Entries 645 / 473 / 913 B against 1437 B
  for the single tool: more across all three, less per load, every parameter described.
  Tools 30 → **32**, tests 255 → **264**.
- **The create tools stopped asking the caller to guess.** Rehearsing all seven against a
  seeded copy refused two of them on the first try — `sex: "M"` and `quantity:
  "concentration"` — because the legal values live in `schema.py` where a model cannot see
  them, and fifty-eight of their parameters shipped as a bare name and a type with nothing to
  say where an id comes from, what a blank means, or which format a field takes. Now every
  parameter on all 32 tools carries a description, and the nine closed sets (container kinds,
  sample types, supply classes, sexes, affected statuses, project statuses, assay quantities
  and result types, link roles) travel in the JSON Schema as `enum`, derived from the same
  `schema.py` tuples validation enforces, so they cannot drift.
  Deliberately **not** `Literal`, though it advertises the identical list: pydantic checks a
  `Literal` before the tool runs, so a wrong value arrives as
  `Error executing tool create_patient: 1 validation error …` plus a docs link — the shape of
  a crash — while the service's own refusal names the row, the field and the set. The enum is
  injected with `json_schema_extra`, so the argument stays a string and the good refusal
  survives; a test asserts both halves (enum equals constant, and an off-vocabulary call still
  reads `WRITE REFUSED`).
  Shape changes that came out of the same rehearsal: `create_container` lost `rows`, `cols`,
  `row_labels` and `col_labels` to `create_box`, so one tool makes furniture and the other a
  grid instead of two making boxes and only the narrow one insisting on a parent;
  `create_supply` gained `position`, `handler` and `quantity_measured_on`, which the service
  always accepted and the surface never offered; `capacity` became `Optional[int]` rather than
  a string where `""` meant "unrecorded"; and `handler`'s habit of defaulting to the writing
  account is now said out loud instead of appearing in the response unexplained.
  Bodies: the seven creates returned 5142 B between them, most of it the commit envelope and an
  echo of what the caller had typed — `create_sample` alone was 1187 B, including join labels
  for a tube just created and an empty `latest_measurements` for something that cannot have
  readings. They answer with a receipt now (**1010 B**, 86–236 each): the minted id, the class
  or kind, the name, and whatever the server derived — a box's capacity from its grid, a
  supply's opening reading and the minute it was true. The same helper covers the writes that
  still returned `CommitResult` (`link_items` 130 B, `delete_item` 91, `delete_container` 55,
  `merge_items` 88, `assign_patient_project` 95; `unassign_patient_project` keeps its derived
  summary whole, and `move` was already lean). Entry sizes rose as promised — the seven went
  6291 → 11594 B loaded, `create_container` 1091 → 1420, `create_sample` 1300 → 2318 — while
  the lines that are always in context got shorter, since the descriptions shed the parameter
  prose they were carrying (`create_sample` 239 → 48 chars, `create_supply` 198 → 38); the
  listing across all 32 tools costs 2450 B. `INSTRUCTIONS` lost its container-kinds bullet to
  the enum. Tests 264 → **271**.
- **Two bugs the rehearsal caught, both mine.** `record_measurement.measured_on` advertised
  "ISO like 2026-10-14T08:25 **or a bare date**" and validation refuses a bare date outright —
  readings are ordered by minute, so a date alone would have to be answered with a midnight
  nobody recorded. The description now says so, `quantity_measured_on` inherited the corrected
  wording, and a test calls it with `2026-10-13` to keep the promise honest. Second: rewriting
  the signatures silently dropped four defaults (`create_container.parent_id`, `.capacity`,
  `create_sample.location_id`, `delete_item.force`), which made arguments required that a
  caller has every right to leave out — invisible until someone registers a tube without saying
  where it is. Rather than trust another per-tool assertion, one test now walks all 32 tools and
  fails if `required` disagrees with the properties that carry no default.
- **Model-facing descriptions are one sentence** on every tool touched since. What a tool is
  for stays in the docstring; how to call it goes in `Field(description=...)`; rules that
  hold for every call moved to `INSTRUCTIONS`, where they cost once per session rather than
  once per tool load. Measured: `search_patients` 634 → 67 chars, `link_items` 116 → 43,
  `merge_items` 153 → 62, `get_occupancy` 428 → 58. Interpretation facts a payload cannot
  argue for itself — slots are never summed across container levels, an absent position does
  not mean a free well — belong in `INSTRUCTIONS`, not in a per-tool essay.
  `get_measurements` was the last tool phrased as a payload rather than a purpose ("Returns
  current and (optionally) historical measurements for a given item", unpunctuated); it says
  what it is for now, and its two history flags stopped reading like near-duplicates —
  "replaced with more recent" against "replaced with more accurate" is the whole difference
  between a series of valid readings and a correction chain, and the old wording gave a caller
  nothing to choose on. Entry 742 → 1163 B, listing line 73 → 58 chars.
- **Deletes are one rule now: clean, or refuse with the list.** No destructive call takes
  a force flag any more. `delete_item` kept `force` as its only way to discard a tube that
  still held material, which framed "we threw it away" as something needing permission —
  while nothing downstream would ever catch it, and the tombstone already stops the row
  claiming a well. It is unconditional now, and the entry shrank 798 → 496 B with the
  parameter gone. What *is* blocked stays blocked, but a refusal has to be actionable on
  its own: `delete_container` used to say `'Box 99' still holds 1 item(s)` for tubes and
  `ctr_wtatnaqq still has 2 child container(s)` for boxes — one label, one key, neither
  naming what to move. Both now lead with the container's id and list every blocker by id,
  capped at ten with `(+N more)`, because an id is what the caller passes to `move` or
  `delete_item`; a name is a label nobody can act on.
- **A retired row naming a box is swept, not refused.** Live residents block a container
  delete; a tombstone that still names one cannot be moved (`move` refuses) or edited (the
  edit tools refuse), so refusing over it would make that container undeletable forever —
  and leaving the reference behind would strand a foreign key across every later write,
  since `fk_dangling` checks rows alive or dead. `delete_container` clears those stale
  locations in the same commit. Only a hand edit produces such a row today — `delete_item`
  and `merge_items` both clear the location — which is exactly the case the tool has to
  survive, because hand edits are why `doctor` exists.
- **`delete_patient` and `delete_project`**, the two destructive calls that were missing.
  What makes a delete clean depends on what the reference *meant*: a sample needs neither a
  participant nor a project, so `delete_patient` cannot be blocked — it drops the
  participant and their enrolments and blanks `patient_id` on every sample row naming them,
  retired ones included. `delete_project` is where the asymmetry bites: an enrolment is what
  makes a sample's project legal at all, so withdrawing someone from their only project
  would leave their tubes illegal against a rule reported on whatever row tripped it — the
  exact shape that locks the database. It re-points each participant's samples at the one
  project they have left, and refuses when that is not a deduction: a subproject still
  hanging off this one (nesting inherits nothing), someone with no other project, or someone
  with several, where the destination is a decision rather than a guess. All three blocks
  come back in one message, ids included, so a single retry can clear the whole list.
- **Success bodies say almost nothing, on purpose.** `{item_id, deleted_at}` for an item —
  the id plus the one thing the server generated; `{container_id}` for a container, which
  hard-deletes and so generates nothing. That cost the old `{container_id, deleted: true}`,
  an echo and a constant. The two sweeping deletes keep their counters, and
  `delete_project` keeps `moved_to`: where material went is a decision the server made on
  the caller's behalf, and it should show its work. Uncapped — a project with a hundred
  participants gets a hundred lines, which is the honest trade for an audit trail of an
  automatic rerouting.
- Measured across the pass: surface 32 → **34 tools**, the always-in-context listing 2450 →
  2633 B, the four delete entries 430–517 B each, still zero undescribed parameters
  anywhere. Tests 271 → **284**.
- **The unit conversion is reported, not just performed.** `create_assay` advertised that a
  reading in another unit of the same quantity is "converted to this on read", and three
  other places said it too (`docs/tools.md`, `schema.py`, `docs/dashboard.md`) — while
  `_tool_reading` dropped the already-computed pair behind a comment saying the arithmetic
  belongs in the exports. So the surface stated something untrue about itself, and the cost
  landed on the reader: comparing a Qubit at 49 ng/ul with a Nanodrop at 0.049 mg/µl meant
  doing the arithmetic to notice they agree. `canonical` now rides on every reading row, on
  `get_item`'s inline readings, and on the `record_measurement` receipt. The stored spelling
  stays the record — what someone wrote down is the entry of fact, and prose quotes that
  back — with the derived figure labelled as derived. Costs ~35 B per reading.
- **Timestamps carry ISO's zone marker in the column.** Asked whether prepending `UTC-`
  would still parse: it would not, and there is a clean convention anyway — ISO 8601 puts
  the zone at the end. `parse_datetime` accepts a trailing `Z` or `±HH:MM` / `±HHMM` and
  folds it into UTC; `Service._timestamp` normalises before the write, so
  `2026-10-13T20:00+11:00` lands as `2026-10-13T09:00Z` and a bad value is refused by name
  at the call rather than as a table-wide validation failure later. `format_datetime` is
  the single writer of the stored spelling, so `measured_on`, `updated_at` and `deleted_at`
  all answer with `Z`. The first cut deliberately left the marker off stored values, on the
  grounds that a column half marked and half not sorts differently from either spelling
  alone; that objection was right and the conclusion was wrong — the data is pre-alpha, so
  the mixed column is accepted rather than deferred to a migration nobody needs yet. What
  makes it safe is that comparison ignores spelling: `validation.strip_zone` asks both
  sides of a `measured_after` bound in the same form, so the exact minute a bound names
  still matches itself whichever way the row was written. Rows naming no zone — anything
  predating this change, including the live Drive folder until it is rewritten — read as
  UTC and raise `datetime_without_zone`, a warning, so `doctor` keeps them in view without
  refusing writes. Nothing migrates the existing rows.
- **Inputs keep the storage names; the reading comes back as `value`.** The columns were
  always `value_num` / `value_text`; the tool took `value` / `text`, so the write spelling
  and the read spelling disagreed, and a qualitative reading arrived under a key named for a
  number. `record_measurement` now takes `value_num` / `value_text` at both layers, answers
  with `measured_on` too (a reading dated by the caller should be confirmable without a
  second call), and its refusals name the parameters that exist.
- **`merge_items` joins notes.** Blanks on the survivor used to be filled field by field,
  notes among them — which silently dropped the duplicate's note whenever the survivor had
  one of its own, exactly the case where both sides hold something worth keeping. Keep's
  text now comes first, then discard's, newline-joined; the tombstone keeps the discarded
  text on its own row, so the joined pair stays separable.
- **A grid refusal lists the rows that actually exist.** A box whose labels skip `I`
  answered "rows are A..J" about a grid with no `I` — pointing at the very row the caller
  reached for, so the retry picked the same well. An overridden axis is now spelled out;
  only a plain run keeps the compact `A..I` form.
- **`delete_assay`, the last missing destructive call,** follows the family rule: clean or
  refuse with the list. It refuses while any reading names the assay and lists those
  readings by id — and says why the blocker cannot be cleared from here ("readings are
  immutable"), rather than handing over a to-do list no tool can act on. Hard delete, whole
  table replacement, `{assay_id}` back. Consequence to remember: an assay with history is
  permanent from the surface, which is the honest reading of immutability but does mean the
  `qa_smear` left by the live pass needs Python to remove, assay and reading together.
- **`Service.get_contents`, so the Python route reads like the tool.** The binding audit
  found exactly one place where the two surfaces disagreed on a name — `tools.get_contents`
  calling `Service.contents` — which is the kind of seam that makes a dashboard author guess
  wrong. Renamed at the service, its one caller, tests and docs; the parity claim is now
  boring: 35 tools, 34 of them same-named methods, the odd one out being `count_items`'
  deliberate detour through `_matching_items` rather than a capped page.
- **Capped reads say what they left out.** `search_items` answered a bare list, so a model
  that passed `limit=20` could not tell twenty tubes from the freezer — and would chart the
  first page and call it the room. It now answers `{matched, offset, truncated, rows}`,
  matching the three searches that already had envelopes, and every one of the four takes
  `offset`. `truncated` means *another page exists*, not *this page came back full*: the two
  differ on the last page only, and getting it backwards is what makes a caller fetch
  forever. Past-the-end answers empty rather than raising, so a poller whose rows were
  deleted draws nothing instead of crashing; a negative window is refused by name.
  `run_query` gained the same `offset`, and `inventory-mcp query --offset` with it.
  Consequence worth stating: this changed `search_items`' answer shape at both layers, so
  ~75 test call sites moved to `["rows"]`.
- Measured across the review round that produced those six: surface 34 → **35 tools**,
  tests 284 → **306**. `INSTRUCTIONS` grew 1853 → **2490 B** for the two conventions
  (readings in and out, times are UTC), which is once per session rather than once per
  tool load. A reading row grew by one nested object (~35 B) and the `record_measurement`
  receipt by two fields; nothing else on the wire changed size, since the other five were
  refusals, receipts and wording.
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
  copy of `.devdb` with the old header, so it degrades rather than lying. Confirmed
  on the live scratch folder 2026-10-14: both warnings appear there, and
  `item_links.csv not present in storage; treating as empty` with them, so the four
  derivation edges are still sitting under the old `item_parents` name. Nothing broke;
  reads degraded exactly as designed.

## Deferred by agreement

Legacy Airtable importer, bulk import, sequencing runs/datasets/pooling entities, an
HTTP facade for dashboards.
