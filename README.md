# inventory-mcp

This is a lightweight Lab Inventory Management System designed with Model use in mind.

The database is backed by either locally stored csv files, or google sheet files stored
on a shared google drive (which additionally is how access control is handled).

This database contains an MCP layer, allowing models to interact with the database in
constrained ways, and a Services layer which provide python bindings to the same tools,
allowing easy creation of a dashboard for a user by those same models. Also, on google drive
the spreadsheet is given a landing page with functions and buttons 

Additionally, as the database is backed by human readable files, it is designed so that
reading and editing it manually is possible, if not recommended. If mal-formatted edits
are made manually, they will need to be rectified before further machine-made edits can be made

## Install

Download this repository to a local directory, create a python venv of version at least 3.10+
and install this project into the venv

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '.[drive,dev]'          # add ,[drive] for the Google Drive backends. Add dev if you'll be adding a dashboard
```

## Setup against the lab's Drive folder

**1. Google authentication**

You will need to authenticate with your google account with visibility over the shared drive
before your machine can use a remotely stored database

**1. Initialisation.**

Create a config file with the init command, specifying the use of the google-drive based backend
and a folder. If you wish to use one that already exists: configuration is read from
`--config <path>` (the flag goes before the subcommand), then`~/.config/inventory-mcp/config.json`,
then `$INVENTORY_MCP_CONFIG`.

If you are not authenticated, and use the drive backend, init will also ask for authentication to allow the
program to read from the google drive being used a backend 

```bash
inventory-mcp init --backend drive \
  --drive-folder <URL of database folder>
inventory-mcp show-config
```

`init` will open the folder its pointed to and create the tables needed for the database.

The `--seed` param can be passed to generate dummy testing data for the tables at initialisation. 
The `--reset-db` param can be passed to discards every entry in the database. and re-initialise it from scratch.

**Destructive Flags and the Admin flag**
Note, as these two flags (seed, and particularly reset) are not used outside a development context, and 
have the potential to wipe the database, and because users will likely be pointing this at an in-use shared 
google drive with write permissions, these two flags are locked behind the additional --admin flag, which declares that
you have the right to modify the database outside of standard user edits. 

With a drive backend the admin flag also requests permissions for the google scripts api. This is used to setup the 
landing page for opening the database as a file. In this landing page, a user can press a button to add/edit samples,
which needs scripts.

**2. Google authentication**

Authentication is actually handled at the init step, but if that fails for whatever reason, of if a user's token
expires and you need to re-authenticate, this command can do that.

```bash
source .venv/bin/activate
inventory-mcp auth --check    # 
```
To check authentication status, and 

```bash
inventory-mcp auth          
```
to actually authenticate.

This automatically opens an authorisation prompt in browser. If you want to do this manually, you can pass the
`--manual` flag and get a url to visit for authentication instead

The token is cached at `~/.config/inventory-mcp/oauth_token.json`, mode `0600`. 

The consent screen belongs to one OAuth *Desktop* client shared by everyone, and its JSON
ships at `inventory_mcp/client_secret.json`. That file holds a client id and secret, which is
supposedly (RFC 8252) how google intends apps to identify themselves. Permissions and authentication
are then granted by the user login, which creates a login token then present only on the user's machine.

A custom client for a new/different lab can instead be placed at `~/.config/inventory-mcp/client_secret.json`
which overrides the one this repository ships with. Changing this in a live environment requires all users to
reauthenticate

**3. Add this repo as an MCP serer for your model.** In many harnesses, the config for this looks as follows, but consult
your harness' specific documentation:

```json
{
  "mcpServers": {
    "lab-inventory": {
      "command": "/path/to/.venv/bin/inventory-mcp",
      "args": ["serve"]
    }
  }
}
```

other optional arguments and env variables may be included, such as '--config' to point to a config
file at a specific path

**4. Post Setup**

On initialising a fresh database, some things should be done first:

- **A fresh database has no assays.** So measurements such as the volume of a sample cannot be recorded. 
Every measurement names an assay and is checked against, so (`create_assay`) should be used to setup some basic measurements
- **A Drive read can time out partway through `init`.** If so, simply re-run init, and existing tables
will be skipped
- **Shared drives work, but only for a client that asks.** A file in a team drive is reported
  as *missing* unless the request passes `supportsAllDrives`. So a 404 on a folder you can see
  in the browser is a client bug, not a lost file — worth remembering before concluding that
  somebody deleted the database.

## Quickstart (local files, not drive)

```bash
# create the table files. Note, unlike in other commands that read the config file, this one *writes*
# the config file to this location
inventory-mcp init --root ./db --actor you

# look at it
inventory-mcp ls                  # items
inventory-mcp ls --containers
inventory-mcp occupancy           # fill levels
inventory-mcp doctor              # integrity report

# ask a question in SQL
inventory-mcp query \
  "select project_name, count(*) n from items_wide where class='sample' group by 1"

# publish flat views + a SQLite mirror for anything else to read
inventory-mcp export
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

## Database structure

There are twelve sheets, which are separate files in csv mode:

