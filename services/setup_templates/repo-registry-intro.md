# Repo registry — $name (read this before assuming a repo name in any skill)

This is the one place repo/branch/workspace facts live. If a skill's prose names a specific repo or
branch instead of pointing here, the skill is out of date. Machine-readable twin:
`services/config/repo_registry.yaml` (both generated from `client/profile.yaml`).

## Read-access rule (applies to every row below)

- **Prod branches are persistently mirrored** at `services/mirror/mirror-active/<repo>/<branch>/`,
  refreshed by the sync service (scheduled + manual, atomic swap). Use `mirror-list` /
  `mirror-read --repo-key <key>`: they resolve the path and check the maintenance flag.
- **Dev and UAT branches are read on demand, live, never mirrored.** A developer's Dev branch is their
  active workspace, not a standing snapshot to poll. Read it only when a ticket or user explicitly
  names that environment.
- **Before any Dev/UAT read, check `presence-claim`.** If someone holds a claim on that ticket or
  branch, say so before reading their in-progress work.
- **A live Dev/UAT read still goes through the query governor**: rate-limited and logged.
- **Default scope for `impact-analysis` and every debugging skill is Prod**, unless the ticket or the
  user names Dev or UAT. With no environment named, assume Prod and say so.
- A repo not listed here → `not yet in the repo registry — hand back to a human to confirm the
  mapping`, never a guessed name.
