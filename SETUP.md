# Deploying Scotri for a new client

Scotri is one engine and many deployments. Everything specific to a business lives in **one profile
file** (`client/profile.yaml`); `python -m app.setup` turns it into the configuration and docs the
agent reads, collects credentials, and checks every connection. A complete worked example for a
fictional retailer is in [`examples/acme-retail/`](examples/acme-retail/).

```
client/profile.yaml ──render──► services/config/*.yaml          (what the code loads)
                                docs/ai/client/client.md, repo-registry.md, repo-map.md   (generated)
                                docs/ai/client/*-architecture.md, conventions, lineage    (skeletons, written by people)
                    ──secrets─► services/.env                     (gitignored; or your vault)
```

## 1. Create the client's repo

1. On GitHub, mark `scotri-agent` as a template (Settings → General → **Template repository**), then
   **Use this template → Create a new repository**: private, named for the client, e.g.
   `contoso-scotri`.
2. Clone it on the host the agent will run on, and keep the engine as an upstream for upgrades:

   ```bash
   git clone https://github.com/<you>/contoso-scotri.git && cd contoso-scotri
   git remote add upstream https://github.com/preyask1/scotri-agent.git
   ```

3. Install (Python 3.11+, Git, and Redis reachable from the host):

   ```bash
   cd services
   python -m venv .venv
   .venv/bin/pip install -r requirements.txt        # Windows: .venv\Scripts\pip install -r requirements.txt
   ```

   Run every `python -m app.setup …` below with that venv's Python, from `services/`.

## 2. Ask the client for access

Every identity is **read-only** except the Jira account's narrow, confirm-gated write. Request these
before you start; `doctor` (step 6) tells you which one is missing.

| For | What to create | Grants |
|---|---|---|
| Databricks | a service principal with an OAuth secret | `USE CATALOG`, `USE SCHEMA`, `SELECT` on the prod (and dev) catalogs; `CAN USE` on one SQL warehouse; the *Databricks SQL access* entitlement. For job-failure tracing, `CAN VIEW` on the relevant jobs. Nothing else. |
| Azure DevOps | a personal access token **per organisation** that holds the repos | **Code (Read)** only. Never Read & Write. |
| Jira | a service account and a scoped API token | `read:jira-work`, `read:jira-user`, `write:jira-work` (comments, Bug creation and attachments, each confirmed by a person first). |
| Power BI / Fabric (if used) | an Entra app registration with a secret | **Viewer** on each workspace, and Build on the semantic models you'll query (or Contributor). Tenant settings: service principals may use Fabric/Power BI APIs; *Dataset Execute Queries REST API* enabled. |
| Azure Data Factory (if used) | an Entra app registration with a **federated credential** trusting the host's managed identity (no secret) | **Reader** on each factory. Works only from the Azure host that carries that identity. |
| The API itself | an Entra app registration for the Scotri API | An app role `scotri.admin` for the people who operate it. Each frontend gets its own registration requesting a token for this API. |
| The model | an Anthropic API key | — |

## 3. Describe the client: `init`

```bash
python -m app.setup init
```

It asks about a dozen questions (business name, the name users should see, Jira site and projects,
whether ADF and Power BI/Fabric are used, Databricks host and catalogs, the ADO organisation and repo
names) and writes `client/profile.yaml`. None of it is secret. Then open the file and add anything the
questions didn't cover: more Fabric repos, a second Prod branch, extra factories, workspaces, notes.
Every field is explained in [`examples/acme-retail/client/profile.yaml`](examples/acme-retail/client/profile.yaml).

To start from a profile you already have: `python -m app.setup init --from path/to/profile.yaml`.

The fields that matter most:

| Field | Why |
|---|---|
| `client.display_name` | the name on the admin panel, the developer portal, the API title and in the agent's replies |
| `client.logo` | an SVG/PNG under `client/brand/`; replaces the built-in mark and favicon |
| `client.host_os` | `windows` or `linux`: picks the venv interpreter path the agent is told to run |
| `components.adf`, `components.fabric` | switch a layer off: its commands answer "not enabled for this deployment" and the agent treats it as out of scope |
| `databricks.catalogs` | the prod catalog is the agent's default for every query; dev only when someone names Dev |
| `databricks.secondary_workspace` | optional: a workspace the agent may reach only when tracing a pipeline failure |
| `repos` | every repo the agent reads, with its branches; only `mirrored: true` (Prod) branches are copied locally |

## 4. Generate config and docs: `render`

```bash
python -m app.setup render            # --dry-run --diff to preview
```

