# Identity, roles, and access model

How Scotri identifies a caller, associates it with a user, and enforces what that user may see or do —
across multiple frontends (Teams, a web portal, …) and both transports (HTTP `app/api.py`, MCP
`app/mcp_server.py`). This is the reference the admin panel (OPEN_ITEMS.md #43) and the webhook/
scheduled-sweep trigger paths (#7) build against, so those don't re-decide it.

Decided 2026-09-30, from a design discussion. Code that already implements parts of this:
`adapters/azure_ad/token_validator.py`, `domain/models.py` (`Principal`), `app/api.py`
(`_authenticate`, `HISTORY_ADMIN_ROLES`, the owner/role checks), `domain/access_policy.py` (the pure
gate helpers).

## 1. What identifies a caller

Every request carries a Bearer token that `identity_provider.validate()` verifies; the result is a
trusted `Principal` (`domain/models.py`) with three fields that matter here:

| field | source | meaning |
|---|---|---|
| `subject` | token `oid` (fallback `sub`) | the **user** — stable per-tenant object id, the same value in every token from every app registration |
| `platform` | token `azp`/`appid` | the **calling app** — which frontend/app registration acquired the token; never a request header (unforgeable) |
| `roles` | token `roles` claim | Entra **app-role** assignments; Scotri reads and enforces them, never assigns them |

**One API audience** (`AZURE_AD_API_AUDIENCE`); **each frontend is its own Entra app registration**
acquiring a token for that audience. So "which app" is established cryptographically from `azp`, not
from anything the caller self-reports.

## 2. Same user across different platforms

Because `subject` is the Entra `oid`, it is **identical across frontends** for the same person — only
`platform` differs:

| | `subject` | `platform` |
|---|---|---|
| Alice via Teams | `alice-oid` | `teams-app-reg-id` |
| Alice via web portal | `alice-oid` | `web-portal-app-reg-id` |

Consequences, all of which the built features already rely on:
- **Unified history/conversations/retrieval** — keyed on `subject`, so Alice sees one history across
  both frontends.
- **One rate-limit budget per human** — the governor counts per `subject`, not per app, so a user
  can't multiply quota by opening a second frontend.
- **Attribution records both** — audit lines carry who (`subject`) and from where (`platform`).

**Caveat:** `oid` is stable *per tenant*. A true guest/B2B user from another tenant has a different
`oid` per tenant — fine for a single-tenant internal tool; if Scotri ever serves external orgs,
"same user" needs `sub`+issuer, not `oid` alone. Not built for now.

## 3. The load-bearing prerequisite: delegated (user) tokens, not app-only

This whole model requires the token to carry the **end user** (`oid`) — i.e. a delegated /
on-behalf-of token from the frontend's user sign-in. Under an **app-only** (client-credentials)
token, `subject` is the frontend's service principal and **every user of that frontend collapses to
one subject** — unified history, per-user budgets, and cross-user isolation all vanish, and any
"user id" passed in the request body would be a self-reported, forgeable value the design forbids.

**Carve-out — the genuine no-user case:** some callers have no end user by construction (a scheduled
backlog sweep, a Jira webhook, pure MCP automation). Those correctly use an **app-only token with a
synthetic `subject`** (e.g. `"scheduled-sweep"`), distinguished by `platform`. The model supports
both: a real user subject when a user exists, a service subject when one doesn't — never "always
app" or a forged user.

## 4. Roles: enforced, not authored; profiles: not Scotri's; privileges: computed

- **Roles — yes, minimally, consumed not authored.** Directory admins assign roles in Entra (app-role
  assignments); Scotri only reads the `roles` claim and enforces it. Keep the set tiny — realistically
  ~2–3: a default analyst (own history, run engagements), a team-visibility role
  (`HISTORY_ADMIN_ROLES`), and an admin role (`ADMIN_ROLES=scotri.admin`, the #43 panel, built 2026-10-04). Scotri does **not** build a
  role-management/assignment surface — that is Entra's job.
- **Profiles — no.** Preferences/UX state belong to the frontend, keyed by the same `oid`. Scotri is
  headless; the only per-user thing it persists is the engagement/audit record, which it owns for a
  functional reason, not a "profile".
- **Privileges — computed, not stored.** A privilege is a pure function of
  `(roles, platform, resource-owner)` evaluated per request (`domain/access_policy.py`), never a
  stored per-user privilege table that would drift from the roles claim.

## 5. How enforcement works — the five categories

All gates share one mechanism: validate → a **pure policy check** in `domain/access_policy.py` →
raise `PolicyError` (→ HTTP 403) on failure. Enforcement is **server-side at the request boundary**,
uniform across the HTTP and MCP transports. Role checks are always set-intersection
(`set(principal.roles) & allowed`).

1. **Ownership-scoped access** — default owner-only, a role *widens* it. Live:
   `GET /v1/conversations/{id}` allows the owner, or a holder of `HISTORY_ADMIN_ROLES`; with that set
   empty (the default) it is strictly owner-only. `/steps` and job-cancel are owner-only with *no*
   widening on purpose (they carry raw tool inputs). → `can_read_owned` / `require_owner_or_role`.
2. **Capability gate** — "may you invoke this verb at all", a flat role check with no owner (the #43
   admin actions). → `require_role`, called first, before any work. Live: every `/v1/admin/*` route
   except `/me` checks `ADMIN_ROLES` (empty = nobody) and writes an admin audit line whether it allowed
   or refused the call (`app/admin_routes.py`).
3. **Data-scoping** — same endpoint, role selects the *filter* (`GET /v1/history`: own-only vs
   team-wide). The un-privileged filter is the default path and is applied *in the query*, so it
   fails closed and can't leak a payload. Scope derives from the token, never a request parameter.
4. **Caller-shape gate** — `platform` refuses a service identity even if it carries a role (a
   human-approval action on the unattended path). → `require_human_caller`.
5. **Governed-resource attribution** — no role exempts a caller from the query governor. An admin
   sees more and can do admin actions, but their query still counts against their per-`subject` rate
   limit and still audits (#29). A higher ceiling for a role is a rate-limit-*number* tuning (#3), not
   a role bypass.

Two invariants across all five: **fail closed** (absent/unreadable roles → least privilege), and **a
role gates but never exempts** (audit and the governor still apply).

## 6. What this model does NOT cover — data-plane (row/column) authorization

**Scotri authorizes access to its own resources (engagements, history, conversations, admin actions)
— it does NOT authorize the *content* of a data query.** Concretely: **nothing today stops one
salesman from asking for a colleague's incentive**, or any caller from reading any row the query
touches. Two reasons:

1. **One shared data-plane identity.** `databricks-select` queries as a single service principal
   (`DATABRICKS_CLIENT_ID`), the same identity for every end user. Databricks/Unity Catalog only ever
   sees the SP, so it cannot apply row-level security keyed to the *asking* user — the SP reads
   whatever the SP is granted.
2. **The `SELECT`-only guard restricts verbs, not rows/columns.** `domain/query_guard.py` blocks
   writes; it says nothing about *which* rows a read may return.

Scotri's intended users this phase are **data engineers investigating PROJ tickets**, not row-level-
scoped business users — so this hasn't bitten yet. But the moment an end-user audience (salesmen,
field users) can put questions to Scotri, it becomes a real authorization gap.

### 6a. Phase-2 design — **parked**

Facts that shape the design (typical of a Power BI estate):

- **Power BI RLS is usually a single-row identity anchor**, e.g. a `Field` role filtering
  `'Store Users'[RepCode] = USERNAME()` and an `HQ` role filtering an access table on
  `USERPRINCIPALNAME()`. The manager→subordinate fan-out lives in the model relationships, so it works
  only because Power BI knows the logged-in user.
- **A precomputed hierarchy-closure table** (one row per rep carrying every ancestor's code) turns
  "who is below X" into a single-column equality, and the column a user's code lands in *is* their
  level.
- **Power BI RLS does not protect the Databricks query path.** It is enforced in the DAX model, so a
  direct `SELECT` against the gold table (Scotri's SP included) sees all rows.

Two options:

- **Option A — Unity Catalog row filter / column mask** (mirror the DAX anchor with `current_user()`).
  Protects every path (Scotri, notebooks, direct SQL) but needs a per-user Databricks identity
  (on-behalf-of). Robust, heavier.
- **Option B — Scotri queries the closure table and injects the filter.** Resolve caller → rep code,
  look up their span, inject `WHERE <level>_CODE = <code>` into the governed SELECT. **Effective only as
  a structural guard**: a `domain/row_scope_policy.py` that *refuses* a `databricks-select` against a
  configured RLS-scoped table unless it carries the caller-derived predicate (same discipline as
  SELECT-only in `query_guard` and the mandatory name filter in `adf_run_policy`), never prose a query
  can skip. Option B protects only Scotri's path.

**Recommendation:** Option B first *if* end users reach this data only through Scotri; Option A later
as defence in depth, the same closure table feeding both. **Non-negotiable either way:** the caller's
*verified* UPN from a delegated token (§3), never a self-reported user id, and never an NL policy layer
guessing intent.

### 6b. General principle (unchanged)

Where such a control must live — **the data plane, not Scotri's prompt or guard**:
- **Databricks Unity Catalog row filters / column masks**, or **Fabric RLS roles**, enforced *where
  the data lives* — keyed to a **per-user identity passed through to the warehouse** (on-behalf-of to
  Databricks), which Scotri does not do today (it uses one SP).
- **Not** a semantic/NL policy layer in Scotri that tries to reason "is this query asking for someone
  else's incentive?" — fragile, bypassable, and conflicts with the hard rule against trusting a model
  to be the security boundary. A lightweight Scotri-side scoping (Option B) is acceptable only as the
  *near-term* control on Scotri's own path with a structural guard, never as the platform-wide boundary.

Tracked in OPEN_ITEMS.md ("Data-plane row/column authorization", parked for phase 2). Until a control
exists, any frontend exposing Scotri to end users who must not see each other's data must not route
those questions to the shared-SP query path, or must itself sit behind a warehouse identity that
already enforces RLS.
