"""Loads config/model_registry.yaml and implements the ModelRegistry port.

Same pattern as yaml_registry_adapter.py (repo registry): this is the one place YAML parsing
happens for model assignments, so a hand-maintained YAML file can become a database or an API call
later without touching anything upstream of this adapter.

Read by app/container.py to pick the engagement model (one model for every stage since
2026-10-07, OPEN_ITEMS.md #4 — see config/model_registry.yaml's header)."""
from __future__ import annotations

from pathlib import Path

import yaml

from domain.models import ModelRef

DEFAULT_MODEL_REGISTRY_PATH = Path(__file__).resolve().parents[2] / "config" / "model_registry.yaml"
ENGINE_DEFAULT_MODEL = {"vendor": "anthropic", "model": "claude-sonnet-5-5", "secret_name": "ANTHROPIC_API_KEY"}
"""Used when config/model_registry.yaml has not been generated; `app.setup render` writes the profile's choice."""


class YamlModelRegistry:
    """Implements ports.model_registry.ModelRegistry."""

    def __init__(self, path: Path = DEFAULT_MODEL_REGISTRY_PATH) -> None:
        self._path = path
        self._by_key: dict[str, ModelRef] = {}
        self._default: ModelRef | None = None
        self._load()

    def _load(self) -> None:
        if not Path(self._path).is_file():
            # The model is the one setting the engine can default: no profile yet means the engine's
            # own choice, not a broken deployment.
            data = {"default": ENGINE_DEFAULT_MODEL}
        else:
            with open(self._path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)

        default_entry = data.get("default")
        if default_entry:
            self._default = ModelRef(
                stage_key="default",
                vendor=default_entry["vendor"],
                model=default_entry["model"],
                secret_name=default_entry["secret_name"],
            )

        for stage_entry in data.get("stages", []):
            stage_key = stage_entry["stage_key"]
            self._by_key[stage_key] = ModelRef(
                stage_key=stage_key,
                vendor=stage_entry["vendor"],
                model=stage_entry["model"],
                secret_name=stage_entry["secret_name"],
            )

    # --- ModelRegistry port -------------------------------------------------------------

    def get(self, stage_key: str) -> ModelRef:
        if stage_key in self._by_key:
            return self._by_key[stage_key]
        if self._default is not None:
            return ModelRef(
                stage_key=stage_key,
                vendor=self._default.vendor,
                model=self._default.model,
                secret_name=self._default.secret_name,
            )
        raise KeyError(f"model_registry.yaml: no entry (and no default) for stage '{stage_key}'")

    def all_stage_keys(self) -> list[str]:
        return list(self._by_key.keys())
