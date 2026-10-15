# Tool catalogue

The authoritative catalogue is generated from the live registry — this page does
not duplicate it, so it cannot drift:

```bash
inventory-mcp tools          # signatures + descriptions
inventory-mcp tools --json   # name, description, JSON schema per tool
```

Anything else on this page is commentary about *how* to use the tools, not a
specification of them. If this page and `inventory-mcp tools` disagree, the
command is right.

## Conventions every tool obeys

- **ids are minted here.** `smp_`, `rgt_`, `kit_`, `csm_`, `ctr_`, `pat_`,
  `prj_`, `asy_`, `msm_`, `lin_` followed by eight characters from an alphabet
  with `i l o u` removed. Never invent one — search for it. `GML001631` is a
  label people read aloud; it is not a key and the server will tell you so when
  one turns up in a foreign-key field.
- **every parameter says what it wants, and the ones with a fixed set of answers
  say the set.** Fifty-eight of the create tools' parameters shipped as a bare name
  and a type, which asks a caller to guess somebody else's schema; where validation
  closes the set — container kinds, sample types, sexes, affected statuses, project
  statuses, assay quantities and result types, link roles — the list travels in the
  JSON Schema as `enum`, where a client can read it before sending. The check itself
  stays in the service rather than becoming a `Literal`: pydantic rejects a `Literal`
  mismatch before the tool runs, and what comes back is `Error executing tool x: 1
  validation error` — the shape of a crash — instead of the row and rule at fault.
- **furniture and boxes are made by different tools.** `create_container` declares how
  many slots a freezer, shelf, rack, fridge or bench has; `create_box` declares a grid
  and works its own capacity out from it, including the boxes whose rows skip `I`. Only
  the narrow one insists on a parent, because a box inside nothing cannot be found.
- **editing is three verbs, and the id says what is being edited.** `edit_name` changes what
  an entry is called — a box's or a participant's label included; `edit_notes` replaces its
  free text, which a project keeps in its `description`; `edit_data` sets one class-specific
  field on an item. Neither a table nor a kind is an argument: the id resolves the row, and
  for `edit_data` the item's own class decides whether it has a `price` or a `collected_on`.
  Names and labels are mutable and are never keys; a GML code counts as data, so it goes
  through `edit_data`.
- **positions are optional and never implied.** An empty position means nobody
  recorded which well, not that the well is free. Occupancy ignores them entirely:
  a tube nobody placed fills a slot exactly like one filed under `A1`, so fill rates
  stay true whether or not the grid was ever written down.
- **capacity counts slots, and a slot takes one thing whatever it is** — a tube in
  a well, a box in a rack. So `occupied` includes nested containers: a rack holding
  six boxes is full, not empty. The numbers are only ever added across containers
  holding the same kind of thing, because five box positions and a hundred wells are
  not two hundred of anything.
- **status follows the tube, not the shelf.** `consumed` releases the slot the item held
  and the well label with it — nothing remembers where it sat, because that well is free
  for someone else now. Marking it `present` or `partially_consumed` again therefore names
  a container to go back into; neither direction asks for a second confirmation, since
  reversing a status is how a mis-record gets fixed.
- **writes either fully apply or don't happen.** Validation runs against a
  candidate state before anything touches storage. A refusal returns every rule
  violated and leaves no partial write, no backup generation, no trace.
- **a write answers with a receipt, not a commit report.** The id that was minted, what
  the entry is, and anything the server worked out for itself — a box's capacity from its
  grid, a supply's opening reading and the minute it was true, which tubes a withdrawal
  left unassigned. What used to come back was the stored row inside
  `operation` / `touched_tables` / `backup` / `warnings`: the first two restate the call
  and sometimes misattribute it (`create_box` reports `create_container`, because it
  delegates), the backup path names a server filesystem nobody here can open, and
  `warnings` carried nothing a tool call could act on — only the CLI's `enrol` migration
  filled it. Where the service now does fill it, `record_measurement` naming the reading
  it retired, the same fact arrives in the receipt itself rather than in a field the
  reply trims. Everything else about the entry is `get_item`'s to say.
- **measurements are immutable, and appending is the only move.** Nothing edits a
  recorded value in place, and there is no correction tool: a wrong reading is
  answered by recording it again. The new row writes down which reading it takes over,
  in `supersedes`, so retirement is a fact in the data rather than something inferred
  from row order. Pass an id when you are correcting a value that was wrong; leave it
  blank and the current reading for that item and assay is retired for you, with the
  reply naming which. Reads report one reading per item and assay — the last one
  *written*, which is not necessarily the one carrying the latest date — and
  `include_historical` returns the series that retirement removed.
- **units are checked against the assay.** `record_measurement` refuses a volume
  where a concentration is expected rather than storing something no later query
  can compare against. A reading then comes back twice: `value` and `unit` exactly as
  recorded, which is what prose quotes back, and `canonical` in the assay's own unit,
  so comparing two tubes written in different units of one quantity is a lookup rather
  than arithmetic in someone's head. A numeric assay takes `value_num`, a qualitative
  one `value_text`; both read back as `value`.
- **times are UTC to the minute, stored with `Z` on the end.** ISO 8601 puts the zone at
  the end of a timestamp rather than in front of it, so that is the spelling the column
  keeps and the answer comes back in. Input may say it out loud (`...Z`, `...+10:00`) or
  not at all — an unmarked value is read as UTC, which is what the server stamps itself,
  and costs a `doctor` warning rather than the row. A bare date is refused rather than
  answered with midnight. Plain dates (`collected_on`, `expires_on`) name no time of day
  and carry no zone.
