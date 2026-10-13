# Scotri — workspace guide

A **near-total-read-only**, headless-server Claude Code workspace for an enterprise data platform
(**ADF → Databricks → Power BI/Fabric**). It reads tickets, analyzes impact, plans in prose and runs
read-only validation queries against a read-only mirror of every Prod branch. It has no product-repo
write path at all; its one write is the confirm-gated `jira-write` (comment / edit-own-comment /
create-Bug / attach-file). Everything else stops at paper and query results. The workspace is one
self-contained, portable folder with no dependency on where sibling workspaces live.

## Operating rules — always in force

@agent-instructions.md

## This deployment — your name, catalogs, projects, repo keys, components

@docs/ai/client/client.md

## Command reference — exact flags, output paths

@docs/ai/command-reference.md

## Skills

| Stage | Skill | Output |
|---|---|---|
| Requirement understanding | `jira-intake` | requirement summary + gaps/ambiguities |
| Bug verdict (**iff** `issuetype` is Bug) | `bug-investigation` | confirmed bug / not a bug / inconclusive + root-cause category + confidence; **stops here unless confirmed** |
| Impact analysis (all repos, Prod by default) | `impact-analysis` (+ `lineage-trace`, `fabric-read`, `adf-databricks-read`) | work order |
| Implementation plan | `implementation-plan` | advisory plan document |
| Test plan + execution | `test-plan-and-results` (+ `databricks-query`, `dax-debug`) | executed test results |
| Optional proposal | `propose-change` | `.patch` + modified copy in `proposals/<ticket-id>/` |
| Optional diagram (any stage, or "draw the model") | `er-diagram` | Mermaid `erDiagram` in `scratch/<id>-er-diagram.md` + the final answer (API `diagrams` field) |
| Handoff doc (skipped unless the bug verdict was "confirmed") | `handoff-doc` | one file, Bug or Story template |
| Optional Jira write, always permission-gated | `jira-write` | comment, new Bug key, or attachment id |

Supporting skills: `presence-claim` (before starting any work), `query-governor` (every governed
call). There is one persona, [`Scotri`](.claude/agents/Scotri.md), used for interactive delegation;
engagements load these instructions directly.

## Where to look

- [`docs/ai/client/repo-registry.md`](docs/ai/client/repo-registry.md) — which repos/branches exist and how each is
  read. Check it first.
- [`docs/ai/client/`](docs/ai/client/) — this client's domain knowledge (architecture, conventions,
  lineage). [`docs/ai/`](docs/ai/) — how the tools work, the same for every client.
- [`docs/ai/client/reports/`](docs/ai/client/reports/) — optional report documents (purpose, KPI
  definitions, RLS) per tree, mapped in `sources.yaml`; their KPI definitions also reach `kpi-lookup`.
  A dated snapshot: the mirror wins.
- [`OPEN_ITEMS.md`](OPEN_ITEMS.md) — the decision log's index. Numbered references in code comments
  (`OPEN_ITEMS.md #N`) point at items in it; record new decisions there.

## Maintaining this workspace

- Client-specific files (`client/profile.yaml`, `services/config/*.yaml`, `docs/ai/client/`) come
  from `python -m app.setup` (see [`SETUP.md`](SETUP.md)). Generated ones say so in their header:
  change the profile and re-render instead of editing them.
- Committing and pushing **this workspace's own repo** is ordinary maintenance, done only when the
  user asks (see the carve-out in `agent-instructions.md`). Product repos and the mirror: never.
- No real credentials in git. Only `services/.env.example` (placeholder names) is tracked; real values
  live in the gitignored `services/.env`, from a vault in a real deployment. The identity model is
  [`docs/ai/identity-model.md`](docs/ai/identity-model.md).
- No `.mcp.json`: Scotri consumes no MCP server. `services/app/mcp_server.py` exposes Scotri *as* MCP
  tools (`scotri_start`/`scotri_status`) alongside `services/app/api.py`; that is the opposite
  direction and adds no dependency.
- Agent-facing docs should state each rule once, as the rule. Put decision history in `OPEN_ITEMS.md`
  and git, not in the instructions the agent reads every turn.
