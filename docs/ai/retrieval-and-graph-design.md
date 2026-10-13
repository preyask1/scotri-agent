---
description: "Design note, nothing built: where retrieval (RAG), a lineage graph store, and a System One classifier (TypeSafe's Jev) would and would not fit in this workspace, ranked against where the token and latency cost actually sits. Written 2026-09-27 in response to a direct question about speed, result quality and token optimisation. Every option here is advisory and undecided — the two that are worth pursuing have matching rows in OPEN_ITEMS.md."
last-validated-commit: n/a — design note, not a registry; supersede it rather than patch it if the lifecycle's cost profile turns out different once it runs end to end
---

# Retrieval, a lineage graph, and Jev — where each one fits

**Status (updated 2026-10-12): §1 and §2 are built; §3 (Jev) is not.** The lineage graph (§1) is
built and now generated from the mirror end to end, with KPI definitions on measure nodes
(OPEN_ITEMS.md #37; storage on Azure decided in #15). Retrieval (§2) is built as BM25 over
history, learnings and `docs/ai/`, indexed one heading section at a time (#38; embeddings are #51).
The reasoning below is the original 2026-09-27 note, kept as written.

## Start from where the cost actually is

Picking a technology before naming the bottleneck is how a platform accumulates infrastructure it
doesn't need. In this lifecycle the cost is concentrated in one stage:

**`impact-analysis` reads file content into the model's context to answer a reachability question.**
TMDL tables, notebook source, pipeline JSON, across the platform's repos. Every other stage is comparatively
cheap: `jira-intake` reads one ticket, `bug-investigation` reads a handful of targeted files and
runs a few governed queries, `implementation-plan` and `handoff-doc` mostly write rather than read.

So the useful question is narrow: **what stops the model having to read forty files to find out what
a change touches?** That question ranks the three options below, and it ranks them differently from
how they'd rank on general merit.

⚠️ **This is reasoned from where the work obviously concentrates, not from measurement.** There is no
per-stage token telemetry yet, because the lifecycle has never run end to end (the Anthropic key
isn't filled in — see OPEN_ITEMS.md). The interaction log would give real numbers once it has. Get
those numbers before committing to the graph build below; if they contradict this note, believe them.

---

## 1. A lineage graph store — the strongest fit, and already latent here

### Why it's the right shape

[`lineage-map.md`](client/lineage-map.md) **is a graph**: gold table ⇄ producing notebook ⇄ consuming
Fabric model, hand-maintained in markdown, with 🔁 markers on edges nobody has re-verified. That is
a graph database with no database. `lineage-trace`'s own instructions are a traversal algorithm
written as prose for a model to execute by reading files.

Nodes: ADF pipeline, dataset, linked service, trigger; Databricks notebook, bronze/silver/gold
table, column; Fabric semantic model, table, measure, report, RLS role; Jira ticket; handoff doc.
Edges: `produces`, `consumes`, `feeds`, `references`, `triggers`, `tested_by`, `investigated_in`.

Blast radius then becomes a two-or-three-hop traversal returning a complete subgraph, instead of an
LLM reading TMDL files and hoping it found them all.

### The property that matters most here

**Completeness becomes provable.** `agent-instructions.md` hard-rules "always trace impact across
every repo … never silently narrow the analysis without saying so." A traversal can report
*"47 nodes reached, 3 edges unverified since 2026-09-12."* A model sweeping files can only report
what it happened to find, and — worse for an unattended agent — it reports it with the same
confidence either way. This converts a behavioural promise into a structural guarantee, which is
the same move the `query_guard` allowlist made for the SELECT-only rule.

### Three open items it also addresses

- **The Fabric migration.** Model the legacy two-repo and new three-repo structures as parallel
  subgraphs. The unconfirmed cutover mapping (`repo-registry.md`) becomes an explicit *missing-edge
  set* that can be shown to the platform team, rather than an assumption nobody can see.
- **Mirror staleness.** Stamp each edge with the sync run that produced it. Per-edge freshness is
  strictly better than the single `status.json` timestamp the open item proposes surfacing.
- **`lineage-map.md`'s 🔁 rows.** "Inherited, not re-confirmed" stops being a markdown convention
  maintained by hand and becomes a queryable property of the edge.

### Build it small

Populate from the mirror as a step in `app/sync_service.py`, which already runs on a schedule and
already has the files open. **Do not buy Neo4j on day one.** At a handful of repos this fits in SQLite with
recursive CTEs, or a NetworkX graph rebuilt per sync and pickled — no new infrastructure, no new
credential, no new vendor, which matters in a workspace whose premise is one portable folder
(`CLAUDE.md`, "Not in this workspace, on purpose").

Behind a port (`ports/lineage_graph.py`) like everything else, so the storage choice stays a
one-adapter swap if it outgrows SQLite.

---

## 2. Retrieval (RAG) — valuable, but not where the instinct puts it

### The trap

The instinct is "RAG over the mirror, so the agent stops reading whole files." **Be careful with
that one.** Retrieval returns *similar* content, not *complete* content. A top-k retriever that
misses the fifth consumer of a gold table produces exactly the silent-narrowing failure the hard
rules forbid — and it fails quietly, with a confident, well-formed, incomplete answer. For an agent
running unattended, that is the worst available failure shape.

**The rule, if RAG is adopted at all:** deterministic exhaustive candidate generation (graph
traversal, or a plain index) first; retrieval only to *rank and order* that set. Never retrieval as
the candidate generator for impact analysis.

### Where it earns its place cleanly

1. **Past engagements — highest value, entirely unbuilt.** Every handoff doc, investigation verdict
   and test-result file this workspace produces is a corpus nothing currently reads back. *"Has
   anyone investigated this table before, and what did they conclude?"* This compounds: the system
   gets cheaper and better the more it has already done. It also has no completeness risk, because
   a missed prior investigation degrades the answer rather than falsifying it.
2. **Jira history.** Similar-ticket retrieval at intake, which sharpens the mandatory "Gaps and
   ambiguities" section by surfacing what turned out to be ambiguous last time.
3. **`docs/ai/` itself.** A small corpus currently loaded wholesale into every engagement.
   Retrieval here is a pure win — it is explanatory prose, not a reachability claim.

### One implementation note

Hybrid BM25 + embeddings, chunked per notebook cell and per TMDL table. Identifiers like `USP_*`,
`NB_*` and `<prod_catalog>.gold.*` are exact-match tokens that pure vector search handles badly, which is
why lexical search stays in the mix rather than being replaced by it.

---

## 3. Jev (TypeSafe System One) — real, narrow, and a question of timing

### What it is

A model that returns **typed decisions with calibrated probabilities instead of text**: you supply
state plus a set of typed questions, it evaluates them in parallel in one pass. Three primitives —
binary, choice (up to 255 options), ordinal score (2–10 levels). No string generation, nothing to
parse. Claimed at roughly 70–500 ms and ~200× faster / ~400× cheaper than an LLM on classification.

### Where it would fit

Front-door routing: Bug or Story? Which repos does this plausibly touch during the migration
transition? Does this ticket need an engagement at all?

### Why the timing is the actual answer

Classifying **one** ticket is not a bottleneck — the LLM does it inside a call it is already making.
Jev's economics only bite when classifying **hundreds**, which is precisely the **scheduled backlog
sweep** trigger path in OPEN_ITEMS. Adopt it before that exists and you buy a third-party
dependency, a new credential, and an ungoverned outbound call, to optimise something that currently
costs nothing.

**So: sequence Jev after the sweep, not before.**

### Two structural reasons it must never produce the bug verdict

This confirms the position already recorded in OPEN_ITEMS (2026-09-26) rather than revisiting it:

- `bug-investigation` requires confidence "tied explicitly to what was actually checked" at a cited
  hop. Jev cannot observe a query result or notebook logic, so its confidence would be untethered
  from evidence — which collides with the hard rule against fabricating a query result or test
  outcome.
- **Jev cannot abstain.** It fills a predefined answer space and always returns a choice. This
  lifecycle has *"inconclusive"* as a first-class, complete verdict. That is a structural mismatch,
  not a threshold to tune.

Also worth carrying forward from its own published guidance: **calibration is not correctness** — a
high-confidence answer can still be wrong — and its documented anti-patterns include *"use
deterministic code first; insert it only where semantic understanding is necessary."* Which means it
is explicitly the wrong tool for `domain/query_guard.py`'s decision. That is deterministic logic and
stays deterministic.

### If it is ever adopted

It is neither Databricks nor Fabric, so the query governor's current scope doesn't cover it. Rather
than answering that twice, fold it into the open decision on whether the governor's scope widens to
"every outbound integration" — the same decision the ungoverned-Jira item needs.

---

## The cheapest win is none of the above

Before adding any technology: **`config/model_registry.yaml` is built, service-ready, and read by
nothing.** Every stage resolves to the same model today. Routing `jira-intake` and triage to a
cheaper model while `impact-analysis` keeps the stronger one is a config edit against scaffolding
already paid for. The governor's result cache is in the same position — built, and unmeasured.

## Suggested sequence

| # | Move | Why now | Rough effort |
|---|---|---|---|
| 1 | Fill in the per-stage model registry | Already built; pure config; no new dependency | Hours |
| 2 | Lineage graph, populated from the sync | Biggest token win; makes completeness provable; no new vendor | Days |
| 3 | RAG over past engagements + `docs/ai/` | Compounds over time; no completeness risk in that scope | Days |
| 4 | Jev as a backlog-sweep pre-filter | Only once the sweep exists and there is volume to filter | Small, but gated |

Nothing above is decided. Items 2 and 3 have rows in OPEN_ITEMS.md; item 1 is already tracked there
as the multi-model routing item; item 4 amends the existing Jev row with the sequencing conclusion.