- **enrolment is exact.** A sample may only pair a participant with a project that
  appears in their own enrolment rows — a subproject of one is not on that list. The
  project tree is how work is organised, not a record of what anyone consented to, so
  widening a participant's scope is always an explicit `assign_patient_project` and
  never a side effect of creating a folder. `create_patient` takes the first project;
  the tool adds further ones, and `unassign_patient_project` removes one — settling the
  participant's samples in the same write, either re-pointed at another project they are
  enrolled in or left with no project, which is legal and findable. Databases written
  while scope inherited downward are migrated by
  `inventory-mcp enrol --from-ancestors`.
- **a delete is either clean or it refuses with the list.** There is no force flag
  anywhere: an operation that cannot be completed without breaking something else is
  refused, not overridden. `delete_item` retires any item whatever its status — discarding
  a tube that still holds material happens, and no later step would catch it — and leaves a
  tombstone so provenance survives. `delete_container` refuses while anything lives inside
  and names each occupant by id, so the caller can empty it and retry; a retired row still
  naming the place is cleared in the same commit, because nothing can move a tombstone and
  leaving the reference would dangle a foreign key over every later write. `delete_patient`
  needs nothing cleared away first — a sample does not need a participant — and sweeps their
  enrolments and their samples' links, retired tubes included, since a foreign key is
  checked whether or not the item is alive. `delete_project` re-points each participant's
  samples at the one project they have left, and refuses where that is not a deduction: a
  subproject still hanging off this one, a participant with no other project, or one with
  several where the destination is a decision rather than a guess. `delete_assay` removes a
  measurement kind nobody has measured with; one with readings stays, because nothing here
  discards a reading and the definition is what makes those numbers comparable.
  `merge_items` joins the two notes and re-points measurements and derivation links before
  retiring the duplicate. A tombstone then accepts
  no further writes: `move`, `set_status` and the edit tools all refuse it.
- **there is no query language on the surface.** Filters are named arguments with
  declared types and bounds, so every question a model can ask is one the server can
  validate, bound, and answer in a payload of predictable size. Ad-hoc SQL is left
  to `inventory-mcp query` for humans and to `inventory_mcp.service` for code someone
  owns; cross-tabs and trends belong with the exports, which are published by
  `inventory-mcp export` rather than by a tool call. Publishing bytes for readers is a
  data-plane job — see [dashboard.md](dashboard.md) — so nothing on the surface takes a
  filesystem path at all.
- **a capped answer says what it left out.** The four searches reply with `{matched,
  offset, truncated, rows}`: `matched` is what the filters found in total, and `truncated`
  means another page exists rather than that this one came back full. Ask for the rest by
  holding the filters still and stepping `offset`; an offset past the end answers empty
  rather than failing, and a negative window is refused by name. A limit on its own would
  be a ceiling on what a caller can ever see — and a model that cannot tell a page from a
  result draws a chart out of twenty tubes and labels it the freezer.

## Groups

| Group | Tools |
| --- | --- |
| Inspect | `doctor` `search_items` `get_item` `count_items` `search_containers` `get_contents` `get_occupancy` `search_patients` `get_patient` `list_assays` `search_projects` `get_measurements` `get_linked_items` |
| Register | `create_container` `create_box` `create_sample` `create_supply` `create_patient` `create_project` `assign_patient_project` `create_assay` |
| Change | `move` `set_status` `edit_name` `edit_notes` `edit_data` `record_measurement` `link_items` |
| Destructive | `delete_item` `delete_container` `delete_patient` `delete_project` `delete_assay` `merge_items` `unassign_patient_project` |

## Reading the refusals

A refusal says which of two things happened, because they call for opposite responses.

**Your call was wrong.** The entries named are the ones it would have written:

```
WRITE REFUSED — nothing was saved. This call would leave invalid:
  - ERROR   samples[smp_zv3k51qn].sample_type: 'plasma_smoothie' is not one of
    whole_blood, pbmc, serum, plasma, tissue, ...
```

**The database was already broken.** Nothing is named, because none of it is yours:

```
WRITE REFUSED — nothing was saved. Nothing in this call is at fault: the database is
locked by 1 violation(s) that were standing before it, most often the residue of
someone editing the files by hand. Run doctor for the failing tables and rows. Do not
repair them on your own initiative -- an entry that looks illegal may well be a
deliberate change -- so check with whoever edits these files first.
```

Both at once gives the first message with a count of the second appended.

The split is computed, not guessed. `_apply` validates the candidate state as it always
has; only when that fails does it validate the stored state too, partitioning the
violations into those already standing and those this write introduces.
`ValidationError.introduced` and `.preexisting` carry the result, while `.violations`
still holds everything — the CLI and the test suite see exactly what they used to.

Why bother: any standing violation blocks *every* write, so quoting the full list handed
a caller a set of rows it never touched, indistinguishable from its own mistakes and
reading like permission to tidy them. In a hand-edited file a fair share of what looks
illegal is somebody's intended change, and tidying one back to its last server-written
value destroys it without leaving a trace. So the surface names what you broke, and sends
you to `doctor` for what somebody else did.

The command line prints the whole list regardless. Someone at a terminal wants the detail
and will not bulk-correct another person's rows on the strength of it.
