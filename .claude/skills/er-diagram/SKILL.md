---
name: er-diagram
description: "Draw a Mermaid erDiagram of a published Fabric/Power BI semantic model (TMDL relationships and columns from the mirror), a set of Databricks Unity Catalog tables (DESCRIBE, declared keys, joins inferred from producing notebooks and marked inferred), or the joins in a plan's draft DDL. Every column type is read from DESCRIBE/TMDL, never guessed. Writes scratch/<id>-er-diagram.md and repeats the block in the final answer. Read-only. Use when: ER diagram, entity relationship diagram, data model diagram, draw the model, show the relationships, schema diagram, star schema of this model, how do these tables join, diagram the joins in this plan/DDL."
---

# ER diagram (Mermaid `erDiagram`, drawn from TMDL or Unity Catalog)

A support skill, not a lifecycle stage: use it alone for "draw me the model", or inside
`impact-analysis`/`handoff-doc` when a picture helps the implementer. Its only write is a markdown
file under `scratch/`.

## Output contract

- One fenced block, info string `mermaid`, first keyword `erDiagram`, optional frontmatter title:
  ````
  ```mermaid
  ---
  title: Sales Analytics Model (<repo_key>/main)
  ---
  erDiagram
      vw_category_daily_sales }o--|| CalendarDate : "Date"
  ```
  ````
- Write it with `Write` to `scratch/<ticket-id>-er-diagram.md` (no ticket:
  `scratch/<model-or-schema-slug>-er-diagram.md`), **and repeat the same block in your final
  message**. The engine lifts mermaid fences from the final answer into the API's `diagrams` field; a
  diagram only in the file renders in the file viewer but API callers won't see it.
- At most 10 diagrams per answer, 50,000 characters each; a bigger block is dropped, not truncated.
  Split by subject area instead (see Size).
- Schema only: table, column and type names. **Never data values** (sample rows, amounts, per-person
  counts); the file is a shared deliverable.

## Mermaid syntax rules (one bad line makes the whole diagram an error)

- **Entity names**: letters, digits, `_`, `-` only. `'Customer Master'` → `Customer_Master`. When you
  rename, add a short "Name map" table under the diagram in the file.
- **Attributes**: `type name [PK|FK|UK] ["comment"]`, one per line inside `Entity { … }`. `type` and
  `name` are single words: `decimal(18,2)` → `decimal`, `array<string>` → `array`, `Customer Id` →
  `Customer_Id`. Comments are double-quoted with no `"` inside.
- **Relationship**: `Left <card>--<card> Right : "label"`. Left markers `||` (one) / `}o` (many);
  right markers `||` (one) / `o{` (many). `--` solid, `..` dashed.
- **Dashed means inferred, never declared.** Don't use it for anything else; mark an inactive Power BI
  relationship in the label instead.
- **Every relationship has both markers.** `A ..|| B` or `A ..o{ B` (no left marker) is the commonest
  mistake and rejects the whole diagram. Unknown left side = `}o`, not nothing.
- **Only joins are edges.** "View V is built from table T" is lineage: no `builds` edges with a guessed
  cardinality. Say which tables feed a view in the text, or draw the view as its own entity.
- **Labels are the join columns**, short (< ~80 chars): `"REP_CODE = user_code"`, plus
  `LEFT` or `inactive` when it matters. No row counts, ratios or figures in labels or comments; put
  findings and numbers in the text under the diagram.

## Never guess a column type or a key

Every type comes from a source you read: TMDL `dataType:` (A) or `DESCRIBE TABLE` (B/C). A type
inferred from a column name is fabrication (e.g. an `int` drawn as `string`, a `string` as `date`).

- "Only read the plan / don't modify X" limits what you write, not what you read. Governed read-only
  `DESCRIBE`/`SELECT` and mirror reads are always in scope here; run them unless the user explicitly
  says not to query at all.
- If you genuinely can't read a source (user said no queries, governor refused, table doesn't exist
  yet): write the type as `unchecked` and list the table under the diagram as `not read — <reason>`.
- A **proposed view** (from draft DDL) takes each column's type from the source column it selects,
  which you `DESCRIBE`d; a computed column (`SUM(...)`, `YEAR(...)`, `COALESCE(...)`) takes the
  expression's return type, with the expression in the comment.

## A. Semantic model (Fabric/Power BI) — authoritative

1. **Find the model**: `mirror-find --pattern "*<name>*.SemanticModel*"` (add `--repo-key` if the
   user named repo/environment). If several trees have that model, draw the one asked about, or say
   which you picked and why.
2. **Read** `<Model>.SemanticModel/definition/relationships.tmdl`. Each `relationship <guid>` has
   `fromColumn: <Table>.<Column>`, `toColumn: …`, optional properties. Defaults when absent:
   `fromCardinality: many`, `toCardinality: one`, `crossFilteringBehavior: oneDirection`,
   `isActive: true`.