- **Generated** (rebuilt on every render; never edit them): `services/config/repo_registry.yaml`,
  `adf_factories.yaml`, `model_registry.yaml`, `client.yaml`, and `docs/ai/client/client.md`,
  `repo-registry.md`, `repo-map.md`.
- **Skeletons** (created once, never overwritten): `docs/ai/client/databricks-architecture.md`,
  `fabric-architecture.md`, `fabric-conventions.md`, `lineage-map.md`, `lineage-graph.yaml`. These
  hold the client's domain knowledge; see step 7.

Change the profile any time and re-run `render`.

## 5. Credentials: `secrets`

```bash
python -m app.setup secrets
```

It asks for each credential the enabled components need, with secrets hidden as you type, and writes
them to `services/.env`, which git ignores. Values the profile already knows (Databricks host, Jira
site and the like) are filled in for you, and the Jira cloud id is fetched from the site. Nothing is
printed back.

For a vault-based deployment (Azure Key Vault, etc.), put the same variable names in the vault
instead; `services/.env.example` lists them all. Two settings decide how callers authenticate:

- **Production:** `AZURE_AD_TENANT_ID` and `AZURE_AD_API_AUDIENCE` (the API's registration from step 2).
- **Local trial only:** `SCOTRI_AUTH_MODE=dev` plus `SCOTRI_DEV_AUTH_TOKEN`. Never in production.

## 6. Check everything: `doctor`

```bash
python -m app.setup doctor            # --offline skips the backend calls
```

One line per check, each with a fix when it fails:

```
  PASS  profile                    Contoso (client/profile.yaml)
  PASS  generated files            match the profile
  PASS  credentials                every required setting is set
  PASS  redis                      reachable
  FAIL  ADO fabric_sales@main      HTTP 401 …
        fix: check the PAT for org 'contoso-bi' (Code (Read)) and that repo 'PowerBI_Sales' has branch 'main'
  PASS  Databricks                 14 schema(s) visible
  …
```

Every backend check is a real read-only call through the same query governor the agent uses, so it
is rate-counted and audited, and never served from cache. A host that doesn't answer is reported after
45 seconds instead of hanging.

## 7. Write the domain docs

The generated docs tell the agent *where* things are. The skeletons in `docs/ai/client/` are where you
tell it *how the platform works*: source systems, schema conventions, orchestration, report families,
RLS, known traps. Fill in every **TODO**; the Acme versions in
[`examples/acme-retail/docs/ai/client/`](examples/acme-retail/docs/ai/client/) show the level of detail
that helps. Time spent here pays back on every investigation.

The lineage map and graph start empty. Seed them with the chains you know; after that, the agent
extends `lineage-map.md` itself during impact analyses, and `lineage-coverage` lists what nobody has
verified yet.

## 8. First sync and start: `bootstrap`

```bash
python -m app.setup bootstrap         # first mirror sync, lineage index, then doctor again
```

Then run the services:

```bash
uvicorn app.api:app --host 0.0.0.0 --port 8000     # the API, /admin and /portal
python -m app.sync_service                          # nightly mirror refresh (SYNC_SCHEDULE_CRON)
```

Or start everything locally from `tools/launcher/`. Optional: copy
`examples/acme-retail/client/benchmark-cases.json` to `client/benchmark-cases.json`, rewrite the
prompts around this client's own tickets and tables, and use `tools/benchmark/` to measure changes.

## 9. Commit the client's files

In the client's repo, commit `client/`, `services/config/` and `docs/ai/client/`. **Never commit
`services/.env`** (it's gitignored). Check with `git status` before every push.

## Upgrading the engine

```bash
git fetch upstream
git merge upstream/main
cd services && python -m app.setup render && python -m pytest -q && python -m app.setup doctor
```

The engine repo never contains `client/`, `services/config/*.yaml` or `docs/ai/client/`, so a merge
never conflicts with a client's files. `render` picks up any new generated content; person-written
docs are left alone.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `… does not exist. It is generated from client/profile.yaml` | `render` hasn't run in this checkout |
| `doctor`: Fabric returns HTTP 203 or HTML | wrong `FABRIC_TENANT_ID`: Power BI may live in a different Entra tenant from Azure DevOps/Databricks |
| `doctor`: ADO 401 for one org only | each ADO organisation needs its own PAT (`ADO_PAT_READONLY_<ORG>`) |
| `doctor`: Databricks 403 on job runs | the SP has `SELECT` but no `CAN VIEW` on jobs; data queries still work |
| A command says `not enabled for this deployment` | that component is off in `client/profile.yaml`; working as intended |
| Every backend check is skipped | Redis isn't reachable; the governor needs it |
