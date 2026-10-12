"""Port for the ADF factory registry (config/adf_factories.yaml) — which Azure Data Factory
instance (subscription/resource group/factory name) corresponds to which environment. Separate
from ports/repo_registry.py: that port resolves a git repo+branch for *content* reads; this one
resolves an ARM resource identity for *run-history* reads (OPEN_ITEMS.md: decided 2026-09-27).
"""
from __future__ import annotations

from typing import Protocol

from domain.models import AdfFactoryRef, Environment


class AdfFactoryRegistry(Protocol):
    def get(self, environment: Environment, factory_name: str | None = None) -> AdfFactoryRef:
        """`factory_name` picks one of several factories in an environment; omitted, the
        environment's default factory is returned. Raises domain.errors.ConfigurationError if the
        environment/factory isn't registered, or if the entry is missing a required field (e.g.
        subscription_id not yet filled in — see config/adf_factories.yaml's own header)."""
        ...
