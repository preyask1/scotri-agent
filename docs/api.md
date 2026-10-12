# Scotri agent-service — API reference

Scotri exposes one engagement engine over two transports:

| Transport | Entry point | Default port | Source |
|---|---|---|---|
| HTTP (REST/JSON) | `uvicorn app.api:app --host 0.0.0.0 --port 8000` | 8000 | `services/app/api.py` |
| MCP (streamable-http) | `uvicorn app.mcp_server:asgi_app --host 0.0.0.0 --port 8100` | 8100 | `services/app/mcp_server.py` |

Run both from inside `services/` with the venv active. Either can run without the other.
FastAPI also serves interactive docs at `/docs` and the OpenAPI schema at `/openapi.json` on the HTTP port.
The developer portal for both transports (`docs/developer/portal.html`: quickstarts, MCP client setup, and a
live "Try it" console) is served unauthenticated at `/portal` on the HTTP port.

Scotri is read-only: no endpoint here writes to a product repo, the mirror, or any data backend. The
only mutation an engagement can cause is the confirm-gated `jira-write` exception, and that is decided
inside the agent run, not through this API. The one write this API itself exposes is recording a human
verdict review (`POST /v1/engagements/{job_id}/review`).

---

## Conventions

### Authentication

Every endpoint except `GET /health` requires a Bearer token validated by the Azure AD identity provider:

```
Authorization: Bearer <token>
```

A missing or malformed header returns `401 {"detail": "Missing Bearer token"}`. An invalid token is
rejected by the identity provider with the error shape below. The validated token yields a *principal*
(`subject`, `platform`, `roles`) that every ownership check uses; nothing the client sends in a body
can override it.

### Asynchronous jobs

An engagement can run for minutes. Creating one returns `202` with a `job_id` immediately; poll the
matching `GET` until `status` is terminal.

| `status` | Meaning | Terminal |
|---|---|---|
| `queued` | Accepted, waiting for a free slot (see Busy: the engagement queue) | no |
| `running` | The agent is working | no |
| `succeeded` | Finished; `result` is populated | yes |
| `failed` | Errored or interrupted by a server restart; `error` is populated | yes |
| `cancelled` | Stopped or never started; `cancel_reason` says which | yes |
| `incomplete` | Ended without a complete result | yes |

#### Busy: the engagement queue

