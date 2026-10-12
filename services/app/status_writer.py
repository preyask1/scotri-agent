"""Writes the mirror's health-status report to disk — a plain JSON file, atomically replaced each
run, so a reader never sees a half-written file mid-update."""
from __future__ import annotations

import json
import os
from pathlib import Path


def write_status(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    os.replace(tmp_path, path)  # atomic on both Windows and POSIX
