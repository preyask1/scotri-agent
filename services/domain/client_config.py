"""The deployment's runtime client settings: which optional components are on, what the agent is
called in front of users, and which Unity Catalog catalogs are the client's.

Generated into services/config/client.yaml by `python -m app.setup render` from client/profile.yaml
(SETUP.md). A missing file means the engine defaults: every component on, display name "Scotri", no
catalog list, so an engine checkout behaves exactly as it did before white-labelling. Pure: the YAML
is parsed by adapters/registry/yaml_client_config.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from domain.errors import ConfigurationError

ENGINE_NAME = "Scotri"

OPTIONAL_COMPONENTS = ("adf", "fabric")
"""Databricks and Jira are always on; these two can be switched off per client."""

COMMAND_COMPONENTS: dict[str, str] = {
    "pipeline-runs": "adf",
    "activity-runs": "adf",
    "trigger-runs": "adf",
    "dax-execute": "fabric",
    "refresh-history": "fabric",
    "report-bindings": "fabric",
}
"""CLI commands that only make sense when their component exists in this deployment."""

COMPONENT_LABELS = {"adf": "Azure Data Factory", "fabric": "Power BI / Fabric"}


@dataclass(frozen=True)
class ClientConfig:
    display_name: str = ENGINE_NAME
    client_name: str = ""
    components: frozenset[str] = field(default_factory=lambda: frozenset(OPTIONAL_COMPONENTS))
    catalogs: tuple[str, ...] = ()
    jira_projects: tuple[str, ...] = ()
    logo: str = ""

    def enabled(self, component: str) -> bool:
        return component in self.components


DEFAULT_CLIENT_CONFIG = ClientConfig()


def client_config_from_dict(data: dict | None) -> ClientConfig:
    """Builds a ClientConfig from client.yaml's parsed content. Unknown components are refused so a
    typo can't silently switch something off."""
    if not data:
        return DEFAULT_CLIENT_CONFIG
    components = data.get("components") or {}
    unknown = sorted(set(components) - set(OPTIONAL_COMPONENTS))
    if unknown:
        raise ConfigurationError(
            f"config/client.yaml: unknown component(s) {unknown}; optional components are "
            f"{list(OPTIONAL_COMPONENTS)} (Databricks and Jira are always on)."
        )
    enabled = frozenset(c for c in OPTIONAL_COMPONENTS if components.get(c, True))
    catalogs = data.get("catalogs") or {}
    return ClientConfig(
        display_name=str(data.get("display_name") or ENGINE_NAME),
        client_name=str(data.get("client_name") or ""),
        components=enabled,
        catalogs=tuple(str(v) for v in catalogs.values() if v) if isinstance(catalogs, dict)
        else tuple(str(v) for v in catalogs if v),
        jira_projects=tuple(str(p).upper() for p in (data.get("jira_projects") or []) if p),
        logo=str(data.get("logo") or ""),
    )


def check_command_enabled(command: str, config: ClientConfig) -> None:
    """Refuses a command whose component is switched off, before any adapter is built. The message
    tells the agent this is scope, not an outage (agent-instructions.md)."""
    component = COMMAND_COMPONENTS.get(command)
    if component is None or config.enabled(component):
        return
    raise ConfigurationError(
        f"{COMPONENT_LABELS[component]} is not enabled for this deployment, so '{command}' is out of "
        f"scope here (config/client.yaml components.{component}: false). Say so in the deliverable; "
        f"this is not an outage."
    )
