# `services/config/` — generated per client

These files are generated from `client/profile.yaml` by `python -m app.setup render` (run from
`services/`; see [`../../SETUP.md`](../../SETUP.md)). Edit the profile and re-render; never edit them
by hand.

| File | Loaded by | Holds |
|---|---|---|
| `repo_registry.yaml` | `adapters/registry/yaml_registry_adapter.py` | every repo, its environments, branches and catalogs |
| `adf_factories.yaml` | `adapters/registry/yaml_adf_factory_registry.py` | ADF factories for run-history reads |
| `model_registry.yaml` | `adapters/registry/yaml_model_registry_adapter.py` | the Claude model engagements run on |
| `client.yaml` | `adapters/registry/yaml_client_config.py` | display name, logo, enabled components, catalogs, Jira projects |

The engine repo ships none of them. Without them the engine still starts with defaults (every
component on, display name "Scotri", the default model), but any command that needs the repo
registry says to run `render`. A worked example: `../../examples/acme-retail/services/config/`.