```
containers        Objects which store other objects, and their hierachy e.g. freezer > shelf > rack > box, plus fridge/bench
items             Objects that you store. Subclassed into e.g. samples, kits etc.
samples           Extends items, records data such as patient of origin
reagents          Extends items, records data such as volumes
kits              Extends items, records data such as uses remaining
consumables       Extends items
patients          Participants, linked to samples and projects. Optionally may contain demographic info
projects          For grouping other objects. Can be nested, with a project as a subproject of another
patient_projects  Table linking which participants are in which projects. (it's not 1-to-1, a patient can be in multiple projects)
item_links        Table linking items relations to each other. Such as a DNA tube extracted from a blood sample tube
assays            Table listing kinds of measurement that may be applied to items (e.g. volume). Needed for database reasons
measurements      List of measurements as applied to items, with history. 
```

Sheets mode includes two additional sheets which are used as an entrypoint for manual edits
```
Start             Contains a number of functions for editing the database. Enter fields in specified boxes, then press a button to submit
database_errors   Shows the output of a doctor command, to show what's currently malformatted in the database
```


Structural dev notes:

- **Items.** An item is an `items` row plus exactly one
  class row. Adding a new kind of item means adding a table.
- **Ids.** Everything has an id of the form `<prefix>_<8 chars>`
  from an alphabet missing `i l o u`.  Human readable naming lives in the `name`,
  `label` and `gml_id` fields, all of which are freetext.
- **Storage** Item storage is a foreign key into the containers table. 
  Position within a container, such as tube row/column, is optional but encouraged, and there
  for user convenience. It is not mandatory based on current practice that location isn't
  often recorded at greater granularity than the box level
- **Projects don't inherit samples.** For samples registered under a patient and a project,
  the chosen project must exactly match one the patient is also registered under.
- **Measurements are immutable.** They are intended as a recorded chain of past measurements
  on a given item. If one is invalid on a given assay, just make a new one to overwrite its
  place as the most recent one. Usually only the most recent is read. Though if you really need to
  nothing is stopping you deleting/editing the most recent one directly in the spreadsheet.

## Database Integrity Violations

Because this structure allows for files to be manually opened and edited in the shared
drive, certain things happen if manual edits break the from the proper database structure:

- **Reads degrade.** A corrupt row doesn't crash the reader. Valid rows come back
  with the problems listed as violations.
- **Writes refuse.** Validation runs against a candidate state first. If anything
  in scope is broken, the write is rejected with every violation listed
- **The `doctor` tool reports violations**, so they can be fixed

Locally, before any accepted write, the pre-write content of the tables being changed is
copied to `backups/<timestamp>-<operation>/`. This way, writes can be reverted by
copying files back. The `keep_backups` config caps how many generations survive.

A Drive backend has no local directory to copy into, so the write result reports
`snapshot: none` and the undo should be performed with Drive's own version history.

Writes also carry a revision token per table. If the file changed underneath —
the classic case being a spreadsheet save while the server holds the database
open — the write aborts instead of clobbering the human's edit. On the local
backend this is genuine compare-and-swap (advisory lock plus atomic replace);
across a network filesystem it degrades to detect-and-abort, which is still the
guarantee that matters.

## Configuration

JSON at `~/.config/inventory-mcp/config.json`, or `$INVENTORY_MCP_CONFIG`, or
`--config <path>` (the flag must come before the subcommand).

| Key | Default | Meaning                                                                         |
| --- | --- |---------------------------------------------------------------------------------|
| `backend` | `local` | `local`, `drive`                                                                |
| `local_root` | — | directory holding `tables/ exports/ backups/`                                   |
| `drive_folder_id` | — | Drive folder id, or a pasted folder URL — both parse                            |
| `drive_file_ids` | `{}` | logical table name → file id, when names collide                                |
| `token_path` | `~/.config/inventory-mcp/oauth_token.json` | OAuth token cache                                                               |
| `client_secret_path` | `~/.config/inventory-mcp/client_secret.json` | an OAuth client of your own; absent that, the one packaged with the app is used |
| `service_account_path` | `~/.config/inventory-mcp/service_account.json` | if this key exists it wins over the OAuth flow                                  |
| `cache_ttl_seconds` | `5.0` | how long a remote read may be served from cache                                 |
| `keep_backups` | `20` | pre-write generations retained                                                  |
| `actor` | derive | Your name, for wherever entries have an `updated_by` field                      |

## Command line Reference

| Command | Purpose                                                                                                                                                 |
| --- |---------------------------------------------------------------------------------------------------------------------------------------------------------|
| `init` | Initialises config and create database tables (`--root`, `--admin`, `--seed`, `--reset-db`, `--reset-config`, `--backend`, `--drive-folder`, `--actor`) |
| `auth` | Drive consent once (`--manual`, `--port`, `--force`, `--check`)                                                                                         |
| `serve` | run the MCP server over stdio                                                                                                                           |
| `doctor` | integrity report; exit 1 when errors exist (`--json`, `--quiet`)                                                                                        |
| `ls` | list items, or `--containers`                                                                                                                           |
| `occupancy` | fill levels per container (`[container_id]`, `--kind`)                                                                                                  |
| `enrol` | give each sample's pairing its own enrolment row (`--from-ancestors`, `--dry-run`)                                                                      |
| `query` | guarded read-only SQL                                                                                                                                   |
| `export` | flat CSV views + SQLite mirror + manifest                                                                                                               |
| `schema` | the field-by-field reference (`--json`)                                                                                                                 |
| `tools` | the live tool catalogue (`--json`)                                                                                                                      |
| `show-config` | resolved configuration                                                                                                                                  |

## Building a dashboard on this

This database also provides python bindings to all tools to allow easily building a dashboard
application. Read the exports or import `inventory_mcp.service`. See
[docs/dashboard.md](docs/dashboard.md) for the view-by-view reference.
