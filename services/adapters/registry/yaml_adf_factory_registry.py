"""Loads config/adf_factories.yaml and implements the AdfFactoryRegistry port.

Same pattern as yaml_registry_adapter.py / yaml_model_registry_adapter.py: the one place this YAML
gets parsed, so it can become a database/API call later without touching anything upstream.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from domain.errors import ConfigurationError
from domain.models import AdfFactoryRef, Environment

DEFAULT_ADF_FACTORIES_PATH = Path(__file__).resolve().parents[2] / "config" / "adf_factories.yaml"


class YamlAdfFactoryRegistry:
    """Implements ports.adf_factory_registry.AdfFactoryRegistry."""

    def __init__(self, path: Path = DEFAULT_ADF_FACTORIES_PATH) -> None:
        self._path = path
        # Several factories can share an environment (three Prod factories as of 2026-10-07);
        # `_default` is the one `get(environment)` returns when no factory is named.
        self._by_name: dict[tuple[Environment, str], AdfFactoryRef] = {}
        self._default: dict[Environment, AdfFactoryRef] = {}
        self._load()

    def _load(self) -> None:
        if not Path(self._path).is_file():
            raise ConfigurationError(
                f"{self._path} does not exist. It is generated from client/profile.yaml: run "
                f"`python -m app.setup render` from services/ (see SETUP.md)."
            )
        with open(self._path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        for entry in data.get("factories", []):
            environment = Environment(entry["environment"])
            ref = AdfFactoryRef(
                environment=environment,
                subscription_id=entry.get("subscription_id") or "",
                resource_group=entry.get("resource_group") or "",
                factory_name=entry["factory_name"],
            )
            self._by_name[(environment, ref.factory_name.lower())] = ref
            if entry.get("default") or environment not in self._default:
                self._default[environment] = ref

    # --- AdfFactoryRegistry port ---------------------------------------------------------

    def get(self, environment: Environment, factory_name: str | None = None) -> AdfFactoryRef:
        if factory_name:
            ref = self._by_name.get((environment, factory_name.lower()))
            if ref is None:
                known = ", ".join(sorted(r.factory_name for (e, _), r in self._by_name.items() if e == environment))
                raise ConfigurationError(
                    f"no ADF factory named '{factory_name}' registered for environment "
                    f"'{environment.value}' in config/adf_factories.yaml (known: {known or '(none)'})."
                )
        else:
            ref = self._default.get(environment)
            if ref is None:
                known = ", ".join(sorted(e.value for e in self._default)) or "(none registered)"
                raise ConfigurationError(
                    f"no ADF factory registered for environment '{environment.value}' in "
                    f"config/adf_factories.yaml (known: {known})."
                )
        if not ref.subscription_id or not ref.resource_group:
            raise ConfigurationError(
                f"config/adf_factories.yaml has no subscription_id/resource_group for factory "
                f"'{ref.factory_name}' ({environment.value}) — a human must fill them in before the "
                f"ADF run-history reader can use it (see OPEN_ITEMS.md #16). This is a configuration "
                f"gap, not a broken adapter or a bad credential."
            )
        return ref

    def all_environments(self) -> list[Environment]:
        return list(self._default.keys())

    def factory_names(self, environment: Environment) -> list[str]:
        return sorted(r.factory_name for (e, _), r in self._by_name.items() if e == environment)