At most `MAX_CONCURRENT_ENGAGEMENTS` (default 3) engagements run at once, across the HTTP API and the
MCP server together (OPEN_ITEMS.md #32). A job that arrives when every slot is taken waits in one
first-in, first-out queue and starts on its own when a slot frees up; from then on it is an ordinary
`running` job with `progress`.

- The start response says `"queued": true` with `queue_position`. While it waits, a poll returns
  `status: "queued"` and a `queue` object: `{"position", "waiting", "running", "max_running"}`
  (`null` once it starts).
- Cancel it with the ordinary cancel route: it leaves the queue and never runs.
- Limits, each a setting: at most `MAX_QUEUED_ENGAGEMENTS_PER_USER` (2) waiting jobs per caller, a
  queue of at most `ENGAGEMENT_QUEUE_MAX_LENGTH` (20), and a wait of at most
  `ENGAGEMENT_QUEUE_MAX_WAIT_SECONDS` (1800). A job that waits longer is cancelled.
- A start refused at the door is answered `429` (the caller already has the most waiting jobs) or
  `503` (the queue is full), with `Retry-After: 60` and a body that carries the prompt:
  `{"ok": false, "category": "transient", "message", "reason": "user_limit" | "queue_full", "job_id", "prompt"}`.
  The job is recorded `cancelled` with `cancel_reason: "busy"`; nothing ran.

`cancel_reason` on a cancelled job is `user` (its owner), `admin` (an admin removed it from the
queue), `queue_timeout` (it waited too long) or `busy` (refused at the door). For every reason but
`user` nothing ran, so the prompt can simply be sent again; the poll and cancel responses carry it.
Waiting jobs survive an API restart. A running job whose process died is failed once its slot lapses
(about 90 seconds), and its slot goes to the next job.

#### Progress while it runs

Polling is the progress mechanism; there is no push or SSE stream (OPEN_ITEMS.md #39, decided
2026-10-04). Poll every 5 to 15 seconds. Once a job starts running, every poll response carries a
`progress` object (`null` while queued):

```json
"progress": {
  "phase": "verdict",
  "phase_label": "Investigating the reported problem",
  "phases_seen": ["starting", "intake", "verdict"],
  "tool_calls": 23,
  "started_at": "2026-10-04T09:00:02+00:00",
  "last_activity_at": "2026-10-04T09:03:41+00:00",
  "elapsed_seconds": 225
}
```

| `phase` | `phase_label` |
|---|---|
| `starting` | Starting |
| `working` | Working (a run that isn't following the ticket lifecycle, e.g. a debugging question) |
| `intake` | Reading the request |
| `verdict` | Investigating the reported problem |
| `impact` | Analysing impact |
| `plan` | Writing the implementation plan |
| `testing` | Running test queries |
| `proposal` | Drafting a proposed change |
| `handoff` | Writing the handoff document |
| `jira_update` | Preparing a Jira update |

The phase comes from the lifecycle skill the agent most recently started, so phases can be skipped
(a bug verdict of "not a bug" ends after `verdict`) and the list may grow. Show `phase_label` as-is,
and treat an unknown `phase` as "Working". A `last_activity_at` that stops moving for several minutes
while `status` stays `running` means the run is stuck; the 20-minute wall-clock limit ends it as
`incomplete`.

`progress` deliberately says nothing about **what** the run is looking at: no ticket, table,
notebook, pipeline, report, query or file. Any authenticated caller can poll a job, so that detail
is only in `/steps`, which is owner-only. `/steps` now also works while the job is running.

Once a job is finished, `elapsed_seconds` stops at the run's last activity instead of growing with
every poll. A run is also capped at `ENGAGEMENT_MAX_TURNS` agent turns (150; `0` = no limit) and,
if set, `ENGAGEMENT_MAX_BUDGET_USD`; hitting either ends it as `incomplete`.

**Answer checks.** Before a run is marked `succeeded`, the large numbers, fully-qualified table names and file
names in its answer are compared with what the run actually read. Anything found nowhere goes back to
the agent once, in the same session, to verify, label as derived or remove, and its reply becomes the
`result`. If that turn can't finish, the original answer is kept with a note listing the unsupported
values. `GROUNDING_CHECK=off` disables it. `result` and `error` are secret-redacted before they are
stored, and the run's presence claims are released when it ends.

**Restarts.** A job that was running when the service restarted ends as `failed`, not resumed:
resuming a half-done run unattended could repeat work it can't see it already did, including a Jira
write. The job keeps its agent session, though, so sending a follow-up with `resume_job_id` set to
the failed job continues with what it had already found.

**No completion notification.** A caller watching a job learns it finished by polling. Push
notification waits on the webhook/sweep trigger paths (OPEN_ITEMS.md #7), which are the runs with no
caller polling.

The job store keeps live jobs for 24 hours. Use `/v1/history` and `/v1/conversations/{id}` for durable
history beyond that.

### Errors

Two shapes, depending on where the error is raised.

Domain errors (`ScotriError`):

```json
{"ok": false, "category": "policy", "message": "…"}
```

| `category` | HTTP | Retry? |
|---|---|---|
| `credential` | 401 | No — needs an admin |
| `not_found` | 404 | No |
| `policy` | 403 | No — a guardrail working as designed |
| `timeout` | 503 | Often |
| `transient` | 503 | Yes |
| `configuration` | 500 | No — needs a config change |
| `unknown` | 500 | Report it |

Route-level errors (FastAPI `HTTPException`): `{"detail": "<message>"}` with 401, 403, 404, 409 or 422.
Request-body validation failures return FastAPI's standard 422 body.

---

## HTTP endpoints

### `GET /health`

Unauthenticated liveness check; pings Redis.

```json
{"ok": true, "redis": true}
```

`ok` and `redis` are `false` if Redis is unreachable. The endpoint never raises.

---

### `POST /v1/chat` — start a free-form engagement

Send any prompt an interactive Scotri session could take: a Jira key, a debugging question, a change
request. Scotri decides what to do by reading it. There is no per-capability endpoint.

**Body**

| Field | Type | Required | Description |
|---|---|---|---|
| `prompt` | string | yes | The request |
| `resume_job_id` | string | no | A prior chat `job_id` of yours whose agent session this turn continues (`403` if it's someone else's) |
| `conversation_id` | string | no | Server-issued conversation to group this turn under |
| `attachment_ids` | string[] | no | `upload_id`s from `POST /v1/uploads` to hand to the agent with this prompt (at most 10; your own uploads only — `404` unknown, `403` someone else's, both before a job is queued) |

Omit `conversation_id` on the first turn; the server issues one. On a follow-up pass `conversation_id`
(or `resume_job_id`). A `conversation_id` already owned by another principal returns `403`. An unknown
one is accepted.

If `resume_job_id` is unknown or has no session yet, the turn runs fresh rather than failing. Check
`resumed` in the response to tell. A `resume_job_id` belonging to another principal returns `403`:
resuming their session would hand this turn everything that session saw.

**Response `202`**

```json
{"job_id": "9f1c…", "resumed": false, "conversation_id": "b27a…", "queued": false}
```

When Scotri is busy: `"queued": true, "queue_position": 2`. Refused at the door: `429`/`503` as in
"Busy: the engagement queue".

**Example**

```bash
curl -X POST http://localhost:8000/v1/chat \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"prompt": "PROJ-2041"}'
```

---

### `GET /v1/chat/{job_id}` — poll a chat job

Any authenticated caller may poll any `job_id` (ids are opaque uuid4 hex). Steps and full history are
owner-only.

**Response `200`**

```json
{
  "job_id": "9f1c…",
  "status": "succeeded",
  "result": "…final answer text…",
  "diagrams": [],
  "error": null,
  "cancel_reason": null,
  "queue": null,
  "progress": { "phase": "handoff", "…": "see Progress while it runs" },
  "prompt": "PROJ-2041"
}
```

`diagrams` lists every fenced ` ```mermaid ` block in `result`, in order (see "Diagrams in a
result" below). It is `[]` when there are none, including while the job is running.

`404` if the job does not exist or has expired.

---

### `POST /v1/chat/{job_id}/cancel` — cancel a job

Owner only (`403` otherwise). Idempotent: an already-finished job is returned as-is, so read `status`
for what actually happened. A job that finished in the same instant comes back `succeeded`, not
`cancelled`.

A job still waiting in the queue is taken out of it and comes back `cancelled` with
`cancel_reason: "user"`; it never ran. A running job is stopped wherever it runs: the API and the MCP
server can each run any job from the shared queue, and the process running it interrupts it within
about 3 seconds of the cancel, whichever process received it.

**Response `200`**: the same shape as the poll response, including `prompt`.

---

### `GET /v1/chat/{job_id}/steps` — what the agent did

Each tool call in order, with a truncated, secret-redacted input and result preview, plus the agent's
narration. Read from the agent's session transcript on the host, so it works while the job is still
running (once its agent session has started) as well as afterwards. Owner only, because steps contain
queries and file contents.

```json
{
  "job_id": "9f1c…",
  "status": "running",
  "steps": [ { "…": "step objects as recorded by the trace reader" } ]
}
```

`404` if the job never got a session or the transcript was cleaned up.

---

### `POST /v1/engagements` — start from a Jira ticket

A narrower input onto the same engine. `ticket_key` becomes the prompt.

**Body**

| Field | Type | Required | Description |
|---|---|---|---|
| `ticket_key` | string | yes | e.g. `PROJ-2041` |
| `attachment_ids` | string[] | no | As on `POST /v1/chat` |

The endpoint also checks the presence-claim store. Presence is advisory and never blocks the run. If
another user has a live claim on the ticket, the response carries a `collision_notice`. The caller then
registers their own claim.

**Response `202`**

```json
{
  "job_id": "9f1c…",
  "conversation_id": "b27a…",
  "collision_notice": "PROJ-2041 is already being investigated by … (optional)"
}
```

---

### `GET /v1/engagements/{job_id}` — poll an engagement

Same as polling a chat job, but returns `ticket_key` instead of `prompt`.

```json
{"job_id": "9f1c…", "ticket_key": "PROJ-2041", "status": "running", "result": null, "diagrams": [],
 "error": null, "progress": {"phase": "intake", "…": "see Progress while it runs"}}
```

### Diagrams in a result

When the agent draws a diagram (the `er-diagram` skill draws ER diagrams of a semantic model or of
Unity Catalog tables), it writes it as a fenced ` ```mermaid ` block in its answer. `result` keeps
the block, so a markdown renderer with mermaid support draws it in place; `diagrams` hands the same
blocks over as data for callers that don't render markdown:

```json
"diagrams": [
  {"format": "mermaid", "kind": "erDiagram", "title": "Category Sales Analytics Model",
   "source": "---
title: Category Sales Analytics Model
---
erDiagram
    vw_category_daily_sales }o--|| CalendarDate : \"Date\"
…"}
]
```

| Field | Description |
|---|---|
| `format` | Always `mermaid` today. |
| `kind` | The diagram's first keyword: `erDiagram`, `flowchart`, … |
| `title` | From the block's frontmatter `title:`, or `null`. |
| `source` | The block's text, unchanged. Render it with mermaid (`securityLevel: "strict"`). |

Derived from `result` on every read (`domain/diagrams.py`), nothing extra is stored. At most 10 per
result; a block over 50,000 characters (mermaid's own limit) is left out rather than cut. The same
field is on each turn of `GET /v1/conversations/{id}` and on MCP `scotri_status`. The diagram is also
in the run's shared files as `scratch/<id>-er-diagram.md`.

### `POST /v1/engagements/{job_id}/cancel`
### `GET /v1/engagements/{job_id}/steps`

Identical to the `/v1/chat/{job_id}/…` equivalents. Chat jobs and engagements share one job store.

---

### `POST /v1/engagements/{job_id}/review` — record a verdict review

The sole write path for a human review of Scotri's verdict. No CLI command or MCP tool can write one,
so Scotri cannot grade its own work. Reviews are append-only and the latest is current.

**Access**: the engagement's owner, or a principal holding a role in `VERDICT_REVIEWER_ROLES`.

**Body**

| Field | Type | Required | Description |
|---|---|---|---|
| `outcome` | string | yes | `upheld`, `partially_upheld` or `overturned`. `unreviewed` cannot be submitted. |
| `reason` | string | no | Free text; validated by the review policy (may be required for some outcomes) |
| `superseded_by_job_id` | string | no | A later engagement that replaces this verdict; must exist |

**Response `201`**

```json
{
  "ok": true,
  "review": {
    "job_id": "9f1c…",
    "outcome": "overturned",
    "reviewer": "<subject>",
    "reviewed_at": "2026-10-04T09:30:00+00:00",
    "reason": "…",
    "superseded_by_job_id": null
  }
}
```

**Errors**

| HTTP | When |
|---|---|
| 403 | Not the owner and no reviewer role |
| 404 | Engagement not in durable history |
| 409 | Status isn't `succeeded`, or the run reached no reviewable conclusion (only a bug verdict or a story's technical design is reviewable) |
| 422 | Unknown `outcome`, unknown `superseded_by_job_id`, or the review policy rejected the input |

---

### `GET /v1/engagements/{job_id}/review` — review trail

**Access**: the owner, or a holder of a `HISTORY_ADMIN_ROLES` or `VERDICT_REVIEWER_ROLES` role.

```json
{
  "job_id": "9f1c…",
  "verdict": "confirmed_bug",
  "reviewable": true,
  "current_outcome": "upheld",
  "reviews": [ { "job_id": "…", "outcome": "upheld", "reviewer": "…", "reviewed_at": "…", "reason": "…", "superseded_by_job_id": null } ]
}
```

`current_outcome` is `unreviewed` when there are no reviews. `verdict` is `null` when the run recorded no
learning.

---

### `GET /v1/history` — your engagement history

The caller's own durable history, most recent first, filtered by the validated principal. It survives
the 24-hour job-store TTL.

| Query | Default | Description |
|---|---|---|
| `limit` | 50 | Clamped to 1–200 |

```json
{
  "subject": "<subject>",
  "count": 1,
  "engagements": [
    {
      "job_id": "9f1c…",
      "conversation_id": "b27a…",
      "prompt": "PROJ-2041",
      "status": "succeeded",
      "created_at": "2026-10-04T09:12:00+00:00",
      "review_outcome": "unreviewed",
      "verdict": "confirmed_bug",
      "reviewable": true
    }
  ]
}
```

`verdict` is one of `confirmed_bug | not_a_bug | inconclusive | story | question`, or `null`.

---

### `GET /v1/conversations/{conversation_id}` — a whole conversation

Every turn, oldest first, including full `result` text. Readable by the conversation's owner or a holder
of a `HISTORY_ADMIN_ROLES` role (`403` otherwise, `404` if unknown).

```json
{
  "conversation_id": "b27a…",
  "owner": "<subject>",
  "turn_count": 2,
  "turns": [
    { "job_id": "…", "conversation_id": "…", "prompt": "…", "status": "succeeded",
      "created_at": "…", "review_outcome": "unreviewed", "verdict": null, "reviewable": false,
      "result": "…", "diagrams": [], "error": null }
  ]
}
```

---

### `POST /v1/conversations/adopt` — group older jobs into a conversation

For threads that predate server-side conversations (OPEN_ITEMS.md #25), which exist as one thread only
in the browser that ran them. `scotri-ui` calls it once per such thread.

**Body:** `{"job_ids": ["…"], "conversation_id": null}`. `job_ids`: 1–200 of your own jobs.
`conversation_id`: optional; one you own (to add older jobs to it) or omitted for a fresh one.

```json
{"conversation_id": "b27a…", "adopted": ["…"], "already": [], "unknown": [], "in_other": []}
```

A job already in a different conversation is never moved (`in_other`); one not in durable history is
`unknown`. `403` if any job or `conversation_id` belongs to someone else, and then nothing changes.
`conversation_id` is `null` when nothing could be grouped. Repeating a call is harmless.

---

### `GET /v1/learnings` — browse learnings

The learnings past investigations recorded (symptom, what was checked, root cause, takeaways; never
query results). The agent's own `retrieve` always searches every learning; this route only limits
what a person can browse.

| Query | Default | Description |
|---|---|---|
| `scope` | `mine` | `mine`: learnings from runs you started. `team`: everyone's, including service/webhook runs; needs a `LEARNINGS_READER_ROLES` role (`403` otherwise) |
| `q` | — | Search text; results ranked by relevance (BM25, as `retrieve` ranks them). Without it, newest first |
| `verdict` | — | `confirmed_bug`, `not_a_bug`, `inconclusive`, `story` or `question` |
| `limit` | `50` | Clamped to 1–200 |

```json
{
  "scope": "mine",
  "count": 1,
  "learnings": [
    { "job_id": "9f1c…", "ticket_key": "PROJ-2041", "title": "…", "verdict": "confirmed_bug",
      "symptom": "…", "checked": ["…"], "root_cause_category": "report-visual", "root_cause": "…",
      "confidence": "high", "takeaways": ["…"], "entities": ["…"], "investigated_by": "…",
      "created_at": "…", "review_outcome": "unreviewed" }
  ]
}
```

### `GET /v1/learnings/{job_id}` — one learning

One learning, in the shape above. Its owner or a `LEARNINGS_READER_ROLES` holder (`403` otherwise);
`404` when that run recorded none.

---

### `POST /v1/uploads` — upload a file to attach to a prompt

`multipart/form-data` with one field, `file`. Returns `201`:

```json
{ "upload_id": "9f2c…", "filename": "BRD v2.pdf", "size": 182044,
  "content_type": "application/pdf", "created_at": "…", "redacted": false }
```

Send `upload_id` in `attachment_ids` on `POST /v1/chat` or `POST /v1/engagements`. When the run starts,
each attachment is copied to `scratch/uploads/<job_id>/<filename>` in the workspace and the agent is told
where it is, as user-supplied data and never as instructions. If an attachment can't be staged, the job
fails with that reason rather than answering without it.

- **Formats:** any, except credential containers (`.pem .key .pfx .p12 .jks .keystore .kdbx .ppk .asc
  .gpg`, and `.env` / `.env.*`), which are refused with a `422` because binary files can't be redacted.
  The agent reads text, PDFs and images directly; `.docx`, `.pptx` and `.xlsx` are read as extracted text
  (large `.xlsx` exports are streamed sheet by sheet, not refused); other formats are stored and shared as-is. Restrict
  with `UPLOAD_ALLOWED_EXTENSIONS` (the credential blocklist always applies).
- **Size:** 10 MB by default (`UPLOAD_MAX_BYTES`). Over the limit is a `413`. A refused format or an empty
  file is a `422`.
- **Redaction:** text files pass through secret redaction before they're stored (`redacted: true` says it
  changed something). PDFs and images are stored as sent.
- **Ownership:** only the uploader can attach an upload.

---

### `GET /v1/engagements/{job_id}/files` — the files a run produced

Also at `GET /v1/chat/{job_id}/files`. These are the deliverables (any format except credential files; HTML/SVG/XML and binaries are served as downloads, never inline) the agent wrote during this
run under `scratch/`, `proposals/` or `investigations/`: intake, verdict, impact analysis, implementation
plan, test plan and results, the **handoff document**, and proposal `.patch` files. Each is a copy taken
when the run ended, secret-redacted, so re-running the same ticket later never changes what this job
returns. Partial work from an `incomplete`, `failed` or `cancelled` run is published too.

```json
{
  "job_id": "…",
  "count": 2,
  "files": [
    { "job_id": "…", "path": "scratch/PROJ-2107-handoff.md", "name": "PROJ-2107-handoff.md",
      "size": 8123, "content_type": "text/markdown", "created_at": "…",
      "url": "/v1/engagements/…/files/scratch/PROJ-2107-handoff.md" }
  ]
}
```

The handoff document is the file whose `name` ends in `-handoff.md`.

### `GET /v1/engagements/{job_id}/files/{path}` — one file's content

Also under `/v1/chat/…`. Returns the raw file (`text/markdown; charset=utf-8` for `.md`). Add
`?download=true` for `Content-Disposition: attachment`. `404` for an unknown path.

### `GET /v1/conversations/{conversation_id}/files` — every turn's files

The same list across a whole conversation, oldest turn first. Each entry carries its own `job_id`, so a
file rewritten by a later turn appears once per turn, newest last.

**Who can read files:** the engagement's owner, or a holder of a `HISTORY_ADMIN_ROLES` or
`FILE_READER_ROLES` role. `FILE_READER_ROLES` is narrower: it lets an implementer fetch a handoff without
being able to read the conversation itself. Deliverables can quote query results, which is why this is
stricter than `GET /v1/chat/{id}`.

### Where files are stored

Set by `ARTIFACT_STORE` (see `services/.env.example`):

| Value | Where | Use |
|---|---|---|
| `local` (default) | `services/data/artifacts/` (`ARTIFACT_STORE_DIR`) | Mac and Windows; one host only |
| `azure_blob` | An Azure Blob container (`ARTIFACT_STORE_AZURE_*`) | Azure / Foundry; shared by every replica. Needs `pip install -r requirements-azure.txt` |

Azure auth is non-interactive only: a connection string, a service principal, or managed identity.
Nothing in the API changes between the two.

**Retention.** Nothing is deleted by default. Set `UPLOAD_RETENTION_DAYS` and `SHARED_FILE_RETENTION_DAYS`
to age out uploads and shared deliverables with the ops-only `artifacts-purge` command (a dry run that
lists what it would delete unless `--confirmed`; refused inside an engagement). No API route triggers it.

**Windows hosts.** Engagements need Git Bash for the agent's Bash tool. The service finds it (next to
`git` on PATH, Scoop, or Program Files) or reads `CLAUDE_CODE_GIT_BASH_PATH`. If the agent still starts
without Bash, the run stops at once as `failed` with an error naming the setting, instead of running
blind and reporting every column as unchecked.

---

## Admin panel (`/v1/admin/*`, page at `/admin`)

For operators debugging Scotri itself (OPEN_ITEMS.md #43). Every route below except `/v1/admin/me`
needs a role listed in `ADMIN_ROLES`, which is `scotri.admin` (set in `.env.example`): an Entra app role
on the Scotri API's app registration, assigned to the people who operate Scotri. If the setting is
unset or empty, the answer is **nobody**, never everyone. Every admin call, allowed or refused, appends one JSON line to
the admin audit log (`ADMIN_AUDIT_LOG_PATH`, default `services/logs/admin-audit.jsonl`): who, which
action, which target, the outcome. Never the payload. If that line cannot be written, the call fails
with `500` and returns nothing.

It reads Scotri's own state. It writes two things: the caller-app register, which is Scotri's own
metadata, and, when an admin clicks **Sync now**, the mirror, through the sync service's own code path
(`refresh_registry_once`, the same call the schedule and `sync-run-once` make). Nothing here touches a
product repo, Jira, Databricks or Fabric, and nothing mints, stores or shows a credential. The manual
sync is a human admin's action: an engagement holds no admin token, so the agent cannot reach it.

| Route | What it returns |
|---|---|
| `GET /v1/admin/me` | `{subject, display_name, platform, roles, is_admin}` for the caller. Any authenticated principal; not audited. Lets a frontend show or hide its admin link. |
| `GET /v1/admin/queue` | The engagement queue (OPEN_ITEMS.md #32): `limits`, `waiting` in start order (`position`, owner, prompt preview, `waited_seconds`) and `running` (`lease_seconds_left`). The page's **Queue** tab shows it, refreshed every 5 s. |
| `POST /v1/admin/queue/{job_id}/cancel` | Take anyone's waiting job out of the queue. Waiting jobs only (`409` otherwise, `404` if unknown); the owner sees `cancel_reason: "admin"` and can send the prompt again. Audited with the owner's subject. |
| `GET /v1/admin/overview` | One debugging snapshot: `redis`, `governor` (in-flight count and cap per backend), `active_jobs`, `queue` (as `GET /v1/admin/queue`), `mirror` (freshness and maintenance flag per mirrored branch), `config` (which settings are set, as booleans, and the role lists). Each section carries its own `ok`/`error`, so a Redis outage shows up as one failed section rather than a `500`. |
| `GET /v1/admin/engagements?limit=&subject=&status=` | Every caller's engagements from history, newest first (`limit` max 500). A 200-character `prompt_preview`, never the full prompt or result. |
| `GET /v1/admin/engagements/{job_id}/steps` | The steps of anyone's job, same shape as `GET …/steps` plus `owner`. The owner-only rule on `…/steps` is widened here for admins only, and each read is audited with the job id. |
| `GET /v1/admin/logs` | The log files the view can open (`api`, `cli`, `mcp`, `sync`) with size and modified time. |
| `GET /v1/admin/logs/{source}?lines=&contains=` | The last `lines` lines (default 200, max 2000) of one of those logs, optionally only lines containing `contains`. Each line is secret-redacted again on the way out. The source is an allowlisted name, never a path: anything else is `404`. |
| `GET /v1/admin/audit?limit=` | The admin audit log, newest first. Reading it is itself audited. |
| `GET /v1/admin/apps` | Registered caller apps merged with the platforms history shows calling Scotri (the token's `azp`/`appid`). A caller seen but not registered comes back `registered: false`; `unregistered` counts them. |
| `PUT /v1/admin/apps/{app_id}` | Register or update one app: `{"name", "kind": "interactive" \| "service", "owner"?, "notes"?}`. `422` if a field looks like it carries a credential. |
| `DELETE /v1/admin/apps/{app_id}` | Remove Scotri's record of an app. The app's tokens stay valid: Entra issues them, not Scotri. |
| `POST /v1/admin/mirror/sync` | Refresh mirrored Prod repos now, exactly as the scheduled sync does. No body syncs every mirrored repo/branch; `{"repo_key", "branch"?}` syncs one key (or one branch of it). A one-repo run merges its row into `status.json`: the other rows keep their own `checked_at`, `last_run_at` stays the last full run's, and the run's time goes in `last_partial_run_at`. Returns `202` at once with the state below; the run continues on a background thread. `404` if nothing mirrored matches; `409` if any sync holds the shared sync lock (another click, the schedule, or `sync-run-once`), with the holder's name in `detail`. Audited twice, with the scope as target for a one-repo run: `sync_mirror` when it starts, `mirror_sync_finished` when it ends (`checked=N failed=M`, or `lock held`). |
| `GET /v1/admin/mirror/sync` | The latest manual sync started from this API process: `{running, started_at, finished_at, started_by, scope, checked, failed, failures: [{repo, branch, category, message}], repos: [{repo_key, repo, branch, phase, percent, ok, message}], lock_holder}`. `scope` is `all`, a key, or `key@branch`. Each `repos` row is one bar: `phase` is `queued`, `fetching`, `swapping`, `done`, `failed` or `skipped`, and `percent` (0-100, never moving backwards) comes from git's own `--progress` (receiving 2-70, resolving 70-85, checkout 85-93, file index 93-96, swap 96-100). Only the stage and the number are read from git's output. `lock_holder` names whoever holds the shared lock now (`sync-service`, `cli:sync-run-once`, `admin:<subject>`, or `admin:<subject> (<scope>)`), or `null`. |

**The sync lock.** Every way of starting a sync (the sync service's schedule, `sync-run-once` and the
button above) takes one Redis lock, `mirror:sync-lock`, for the whole run, so two runs can never
fetch into the same staging tree at once. The lock is renewed after each repo and released at the end,
even when a repo fails; a process that dies mid-run frees it after 30 minutes. A scheduled tick that
finds it held logs a warning and skips; `sync-run-once` prints a `transient` JSON error naming the
holder.

`GET /admin` serves a small page over these routes. It holds no data: it asks for a bearer token, keeps
it in that browser tab's session storage, and calls the API above. scotri-ui links to it from its top
bar for admins; when the page is served on the UI's origin (the Vite dev proxy), it reuses the UI's
session token (`scotri.devAuthToken`) instead of asking. "Forget token" clears both. The page follows
the brand (`docs/brand/README.md`): the overview's Mirror card carries the **Sync now** button and a
**Sync** button and progress bar on every repo row. While a run is going, the mark beside Sync now is
the determinate ring (the share of the run's total percent done), each row's bar fills as its repo
fetches and swaps, and every button is disabled; the mark steps (queued) while another sync holds the
lock, and settles once when a sync started there finishes cleanly.

---

## Access control summary

| Endpoint | Who may call |
|---|---|
| `GET /health` | Anyone |
| `POST /v1/chat`, `POST /v1/engagements` | Any authenticated principal |
| `GET /v1/chat/{id}`, `GET /v1/engagements/{id}` | Any authenticated principal |
| `…/cancel`, `…/steps` | Job owner only |
| `POST …/review` | Owner, or `VERDICT_REVIEWER_ROLES` |
| `GET …/review` | Owner, or `HISTORY_ADMIN_ROLES` / `VERDICT_REVIEWER_ROLES` |
| `GET /v1/history` | Self only |
| `GET /v1/conversations/{id}` | Owner, or `HISTORY_ADMIN_ROLES` |
| `POST /v1/conversations/adopt` | Self only, own jobs only |
| `GET /v1/learnings` | Own learnings for anyone; `scope=team` needs `LEARNINGS_READER_ROLES` |
| `GET /v1/learnings/{job_id}` | Owner, or `LEARNINGS_READER_ROLES` |
| `GET …/files`, `GET …/files/{path}`, `GET /v1/conversations/{id}/files` | Owner, or `HISTORY_ADMIN_ROLES` / `FILE_READER_ROLES` |
| `POST /v1/uploads` | Any authenticated principal; only the uploader can attach the result |
| `GET /admin` | Anyone (a static page with no data in it, like `/health`) |
| `GET /v1/admin/me` | Any authenticated principal |
| Every other `/v1/admin/*` route | `ADMIN_ROLES` only; every call audited |

The role sets are comma-separated lists read from the `HISTORY_ADMIN_ROLES`, `VERDICT_REVIEWER_ROLES`,
`FILE_READER_ROLES`, `LEARNINGS_READER_ROLES` and `ADMIN_ROLES` settings. The first four are empty by
default, which means owner-only. `ADMIN_ROLES` is `scotri.admin`; unset or empty, it means nobody. The admin role does not imply the others: an admin reads prompt previews and
steps, but a full conversation or a deliverable still needs `HISTORY_ADMIN_ROLES` or `FILE_READER_ROLES`.

---

## MCP tools

The MCP server exposes Scotri *as* tools for an external Claude or agent client. It adds nothing to
Scotri's own integrations, which stay MCP-free. Authentication is the same Bearer token, read from the
transport's HTTP headers. stdio carries no headers and so cannot authenticate.

| Tool | Arguments | Returns |
|---|---|---|
| `scotri_upload` | `filename: string`, `content: string`, `encoding?: "text" \| "base64"` | `{"upload_id", "filename", "size", "content_type", "created_at", "redacted"}` |
| `scotri_start` | `prompt: string`, `attachment_ids?: string[]`, `conversation_id?: string`, `resume_job_id?: string` | `{"job_id", "resumed", "conversation_id", "queued"}` immediately (plus `queue_position` when Scotri is busy); the run continues in the background. Refused when busy as a tool error saying so |
| `scotri_status` | `job_id: string` | `{"job_id", "status", "prompt", "result", "diagrams", "error", "cancel_reason", "queue", "progress"}` (see Progress while it runs and Busy: the engagement queue) |
| `scotri_cancel` | `job_id: string` | Your own job, as `scotri_status` returns it: a waiting job leaves the queue, a running one is stopped |
| `scotri_list_files` | `job_id: string` | `{"job_id", "files": [{"path", "size", "content_type", "created_at"}]}` |
| `scotri_get_file` | `job_id: string`, `path: string` | `{"job_id", "path", "content_type", "content"}` — the full text |

These mirror `POST /v1/uploads`, `POST /v1/chat`, `GET /v1/chat/{job_id}`, `POST /v1/chat/{job_id}/cancel` and the two `…/files` routes,
with the same rules. `scotri_upload` takes the file as UTF-8 text, or as base64 (`encoding: "base64"`)
for PDFs, images and other binary files; the size limit applies to the decoded file. `scotri_start`
groups and resumes turns exactly as `POST /v1/chat` does: both `conversation_id` and `resume_job_id`
must be the caller's own. Errors surface as MCP tool errors carrying the same user-facing message as
the HTTP `message` field. The one difference from HTTP: jobs started over MCP have no API origin tag.

---

## Typical flow

```bash
# 1. Start
JOB=$(curl -s -X POST localhost:8000/v1/engagements \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"ticket_key":"PROJ-2041"}' | jq -r .job_id)

# 2. Poll until terminal
curl -s localhost:8000/v1/engagements/$JOB -H "Authorization: Bearer $TOKEN"

# 3. Fetch the handoff document it wrote
curl -s localhost:8000/v1/engagements/$JOB/files -H "Authorization: Bearer $TOKEN" | jq -r '.files[].path'
curl -s localhost:8000/v1/engagements/$JOB/files/scratch/PROJ-2041-handoff.md -H "Authorization: Bearer $TOKEN"

# 4. Optionally inspect what it did, then review the verdict
curl -s localhost:8000/v1/engagements/$JOB/steps -H "Authorization: Bearer $TOKEN"
curl -s -X POST localhost:8000/v1/engagements/$JOB/review \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"outcome":"upheld","reason":"Root cause confirmed in the gold table"}'
```

Attaching a file to a prompt:

```bash
UPLOAD=$(curl -s -X POST localhost:8000/v1/uploads -H "Authorization: Bearer $TOKEN" \
  -F "file=@BRD.pdf" | jq -r .upload_id)
curl -s -X POST localhost:8000/v1/chat \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"prompt\":\"Does PROJ-2041 cover everything in this BRD?\",\"attachment_ids\":[\"$UPLOAD\"]}"
```
