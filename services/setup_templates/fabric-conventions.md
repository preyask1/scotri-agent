---
description: "Power BI / Fabric conventions for $name: folder map, PBIP formats, connection parameters, table and RLS naming, clone patterns. Written by people; created once by app.setup render."
---

# Fabric conventions — $name

> **TODO (people):** fill in the folder map and naming rules. Example:
> `examples/acme-retail/docs/ai/client/fabric-conventions.md`.

## Folder map

| Repo / folder | Domain |
|---|---|
| TODO | TODO |

## PBIP file formats (standard)

| File | Purpose |
|---|---|
| `definition.pbir` | report → model binding (`datasetReference.byPath.path`, can point cross-folder) |
| `definition/tables/*.tmdl` | columns, DAX measures, M partition (source table) |
| `definition/expressions.tmdl` | connection parameters (absent in hardcoded-source models) |
| `definition/relationships.tmdl` | relationships |
| `definition/roles/*.tmdl` | RLS roles |
| report layout | legacy `<Report>/report.json`, or PBIR `definition/pages/<id>/visuals/<id>/visual.json` |

For "what does this report show", use `report-bindings`, never a whole `report.json`.

## Naming

- **TODO:** table name patterns (fact/dim prefixes, `*_rls`, `param_*`), RLS hierarchy columns and
  their level order, connection parameter names.
- **TODO:** clone-per-region or clone-per-audience report sets, and whether clones have drifted.
