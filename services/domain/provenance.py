# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""Which engine a deployment runs. Every API response carries ENGINE_HEADER and every page the
service serves or generates carries a `<meta name="generator">` with ENGINE_MARKER, so a deployment
can be identified as this engine from outside, whatever display name it has been given.

The marker is lower-case on purpose: domain/branding.py rewrites the capitalised engine name to the
deployment's display name, and this must survive that. Pure: no I/O.
"""
from __future__ import annotations

ENGINE_ID = "c51c3469"
ENGINE_MARKER = f"scotri-engine/{ENGINE_ID}"
ENGINE_HEADER = "X-Engine"
GENERATOR_META = f'<meta name="generator" content="{ENGINE_MARKER}">'
