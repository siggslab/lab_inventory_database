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
- **positions are optional and never implied.** An empty position means nobody
  recorded which well, not that the well is free. Occupancy ignores them entirely:
  a tube nobody placed fills a slot exactly like one filed under `A1`, so fill rates
  stay true whether or not the grid was ever written down.
- **capacity counts slots, and a slot takes one thing whatever it is** — a tube in
  a well, a box in a rack. So `occupied` includes nested containers: a rack holding
  six boxes is full, not empty. The numbers are only ever added across containers
  holding the same kind of thing, because five box positions and a hundred wells are
  not two hundred of anything.
- **writes either fully apply or don't happen.** Validation runs against a
  candidate state before anything touches storage. A refusal returns every rule
  violated and leaves no partial write, no backup generation, no trace.
- **measurements are immutable.** Corrections append via `correct_measurement`
  and mark the original superseded. Nothing edits a recorded value in place.
- **units are checked against the assay.** `record_measurement` refuses a volume
  where a concentration is expected rather than storing something no later query
  can compare against. Values are converted to the assay's canonical unit on read.
- **destructive calls are guarded.** `delete_item` requires the item to be
  consumed unless `force=true`, and tombstones rather than erasing.
  `delete_container` refuses a non-empty container. `merge_items` re-points
  measurements and derivation links before retiring the duplicate.

## Groups

| Group | Tools |
| --- | --- |
| Inspect | `doctor` `search_items` `get_item` `count_items` `list_containers` `container_tree` `get_contents` `get_occupancy` `search_patients` `rollup_by_project` `get_measurements` `get_linked_items` `run_query` |
| Register | `create_container` `create_box` `create_sample` `create_supply` `create_patient` `create_project` `assign_patient_project` `create_assay` |
| Change | `move_item` `set_status` `update_fields` `record_measurement` `correct_measurement` `link_items` |
| Destructive | `delete_item` `delete_container` `merge_items` |
| Publish | `export_data` |

## Reading the refusals

A refused write comes back as text, not a traceback:

```
WRITE REFUSED — nothing was saved.
  - ERROR   samples[smp_a1b2c3d4].project_id: project 'VEXAS' is not one the
    participant is enrolled in (or an ancestor of one)
```

That message is the interface. It names the table, the row, the field and the
rule, because the person receiving it is usually mid-task at the bench with a
tube in one hand and needs to know which of the two things they just typed is wrong.
