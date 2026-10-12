---
name: databricks-query
description: "Run SELECT-only queries against gold/silver Unity Catalog tables (or inspect schema/metadata) through the governed databricks-select command, never a direct SDK call. Establishes gold-table facts for impact analysis and executes test-plan assertions. Use when: query gold table, run SQL on Databricks, check a number, inspect table schema, count rows, validate data, sample a gold/silver table, list tables in a schema, run a test-plan assertion."
---

# Query Databricks (`databricks-select`, governed, read-only)

Rules on catalogs, the `SELECT`/`WITH`/`DESCRIBE`/`DESC`/`SHOW` allowlist, one statement per call,
SP auth and the governor are in agent-instructions.md. Domain detail (catalogs, schemas, standing
rules): `docs/ai/client/databricks-architecture.md`; catalog names and catalog ↔ branch:
`docs/ai/client/client.md` and `docs/ai/client/repo-registry.md`.

## Usage notes

- `databricks-select` does the guard, SP OAuth and full governor sequence itself. Never build an SDK or
  warehouse call, and never assume another deployment's warehouse id or host.
- `EXPLAIN` is not on the allowlist. One trailing `;` is fine.
- If a check needs SQL text that contains a forbidden keyword (e.g. reviewing a statement as a
  string), read the text; never execute it.
- Fully qualify `catalog.schema.table`; filter `IS_CURRENT_FLAG = 1` for current rows unless the
  question is about history.
- `<dev_catalog>` is read-only exactly like `<prod_catalog>` (no write grant on either). Never query it by default
  or as a routine check; with Dev named, presence check first and keep the query narrow, not
  exploratory.
- Output is redacted: a `[REDACTED]` cell means a credential-shaped value was stripped, not that data
  is missing. Don't work around it.

## Result

- Success: `{"ok": true, "columns": [...], "rows": [[...]], "row_count": N, "source": "..."}`
- Failure: `{"ok": false, "category": ..., "message": ...}`:
  - `credential` → hand back to an admin
  - `transient` / `timeout` → safe to retry once
  - `policy` → the guard blocked it on purpose

## Procedure

1. **Check the statement text yourself** against the allowlist before running; the guard is a
   backstop, not the only check.
2. **Run `databricks-select`** with the exact, fully-qualified statement.
3. **Read the JSON.** On `"ok": false`, surface category + message plainly; don't retry blindly or
   invent a result.
4. **Report** the exact statement, the catalog and the actual result (or failure). Impact analysis:
   feed the fact into that skill's output. Test execution: paste it into the matching
   `scratch/<ticket-id>-test-results.md`.
