# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""adapters/registry/yaml_adf_factory_registry.py — loads config/adf_factories.yaml and refuses to
hand back a factory ref with no subscription_id, since that field is genuinely unfilled in this
deployment today (OPEN_ITEMS.md) and a silent empty string would produce a confusing ARM 404/400
instead of a clear, actionable error."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from adapters.registry.yaml_adf_factory_registry import YamlAdfFactoryRegistry
from domain.errors import ConfigurationError
from domain.models import Environment


def _write_registry(tmp_path: Path, factories: list[dict]) -> Path:
    path = tmp_path / "adf_factories.yaml"
    path.write_text(yaml.safe_dump({"factories": factories}), encoding="utf-8")
    return path


def test_missing_subscription_id_raises_configuration_error(tmp_path):
    path = _write_registry(tmp_path, [
        {"environment": "prod", "subscription_id": "", "resource_group": "rg", "factory_name": "f"}
    ])
    registry = YamlAdfFactoryRegistry(path)
    with pytest.raises(ConfigurationError):
        registry.get(Environment.PROD)


def test_fully_configured_environment_resolves(tmp_path):
    path = _write_registry(tmp_path, [
        {"environment": "prod", "subscription_id": "sub-123", "resource_group": "rg", "factory_name": "f"}
    ])
    registry = YamlAdfFactoryRegistry(path)
    ref = registry.get(Environment.PROD)
    assert ref.subscription_id == "sub-123"
    assert ref.resource_group == "rg"
    assert ref.factory_name == "f"
    assert ref.environment == Environment.PROD


def test_named_factory_and_default_selection(tmp_path):
    path = _write_registry(tmp_path, [
        {"environment": "prod", "subscription_id": "s1", "resource_group": "rg1", "factory_name": "A", "default": True},
        {"environment": "prod", "subscription_id": "s2", "resource_group": "rg2", "factory_name": "B"},
    ])
    registry = YamlAdfFactoryRegistry(path)
    assert registry.get(Environment.PROD).factory_name == "A"
    assert registry.get(Environment.PROD, "b").resource_group == "rg2"
    with pytest.raises(ConfigurationError):
        registry.get(Environment.PROD, "nope")


def test_named_factory_without_ids_raises_configuration_error(tmp_path):
    path = _write_registry(tmp_path, [
        {"environment": "prod", "subscription_id": "s1", "resource_group": "rg1", "factory_name": "A"},
        {"environment": "prod", "subscription_id": "", "resource_group": "", "factory_name": "B"},
    ])
    registry = YamlAdfFactoryRegistry(path)
    with pytest.raises(ConfigurationError):
        registry.get(Environment.PROD, "B")


def test_unregistered_environment_raises_configuration_error(tmp_path):
    path = _write_registry(tmp_path, [
        {"environment": "prod", "subscription_id": "sub-123", "resource_group": "rg", "factory_name": "f"}
    ])
    registry = YamlAdfFactoryRegistry(path)
    with pytest.raises(ConfigurationError):
        registry.get(Environment.DEV)