3. **Map** (from-table on the left):
   - many → one (default): `From }o--|| To`
   - `toCardinality: many`: `From }o--o{ To`
   - `fromCardinality: one`: `From ||--|| To` (`From ||--o{ To` with `toCardinality: many`)
   - `isActive: false`: label `"Date (inactive)"`
   - `crossFilteringBehavior: bothDirections`: append `both`, e.g. `"Station (inactive, both)"`

   Label = join column(s): `"Date"`, or `"Store = Customer Id"` when names differ.
4. **Read the tables drawn**: one `batch` of `mirror-read --as-text` on each
   `definition/tables/<Table>.tmdl`. Take `column <name>` with `dataType:`; `isKey: true` → `PK`;
   each relationship's from-column → `FK`.
5. **Leave out** auto date tables (`LocalDateTable_*`, `DateTableTemplate_*`) and their relationships,
   and model-internal tables (`param_*`, `Role Parameter *`, measure-only tables with no columns). Say
   in one line under the diagram what was left out and how many.

## B. Databricks Unity Catalog tables — keys often undeclared

1. **Columns**: one `batch` of `DESCRIBE TABLE <prod_catalog>.<schema>.<table>` per table.
2. **Declared keys** — the only edges drawn solid:
   ```sql
   SELECT tc.table_name, tc.constraint_type, kcu.column_name, rc.unique_constraint_name
   FROM <prod_catalog>.information_schema.table_constraints tc
   JOIN <prod_catalog>.information_schema.key_column_usage kcu
     ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema
   LEFT JOIN <prod_catalog>.information_schema.referential_constraints rc
     ON tc.constraint_name = rc.constraint_name
   WHERE tc.table_schema = '<schema>' AND tc.table_name IN ('<t1>', '<t2>')
   ```
   Empty is normal: "nothing declared", not "no relationships".
3. **Inferred joins**: find the producing notebooks (`adf-databricks-read`; `mirror-grep --repo-key
   databricks` on the table name) and read their join predicates. Draw each **dashed**, labelled with
   the columns, with `%% inferred from <notebook path>` on the line above. Cardinality is a guess
   unless checked: if it matters, run `SELECT COUNT(*), COUNT(DISTINCT <col>) FROM …` and say so in
   the file; otherwise draw `}o..o{` (unknown) rather than inventing `||`.
4. Legend under the diagram, one line: solid = declared constraint, dashed = inferred from notebook
   joins.

## C. Joins in a plan or draft DDL (tables/views that may not exist yet)

1. Parse every `JOIN ... ON` (and `WHERE` filters / `ROW_NUMBER() ... rn = 1` restricting a side).
   Each becomes a **dashed** edge with `%% inferred from <file>, <step>` above it.
2. `DESCRIBE` every **existing** table the DDL reads (one `batch`) and run B step 2's constraints
   query on them. Proposed views get columns from their `SELECT` lists.
3. **Check the "one" side** of every join you'd draw `||` or `o|`, and every join onto an SCD2 table
   (`IS_CURRENT_FLAG`, `VALID_FROM`/`VALID_TO`): `SELECT COUNT(*), COUNT(DISTINCT <join cols>) FROM
   <table> [WHERE <the DDL's own filter on that side>]`. An SCD2 join with no current/latest filter
   and differing counts fans out: draw it many and report it as a finding (it usually breaks a "one
   row per X" design and row-count tests).
4. Draw existing tables and proposed views in separate diagrams (or clearly titled groups) when that
   keeps each under the size limit; say which is which.

## `diagram-lint` before answering (required)

You can't see the render. After writing the file run `diagram-lint --file
scratch/<id>-er-diagram.md`. Fix every error it reports (by file line) and re-run until `"ok": true`;
fix warnings too (over-long label, relationship to an entity with no block). Only then copy the
blocks from the linted file into your answer (don't retype). If it still fails after a couple of
attempts, say so and name the lines; never present a diagram you know won't render as fine.

## Size

About 25 entities; per entity the key/join columns plus up to ~8 others (add a last attribute
`string more_columns "N more"` when you drop some). Bigger models: one diagram per subject area (a
fact table with its dimensions), each its own block and title, and list in the file what each covers.
Never silently drop a relationship to make it fit; say what was left out.

## What to say alongside it

- Source: repo key + branch + model path (A), or catalog.schema + notebooks read (B/C), plus any
  mirror `freshness` warning.
- What was excluded, what was inferred, and any disagreement (a relationship column missing from its
  table's TMDL, a declared FK to a table that no longer exists): a disagreement is a finding.
- Never fabricate an edge or key. If a table couldn't be read (mirror mid-sync, query refused,
  governor queue), draw what you could and list the rest as `not read — <reason>`.
