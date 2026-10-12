"""The hexagonal boundary, checked: domain/ and ports/ import only the standard library, each other
and nothing else, so a business rule can never reach a vendor SDK, Redis or the app layer directly.
Every concrete integration lives in adapters/ and is wired in app/container.py. This is what makes
Scotri re-targetable (another customer's repos, another ticket system) by swapping adapters and
config, not by editing the rules."""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

SERVICES = Path(__file__).resolve().parents[1]
INNER_LAYERS = ("domain", "ports")
ALLOWED_FIRST_PARTY = {"domain", "ports"}
STDLIB = set(sys.stdlib_module_names) | {"__future__"}


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.append(node.module.split(".")[0])
    return names


@pytest.mark.parametrize("path", sorted(p for layer in INNER_LAYERS for p in (SERVICES / layer).rglob("*.py")),
                         ids=lambda p: str(p.relative_to(SERVICES)))
def test_inner_layers_import_only_stdlib_domain_and_ports(path):
    bad = sorted({m for m in _imports(path) if m not in STDLIB and m not in ALLOWED_FIRST_PARTY})
    assert not bad, f"{path.relative_to(SERVICES)} imports {bad}; route it through a port and an adapter"


@pytest.mark.parametrize("path", sorted((SERVICES / "adapters").rglob("*.py")),
                         ids=lambda p: str(p.relative_to(SERVICES)))
def test_adapters_never_import_the_app_layer(path):
    assert "app" not in _imports(path), f"{path.relative_to(SERVICES)} imports app/; only app/ wires adapters"
