# Status — 2026-10-06 end of day

Working notes for whoever picks this up next. The user-facing description lives in
[README.md](README.md); this file records state, decisions and open questions.

## Where we are

The database design, validation layer, service layer, MCP tools, CLI, exports and
local CSV backend are done and verified. The Google Drive backends are written and
exercised offline against a fake Drive, but **have never touched a real folder** —
that pass is blocked on one decision (below) plus credentials.

Nothing here is shaped around the legacy Airtable export. Importing it is a separate
task with its own problem (thousands of rows, dangling references), and folding it in
would have bent the schema for the wrong reason.

## Verified right now

```
.venv/bin/python -m pytest -q          # 105 passed
inventory-mcp doctor                   # 0 error(s), 0 warning(s) -> OK   (on .devdb)
inventory-mcp tools                    # 31 tools, real parameter schemas
inventory-mcp init --seed              # items=13 samples=10 measurements=18, integrity clean
inventory-mcp export                   # items_wide, occupancy, measurements_long,
                                       # lineage, patient_projects, inventory.sqlite
```

Use the project venv (`.venv/bin/...`), never the global interpreter.

## The open decision: how to authenticate to Drive

Everything else about the Drive work is settled; this is the only thing standing
between here and the live pass. Hard constraint: **there is no Google-API path that
avoids Google Cloud Console.** OAuth client ids, API keys and service-account keys are
all minted in a Cloud project; there is no local generation and no self-serve route.
What varies is who touches the console and how often.

1. **Desktop OAuth** (`auth.py`, built and tested). One person does the console setup
   once, ever: enable Drive + Sheets APIs, External consent screen in Testing, create a
   **Desktop app** OAuth client, download the JSON to
   `~/.config/inventory-mcp/client_secret.json`. Every user then runs
   `inventory-mcp auth` and signs in with their institute account. Desktop-app client
   ids are not treated as secrets by Google, so that file can live in an internal repo.
   Friction: the "app isn't verified" interstitial on first consent, and refresh tokens
   expire after ~7 days while the app sits in Testing (`auth --force`).
2. **Service account** (also already supported — `service_account_path` wins over the
   OAuth flow). Key created once by the project owner, folder shared with that address.
   Zero prompts forever. Cost: writes are attributed to one robot identity in Drive's
   version history rather than the human who made them, which matters for something
   whose whole justification is auditability.
3. **No Google credentials at all.** Google Drive for Desktop syncs the folder using the
   account already signed in; point the existing `local` backend at the synced path. You
   keep real compare-and-swap and local `backups/`. Cost: requires the sync client on
   each machine, and two editors inside the sync window can still clobber — Drive
   resolves that by creating a conflicting copy rather than refusing a write.

My reading of the earlier exchange is that option 1 was chosen ("a standard RA won't
have a fancy GCP account, it'll just be their institute Google account"), and that the
confusion was about where `client_secret.json` comes from — a ten-minute one-time
console visit by someone, then a download button. If the intent was really zero console
involvement, option 3 is the only honest version of that, and the Drive stores become an
opt-in remote mode instead of the default.

## Test folder

`https://drive.google.com/drive/folders/16QfHIYtRsQBmtvs77Ke-zL5uvxBNUEm6`
(id `16QfHIYtRsQBmtvs77Ke-zL5uvxBNUEm6`) — agreed as a scratch folder this tool may
create and delete inside. Nothing has been written to it yet.

Live pass plan, in order: `auth` → `init --backend drive_sheets --drive-folder <url>
--seed` → `doctor` → then the adversarial cases specifically:

- someone edits a cell in the browser mid-session (expect the next commit to abort);
- two sessions writing concurrently (expect detect-and-abort, and measure how often it
  misses — this is the case the code cannot fully cover);
- a tab or file deleted from under us (must not resurrect it);
- network failure between read and write;
- `drive_csv` in the same folder, including whether Drive converts the upload.

Namespace anything created (`_scratch-inventory-mcp/`) and never overwrite a non-table
file. Offline fakes in [tests/test_drive.py](tests/test_drive.py) already cover all five
shapes; the live run verifies them against what Google actually does.

## What exists

| Path | Role |
| --- | --- |
| `inventory_mcp/schema.py` | the 12 table definitions, field by field |
| `inventory_mcp/validation.py` | reads degrade / writes refuse; `doctor` reports everything |
| `inventory_mcp/service.py` | every mutation, all through one `_apply` commit path |
| `inventory_mcp/store/` | `base` (revisions, `TableStore`), `local`, `drive` |
| `inventory_mcp/auth.py` | installed-app OAuth flow, manual relay, service-account precedence |
| `inventory_mcp/tools.py` | the 31 MCP tools |
| `inventory_mcp/query.py` | guarded read-only SQL over an in-memory SQLite mirror |
| `inventory_mcp/exports.py` | denormalised views + SQLite mirror + manifest |
| `inventory_mcp/seed.py` | development fixture, built by calling the service |
| `docs/tools.md` | tool conventions |
| `docs/dashboard.md` | how a third party reads this (not via MCP as an API) |

## Deliberate limits, restated so they don't get mistaken for bugs

- On Drive, concurrency is **detect-and-abort**, not compare-and-swap: no per-range
  precondition exists in the API. Two sessions that loaded the same revision both pass
  and the later write wins. Drive version history is the backstop for that case.
- CAS covers only the tables a commit touches. A hand edit to `patients` while we write
  `items` is invisible until the next load.
- No local `backups/` on a Drive backend — the write result reports `snapshot: none`.
- Reads may be `cache_ttl_seconds` stale; `0` buys read-your-writes at one API call per
  table per operation.
- Sheets re-parses cell values on input. Unrecognised renderings raise on the post-write
  verification read rather than persisting a silent mutation. Which coercions actually
  survive is what the live pass will pin down.

## Deferred by agreement

Legacy Airtable importer, bulk import, sequencing runs/datasets/pooling entities, an
HTTP facade for dashboards.
